# -*- coding: utf-8 -*-
"""persona：读侧系统提示词分层装配。

五层结构（越往后越优先）：
    1. base            基础身份提示词（NATIVE_SYSTEM_PROMPT），永远在
    2. USER.md         用户级偏好（用户的"宪法"，用户手写）
    3. memory/MEMORY.md  用户级记忆索引（agent 写侧捕获的"判例"）
    4. AGENTS.md       项目级约定（仓库自带的协作规则）
    5. .mychat/memory/MEMORY.md  项目级记忆索引

用户级文件在 ~/.mychat/ 下（MYCHAT_HOME 可覆盖），项目级在项目根下。
冲突规则：可选层存在时末尾追加"项目级优先于用户级"。

超长处理：
    - USER.md / AGENTS.md 超 2000 字 → LLM 摘要要点（summarizer 注入，
      缓存按 (mtime, size) 指纹存 .summary 文件，源文件没变不重摘）；
      摘要失败回退硬截断。摘要段自带说明，全文路径告知模型可自行读取。
    - 记忆索引超 100 行 → 硬截断（索引本来就该一行一条，超限说明写侧
      该清理了），同样告知全文路径。

写侧约定（本模块不管，但要知道）：USER.md 首次缺失时播种模板，
未编辑的模板不算一层（比对内容 == 模板原文）；MEMORY.md 不播种——
索引是 agent 写侧的产物，读侧保持"哑"的。

直跑自测：python persona/loader.py（假 summarizer + 临时目录，不调真模型）
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger

log = get_logger(__name__)

# ---- 可调常量 ----
SUMMARIZE_THRESHOLD = 2000    # USER.md / AGENTS.md 超过此字符数走摘要
INDEX_MAX_LINES = 100        # 记忆索引最多取前 N 行
TRUNCATE_KEEP = 2000         # 硬截断保留的字符数

# USER.md 播种模板：HTML 注释包裹说明 + 占位内容
_USER_MD_TEMPLATE = """<!--
mychat 用户画像（USER.md）
这是你的宪法文件：写在这里的偏好，每次对话都会作为最高优先级被加载。
编辑下方模板、删掉这段注释，保存即生效；保持 "# 模板" 原样 = 未填写，
未填写的文件不会被加载。
-->

# 模板
-（例如：回答简洁直接，先结论后展开）
-（例如：代码注释用简体中文）
-（例如：不要主动改我没提到的文件）
"""

_SENTINEL = "# 模板"    # 未填写标记：正文含它视为还没编辑


def user_home() -> Path:
    """用户级目录根：MYCHAT_HOME 覆盖，默认用户主目录（自测隔离用）。"""
    return Path(os.environ.get("MYCHAT_HOME", str(Path.home())))


def build_system_prompt(base: str, project_root: Path,
                       summarizer=None) -> str:
    """装配五层系统提示词。

    :param base: 基础身份提示词（永远在最前）
    :param project_root: 项目根目录（AGENTS.md / .mychat/ 的位置）
    :param summarizer: 摘要函数 (text, hint) -> 要点文本；None 则超长文件
                       硬截断（没有 LLM 可用时的兜底保护）。真模型实现由
                       调用方注入，本模块不依赖 model 包（保持可独立自测）
    :return: 拼接后的完整系统提示词
    """
    root = Path(project_root)
    home = user_home()

    sections = []       # [(标题, 文本)]，可选层

    # 层 2/3：用户级
    user_md = _load_user_md(home / ".mychat" / "USER.md")
    if user_md is not None:
        sections.append(("[用户偏好]", user_md,
                         home / ".mychat" / "USER.md"))
    user_idx = _memory_index(home / ".mychat" / "memory" / "MEMORY.md")
    if user_idx is not None:
        sections.append(("[用户记忆]", user_idx,
                         home / ".mychat" / "memory" / "MEMORY.md"))

    # 层 4/5：项目级
    agents_md = _load_lean_file(root / "AGENTS.md", summarizer)
    if agents_md is not None:
        sections.append(("[项目约定]", agents_md, root / "AGENTS.md"))
    proj_idx = _memory_index(root / ".mychat" / "memory" / "MEMORY.md")
    if proj_idx is not None:
        sections.append(("[项目记忆]", proj_idx,
                         root / ".mychat" / "memory" / "MEMORY.md"))

    if not sections:
        return base
    parts = [base]
    for title, text, src in sections:
        parts.append(f"{title}\n{text}")
    parts.append("[冲突规则] 各层内容冲突时，项目级优先于用户级；"
                 "与基础身份冲突时，以后到的具体规则为准。")
    return "\n\n".join(parts)


# ---- 各层加载 ----

def _load_user_md(path: Path) -> str | None:
    """读 USER.md；缺失则播种模板；内容仍等于模板（未编辑）返回 None。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_USER_MD_TEMPLATE, encoding="utf-8")
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip() or _SENTINEL in text:
        return None
    return text.strip()


