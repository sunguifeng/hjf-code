# -*- coding: utf-8 -*-
"""registry：子 agent 档位登记表——扫描 config/agents.json 注册。

档位 = 工具白名单 + shell 隔离级别，二者合起来就是"失败半径"的刻度：
    explore  零          —— 只读工具，想产生副作用都没有手段
    verify   只读命令     —— run_command 走只读白名单，白名单外直接拒
    general  一个 worktree —— 写操作全部圈在可丢弃的隔离分支里

配置管"档位是什么"（config/agents.json，加档改文件即可，tool_spawn
的路由表自动跟上）；代码管"档位开没开"——READY 是实现进度的门禁
（general 的 worktree/审批排队 M3 才落地，配置里写了也跑不起来），
不进配置。

AgentSpec.description 一文两用：起线程时它是子 agent 的系统提示词
（后台 agent 看不到主对话，纪律全在这里）。AgentSpec.route 则相反——
它是唯一进主模型路由表的字段（tool_spawn.py 拼档位表用），一句话写
"什么形状的任务选我"，必须写判据不写标签。

直跑自测：python agent/registry.py
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent

# 配置文件位置：默认 <项目根>/config/agents.json，
# MYCHAT_AGENTS_DIR 可覆盖（自测隔离、多套档位实验用）
CONFIG_PATH = Path(os.environ.get("MYCHAT_AGENTS_DIR", _ROOT / "config")) / "agents.json"

# AgentSpec 的全部合法字段——多一个键就报错，防手滑 typo 静默失效
_FIELDS = {"name", "label", "route", "description", "tools",
           "shell", "worktree"}
_SHELLS = {None, "readonly", "own"}


@dataclass
class AgentSpec:
    name: str                 # 档位名（spawn_agent 的 agent_type）
    label: str                # TUI 面板显示用的短标签
    description: str          # 子 agent 系统提示词（运行纪律，子 agent 视角）
    route: str                # 一句话派发判据（什么形状的任务选我）——
                              # 唯一进主模型路由表的字段，必须是"判据"不是
                              # "标签"：任务形状对上就选，职责罗列没有用
    tools: list[str] = field(default_factory=list)   # 工具白名单（按名过滤）
    shell: str | None = None  # None=无 shell / "readonly"=只读白名单 / "own"=独立常驻
    worktree: bool = False    # True = 写操作圈进 git worktree（general 档）


def _fail(entry_no: int, why: str) -> ValueError:
    """坏档错误：带配置路径 + 序号 + 原因，启动即定位。"""
    return ValueError(f"{CONFIG_PATH} 第 {entry_no} 个档位不合法: {why}")


def _load_config(path: Path = None) -> dict[str, AgentSpec]:
    """扫描并校验配置文件，构建 REGISTRY。纯函数：path 可注入，自测用。

    fail-fast：配置是开发者资产，坏了宁可不启动，不悄悄降级——
    坏一档在启动时就报清楚，比运行中派活时炸好。
    注意这里不校验"工具名是否真实存在"：那要加载全部工具模块，而
    tool_spawn 又依赖本模块（环形）。真实强制点在 spawn 时——
    ToolManager(only=白名单) 遇未知工具名会 KeyError；自测另兜底。"""
    path = Path(path or CONFIG_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"档位配置不存在: {path}（应有 config/agents.json）")
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} 不是合法 JSON: {e}")

    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} 应是非空数组，每个对象一个档位")

    known_tools = None     # 不在此校验工具名真实性，见 _load_config 文档
    registry: dict[str, AgentSpec] = {}
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise _fail(i, "应为 JSON 对象")
        unknown = set(item) - _FIELDS
        if unknown:
            raise _fail(i, f"未知字段 {sorted(unknown)}，合法字段: {sorted(_FIELDS)}")
        missing = _FIELDS - set(item)
        if missing:
            raise _fail(i, f"缺字段 {sorted(missing)}")
        name, label, route = item["name"], item["label"], item["route"]
        if not isinstance(name, str) or not name.strip() \
                or not all(c.isalnum() or c in "-_" for c in name):
            raise _fail(i, f"name {name!r} 应是非空字母数字/-/_ 串")
        if name in registry:
            raise _fail(i, f"档位名 {name!r} 重复")
        if not isinstance(item["description"], str) or not item["description"]:
            raise _fail(i, f"{name} description 不能为空")
        tools = item["tools"]
        if not isinstance(tools, list) or not tools \
                or not all(isinstance(t, str) for t in tools):
            raise _fail(i, f"{name} tools 应为非空字符串数组")
        shell = item["shell"]
        if shell not in _SHELLS:
            raise _fail(i, f"{name} shell 应为 null/'readonly'/'own'，"
                           f"实际 {shell!r}")
        worktree = item["worktree"]
        if not isinstance(worktree, bool):
            raise _fail(i, f"{name} worktree 应为 bool，实际 {worktree!r}")
        registry[name] = AgentSpec(name=name, label=label, description=item["description"],
                                   route=route, tools=list(tools), shell=shell,
                                   worktree=worktree)
    return registry


def _tool_names() -> list[str]:
    """真实工具名清单（自测兜底用；注册路径不调，见 _load_config 文档）。"""
    from tool.base_tool import ToolManager
    return list(ToolManager(_ROOT / "tool").tools)


# 进程启动扫描一次：REGISTRY = 配置内容；READY = 代码侧开放门禁。
# 档位开放进度（里程碑推进逐档放开）：
#   M1 explore / M2 verify —— 已开放
#   M3 general             —— 等 worktree 隔离 + 审批排队
# 配置里改 name 不会动这里；模型派了未开放档会收到明确文本，
# 由它自己降级处理（tool_spawn 的 READY 门禁）
READY = {"explore", "verify"}

try:
    REGISTRY: dict[str, AgentSpec] = _load_config()
except ValueError as e:
    raise SystemExit(f"[registry] {e}")


def get_spec(name: str) -> AgentSpec:
    """按名取档位；不存在时抛 KeyError（调用方转成给模型的错误文本）。"""
    if name not in REGISTRY:
        raise KeyError(f"未知的 agent 类型 {name!r}，可选: "
                       f"{', '.join(REGISTRY)}")
    return REGISTRY[name]


if __name__ == "__main__":
    # 自测：真配置三档断言 + 临时配置的坏档校验链路
    import tempfile

    # —— A. 真实仓库配置：三档完整、逐级扩大、查表正常 ——
    assert set(REGISTRY) == {"explore", "verify", "general"}, set(REGISTRY)
    assert READY <= set(REGISTRY), "开放档必须在登记表里"
    specs = list(REGISTRY.values())

    for s in specs:
        assert s.tools and s.description, s.name
        assert "简体中文" in s.description, f"{s.name} 报告语言须约束为简体中文"
        assert s.route, f"{s.name} 必须有派发判据（进主模型路由表）"
        assert s.label not in s.route, f"{s.name} route 应写任务形状，不是复述标签"

    exp, ver, gen = (REGISTRY["explore"], REGISTRY["verify"],
                     REGISTRY["general"])
    # 工具白名单逐级扩大（失败半径递增）
    assert set(exp.tools) < set(ver.tools) < set(gen.tools), \
        "三档工具集应逐级扩大"
    assert "edit_file" not in exp.tools and "edit_file" not in ver.tools
    assert "run_command" not in exp.tools
    # 隔离级别递增
    assert exp.shell is None and ver.shell == "readonly" and gen.shell == "own"
    assert gen.worktree and not exp.worktree and not ver.worktree

    # 工具名真实性兜底（注册路径不查——环形依赖；spawn 时 ToolManager
    # 的 only= 会 KeyError，这里提前兜住配置手滑）
    real = set(_tool_names())
    for s in specs:
        bad = [t for t in s.tools if t not in real]
        assert not bad, f"{s.name} 引用了不存在的工具: {bad}"
    print("真实配置       OK: 三档定义/递进关系/工具名全真实")

    # 查表 + 未知类型
    assert get_spec("explore") is exp
    try:
        get_spec("hacker")
    except KeyError as e:
        assert "explore" in str(e)
        print("未知类型       OK:", str(e)[:50], "…")
    else:
        raise AssertionError("未知类型应抛 KeyError")

    # —— B. 坏档校验：临时目录写配置，逐个断言错误文本 ——
    def cfg_error(content) -> str:
        d = Path(tempfile.mkdtemp(prefix="mychat-reg-"))
        p = d / "agents.json"
        p.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        try:
            _load_config(p)
        except ValueError as e:
            return str(e)
        raise AssertionError("坏配置不该注册成功")

    ok = {"name": "x", "label": "标签", "route": "判据一句话",
          "description": "提示词", "tools": ["ls"], "shell": None,
          "worktree": False}
    e = cfg_error([{**ok, "tools": []}])
    assert "应为非空字符串数组" in e, e
    print("空 tools        OK:", e[-45:])
    e = cfg_error([{**ok, "name": "x"}, {**ok, "name": "x"}])
    assert "重复" in e, e
    print("重名           OK:", e[-45:])
    e = cfg_error([{**ok, "desc": "手滑字段"}])
    assert "未知字段" in e, e
    print("未知字段       OK:", e[-45:])
    e = cfg_error([{k: v for k, v in ok.items() if k != "route"}])
    assert "缺字段" in e, e
    print("缺字段         OK:", e[-45:])
    e = cfg_error([{**ok, "shell": "root"}])
    assert "shell 应为" in e, e
    print("非法 shell     OK:", e[-45:])
    e = cfg_error([])
    assert "非空数组" in e, e
    print("空配置         OK:", e[-45:])

    import os as _os
    missing = Path(tempfile.mkdtemp(prefix="mychat-reg-")) / "agents.json"
    try:
        _load_config(missing)
    except ValueError as e:
        assert "不存在" in str(e), e
        print("配置缺失       OK:", str(e)[:60], "…")
    else:
        raise AssertionError("缺文件应报错")

    print("\n全部测试通过")
