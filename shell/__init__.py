# -*- coding: utf-8 -*-
"""持久 cmd.exe 会话包：跨命令保留环境变量与当前目录。

位置在根目录（与 permission/ 同级，同属内部能力），不在 tool/ 下——
ToolManager 扫描根本不会碰到它（由 tool_cmd.py 调用）。
"""
