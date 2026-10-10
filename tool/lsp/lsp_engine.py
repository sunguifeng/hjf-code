# -*- coding: utf-8 -*-
"""LSP 引擎公共调用层：jedi / pyflakes / pycodestyle 的进程内直调封装。

lsp/ 下三个工具（lint / references / completion）共用的读写与检查函数。
引擎直接跑在本进程里，无服务端、无网络——pylsp 协议层后面也是这几份调用。
"""

import io
import re
from pathlib import Path

import jedi
import pycodestyle
from pyflakes.api import check as pyflakes_check
from pyflakes.reporter import Reporter

# 相对路径的起点：项目根目录（tool/lsp/ 的上两级）
_ROOT = Path(__file__).resolve().parent.parent.parent

# 跨文件引用搜索的项目范围
PROJECT = jedi.Project(_ROOT)


def resolve_path(path: str) -> Path:
    """相对路径拼上项目根目录；绝对路径原样返回。"""
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    return p


def read_source(path: str) -> tuple:
    """读源码文件，返回 (源码文本, 绝对路径)。不存在则抛 OSError（框架转 ERROR 文本）。"""
    full = resolve_path(path)
    if not full.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    return full.read_text(encoding="utf-8", errors="replace"), full


def rel_display(full: Path) -> str:
    """展示用相对路径（相对项目根），根外文件回退绝对路径。"""
    try:
        return full.relative_to(_ROOT).as_posix()
    except ValueError:
        return str(full)


def make_script(path: str) -> jedi.Script:
    """读文件并构造 jedi Script（附项目范围，跨文件能力由此而来）。"""
    code, full = read_source(path)
    return jedi.Script(code=code, path=str(full), project=PROJECT)


# ---- 语法校验（jedi） ----

def syntax_errors(code: str) -> list:
    """语法错误列表，每项 (行, 列, 消息)。行 0 起、列 0 起。"""
    errs = jedi.Script(code=code, path=str(_ROOT / "_lsp_probe.py")).get_syntax_errors()
    return [(e.line - 1, e.column, e.get_message()) for e in errs]


# ---- 语义校验（pyflakes） ----

class _FlakeCollector(Reporter):
    def __init__(self):
        self.msgs = []
        super().__init__(io.StringIO(), io.StringIO())

    def flake(self, message):
        self.msgs.append((message.lineno, message.message % message.message_args))

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.msgs.append((lineno, f"语法错误 {msg}"))


def pyflakes_errors(code: str) -> list:
    """语义问题列表（未定义名、未用导入等），每项 (行, 消息)。行 0 起。"""
    r = _FlakeCollector()
    pyflakes_check(code, "_lsp_probe.py", r)
    return [(ln - 1, msg) for ln, msg in r.msgs]


# ---- 格式校验（pycodestyle, PEP 8） ----

class _StyleCollector(pycodestyle.BaseReport):
    def __init__(self, options):
        super().__init__(options)
        self.msgs = []

    def error(self, line_number, offset, text, check):
        code_ = super().error(line_number, offset, text, check)
        if code_:
            # text 形如 "E225 missing whitespace around operator"，去掉重复的码前缀
            msg = text.split(None, 1)[1] if " " in text else text
            self.msgs.append((line_number - 1, f"{code_} {msg}"))
        return code_


def pycodestyle_errors(code: str) -> list:
    """格式问题列表（E/W 码），每项 (行, 消息)。行 0 起。"""
    sg = pycodestyle.StyleGuide(reporter=_StyleCollector)
    report = sg.init_report()
    sg.input_file("_lsp_probe.py", lines=code.splitlines(True))
    return list(report.msgs)


# ---- 完整三层 lint 报告（编辑后自动检验用） ----

def check_file(path: str, max_issues: int = 30) -> str:
    """完整三层校验报告：语法（jedi）+ 语义（pyflakes）+ 格式（pycodestyle）。

    分节列出 [行号] 问题描述，行号 0 起（与 read_file_range 一致，
    可直接翻页读取对应行）；全过时返回通过信息。
    供 tool_lint（手动 lint）和 tool_edit（写盘后自动检验）共用。
    引擎依赖不可用时由调用方兜底（这里只管报告）。"""
    code, full = read_source(path)
    sections = [
        ("语法错误（jedi）",
         [(ln, f"({col} 列) {msg}") for ln, col, msg in syntax_errors(code)]),
        ("语义问题（pyflakes）", pyflakes_errors(code)),
        ("格式问题（pycodestyle, PEP 8）", pycodestyle_errors(code)),
    ]
    out = []
    total = 0
    clean = True
    for title, issues in sections:
        total += len(issues)
        if not issues:
            out.append(f"\n[{title}] 通过，无问题")
            continue
        clean = False
        out.append(f"\n[{title}] {len(issues)} 个问题:")
        for ln, msg in issues[:max_issues]:
            out.append(f"  行 {ln}: {msg}")
        if len(issues) > max_issues:
            out.append(f"  ...（仅展示前 {max_issues} 条，共 {len(issues)} 条）")
    if clean:
        out.append("\n三层检查全部通过：语法 / 语义 / 格式 均无问题。")
    else:
        out.append(f"\n共 {total} 个问题。")
    return "\n".join(out)


# ---- 符号定位（references 工具用） ----

def find_symbols(lines: list, symbol: str, from_line: int = 0) -> list:
    """在文件行列表里找符号名的所有出现位置（整词匹配）。

    返回 [(行索引, 列), ...] 按行序。调用方应逐个位置向引擎求定义，
    取第一个能解析的——docstring/注释里的同名文本解析不出来，自然被跳过。
    """
    pat = re.compile(rf"\b{re.escape(symbol)}\b")
    hits = []
    for idx in range(max(from_line, 0), len(lines)):
        for m in pat.finditer(lines[idx]):
            hits.append((idx, m.start()))
    return hits
