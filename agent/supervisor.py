  # -*- coding: utf-8 -*-
"""supervisor：子 agent 全生命周期登记簿（进程级单例，同 permission_service）。

职责收得很窄——只管"登记簿"：
    spawn    分配 id、建 handle、落 agents 表（本阶段不起线程，
             线程由 M1 的 runtime 起并把 handle.thread 填上）
    finish   落表（status/report）、set done_event、更新 handle
    running_handles / get / cancel_all  给汇合屏障和退出清场用

预算上限不藏私：达到上限时 spawn 不报错，返回现状文本由调用方转告
模型（让它自己决定排队还是砍任务）。

done_event 是"本轮内汇合"设计的支点：主 agent 的 on_turn 屏障在
上面 wait()，子 agent 收尾时 set()。

直跑自测：python agent/supervisor.py（MYCHAT_DB 指临时库，不污染真实数据）
"""

import os
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger
from db import db
from agent.registry import get_spec
from model.model import get_usage_context

log = get_logger(__name__)

# 预算上限（常量，跑真实对话后再调）
MAX_CONCURRENT = 5        # 同时 running 的子 agent 上限
MAX_PER_SESSION = 10       # 单主会话累计派出上限

# 进程级单例状态（模块导入即初始化；测试用 _reset 清场）
_handles: dict[str, "AgentHandle"] = {}
_lock = threading.Lock()

# 子 agent 挂靠的主会话 uuid：TUI 启动/切会话时登记；工具层不感知
# 会话概念，spawn 落空时回退 "cli"（直跑命令行的主 agent）
current_parent_id: str | None = None


def set_parent(session_id: str | None) -> None:
    """TUI 在开新会话/切换会话时调用。"""
    global current_parent_id
    current_parent_id = session_id


class AgentHandle:
    """一个子 agent 的登记项。线程/信箱字段随里程碑逐步启用。"""

    def __init__(self, agent_id: str, spec, prompt: str, parent_id: str,
                 dialog_id: int | None = None):
        self.id = agent_id
        self.spec = spec
        self.prompt = prompt
        self.parent_id = parent_id
        self.dialog_id = dialog_id     # spawn 时刻主 agent 所在对话（token 归属用）
        self.status = "running"     # running / done / failed / cancelled
        self.report = ""
        self.done_event = threading.Event()
        self.thread = None         # M1: runtime 起的线程
        self.transcript = None     # M1: JSONL 路径

    def __repr__(self):
        return (f"<AgentHandle {self.id} {self.spec.name} "
                f"{self.status}>")


def spawn(spec_name: str, prompt: str, parent_id: str | None = None) -> AgentHandle:
    """登记一个子 agent：查表 → 分配 id → 落表 → 存 handle。
    parent_id 缺省用 current_parent_id（TUI 登记的当前会话），再落空
    记作 "cli"。达上限时抛 BudgetError（调用方转成给模型的现状文本）。"""
    spec = get_spec(spec_name)          # 未知类型 KeyError，调用方处理
    if parent_id is None:
        parent_id = current_parent_id or "cli"
    with _lock:
        running = [h for h in _handles.values() if h.status == "running"]
        if len(running) >= MAX_CONCURRENT:
            raise BudgetError(
                f"已达并发上限（{MAX_CONCURRENT}），运行中: "
                + ", ".join(f"{h.id}({h.spec.name})" for h in running)
                + "。可稍后再派，或等现有任务完成。")
        mine = [h for h in _handles.values() if h.parent_id == parent_id]
        if len(mine) >= MAX_PER_SESSION:
            raise BudgetError(
                f"本会话累计已派 {len(mine)} 个子 agent（上限 "
                f"{MAX_PER_SESSION}），不再受理新任务。")
        agent_id = "agent-" + uuid.uuid4().hex[:6]
        while agent_id in _handles:      # 万一撞车（6 位 hex 撞概率极低）
            agent_id = "agent-" + uuid.uuid4().hex[:6]
        # token 归属：ContextVar 过不了线程，spawn 时（还在主 agent 线程，
        # 上下文齐全）把 dialog_id 捕获到 handle 上，由 runtime 捎进子线程
        ctx = get_usage_context()
        handle = AgentHandle(agent_id, spec, prompt, parent_id,
                             dialog_id=ctx[1] if ctx else None)
        db.register_agent(agent_id, parent_id, spec_name, prompt)
        _handles[agent_id] = handle
        log.info("spawn %s(%s) parent=%s task=%.100s",
                 agent_id, spec_name, parent_id, prompt)
        return handle


def finish(agent_id: str, status: str, report: str) -> None:
    """收尾：落表 + set done_event + 更新 handle。幂等：重复收尾忽略。"""
    with _lock:
        handle = _handles.get(agent_id)
        if handle is None or handle.status != "running":
            return
        handle.status = status
        handle.report = report
    db.finish_agent(agent_id, status, report)
    log.info("finish %s %s report=%.200s",
             agent_id, status, report)
    handle.done_event.set()      # 屏障在这醒来


