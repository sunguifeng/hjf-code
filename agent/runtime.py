# -*- coding: utf-8 -*-
"""runtime：子 agent 线程入口——装配工具集、跑 ReAct、落转录、收尾。

子 agent 就是同一个 react_agent，只是换三样东西：
    工具集    ToolManager(only=spec.tools)   —— 白名单即安全边界
    系统提示  spec.description               —— 后台 agent 看不到主对话，
              纪律全在档位说明里
    上下文    question=任务书（自包含），history 从零开始
             —— 中间过程全烧在线程局部 messages，主对话一个字节不进

launch() 毫秒级返回（起线程不等待）；run_agent 是线程本体。
M2 起 verify/general 档会在这里加自己的 shell / worktree 装配。

直跑自测：python agent/runtime.py（假 agent + 临时库，不调真模型）
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import supervisor, transcript
from agent.reactAgent import react_agent
from model.model import set_usage_context
from tool.base_tool import ToolManager

from applog import get_logger

_ROOT = Path(__file__).resolve().parent.parent

log = get_logger(__name__)

# verify 档的命令次数预算（防失控刷命令；拒绝文本会带回剩余数）
VERIFY_MAX_CALLS = 15


def launch(handle) -> threading.Thread:
    """起子 agent 线程并登记到 handle。daemon=True：进程退出不陪葬。"""
    t = threading.Thread(target=run_agent, args=(handle,),
                         name=handle.id, daemon=True)
    handle.thread = t
    t.start()
    return t


def run_agent(handle) -> None:
    """线程入口：转录任务书 → 装配档位 → 跑 ReAct → 收尾。任何异常都
    转为 failed 状态收尾——done_event 必 set，主对话的汇合屏障永远不会
    被晾死。收尾顺带收掉该 agent 的专属 shell（懒起，下次复活再建）。"""
    # token 归属：裸线程拿到的是空上下文，必须显式 set——子 agent 的
    # 调用记到主会话、spawn 时刻所属的对话名下，agent_id 标记是谁花的
    set_usage_context(handle.parent_id, handle.dialog_id, handle.id)
    transcript.append(handle.id, {"type": "user", "content": handle.prompt})
    handle.transcript = transcript.path_for(handle.id)
    try:
        manager = ToolManager(_ROOT / "tool", only=handle.spec.tools)
        _dress_tools(handle, manager)   # 档位换装（只读 shell 等）
        report = react_agent(
            handle.prompt,
            verbose=False,
            tool_manager=manager,
            system_prompt=handle.spec.description,   # 档位说明当系统提示词
            on_step=lambda ev, data: transcript.append(
                handle.id, {"type": ev, "content": data}),
        )
        transcript.append(handle.id,
                          {"type": "final", "content": report})
        supervisor.finish(handle.id, "done", report)
    except Exception as e:
        log.exception("子 agent %s 崩溃", handle.id)
        supervisor.finish(handle.id, "failed",
                          f"后台运行出错 {type(e).__name__}: {e}")
    finally:
        if handle.spec.shell:
            from agent.shell_pool import close_agent_shell
            close_agent_shell(handle.id)


def _dress_tools(handle, manager) -> None:
    """按档位换装工具实例。白名单 ToolManager 装的是默认实例（主会话
    单例 shell + 审批），档位需要注入自己的实例：

    verify（spec.shell == "readonly"）: run_command 换成只读变体——
        自己的 shell（shell_pool 懒起）+ 只走白名单 + 15 次预算。
        安全即白名单：测试/查看命令直接跑，改状态的一律拒，无人审批。
    general（"own"，M3）：等 worktree 落地后在此注入 worktree 版实例。"""
    if handle.spec.shell == "readonly":
        from agent.shell_pool import get_agent_shell
        from tool.tool_cmd import RunCommandTool
        manager.tools["run_command"] = RunCommandTool(
            shell_getter=lambda: get_agent_shell(handle.id),
            readonly=True,
            max_calls=VERIFY_MAX_CALLS,
        )


if __name__ == "__main__":
    # 自测：假 react_agent + 临时库/转录目录全隔离，不调真模型。
    # db 模块在 import 时就固化了 DB_PATH，所以这里直接改属性而非改 env
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mychat-runtime-"))
    old_td = transcript.TRANSCRIPT_DIR
    transcript.TRANSCRIPT_DIR = tmp / "transcripts"

    # react_agent 打桩：验证传参 + 产出可预期报告
    calls = []

    def fake_react_agent(question, **kw):
        calls.append(kw)
        assert question == "调研任务书"
        # 模拟子 agent 干活：走一遍 on_step 事件链
        on_step = kw["on_step"]
        on_step("thinking", "先定位再精读")
        on_step("action", "grep('permission')")
        on_step("observation", "permission/permission.py:66:...")
        return "调研结论：三道闸"

    runtime_react = react_agent
    globals()["react_agent"] = fake_react_agent

    from db import db
    old_db = db.DB_PATH
    db.DB_PATH = tmp / "test.db"
    db.init_db()
    supervisor._reset()
    session = db.new_session()

    # 1) launch 即返回（<1s）且线程干活后 handle 全链收尾
    import time as _time
    handle = supervisor.spawn("explore", "调研任务书", session)
    t0 = _time.monotonic()
    launch(handle)
    launch_elapsed = _time.monotonic() - t0
    assert launch_elapsed < 1.0, "launch 应毫秒级返回"
    assert handle.done_event.wait(10), "假 agent 跑完后 done_event 应 set"
    assert handle.status == "done" and handle.report == "调研结论：三道闸"
    print("launch 秒回    OK: %.3fs" % launch_elapsed)

    # 2) 传参断言：白名单工具集 + 档位系统提示
    kw = calls[0]
    assert set(kw["tool_manager"].tools) == {"ls", "read_file_range", "grep"}, \
        "explore 白名单应只有三个只读工具"
    assert "explore" in kw["system_prompt"], kw["system_prompt"][:30]
    assert "后台调研 agent" in kw["system_prompt"], "系统提示应是档位说明"
    assert "spawn_agent" not in kw["tool_manager"].tools, \
        "子 agent 不能再派子 agent（白名单天然防递归）"
    print("装配断言      OK: 白名单/系统提示/防递归")

    # 3) 转录：任务书 + 三事件 + final 全在，行行可解析
    rows = transcript.read_all(handle.id)
    types = [r["type"] for r in rows]
    assert types == ["user", "thinking", "action", "observation", "final"], types
    assert rows[-1]["content"] == "调研结论：三道闸"
    print("转录完整      OK:", types)

    # 4) DB 落表：done + report
    row = db.list_agents(status="done")[0]
    assert row["id"] == handle.id and row["report"] == "调研结论：三道闸"
    print("落表          OK: done + report")

    # 5) 子 agent 崩溃 → failed 收尾，事件照 set（屏障不被晾死）
    def boom(question, **kw):
        raise RuntimeError("接口超时")
    globals()["react_agent"] = boom
    h2 = supervisor.spawn("explore", "会崩的任务", session)
    launch(h2)
    assert h2.done_event.wait(10)
    assert h2.status == "failed" and "接口超时" in h2.report
    print("崩溃兜底      OK: failed + 事件放行")

    # 6) verify 档装配：run_command 换装只读变体（自己的 shell + 预算）
    seen = {}

    def spy_react_agent(question, **kw):
        seen.update(kw)
        return "验证结论"

    globals()["react_agent"] = spy_react_agent
    hv = supervisor.spawn("verify", "跑 shell 自测", session)
    launch(hv)
    assert hv.done_event.wait(10)

    # 6a) 收尾即收壳：池里已无该 agent 的条目（须在触发懒起之前验）
    from agent.shell_pool import _shells, close_agent_shell, get_agent_shell
    assert hv.id not in _shells, "收尾应收掉专属 shell"

    run_tool = seen["tool_manager"].tools["run_command"]
    assert run_tool._readonly is True, "verify 的 run_command 应是只读变体"
    assert run_tool._max_calls == VERIFY_MAX_CALLS
    # 6b) 白名单外零执行零审批（readonly 拒绝不落 shell）
    try:
        run_tool.execute("copy NUL z.txt", 5)
    except Exception as e:
        assert "readonly 模式" in str(e), e
    else:
        raise AssertionError("verify 档白名单外应被拒")
    # 6c) shell_getter 落到 shell_pool：按 agent_id 取到隔离活壳
    #     （getter 是懒起语义，这里会重建一个——取完手动收走）
    sh = run_tool._shell_getter()
    assert sh is get_agent_shell(hv.id) and sh.alive, "shell 应从 pool 懒起"
    close_agent_shell(hv.id)
    print("verify 装配    OK: 只读变体 + 收尾收壳 + 自己的 shell")

    globals()["react_agent"] = runtime_react
    transcript.TRANSCRIPT_DIR = old_td
    db.DB_PATH = old_db
    print("\n全部测试通过")
