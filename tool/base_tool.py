"""工具基类与加载器。

用法:
    from base_tool import ToolManager

    manager = ToolManager("tool")     # 初始化时递归加载 tool/ 下所有 tool_*.py
    tools = manager.get_openai_tools()   # OpenAI chat.completions 的 tools 参数
    result = manager.execute("read_file_range",
                             {"path": r"C:\\x.txt", "start": 0, "offset": 50})
"""

import importlib.util
import inspect
import sys
from abc import ABC, abstractmethod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger

log = get_logger(__name__)


class Tool():
    """所有工具的顶级父类。

    子类必须设置 name / description / parameters，并实现 execute。
    """

    name: str = ""          # 工具名称（注册键，模型调用的 function name）
    description: str = ""   # 工具描述（模型靠它决定何时调用）
    parameters: dict = {}   # OpenAI 参数 schema（JSON Schema，type=object）

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """统一的执行入口，返回文本结果。"""


class ToolError(Exception):
    """工具可预期的业务异常。manager 统一捕获并转成错误文本返回给模型。"""


# 不作为 agent 工具加载的子目录（转为内部能力，由其它工具自动调用）：
# lsp/ —— 检验（lint）由 edit_file 写盘后自动跑（lsp_engine.check_file），
#         references / completion 也不再暴露给 agent
INTERNAL_ONLY_DIRS = ("lsp",)


# 保证无论本模块以 "base_tool" 还是 "tool.base_tool" 被导入，
# 工具文件里的 `from base_tool import Tool` 拿到的都是同一个类对象
sys.modules.setdefault("base_tool", sys.modules[__name__])


class ToolManager:
    """初始化时扫描 tool_dir 下所有 tool_*.py，动态加载其中的 Tool 具体子类。

    execute 只统一处理 expected_exceptions 中的"可预期"异常（转成错误文本），
    其余异常原样抛出——通常是代码 bug，不应被吞掉。
    """

    # 可预期异常：转成错误文本返回；不在此列的异常原样抛出
    expected_exceptions = (ToolError, ValueError, OSError)

    def __init__(self, tool_dir="tool", exclude=INTERNAL_ONLY_DIRS,
                 only=None):
        # tool_dir 支持单个目录，也支持目录列表（多个目录的工具一起注册）。
        # exclude：整个排除的子目录名集合（转为内部能力的不再注册给 agent）
        # only：加载后按名过滤（子 agent 档位工具白名单）；None 注册全部。
        #        白名单里出现不存在的工具名直接报错——宁可启动失败，
        #        不可静默少给工具（registry 自测依赖这个行为）
        dirs = [tool_dir] if isinstance(tool_dir, (str, Path)) else list(tool_dir)
        self.exclude = set(exclude)
        self.tool_dirs = []
        for d in dirs:
            d = Path(d).resolve()
            if not d.is_dir():
                raise FileNotFoundError(f"工具目录不存在: {d}")
            # 让工具文件里的 `from xxx import ...` 直接可用
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            self.tool_dirs.append(d)
        self.tool_dir = self.tool_dirs[0]     # 兼容旧属性
        self.tools = {}   # name -> 工具实例
        self._load_all()
        if only is not None:
            missing = [n for n in only if n not in self.tools]
            if missing:
                raise KeyError(f"only 白名单引用了不存在的工具: {missing}")
            self.tools = {n: self.tools[n] for n in only}

    def _load_all(self):
        for d in self.tool_dirs:
            # 递归扫描（含子目录），排除内部能力子目录（如 tool/lsp/ 整个跳过）
            for py in sorted(d.rglob("tool_*.py")):
                if py.parent.name in self.exclude:
                    continue
                self._load_module(py)

    def _load_module(self, py: Path):
        module_name = py.stem   # 如 tool_read_range
        # 工具文件同目录的辅助模块（如 tool/lsp/lsp_engine.py）可直接 import
        if str(py.parent) not in sys.path:
            sys.path.insert(0, str(py.parent))
        spec = importlib.util.spec_from_file_location(module_name, py)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载工具模块: {py}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise ImportError(f"加载 {py.name} 失败: {e}") from e

        found = 0
        for _, cls in inspect.getmembers(module, inspect.isclass):
            # 只收本模块定义的、Tool 的具体（非抽象）子类
            if (issubclass(cls, Tool)
                    and cls is not Tool
                    and not inspect.isabstract(cls)
                    and cls.__module__ == module_name):
                self._register(cls())
                found += 1
        if found == 0:
            print(f"警告: {py.name} 中没有 Tool 的具体子类，已跳过")

    def _register(self, tool: Tool):
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError(f"{type(tool).__name__}: name 必须是非空字符串")
        if not isinstance(tool.description, str) or not tool.description:
            raise ValueError(f"{tool.name}: description 必须是非空字符串")
        if (not isinstance(tool.parameters, dict)
                or tool.parameters.get("type") != "object"):
            raise ValueError(f"{tool.name}: parameters 必须是 type=object 的 JSON Schema")
        if tool.name in self.tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self.tools[tool.name] = tool

    def get_openai_tools(self):
        """导出 OpenAI chat.completions 的 tools 参数格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools.values()
        ]

    def get_anthropic_tools(self):
        """导出 Anthropic messages API 的 tools 格式。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in self.tools.values()
        ]

    def execute(self, name: str, arguments: dict) -> str:
        """统一执行入口。只处理 expected_exceptions 中的可预期异常：
        转成固定 ERROR 头的错误文本返回（异常消息原样保留），由模型自行处理；
        其余异常原样抛出。"""
        if name not in self.tools:
            log.warning("工具 %s 未注册", name)
            return f"ERROR: 未注册的工具 {name}，可用工具: {', '.join(self.tools)}"
        try:
            return self.tools[name].execute(**(arguments or {}))
        except self.expected_exceptions as e:
            log.warning("工具 %s 失败: %s", name, e)
            return f"ERROR: {e}"