def running_handles() -> list[AgentHandle]:
    """当前 running 的 handle 列表（汇合屏障每轮问一次）。"""
    with _lock:
        return [h for h in _handles.values() if h.status == "running"]


def get(agent_id: str) -> AgentHandle | None:
    with _lock:
        return _handles.get(agent_id)


def handles_of(parent_id: str) -> list[AgentHandle]:
    """某主会话名下的全部 handle（含已结束的）。"""
    with _lock:
        return [h for h in _handles.values() if h.parent_id == parent_id]


def cancel_all() -> None:
    """进程退出清场用：把还没收尾的统统置 cancelled 并放行屏障。
    正在跑的线程杀不掉（Python 无安全取消），但状态和 DB 会如实记录，
    事件也会 set——等在屏障上的主对话不会被晾着。"""
    with _lock:
        pending = [h for h in _handles.values() if h.status == "running"]
        for h in pending:
            h.status = "cancelled"
    for h in pending:
        try:
            db.finish_agent(h.id, "cancelled", "应用退出，被取消")
        except Exception:
            log.exception("取消时落表失败 %s", h.id)
        h.done_event.set()


def _reset() -> None:
    """自测清场专用：进程内单例还原。"""
    global _handles
    _handles = {}
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class BudgetError(Exception):
    """预算上限（并发/单会话累计）——可预期异常，转成文本告诉模型。"""


if __name__ == "__main__":
    # 自测：临时库隔离 + 全生命周期
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile

    old_db = db.DB_PATH
    db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
    db.init_db()
    session = db.new_session()
    _reset()

    # 1) spawn：登记 + 落表 + 查表可见
    h1 = spawn("explore", "调研权限链路", session)
    h2 = spawn("verify", "跑 shell 自测", session)
    assert h1.id.startswith("agent-") and len(h1.id) == 12
    rows = db.list_agents(parent_id=session)
    assert {r["id"] for r in rows} == {h1.id, h2.id}
    assert all(r["status"] == "running" for r in rows)
    assert running_handles() == [h1, h2]
    print("spawn 登记      OK:", h1, h2)

    # 2) finish：落表 + 事件 + 幂等
    finish(h1.id, "done", "审批链路报告…")
    assert h1.done_event.is_set()
    assert [h.id for h in running_handles()] == [h2.id]
    row = next(r for r in db.list_agents(status="done") if r["id"] == h1.id)
    assert row["report"] == "审批链路报告…" and row["finished_at"]
    finish(h1.id, "done", "再来一次")       # 重复收尾忽略
    assert h1.report == "审批链路报告…", "重复收尾不应覆盖"
    print("finish 收尾    OK: 落表 + 事件 + 幂等")

    # 3) get / handles_of
    assert get(h2.id) is h2 and get("agent-nope") is None
    assert handles_of(session) == [h1, h2]
    print("get / 过滤     OK")

    # 4) 未知类型 → KeyError（spawn 工具层转 ERROR 文本）
    try:
        spawn("hacker", "x", session)
    except KeyError:
        print("未知类型       OK: KeyError")

    # 5) 并发上限：全局 running 满 5 后第 6 个被拒
    hs = [spawn("explore", f"任务{i}", session) for i in range(4)]
    assert len(running_handles()) == MAX_CONCURRENT, "h2 + 4 个 = 5 满员"
    try:
        spawn("explore", "第 6 个", session)
    except BudgetError as e:
        assert "并发上限" in str(e) and h2.id in str(e), e
        print("并发上限       OK:", str(e)[:60], "…")
    else:
        raise AssertionError("满员后第 6 个应被拒")

    # 6) 完成的让位：h2 收尾释放一个并发位
    finish(h2.id, "done", "自测通过")
    h7 = spawn("verify", "让位后再派", session)
    assert len(running_handles()) == MAX_CONCURRENT
    print("让位再派       OK: done 释放并发位")

    # 7) 单会话累计上限：边收边派填满 10 个，第 11 个被拒
    for h in hs[:3]:
        finish(h.id, "done", "x")             # 腾并发位
    while len(handles_of(session)) < MAX_PER_SESSION:
        finish(spawn("explore", "凑数", session).id, "done", "x")
    assert len(handles_of(session)) == MAX_PER_SESSION
    try:
        spawn("explore", "超额", session)
    except BudgetError as e:
        assert "本会话累计" in str(e), e
        print("会话累计上限   OK:", str(e)[:50], "…")
    else:
        raise AssertionError("单会话超额应被拒")

    # 8) cancel_all：running 全部置 cancelled + 事件放行
    finish(h7.id, "done", "x")        # 凑一个 done，cancel_all 应不动它
    cancel_all()
    assert not running_handles(), "清场后不应有 running"
    cancelled = db.list_agents(status="cancelled")
    assert all(r["report"] == "应用退出，被取消" for r in cancelled)
    assert not any(r["id"] == h7.id for r in cancelled), "done 不应被改写"
    print("cancel_all      OK: running 全部收尾，done 不动")

    db.DB_PATH = old_db
    print("\n全部测试通过")
