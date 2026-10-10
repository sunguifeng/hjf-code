"""grep 工具：按内容正则搜索项目文件，返回 文件:行号:行内容。

定位互补关系：ls 管"有什么文件"，read_file_range 管"这文件写了啥"，
grep 管"哪句话在哪"。模型先 grep 拿 文件:行号，再用 read_file_range
精读上下文——避免为找一个符号盲读整个文件。

实现为纯 Python 遍历（os.walk + re），不用 findstr：findstr 按系统码页
（GBK）读文件，项目文件是 UTF-8，中文 pattern / 中文内容会错乱漏匹配；
标准 re 也和模型的正则常识一致。本项目量级下性能足够（毫秒~秒级）。

只读工具：不弹权限窗；不登记 file_state（edit_file 的"编辑前必读"
仍以 read_file_range 为准——grep 只负责定位，不替代阅读）。
直跑自测：python tool/tool_grep.py
"""

import fnmatch
import os
import re
from pathlib import Path

from base_tool import Tool, ToolError

# 相对路径的起点：项目根目录（tool/ 的上一级）
_ROOT = Path(__file__).resolve().parent.parent

MAX_MATCHES = 200       # 命中条数上限：达到即停止扫描
MAX_OUTPUT = 30000      # 输出字符上限：超出则头尾截断（与 tool_cmd 一致）
MAX_FILE_BYTES = 2 * 1024 * 1024   # 超过 2MB 的文件直接跳过
LINE_SNIPPET = 200      # 单行结果截断长度

# 递归时剪掉的目录名（结果垃圾 + 不该被搜到的隔离区）
SKIP_DIRS = {".git", "__pycache__", ".agents", ".agentworktrees",
             "node_modules", ".venv", ".idea", "build", "dist"}


def grep(pattern: str, path: str = "", glob: str = "",
         ignore_case: bool = False) -> str:
    """在 path 下递归搜索 pattern（标准正则）；path 也可是单个文件。

    返回统计 + '相对路径:行号:行内容' 命中列表。"""
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"正则表达式非法: {e}")

    base = Path(path) if path else _ROOT
    if not base.is_absolute():
        base = _ROOT / base
    if not base.exists():
        raise ToolError(f"搜索起点不存在: {base}")
    if base.is_file() and not _is_candidate(base, glob):
        return f"文件被过滤条件排除（glob={glob!r}）或超限/二进制: {base.name}"

    matches = []          # ["相对路径:行号:行内容", ...]
    files_hit = set()
    files_scanned = 0

    if base.is_file():    # 单文件模式：直接搜这一个
        files_scanned = 1
        _grep_file(base, rx, base.parent, matches, files_hit)
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            # 原地剪枝：跳过目录本身（os.walk 约定的写法）
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                fp = Path(dirpath) / fname
                if not _is_candidate(fp, glob):
                    continue
                files_scanned += 1
                _grep_file(fp, rx, base, matches, files_hit)
                if len(matches) >= MAX_MATCHES:
                    break
            if len(matches) >= MAX_MATCHES:
                break

    if not matches:
        return f"未找到匹配。已扫描 {files_scanned} 个文件。"

    truncated = len(matches) >= MAX_MATCHES
    out = "\n".join(matches)
    if len(out) > MAX_OUTPUT:
        keep = MAX_OUTPUT // 2
        out = out[:keep] + f"\n…（输出超 {MAX_OUTPUT} 字符，中间截断）\n" + out[-keep:]
    tail = (f"\n\n共命中 {len(matches)} 条（文件 {len(files_hit)} 个），"
            f"已扫描 {files_scanned} 个文件。")
    if truncated:
        tail += f"已达 {MAX_MATCHES} 条上限提前停止，建议收窄 pattern/path/glob 继续。"
    return out + tail