def _load_lean_file(path: Path, summarizer) -> str | None:
    """读普通层文件（USER.md 之外的散文层）。超阈值摘要，失败硬截断。"""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) <= SUMMARIZE_THRESHOLD:
        return text

    cache = _summary_cache_path(path)
    hit = _read_cache(cache, path)
    if hit is not None:
        return hit + _summary_note(path)

    if summarizer is None:
        return _hard_truncate(text, path)
    try:
        summary = summarizer(text, f"提炼 {path.name} 的要点")
    except Exception:
        log.exception("persona 摘要失败，回退硬截断: %s", path)
        return _hard_truncate(text, path)
    if not summary or not summary.strip():
        return _hard_truncate(text, path)
    summary = summary.strip()
    _write_cache(cache, path, summary)
    return summary + _summary_note(path)


def _memory_index(path: Path) -> str | None:
    """读记忆索引：一行一条，超 100 行硬截断（并告知全文路径）。"""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    lines = text.splitlines()
    if len(lines) <= INDEX_MAX_LINES:
        return text
    kept = "\n".join(lines[:INDEX_MAX_LINES])
    return (kept + f"\n（索引超过 {INDEX_MAX_LINES} 行，仅显示前 "
            f"{INDEX_MAX_LINES} 行；全文在 {path}，"
            f"可用 read_file_range 工具读取）")


# ---- 摘要缓存：.summary 文件，首行 JSON 指纹 (mtime, size) ----

def _summary_cache_path(path: Path) -> Path:
    """缓存与源文件配套：项目层进 <root>/.mychat/cache/，
    用户层进 ~/.mychat/cache/。按源文件名命名，互不覆盖。"""
    if ".mychat" in path.parts:
        # 已在 .mychat 体系内（USER.md）→ 用户级缓存目录
        cache_dir = user_home() / ".mychat" / "cache"
    else:
        cache_dir = path.parent / ".mychat" / "cache"
    return cache_dir / (path.name + ".summary")


def _read_cache(cache: Path, source: Path):
    """指纹匹配（mtime + size）才返回缓存摘要，否则 None。"""
    try:
        first, _, body = cache.read_text(encoding="utf-8").partition("\n")
        fp = json.loads(first)
        st = source.stat()
        if fp.get("mtime") == st.st_mtime and fp.get("size") == st.st_size:
            return body
    except (OSError, ValueError):
        pass
    return None


def _write_cache(cache: Path, source: Path, summary: str):
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        st = source.stat()
        fp = json.dumps({"mtime": st.st_mtime, "size": st.st_size})
        cache.write_text(fp + "\n" + summary, encoding="utf-8")
    except OSError:
        pass        # 缓存写失败不致命：下次重摘即可


def _summary_note(path: Path) -> str:
    return (f"\n（原文较长，以上为要点摘要；完整原文在 {path}，"
            f"可用 read_file_range 工具读取）")


def _hard_truncate(text: str, path: Path) -> str:
    return (text[:TRUNCATE_KEEP]
            + f"\n（原文较长且摘要不可用，此处硬截断；完整原文在 {path}，"
              f"可用 read_file_range 工具读取）")