if __name__ == "__main__":
    # 自测：以 base_tool.py 所在目录为工具目录
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import json

    me = Path(__file__).resolve()
    manager = ToolManager(me.parent)

    assert "read_file_range" in manager.tools, "应加载 tool_read_range.py"
    # lsp 整目录排除：lint/completion/references 都不注册（转为内部能力）
    for t in ("lint", "completion", "references"):
        assert t not in manager.tools, f"{t} 应随 lsp 目录一起排除"
    print("已加载工具     OK:", list(manager.tools))

    # OpenAI 格式导出
    openai_tools = manager.get_openai_tools()
    assert all(t["type"] == "function" for t in openai_tools)
    names = [t["function"]["name"] for t in openai_tools]
    assert "read_file_range" in names
    print("OpenAI 格式    OK:", names)

    # Anthropic 格式导出
    anthropic_tools = manager.get_anthropic_tools()
    assert all(t["input_schema"]["type"] == "object" for t in anthropic_tools)
    assert {t["name"] for t in anthropic_tools} == set(names)
    print("Anthropic 格式 OK:", len(anthropic_tools), "个工具")

    # 正常执行：读本文件前 3 行
    head = manager.execute("read_file_range", {"path": str(me), "start": 0, "offset": 3})
    assert head.startswith('"""工具基类与加载器。'), "应读到本文件前 3 行"
    print("正常执行       OK:", repr(head[:30]), "...")

    # start 越界 -> 空字符串
    assert manager.execute("read_file_range",
                            {"path": str(me), "start": 99999, "offset": 10}) == ""
    print("start 越界     OK: 返回空字符串")

    # 工具内部异常 -> 固定 ERROR 头 + 原始异常消息
    r = manager.execute("read_file_range",
                        {"path": r"C:\no\such\file.txt", "start": 0, "offset": 5})
    assert r.startswith("ERROR: 文件不存在"), "异常应转成 ERROR 头的错误文本"
    print("异常转文本     OK:", r)

    # 参数校验异常 -> 固定 ERROR 头
    r = manager.execute("read_file_range", {"path": str(me), "start": -1, "offset": 5})
    assert r.startswith("ERROR: start"), "非法参数应转成 ERROR 头的错误文本"
    print("非法参数       OK:", r)

    # 未知工具
    r = manager.execute("not_exist", {})
    assert r.startswith("ERROR: 未注册的工具"), "未知工具应返回 ERROR 头的错误文本"
    print("未知工具       OK:", r)

    # 截断异常（TooManyLinesError -> ToolError）-> ERROR 头 + 原始消息，含翻页位置
    import os
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as tf:
        for i in range(2100):
            tf.write(f"line-{i:03d} " + "x" * 40 + "\n")
        big = os.path.abspath(tf.name)
    r = manager.execute("read_file_range", {"path": big, "start": 0, "offset": 9999})
    assert r.startswith("ERROR: 单次最多读取") and "start=2000" in r, \
        "截断应转成 ERROR 头文本并含下一页位置"
    print("截断异常       OK:", r[:80], "...")
    os.remove(big)

    # 未预期的异常 -> 原样抛出，不做统一处理
    class _BrokenTool(Tool):
        name = "_broken_for_test"
        description = "测试用"
        parameters = {"type": "object", "properties": {}}

        def execute(self):
            raise RuntimeError("未预期的 bug")

    manager.tools["_broken_for_test"] = _BrokenTool()
    try:
        manager.execute("_broken_for_test", {})
    except RuntimeError:
        print("未预期异常     OK: 原样抛出，未被统一处理")
    else:
        raise AssertionError("未预期异常应原样抛出")
    finally:
        del manager.tools["_broken_for_test"]

    print("\n全部测试通过")
