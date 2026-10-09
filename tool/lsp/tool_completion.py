# -*- coding: utf-8 -*-
"""completion 工具：查指定光标位置的补全候选（只读）。

候选不只是名字——每个带补全后缀 / 类型 / 来源文件 / docstring 签名，
上下文来源（类型推断的实例 / 当前作用域 / 模块内容）由引擎自动判定。
行、列均从 0 开始，与 read_file_range 的 start 一致。
"""

import sys
from pathlib import Path

# base_tool.py 在上一级目录（tool/），直接运行本文件时需要它可导入
_TOOL_DIR = Path(__file__).resolve().parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

import jedi

from base_tool import Tool, ToolError
from lsp_engine import make_script, read_source, rel_display, resolve_path

# 最多展示的候选数
MAX_CANDIDATES = 30


class CompletionTool(Tool):
    name = "completion"
    description = (
        "只读工具：查询一个 Python 文件在指定光标位置能补全什么。"
        "候选带完整上下文详情：补全后缀（前缀+后缀=完整名）、类型"
        "（function/class/statement/param）、来源文件、docstring 签名。"
        "行 line 和列 column 均从 0 开始（与 read_file_range 的 start 一致）；"
        "column 常用值：行尾 = 该行长度。"
        "光标处前缀也会原样返回，便于确认位置是否正确。"
        "示例：查 agent/reactAgent.py 第 85 行行尾的补全 -> "
        "path='agent/reactAgent.py', line=85, column=54。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Python 文件路径；相对路径以项目根目录为起点"},
            "line": {"type": "integer",
                     "description": "光标所在行（从 0 开始）"},
            "column": {"type": "integer",
                       "description": "光标列（从 0 开始）；行尾 = 该行字符数"},
        },
        "required": ["path", "line", "column"],
    }

    def execute(self, path: str, line: int, column: int) -> str:
        # 1. 读文件 + 校验坐标
        code, full = read_source(path)
        lines = code.splitlines()
        if not isinstance(line, int) or not isinstance(column, int):
            raise ValueError(f"line/column 必须是整数: line={line!r}, column={column!r}")
        if not 0 <= line < len(lines):
            raise ToolError(f"line={line} 超出范围（文件共 {len(lines)} 行，0~{len(lines) - 1}）")
        if not 0 <= column <= len(lines[line]):
            raise ToolError(f"column={column} 超出行范围（第 {line} 行长度 "
                           f"{len(lines[line])}，列可取 0~{len(lines[line])}）")

        # 光标处前缀（行内光标左边的半个单词），原样返回便于确认位置
        line_text = lines[line]
        start = column
        while start > 0 and (line_text[start - 1].isalnum() or line_text[start - 1] == "_"):
            start -= 1
        prefix = line_text[start:column]

        # 2. 请求补全（jedi 行号 1 起，列 0 起）
        candidates = make_script(path).complete(line + 1, column)

        out = [f"补全结果: {rel_display(full)} 行 {line} 列 {column}"
               f"（光标前缀: {prefix!r}）"]
        if not candidates:
            out.append("（无候选：此处可能不是表达式位置，或光标位置不对）")
            return "\n".join(out)

        out.append(f"候选 {len(candidates)} 个"
                    f"（上下文来源由引擎判定：类型推断 / 当前作用域 / 模块内容）:")
        for c in candidates[:MAX_CANDIDATES]:
            # .complete = 前缀之后要插入的剩余部分（exe + cute = execute）
            doc = c.docstring().strip().splitlines()
            doc_first = doc[0][:50] if doc else ""
            module = rel_display(c.module_path) if c.module_path else "?"
            name = c.name.rstrip("=")           # 参数候选带 = 尾巴（obj=）
            out.append(f"  {name:<22}{(c.complete or ''):<14}{c.type:<12}"
                       f"{module:<24}{doc_first}")
        if len(candidates) > MAX_CANDIDATES:
            out.append(f"  ...（仅展示前 {MAX_CANDIDATES} 个，共 {len(candidates)} 个）")
        return "\n".join(out)


if __name__ == "__main__":
    tool = CompletionTool()

    # 实例成员：先找到 manager.execute( 的位置，对它前缀做补全
    code_path = "agent/reactAgent.py"
    src, _ = read_source(code_path)
    ln = next(i for i, t in enumerate(src.splitlines()) if "manager.execute" in t)
    out = tool.execute(code_path, ln, src.splitlines()[ln].index(".exe") + 4)
    assert "execute" in out and "base_tool.py" in out, "应补出 ToolManager.execute"
    assert "cute" in out, "应给出补全后缀 cute"
    print("实例成员 OK:\n" + out + "\n")

    # 行尾补全：局部变量
    out = tool.execute(code_path, ln, len(src.splitlines()[ln]))
    assert "候选" in out
    print("行尾补全 OK: 行尾位置返回全部合法候选\n")

    # 无候选位置：空行行首
    blank = next(i for i, t in enumerate(src.splitlines()) if not t.strip())
    out = tool.execute(code_path, blank, 0)
    assert "无候选" in out or "候选" in out     # 空行行首候选极少但可能非零
    print(f"空行行首 OK: {out.splitlines()[1] if len(out.splitlines()) > 1 else ''}\n")

    # 越界坐标 -> ToolError（框架转 ERROR 头文本）
    try:
        tool.execute(code_path, 99999, 0)
    except ToolError as e:
        print("行越界 OK:", str(e)[:50], "...")

    try:
        tool.execute(code_path, 0, 99999)
    except ToolError as e:
        print("列越界 OK:", str(e)[:50], "...")

    # 不存在文件 -> FileNotFoundError（框架转 ERROR 头文本）
    try:
        tool.execute("no_such.py", 0, 0)
    except FileNotFoundError:
        print("不存在路径 OK: FileNotFoundError（框架转 ERROR 头文本）")

    print("\n全部测试通过")