if __name__ == "__main__":
    # 自测：MYCHAT_HOME + 临时项目根全隔离，假 summarizer，不调真模型
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile

    calls = {"n": 0}

    def fake_summarizer(text, hint):
        calls["n"] += 1
        return f"[摘要] {hint}: {text[:20]}..."

    old_home = os.environ.get("MYCHAT_HOME")
    home = Path(tempfile.mkdtemp(prefix="mychat-persona-home-"))
    root = Path(tempfile.mkdtemp(prefix="mychat-persona-root-"))
    os.environ["MYCHAT_HOME"] = str(home)
    try:
        # 1) 空环境：只回 base，一个字不差
        out = build_system_prompt("BASE_PROMPT", root)
        assert out == "BASE_PROMPT", out
        print("空环境           OK: 原样返回 base")

        # 2) USER.md 首次缺失 → 播种模板且不算层
        umd = home / ".mychat" / "USER.md"
        assert umd.exists(), "缺失时应播种模板"
        assert build_system_prompt("BASE_PROMPT", root) == "BASE_PROMPT"
        print("播种模板         OK: 未编辑不算层")

        # 3) 填写 USER.md → [用户偏好] 出现
        umd.write_text("- 回答用简体中文\n- 先结论后展开\n", encoding="utf-8")
        out = build_system_prompt("BASE_PROMPT", root)
        assert "[用户偏好]" in out and "先结论后展开" in out, out
        assert out.startswith("BASE_PROMPT"), "base 必须在最前"
        print("用户偏好层       OK")

        # 4) 用户记忆索引 > 100 行 → 截断 + 提示
        idx = home / ".mychat" / "memory" / "MEMORY.md"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("\n".join(f"[记忆{i}] 内容" for i in range(150)),
                       encoding="utf-8")
        out = build_system_prompt("BASE_PROMPT", root)
        assert "[用户记忆]" in out and "[记忆99]" in out and "[记忆100]" not in out
        assert "仅显示前 100 行" in out
        print("索引截断         OK: 150 行取前 100 行")

        # 5) AGENTS.md → [项目约定]
        (root / "AGENTS.md").write_text("# 项目约定\n测试必须全过再提交\n",
                                        encoding="utf-8")
        out = build_system_prompt("BASE_PROMPT", root)
        assert "[项目约定]" in out and "测试必须全过" in out
        print("项目约定层       OK")

        # 6) 项目记忆索引 → [项目记忆]
        pidx = root / ".mychat" / "memory" / "MEMORY.md"
        pidx.parent.mkdir(parents=True, exist_ok=True)
        pidx.write_text("[项目记忆] 用 taskkill 杀树\n", encoding="utf-8")
        out = build_system_prompt("BASE_PROMPT", root)
        assert "[项目记忆]" in out and "taskkill" in out
        print("项目记忆层       OK")

        # 7) 有可选层必有冲突规则；空环境没有（已由 1 验证）
        assert "[冲突规则]" in out and "项目级优先" in out
        print("冲突规则         OK: 仅在可选层存在时追加")

        # 8) 层序：用户层在前、项目层在后（优先级越靠后越高，与文档一致）
        assert (out.index("[用户偏好]") < out.index("[用户记忆]")
                < out.index("[项目约定]") < out.index("[项目记忆]")), out
        print("层序             OK: 用户级在前，项目级在后")

        # 9) 超长 AGENTS.md 摘要 + 缓存命中（第二次不再调 summarizer）
        big = "超长约定内容。" * 300        # ~2100 字
        (root / "AGENTS.md").write_text(big, encoding="utf-8")
        n0 = calls["n"]
        out = build_system_prompt("BASE_PROMPT", root, summarizer=fake_summarizer)
        assert "[摘要]" in out and "要点摘要" in out, out
        assert calls["n"] == n0 + 1
        out2 = build_system_prompt("BASE_PROMPT", root, summarizer=fake_summarizer)
        assert calls["n"] == n0 + 1, "指纹未变应命中缓存"
        assert out == out2
        cache_file = root / ".mychat" / "cache" / "AGENTS.md.summary"
        assert cache_file.exists()
        print("摘要+缓存        OK: 第二次命中缓存不再调用")

        # 10) 源文件变了 → 缓存失效重摘
        (root / "AGENTS.md").write_text(big + "新加的内容", encoding="utf-8")
        build_system_prompt("BASE_PROMPT", root, summarizer=fake_summarizer)
        assert calls["n"] == n0 + 2, "指纹变化应重新摘要"
        print("缓存失效         OK: mtime/size 变化触发重摘")

        def _convention_body(out):
            """取 [项目约定] 段的正文（不含说明尾巴）：只数正文本身，
            避开说明文本里路径/措辞中的偶发字符。"""
            sec = out[out.index("[项目约定]"):]
            return sec[:sec.index("（原文较长")]

        # 11) summarizer 抛异常 → 硬截断回退，不崩
        (root / "AGENTS.md").write_text("超" * 2500, encoding="utf-8")
        out = build_system_prompt("BASE_PROMPT", root,
                                  summarizer=lambda t, h: 1 / 0)
        assert _convention_body(out).count("超") == 2000 and "硬截断" in out, out
        print("摘要失败回退     OK: 硬截断 + 说明")

        # 12) summarizer=None + 超长 → 硬截断兜底（没有 LLM 时也保护上下文）
        out = build_system_prompt("BASE_PROMPT", root, summarizer=None)
        assert _convention_body(out).count("超") == 2000 and "硬截断" in out, out
        print("无summarizer     OK: 超长硬截断兜底")

        # 13) MYCHAT_HOME 覆盖生效（本次自测全程依赖它，间接验证）
        #     直接再验证一次：换个 home，旧 home 的层不再出现
        home2 = Path(tempfile.mkdtemp(prefix="mychat-persona-home2-"))
        os.environ["MYCHAT_HOME"] = str(home2)
        out = build_system_prompt("BASE_PROMPT", root)
        assert "[用户偏好]" not in out, "换 home 后旧用户层不应出现"
        print("MYCHAT_HOME覆盖  OK")

        print("\n全部测试通过")
    finally:
        if old_home is None:
            os.environ.pop("MYCHAT_HOME", None)
        else:
            os.environ["MYCHAT_HOME"] = old_home
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
