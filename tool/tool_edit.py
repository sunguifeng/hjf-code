# -*- coding: utf-8 -*-
"""edit_file 工具：先读后改的字符串替换编辑（自包含，实现逻辑在本文件）。

三路分发（每路都有 early return，不会串路）：
- old_str 为空    -> 创建新文件（内容是 new_str）
- new_str 为空    -> 删除 old_str（替换成空串）
- 都非空          -> 用 new_str 替换 old_str

前置检查（read-before-edit）：文件必须先用 read_file_range 读过
（read_state 里登记过），且读后没被外部改过（mtime 没变），
防止模型凭旧印象盲改。

写盘前过审批（permission）：新内容和 diff 先在内存里算好，经
permission_service.request 弹给用户看（a 允许 / s 本会话都允许 /
d 拒绝）；拒绝抛 ToolError → ERROR 文本 observation 回填给模型
（错误也是观察，agent 继续跑，模型自己消化拒绝信息）。
headless（无 ask_ui 回调，如 CLI 自测）自动放行。

输出契约：首行摘要（已替换 path（+N -M） / 已创建 / 已删除），
随后是 unified diff；.py 文件附"编辑后诊断"（完整三层 lint：语法 / 语义 /
格式，lsp_engine.check_file——检验不再作为独立 agent 工具，写盘后自动跑）。
TUI 端 DiffBlock 靠这个格式识别 diff 渲染。
"""

import difflib
import os
import sys
from pathlib import Path

# 相对路径的起点：项目根目录（tool/ 的上一级）。
# 根进 sys.path：permission/ 包在根下（直接运行本文件时 cwd 是 tool/）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from applog import get_logger
from base_tool import Tool, ToolError
from permission.permission import PermissionRequest, permission_service
from read_state import file_state

log = get_logger(__name__)

# diff 展示的行数上限（防刷屏；文件本身照常全量写入）
MAX_DIFF_LINES = 100

