# -*- coding: utf-8 -*-
"""Agent 启动入口：加载 tool/ 下所有内部工具，交给 ReActAgent 使用。

用法:
    python agent/main.py 现在是几点？再算一下 123 乘以 456 等于多少

    # 或在代码中:
    from agent.main import create_agent, ask
    print(ask("帮我看看 C:/code/Main.java 的前 30 行"))
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tool.base_tool import ToolManager


def create_agent(tool_dir=None) -> ToolManager:
    """启动 agent：递归扫描 tool/ 下所有 tool_*.py（含子目录，如 tool/lsp/），
    注册进 agent 上下文，返回工具管理器（含对话所需的工具 schema）。"""
    return ToolManager(tool_dir or (Path(__file__).resolve().parent.parent / "tool"))


def ask(question: str, verbose: bool = True) -> str:
    """便捷入口：启动 agent 并提问，返回最终答案。"""
    from agent.reactAgent import react_agent
    return react_agent(question, verbose=verbose, tool_manager=create_agent())


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "现在是几点？在帮我看下reactAgent.py 文件的第30到40行"
    print(f"问题: {question}\n{'=' * 50}")
    print(f"\n[最终答案] {ask(question)}")