def _is_candidate(fp: Path, glob: str) -> bool:
    """预检一个文件是否参与搜索：glob 过滤 + 大小上限 + 二进制跳过。"""
    if glob and not fnmatch.fnmatch(fp.name, glob):
        return False
    try:
        if fp.stat().st_size > MAX_FILE_BYTES:
            return False
        with fp.open("rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return b"\x00" not in head


def _grep_file(fp: Path, rx, base: Path, matches: list, files_hit: set):
    """搜单个文件，命中追加 '相对路径:行号:行内容'。"""
    try:
        with fp.open(encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                if rx.search(line):
                    rel = fp.relative_to(base)
                    text = line.rstrip("\r\n")
                    if len(text) > LINE_SNIPPET:
                        text = text[:LINE_SNIPPET] + "…"
                    matches.append(f"{rel}:{lineno}:{text}")
                    files_hit.add(str(rel))
                    if len(matches) >= MAX_MATCHES:
                        return
    except OSError:
        pass       # 读不了的文件（权限/占用）静默跳过


class GrepTool(Tool):
    name = "grep"
    description = (
        "只读工具：按内容正则搜索项目文件，返回 '文件路径:行号:行内容' 列表。"
        "pattern 为标准正则表达式；path 为搜索起点（相对路径以项目根目录为起点，"
        "默认整个项目）；glob 为文件名过滤（fnmatch 语法，如 '*.py'）；"
        "ignore_case 默认关闭。"
        "拿到 行号 后用 read_file_range(path, start=行号附近, offset=…) 精读上下文，"
        "再决定后续动作。"
        "命中上限 200 条、输出上限 30000 字符，超限会提示收窄搜索条件。"
        "示例：找 permission_service 的调用处 -> "
        "pattern='permission_service', glob='*.py'"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "搜索内容，标准正则表达式（Python re 语法）"},
            "path": {"type": "string",
                     "description": "搜索起点目录，默认整个项目；"
                                    "相对路径以项目根目录为起点"},
            "glob": {"type": "string",
                     "description": "文件名过滤，fnmatch 语法，如 '*.py'；默认不过滤"},
            "ignore_case": {"type": "boolean",
                            "description": "忽略大小写，默认 false"},
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = "", glob: str = "",
                ignore_case: bool = False) -> str:
        return grep(pattern, path, glob, ignore_case)


if __name__ == "__main__":
    # 自测：临时目录造文件覆盖各分支 + 项目根真实搜索
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件
    # （本模块不直接打点，但自测经 base_tool 间接产生日志）

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mychat-grep-"))
    (tmp / "a.py").write_text(
        "import os\n"
        "# 权限审批在这里\n"
        "def approve():\n"
        "    return 'permission_service'\n", encoding="utf-8")
    sub = tmp / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("hello world\nHello again\n", encoding="utf-8")
    (sub / "bin.dat").write_bytes(b"\x00\x01binary\x00")
    (sub / "skipme.log").write_text("wasted\n" * 500000, encoding="utf-8")
    (tmp / "skipdir").mkdir()
    (tmp / "skipdir" / "c.py").write_text("permission_service in skipdir\n",
                                          encoding="utf-8")
    (tmp / "skipdir" / "__pycache__").mkdir()
    (tmp / "skipdir" / "__pycache__" / "d.py").write_text(
        "permission_service in pycache\n", encoding="utf-8")

    # 1) 基本命中 + 行号正确（approve 在 a.py 第 3 行）
    r = grep("approve", path=str(tmp))
    assert "a.py:3:def approve():" in r, r
    assert "共命中 1 条" in r, r
    print("基本命中       OK:", r.splitlines()[0])

    # 2) 正则语法（def 开头的行）
    r = grep(r"^def \w+", path=str(tmp), glob="*.py")
    assert "a.py:3:def approve():" in r and "b.txt" not in r, r
    print("正则语法       OK")

    # 3) 忽略大小写：默认只中 1 条，开启后 2 条都中
    r = grep("hello", path=str(tmp / "sub"))
    assert "hello world" in r and "Hello again" not in r, r
    r = grep("hello", path=str(tmp / "sub"), ignore_case=True)
    assert "hello world" in r and "Hello again" in r, r
    print("忽略大小写     OK: 默认1条 / 忽略后2条")

    # 4) glob 过滤
    r = grep("world|approve", path=str(tmp), glob="*.py")
    assert "a.py" in r and "b.txt" not in r, r
    print("glob 过滤      OK")

    # 5) 二进制文件跳过（bin.dat 里的 "binary" 不命中）
    r = grep("binary", path=str(tmp))
    assert "bin.dat" not in r, r
    print("二进制跳过     OK")

    # 6) 排除目录生效（skipdir/__pycache__ 不搜，但 skipdir 本身要搜）
    r = grep("permission_service", path=str(tmp))
    assert "skipdir" in r and "__pycache__" not in r, r
    print("目录排除       OK")

    # 7) 中文 pattern 命中中文注释
    r = grep("权限审批", path=str(tmp))
    assert "a.py:2:# 权限审批在这里" in r, r
    print("中文匹配       OK")

    # 8) 无命中
    r = grep("不存在的词xyz", path=str(tmp))
    assert "未找到匹配" in r, r
    print("无命中         OK")

    # 9) 非法正则 -> ToolError（manager 会转 ERROR 头文本）
    try:
        grep("([bad", path=str(tmp))
    except ToolError as e:
        assert "正则表达式非法" in str(e)
        print("非法正则       OK:", str(e)[:40], "…")
    else:
        raise AssertionError("非法正则应抛 ToolError")

    # 10) 上限刹车 + 单文件模式：big.py 里 300 行 needle
    big = tmp / "big.py"
    big.write_text("needle\n" * 300, encoding="utf-8")
    r = grep("needle", path=str(big))
    assert "已达 200 条上限提前停止" in r and "共命中 200 条" in r, r
    assert "big.py:1:needle" in r, r
    print("200 条上限     OK（单文件模式路径正确）")

    # 11) 经 ToolManager 走完整链路（注册/执行/ERROR 头）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from base_tool import ToolManager
    mgr = ToolManager(Path(__file__).parent)
    assert "grep" in mgr.tools, "grep 应被 ToolManager 加载"
    r = mgr.execute("grep", {"pattern": "([bad"})
    assert r.startswith("ERROR: 正则表达式非法"), r
    r = mgr.execute("grep", {"pattern": "权限审批", "path": str(tmp)})
    assert "a.py:2" in r, r
    print("ToolManager     OK: 注册 + ERROR 头 + 执行")

    # 12) 项目根真实搜索（回对象本身：能搜到本工具文件）
    r = grep("permission_service", glob="*.py")
    assert "tool_grep.py" in r or "permission" in r, r
    print("项目根搜索     OK: 命中", r.strip().splitlines()[-1])

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n全部测试通过")