# lsp 引擎目录（诊断函数在里面），懒加载时把目录挂进 sys.path
_LSP_DIR = Path(__file__).resolve().parent / "lsp"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "编辑工具：用 new_str 精确替换文件中的 old_str（字符串精确匹配）。"
        "必须先用 read_file_range 读过该文件才能编辑（防止凭旧印象盲改）。"
        "old_str 必须在文件中恰好出现一次（多次出现会报错，请带上更多上下文"
        "让它唯一）。"
        "三路分发：old_str 为空 -> 创建新文件（内容为 new_str）；"
        "new_str 为空 -> 从文件中删除 old_str；都非空 -> 替换。"
        "路径为相对路径时以项目根目录为起点。"
        "返回摘要 + unified diff；Python 文件另附编辑后诊断。"
        "示例：把 main.py 里的 'x=1' 改成 'x = 2' -> "
        "path='main.py', old_str='x=1', new_str='x = 2'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "文件路径：相对路径以项目根目录为起点"},
            "old_str": {"type": "string",
                        "description": "要被替换的原文。空串表示创建新文件；"
                                       "必须在文件中恰好出现一次"},
            "new_str": {"type": "string",
                        "description": "替换后的新文本。空串表示删除 old_str"},
        },
        "required": ["path", "old_str", "new_str"],
    }

    def execute(self, path: str, old_str: str, new_str: str) -> str:
        # 三路分发，每路 return 收尾：不落到下一段
        if old_str == "":
            return self._create_file(path, new_str)
        if new_str == "":
            return self._replace(path, old_str, "")
        return self._replace(path, old_str, new_str)

    # ---- 三路各自的实现 ----

    def _create_file(self, path: str, content: str) -> str:
        """old_str 为空：创建新文件。已存在则拒绝（防止静默清空）。"""
        full = _resolve(path)
        if os.path.exists(full):
            raise ToolError(f"文件已存在，不能创建: {path}（如要修改请先读取，"
                           f"old_str 传要改的原文）")
        rel = file_state.key(full)
        diff = _diff("", content, rel)
        _request_permission(rel, diff)     # 审批在写盘前：拒绝时磁盘零改动
        full.parent.mkdir(parents=True, exist_ok=True)
        # newline="": content 原样写入，\n 不被转换成 \r\n
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        file_state.record(full)      # 写即读过：刚写的内容当然是最新的
        return f"已创建 {rel}（+{_count(diff, '+')} 行）\n{diff}"

    def _replace(self, path: str, old_str: str, new_str: str) -> str:
        """替换 / 删除：7 步流程（前置检查 -> 读 -> 定位 -> 拼接 -> 写 -> 记录 -> 诊断）。"""
        full = _resolve(path)
        rel = file_state.key(full)

        # 1) 前置检查：读过没有？
        if rel not in file_state.viewed_at:
            raise ToolError(
                f"尚未读取过 {rel}，请先用 read_file_range 查看文件内容，"
                f"确认 old_str 与文件当前内容一致后再编辑")
        # 2) 前置检查：读后有没有被外部改过（mtime 晚于读取时刻）？
        mtime = os.stat(full).st_mtime
        if mtime > file_state.viewed_at[rel] + 1e-6:
            raise ToolError(
                f"{rel} 在上次读取之后被外部修改过，请重新 read_file_range 后再编辑")

        # 3) 读全文：newline="" 关闭换行转换，\r\n 原样保留
        with open(full, encoding="utf-8", errors="replace", newline="") as f:
            content = f.read()

        # 4) 定位 old_str：必须存在且唯一（find == rfind 才唯一）
        idx = content.find(old_str)
        if idx == -1:
            raise ToolError(f"old_str 未在 {rel} 中找到，请先 read_file_range "
                            f"核对原文（注意缩进/空格/换行也要完全一致）")
        if content.rfind(old_str) != idx:
            raise ToolError(f"old_str 在 {rel} 中出现多次（至少 2 处），"
                            f"请扩大 old_str 范围（多带几行上下文）使它唯一")

        # 5) 拼接新内容并算 diff（先算不写：审批块给用户看的就是这份 diff）
        new_content = content[:idx] + new_str + content[idx + len(old_str):]
        diff = _diff(content, new_content, rel)
        plus, minus = _count(diff, "+"), _count(diff, "-")
        verb = "已删除" if new_str == "" else "已替换"

        # 6) 写盘前的最后一关：审批（拒绝 → ToolError → ERROR 观察）
        _request_permission(rel, diff)

        # 7) 写回（换行符原样：没被替换的部分保持 \r\n / \n 不动）+ 记录
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        file_state.record(full)      # 写即读过：编辑后的内容是最新状态

        out = f"{verb} {rel}（+{plus} -{minus}）\n{diff}"

        # 8) Python 文件追加编辑后诊断（完整三层 lint；引擎不可用时静默跳过）
        if full.suffix == ".py":
            diag = _lint_after_edit(full)
            if diag:
                out += "\n\n[编辑后诊断]\n" + diag
        return out


# ---- 模块级辅助 ----

def _resolve(path: str) -> Path:
    """相对路径以项目根目录为起点解析；绝对路径原样使用。"""
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    return p


def _request_permission(rel: str, diff: str) -> None:
    """写盘前的最后一关：审批。拒绝时抛 ToolError（走 ERROR 观察，agent 继续）。"""
    req = PermissionRequest(tool="edit_file", action="write", path=rel, diff=diff)
    if not permission_service.request(req):
        raise ToolError(f"用户拒绝了本次编辑（deny）：{rel}，"
                        f"如需修改请先询问用户意图")


def _diff(old: str, new: str, rel: str) -> str:
    """unified diff（展示用），超过 MAX_DIFF_LINES 行截断。"""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES]
        lines.append(f"...（diff 超过 {MAX_DIFF_LINES} 行，仅展示前 {MAX_DIFF_LINES} 行）")
    return "\n".join(lines)


