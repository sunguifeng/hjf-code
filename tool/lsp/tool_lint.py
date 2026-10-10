# -*- coding: utf-8 -*-
"""lint 工具：严格校验 Python 文件（只读）。

三层校验一次跑完：语法（jedi）+ 语义（pyflakes）+ 格式（pycodestyle/PEP 8），
输出行号从 0 开始，与 read_file_range 的 start 一致，可直接衔接翻页读取。

注意：tool/lsp/ 整个目录在 ToolManager 的排除清单（INTERNAL_ONLY_DIRS）里，
不注册为 agent 工具——检验已由 edit_file 写盘后自动跑（同一份逻辑
lsp_engine.check_file）。手动直跑自测仍然可用：python tool/lsp/tool_lint.py
"""

import sys
from pathlib import Path

# base_tool.py 在上一级目录（tool/），直接运行本文件时需要它可导入
_TOOL_DIR = Path(__file__).resolve().parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

from base_tool import Tool, ToolError
from lsp_engine import check_file

# 每层最多展示的问题条数（长文件防刷屏）
MAX_ISSUES = 30


class LintTool(Tool):
    name = "lint"
    description = (
        "只读工具：严格校验一个 Python 文件，三层检查一次跑完——"
        "语法错误（编译不过）、语义问题（未定义名 / 导入未使用等，pyflakes）、"
        "格式问题（PEP 8，如缺空格 / 空行数不对，pycodestyle）。"
        "返回分节报告，每节列出 [行号] 问题描述，行号从 0 开始"
        "（与 read_file_range 的 start 一致，可直接用它读取对应行）。"
        "三层全过时返回通过信息。"
        "示例：校验项目里的 agent/reactAgent.py -> path='agent/reactAgent.py'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Python 文件路径；相对路径以项目根目录为起点"},
        },
        "required": ["path"],
    }

    def execute(self, path: str) -> str:
        # 三层检查 + 分节报告都在 lsp_engine.check_file（edit 工具写盘后
        # 也自动调它，一份逻辑两处用）
        return f"lint 结果: {path}\n" + check_file(path, max_issues=MAX_ISSUES)


if __name__ == "__main__":
    import tempfile

    tool = LintTool()

    # 坏样例：语法 + 语义 + 格式 全有问题
    bad = "import os\nx=1\ndef f( a ):\n    return a + undefined_name\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(bad)
        bad_path = tf.name
    out = tool.execute(bad_path)
    assert "invalid syntax" not in out          # 语法没错，但语义/格式有
    assert "undefined name 'undefined_name'" in out, "应抓到未定义名"
    assert "E225" in out, "应抓到格式问题 E225"
    print("坏样例 OK:\n" + out + "\n")

    # 语法错误样例
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("def broken(:\n    pass\n")
    out = tool.execute(bad_path)
    assert "invalid syntax" in out, "应抓到语法错误"
    print("语法错误 OK:\n" + out.splitlines()[2] + "\n")

    # 好样例：全过
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("import os\n\n\nprint(os.getcwd())\n")
    out = tool.execute(bad_path)
    assert "全部通过" in out, "干净文件应返回通过"
    print("好样例 OK:", out.splitlines()[-1])

    # 项目自身文件
    out = tool.execute("agent/reactAgent.py")
    assert out.startswith("lint 结果"), "应能 lint 项目文件"
    print("\n项目文件 agent/reactAgent.py:")
    print(out if len(out) < 600 else out[:600] + "\n...（截断展示）")

    # 不存在 -> FileNotFoundError（框架会转 ERROR 头文本）
    import os
    try:
        tool.execute("no_such.py")
    except FileNotFoundError:
        print("不存在路径 OK: FileNotFoundError（框架转 ERROR 头文本）")
    else:
        raise AssertionError("不存在的路径应抛 FileNotFoundError")
    os.remove(bad_path)

    print("\n全部测试通过")
