# -*- coding: utf-8 -*-
"""spawn_agent 工具：主模型派后台子 agent 的唯一入口。

description 就是路由表（WHEN / WHEN NOT）——主模型每轮都带着它做
触发判断，没有代码在"检测"；要调触发率改这段文本即可，不动代码。
其中"档位表"段落从 registry 的 route 字段自动拼装（_tier_table），
加新档位只改 registry 一处，这里不漂移；WHEN/WHEN NOT 是主模型视角
的行为策略，留在本文件。

执行链：查档位（READY 拦未开放档）→ supervisor.spawn（预算检查 +
落 agents 表）→ runtime.launch 起线程 → 毫秒级返回"已派出"。
报告不从这返回：子 agent 收尾后，由主对话的 on_turn 汇合屏障注入
（mychat/tui.py 的 _agent_barrier）——本回合内自动等全部完成再汇总。

任务书 prompt 必须自包含：子 agent 看不到主对话，背景/目标/报告
要求都要写进去，缺背景任务就废。
直跑自测：python tool/tool_spawn.py（假 runtime，不调真模型）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import registry, runtime, supervisor
from base_tool import Tool, ToolError

from applog import get_logger

log = get_logger(__name__)

# 汇合机制说明（写给主模型的行为约定，也是本档位的真实语义）
_REPORT_HINT = "派出后立即返回，不要轮询等待；所有已派出的子 agent 完成后，它们的报告会在本回合内自动注入对话。"


def _tier_table() -> str:
    """档位表从 registry 自动拼装——单点事实来源，加档改 registry 即可。

    route 字段写判据（什么形状的任务选我），label 括注，未开放档
    自动带标注（和 READY 门禁同一份事实），主模型不用试错。"""
    lines = []
    for spec in registry.REGISTRY.values():
        mark = "" if spec.name in registry.READY else "（尚未开放）"
        lines.append(f"- {spec.name}（{spec.label}）{mark}: {spec.route}")
    return "\n".join(lines)


def _tier_list() -> str:
    """agent_type 参数用的短清单，同样自动拼装（和档位表不漂移）。"""
    return " / ".join(f"{s.name}({s.label})"
                      for s in registry.REGISTRY.values())


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    description = (
        "派出后台子 agent 独立完成子任务（一次可派多个，各发一个调用）。"
        "agent_type 选档位，prompt 是自包含任务书——子 agent 看不到当前对话，"
        "背景、目标、报告要求必须全写进 prompt。\n"
        "档位：\n" + _tier_table() + "\n"
        "WHEN 派（命中任一条即派）：\n"
        "1. 调研需要读大量文件内容 → explore\n"
        "2. 任务要跑起来才知道结果 → verify\n"
        "3. 改写型任务可独立闭环 → general\n"
        "4. 用户一条消息含多个互相独立的任务 → 并发各派一个\n"
        "WHEN NOT 派：\n"
        "1. 一句话能答、读一两个文件就够 → 自己干\n"
        "2. 任务强依赖当前对话上下文（拆不出去）→ 自己干\n"
        "3. 用户明确说'别派子任务'/'直接干' → 服从\n"
        "纪律：prompt 要写得具体（目标、范围、报告格式）；"
        + _REPORT_HINT
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_type": {"type": "string",
                           "description": "档位: " + _tier_list()},
            "prompt": {"type": "string",
                       "description": "自包含任务书：背景、目标、范围、"
                                      "报告要求全在这里（子 agent 看不到"
                                      "当前对话）"},
        },
        "required": ["agent_type", "prompt"],
    }

    def execute(self, agent_type: str, prompt: str) -> str:
        if not prompt or not prompt.strip():
            raise ToolError("prompt 不能为空：任务书必须自包含（子 agent "
                            "看不到当前对话）")
        try:
            spec = registry.get_spec(agent_type)
        except KeyError as e:
            log.warning("spawn 被拒: 未知档位 %s", agent_type)
            # KeyError 的 str() 会带一层引号，取原始消息给模型
            raise ToolError(e.args[0] if e.args else str(e))
        if agent_type not in registry.READY:
            log.warning("spawn 被拒: %s 档尚未开放", agent_type)
            raise ToolError(
                f"{agent_type} 档尚未开放（当前开放: "
                f"{', '.join(sorted(registry.READY))}），请改用已开放的档位，"
                f"或自己直接完成该任务。")
        try:
            handle = supervisor.spawn(agent_type, prompt.strip())
        except supervisor.BudgetError as e:
            log.warning("spawn 被拒: 预算上限（%s）", agent_type)
            raise ToolError(str(e))     # 预算现状文本，模型自己排队/砍任务
        runtime. launch(handle)
        return (f"Agent {handle.id} launched ({agent_type} · {spec.label})。"
                f"任务书已受理。" + _REPORT_HINT)


if __name__ == "__main__":
    # 自测：打桩 runtime.run_agent（起真线程但跑假逻辑），不调真模型
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile
    import time as _time

    from db import db

    old_db = db.DB_PATH
    db.DB_PATH = Path(tempfile.mkdtemp(prefix="mychat-spawn-")) / "test.db"
    from agent import transcript
    transcript.TRANSCRIPT_DIR = db.DB_PATH.parent / "transcripts"
    db.init_db()
    supervisor._reset()

    # run_agent 打桩：直接收尾，让 launch/收尾链路真跑
    real_run = runtime.run_agent

    def stub_run(handle):
        transcript.append(handle.id, {"type": "user", "content": handle.prompt})
        supervisor.finish(handle.id, "done", f"[stub] {handle.prompt} 完成")

    runtime.run_agent = stub_run
    tool = SpawnAgentTool()

    # 1) 正常派出：返回文本含 id，后台（打桩）收尾
    r = tool.execute("explore", "调研 permission 模块的审批链路")
    assert "launched (explore" in r and "自动注入" in r, r
    agent_id = r.split()[1]
    _time.sleep(0.3)            # 等打桩线程收尾
    h = supervisor.get(agent_id)
    assert h and h.status == "done" and "[stub]" in h.report, h
    print("派出+收尾     OK:", r[:60], "…")

    # 1b) 档位表自动拼装：三档 route 判据全在，general 带未开放标注
    desc = tool.description
    for spec in registry.REGISTRY.values():
        assert f"{spec.name}（{spec.label}）" in desc, spec.name
        assert spec.route in desc, f"{spec.name} 判据应进路由表"
    assert "general（改写闭环）（尚未开放）" in desc, "未开放档应带标注"
    assert "explore（只读调研）:" in desc, "已开放档不应带标注"
    print("档位表拼装     OK: route 判据入表 + 未开放标注")

    # 2) 未开放档（M3 前 general 仍关着）：明确文本，不崩
    try:
        tool.execute("general", "改代码")
    except ToolError as e:
        assert "general 档尚未开放" in str(e) and "explore" in str(e), e
        assert "verify" in str(e), "开放列表应含 verify"
        print("未开放档     OK:", str(e)[:50], "…")
    else:
        raise AssertionError("未开放档应被拒")

    # 3) 未知档位 / 空任务书 / 预算文本（预算上限场景由 supervisor
    #    自测覆盖，这里验异常转换链路）
    try:
        tool.execute("hacker", "x")
    except ToolError as e:
        assert "未知的 agent 类型" in str(e)
        print("未知档位     OK")
    try:
        tool.execute("explore", "   ")
    except ToolError as e:
        assert "prompt 不能为空" in str(e)
        print("空任务书     OK")
    else:
        raise AssertionError("空 prompt 应被拒")

    # 4) 经 ToolManager 完整链路（真实注册 + execute + ERROR 头）
    runtime.run_agent = real_run
    sys.path.insert(0, str(Path(__file__).parent))
    from base_tool import ToolManager
    mgr = ToolManager(Path(__file__).parent)
    assert "spawn_agent" in mgr.tools
    runtime.run_agent = stub_run
    r = mgr.execute("spawn_agent",
                    {"agent_type": "nope", "prompt": "调研"})
    assert r.startswith("ERROR: 未知的 agent 类型"), r
    print("ToolManager   OK: 注册 + ERROR 头")

    runtime.run_agent = real_run
    db.DB_PATH = old_db
    print("\n全部测试通过")