def _count(diff: str, prefix: str) -> int:
    """diff 里以 prefix 开头的行数（+ / - 统计，文件头 --- +++ 不计）。"""
    return sum(1 for l in diff.splitlines()
               if l.startswith(prefix) and not l.startswith((prefix * 3,)))


def _lint_after_edit(full: Path) -> str:
    """编辑后自动检验：完整三层 lint（lsp_engine.check_file，读刚写好的文件）。
    引擎或其依赖不可用时返回空串——诊断是附加信息，不该挡编辑。"""
    try:
        if str(_LSP_DIR) not in sys.path:
            sys.path.insert(0, str(_LSP_DIR))
        from lsp_engine import check_file
        return check_file(str(full))
    except Exception:
        log.exception("edit 后 lint 不可用（附加诊断，不挡编辑）: %s", full)
        return ""


if __name__ == "__main__":
    # 自测：临时目录里跑全部分支，不碰项目文件
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile
    import time as _time

    tmp = Path(tempfile.mkdtemp())
    f = tmp / "demo.txt"

    # 1) 未读先改 -> 拒绝
    tool = EditFileTool()
    try:
        tool.execute(str(f), "a", "b")
    except ToolError as e:
        assert "尚未读取" in str(e), e
        print("未读先改拒绝   OK")
    else:
        raise AssertionError("未读先改应被拒绝")

    # 2) 创建（old_str 为空）
    out = tool.execute(str(f), "", "line1\nline2\nline3\n")
    assert "已创建" in out and "+line2" in out, out
    assert f.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    print("创建           OK:", out.splitlines()[0])

    # 3) 已存在再创建 -> 拒绝
    try:
        tool.execute(str(f), "", "x")
    except ToolError as e:
        assert "已存在" in str(e), e
        print("重复创建拒绝   OK")
    else:
        raise AssertionError("对已存在文件创建应被拒绝")

    # 4) 读后替换 + diff
    out = tool.execute(str(f), "line2", "LINE 2")
    assert "已替换" in out and "-line2" in out and "+LINE 2" in out, out
    assert f.read_text(encoding="utf-8") == "line1\nLINE 2\nline3\n"
    print("读后替换       OK:", out.splitlines()[0])

    # 5) old_str 未找到
    try:
        tool.execute(str(f), "no-such-text", "x")
    except ToolError as e:
        assert "未在" in str(e) or "未找到" in str(e), e
        print("未找到拒绝     OK")
    else:
        raise AssertionError("old_str 未找到应报错")

    # 6) 多次出现 -> 拒绝
    f.write_text("a\na\na\n", encoding="utf-8")
    file_state.record(str(f))
    try:
        tool.execute(str(f), "a", "b")
    except ToolError as e:
        assert "多次" in str(e), e
        print("多次出现拒绝   OK")
    else:
        raise AssertionError("多次出现应报错")

    # 7) 外部修改（mtime 变化）-> 拒绝
    f.write_text("fresh\n", encoding="utf-8")
    os.utime(f, (os.stat(f).st_atime, os.stat(f).st_mtime + 10))  # mtime 拨到未来
    try:
        tool.execute(str(f), "fresh", "stale")
    except ToolError as e:
        assert "外部修改" in str(e), e
        print("外部修改拒绝   OK")
    finally:
        os.utime(f)   # 恢复当前时间，后续步骤不受影响

    # 8) 删除（new_str 为空）—— 用无换行内容，删完即空文件
    f.write_text("fresh", encoding="utf-8")
    file_state.record(str(f))
    out = tool.execute(str(f), "fresh", "")
    assert "已删除" in out and "-fresh" in out, out
    assert f.read_text(encoding="utf-8") == ""
    print("删除           OK:", out.splitlines()[0])

    # 9) \r\n 保留：没被替换的部分原样
    f.write_text("keep\r\nold\r\nkeep\r\n", encoding="utf-8", newline="")
    file_state.record(str(f))
    tool.execute(str(f), "old", "new")
    raw = f.read_bytes()
    assert b"keep\r\nnew\r\nkeep\r\n" == raw, raw
    print("\\r\\n 保留     OK")

    # 10) .py 文件带完整三层诊断（改出未定义名）
    p = tmp / "probe.py"
    p.write_text("x = 1\nprint(y)\n", encoding="utf-8")
    file_state.record(str(p))
    out = tool.execute(str(p), "print(y)", "print(undefined_zz)")
    assert "编辑后诊断" in out and "undefined" in out, out
    # 三层分节都要出现（语法/格式过=通过行，语义=1 个问题）
    for sec in ("[语法错误", "[语义问题", "[格式问题"):
        assert sec in out, sec
    print(".py 诊断       OK:", out.splitlines()[-1].strip())

    # 11) 连续编辑：改完直接再改（写即读过，无需重新读）
    out = tool.execute(str(p), "undefined_zz", "x")
    assert "已替换" in out, out
    print("连续编辑       OK")

    # 12) diff 截断：小改动不截断；大段新增（diff 超百行）只展示前 MAX_DIFF_LINES 行
    big = tmp / "big.txt"
    big.write_text("".join(f"old{i}\n" for i in range(200)), encoding="utf-8")
    file_state.record(str(big))
    out = tool.execute(str(big), "old100", "new100")
    assert "仅展示前" not in out, "小改动 diff 不应触发截断"
    flood = "\n".join(f"new line {i}" for i in range(150))   # 一处替换引入 150 行
    out2 = tool.execute(str(big), "old101", flood)
    assert "仅展示前" in out2, "超百行的 diff 应被截断"
    print("diff 截断      OK")

    # 13) 审批拒绝：ToolError，磁盘零改动
    f.write_text("approved\n", encoding="utf-8")
    file_state.record(str(f))
    permission_service.ask_ui = lambda r: permission_service.deny(r)
    try:
        tool.execute(str(f), "approved", "denied")
    except ToolError as e:
        assert "拒绝" in str(e), e
        assert f.read_text(encoding="utf-8") == "approved\n", "拒绝时磁盘必须零改动"
        print("审批拒绝       OK: 文件保持原样")
    else:
        raise AssertionError("拒绝时应抛 ToolError")

    # 14) 审批放行：正常写盘
    permission_service.ask_ui = lambda r: permission_service.allow(r)
    out = tool.execute(str(f), "approved", "granted")
    assert "已替换" in out and f.read_text(encoding="utf-8") == "granted\n"
    print("审批放行       OK")

    # 15) allow_for_session：同一文件第二次不再问，换文件重新问
    calls = []

    def _ask_session(r):
        calls.append(r.path)
        permission_service.allow_for_session(r)

    permission_service.ask_ui = _ask_session
    tool.execute(str(f), "granted", "again")
    assert len(calls) == 1, calls
    tool.execute(str(f), "again", "more")        # 缓存命中，不问
    assert len(calls) == 1 and f.read_text(encoding="utf-8") == "more\n"
    g = tmp / "other.txt"
    g.write_text("other\n", encoding="utf-8")
    file_state.record(str(g))
    tool.execute(str(g), "other", "OTHER")        # 换文件重新问
    assert len(calls) == 2, calls
    print("会话缓存       OK: 问了", calls)

    # 16) 通配会话允许：一个文件触发后，其它文件也不再问
    permission_service._granted.clear()          # 清掉 15 的文件级缓存再测
    calls.clear()

    def _ask_all(r):
        calls.append(r.path)
        permission_service.allow_for_session_all(r)

    permission_service.ask_ui = _ask_all
    tool.execute(str(f), "more", "x")
    assert len(calls) == 1, calls
    tool.execute(str(g), "OTHER", "other2")       # 另一个文件也不问
    assert len(calls) == 1, calls
    print("通配会话允许   OK: 只问了", calls)
    permission_service.ask_ui = None             # 还原 headless（后续用例）
    permission_service._granted.clear()

    # 清理
    import shutil
    shutil.rmtree(tmp)
    print("\n全部测试通过")
