# -*- coding: utf-8 -*-
"""references 工具：查符号的定义位置与全部引用（只读）。

跳转定义（goto）+ 全项目引用搜索（get_references），跨文件能力
由 jedi.Project(项目根) 提供——引擎自己扫盘，无需把代码传给它。
输出行号从 0 开始，与 read_file_range 的 start 一致。
"""

import sys
from pathlib import Path

# base_tool.py 在上一级目录（tool/），直接运行本文件时需要它可导入
_TOOL_DIR = Path(__file__).resolve().parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

from base_tool import Tool, ToolError
from lsp_engine import find_symbols, make_script, read_source, rel_display

# 最多展示的引用条数
MAX_REFS = 50


class ReferencesTool(Tool):
    name = "references"
    description = (
        "只读工具：查一个符号的定义位置（goto）和它在整个项目里的全部引用"
        "（跨文件，引擎扫全项目源码）。"
        "path 为符号所在文件，symbol 为符号名（变量/函数/类/方法名），"
        "同一文件有同名符号时可用 line 指定从哪行开始找（默认从头找第一个）。"
        "返回定义的 文件:行 + 源码行，以及逐条引用的 文件:行 + 代码摘录，"
        "行号从 0 开始（与 read_file_range 的 start 一致）。"
        "示例：查 reactAgent.py 中 manager.execute 的引用 -> "
        "path='agent/reactAgent.py', symbol='execute'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "符号所在文件路径；相对路径以项目根目录为起点"},
            "symbol": {"type": "string",
                       "description": "符号名，如 'execute'、'ToolManager'"},
            "line": {"type": "integer",
                     "description": "同名符号消歧用：从该行（0 起）开始找第一个匹配，"
                                    "默认 0（从文件头找）"},
        },
        "required": ["path", "symbol"],
    }

    def execute(self, path: str, symbol: str, line: int = 0) -> str:
        # 1. 读文件 + 收集符号所有出现位置（整词匹配）
        code, full = read_source(path)
        lines = code.splitlines()
        if not isinstance(line, int) or line < 0:
            raise ValueError(f"line 必须是非负整数: {line!r}")
        hits = find_symbols(lines, symbol, line)
        if not hits:
            raise ToolError(f"{path} 第 {line} 行起未找到符号 {symbol}（注意是整词匹配，"
                           f"方法名不要带点号或括号）")
        script = make_script(path)

        # 2. 逐个位置向引擎求定义，取第一个能解析的
        #（docstring/注释里的同名文本解析不出来，自然被跳过）
        defs, idx, col = [], hits[0][0], hits[0][1]
        for hit_idx, hit_col in hits:
            defs = script.goto(hit_idx + 1, hit_col)   # jedi 行号 1 起，列 0 起
            if defs:
                idx, col = hit_idx, hit_col
                break

        out = [f"符号 {symbol}（{rel_display(full)} 行 {idx}）:"]
        if defs:
            for d in defs:
                def_line = (d.get_line_code() or "").strip()
                out.append(f"\n定义于 {rel_display(d.module_path)} 行 {d.line - 1}:"
                           f"\n  {def_line}")
        else:
            out.append("\n（未能解析出定义，可能是内置名或仅在注释/docstring 中出现）")

        # 3. 全项目引用搜索
        refs = script.get_references(idx + 1, col)
        out.append(f"\n全部引用（共 {len(refs)} 处，"
                   f"横跨 {len({str(r.module_path) for r in refs})} 个文件）:")
        for r in refs[:MAX_REFS]:
            snip = (r.get_line_code() or "").strip()
            out.append(f"  {rel_display(r.module_path)}:{r.line - 1:<4} {snip[:70]}")
        if len(refs) > MAX_REFS:
            out.append(f"  ...（仅展示前 {MAX_REFS} 条，共 {len(refs)} 条）")
        return "\n".join(out)


if __name__ == "__main__":
    tool = ReferencesTool()

    # 项目真实符号：reactAgent.py 里的 execute 调用点
    out = tool.execute("agent/reactAgent.py", "execute")
    assert "定义于" in out and "tool/base_tool.py" in out, "应跳到 base_tool 的定义"
    assert "全部引用" in out and "reactAgent.py" in out, "应列出跨文件引用"
    print("跨文件引用 OK:\n" + out[:900] + "\n...（截断展示）\n")

    # 类符号：ToolManager
    out = tool.execute("tool/base_tool.py", "ToolManager")
    assert "class ToolManager" in out, "应跳到类定义"
    assert "agent/reactAgent.py" in out, "agent 文件里应引用过 ToolManager"
    print("类符号 OK: 跳到 class ToolManager 定义，引用横跨 agent/ 和 tool/\n")

    # 同名消歧：base_tool.py 里有两个 execute 定义（Tool 和 ToolManager），
    # line=100 越过 Tool.execute，应定位到 ToolManager 的那个
    out = tool.execute("tool/base_tool.py", "execute", line=100)
    assert "def execute(self, name: str, arguments: dict)" in out, \
        "line=100 应定位到 ToolManager.execute"
    print("同名消歧 OK: line=100 -> ToolManager.execute\n")

    # 找不到符号 -> ToolError（框架转 ERROR 头文本）
    try:
        tool.execute("agent/reactAgent.py", "no_such_symbol")
    except ToolError as e:
        print("未知符号 OK:", str(e)[:60], "...")
    else:
        raise AssertionError("未知符号应抛 ToolError")

    # 不存在文件 -> FileNotFoundError（框架转 ERROR 头文本）
    try:
        tool.execute("no_such.py", "x")
    except FileNotFoundError:
        print("不存在路径 OK: FileNotFoundError（框架转 ERROR 头文本）")

    print("\n全部测试通过")
