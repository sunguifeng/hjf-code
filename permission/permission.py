# -*- coding: utf-8 -*-
"""permission：工具写操作的审批服务（进程内，无 UI 依赖）。

三道闸（顺序固定）：
1. ask_ui 未注册（headless：CLI 自测 / reactAgent 直跑）→ 直接放行
2. (tool, action, path) 在 _granted 缓存里（用户按过 s）→ 直接放行
3. 回调 ask_ui 把请求投给 UI，agent 线程 Event.wait() 挂起等按键

ask_ui 只负责"把请求投给 UI"，必须立即返回、不许阻塞——
等待（以及取消）是 service 自己的事（request 里 Event.wait）。
UI 侧决策后调 allow / allow_for_session / deny 唤醒 agent 线程。

投递机制：agent 线程经 ask_ui 回调发 Textual 的 post_message
（线程安全），UI 侧挂审批块；挂起/唤醒用 threading.Event。

缓存的边界：
- 会话级：内存 set，不落库，进程重启即清空（"session" = 进程生命周期）
- 文件级：键是精确文件相对路径——s 过 banner.txt 只免审 banner.txt，
  换文件照问（目录级放行等于放行整个盘符，不做）
- 通配级：路径位放 ANY_FILE 表示"本会话内该工具+动作全部免审"
  （审批块第 4 选项"允许本会话中所有文件的修改"）
"""

import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger

log = get_logger(__name__)

# 会话级通配键：路径位置放它 = 本会话内该 (tool, action) 全部免审
ANY_FILE = "*"


@dataclass
class PermissionRequest:
    """一次写操作审批请求。diff 由工具生成好带过来，UI 不自己算。"""
    tool: str        # 工具名（"edit_file"，将来 bash/fetch 复用）
    action: str       # 动作类型（"write"）
    path: str         # 相对项目根的文件路径（缓存键的一部分）
    diff: str = ""    # 将要写入的改动（审批块渲染用）
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class PermissionService:
    """审批状态机 + 等待队列。agent 线程调 request，UI 线程调三档响应。"""

    def __init__(self):
        self.ask_ui = None                # UI 回调：Callable[[PermissionRequest], None]
        self._granted: set = set()        # (tool, action, path) 会话级授权缓存
        self._pending: dict = {}          # id -> Event（等待用户按键的请求）
        self._results: dict = {}          # id -> bool（决策结果）
        self._lock = threading.Lock()     # agent 线程读、UI 线程写，都要持锁

    def request(self, req: PermissionRequest) -> bool:
        """agent 线程调用：True 放行，False 拒绝（拒绝语义由工具层处理）。"""
        if self.ask_ui is None:           # 闸 1：headless 自动放行
            return True
        key = (req.tool, req.action, req.path)
        with self._lock:
            if (key in self._granted                # 闸 2a：这个文件 s 过
                    or (req.tool, req.action, ANY_FILE) in self._granted):
                return True                           # 闸 2b：本会话全 s 过
            ev = threading.Event()
            self._pending[req.id] = ev
        try:
            log.info("审批请求: %s %s %s", req.tool, req.action, req.path)
            self.ask_ui(req)              # 闸 3：投给 UI（只上屏，立即返回）
            ev.wait()                     # agent 线程挂起，等按键唤醒
        finally:
            with self._lock:
                self._pending.pop(req.id, None)
        with self._lock:
            granted = self._results.pop(req.id, False)
        log.info("审批结果: %s %s -> %s", req.tool, req.path,
                 "允许" if granted else "拒绝")
        return granted

    # ---- UI 线程调用的三档响应 ----

    def allow(self, req: PermissionRequest) -> None:
        """a：只放行这一次（不进缓存）。"""
        self._respond(req, granted=True, remember=False)

    def allow_for_session(self, req: PermissionRequest) -> None:
        """s：放行 + 进缓存，本会话内同一文件的同类操作不再问。"""
        self._respond(req, granted=True, remember=True)

    def allow_for_session_all(self, req: PermissionRequest) -> None:
        """放行 + 进通配缓存：本会话内该工具+动作（不限文件）不再问。"""
        self._respond(req, granted=True, remember=True, wildcard=True)

    def deny(self, req: PermissionRequest) -> None:
        """d / esc：拒绝。"""
        self._respond(req, granted=False, remember=False)

    def cancel_all(self) -> None:
        """唤醒所有等待中的请求并一律拒绝（TUI 退出 / 会话中止时防挂死）。"""
        with self._lock:
            events = list(self._pending.values())
            for rid in self._pending:
                self._results[rid] = False
            self._pending.clear()
        for ev in events:
            ev.set()

    def _respond(self, req: PermissionRequest, granted: bool, remember: bool,
                 wildcard: bool = False) -> None:
        """写结果并唤醒等待的 agent 线程。请求已不存在（过期/被取消）则忽略。
        wildcard：缓存键路径位放 ANY_FILE（会话内不限文件）。"""
        with self._lock:
            ev = self._pending.get(req.id)
            if ev is None:
                return
            if granted and remember:
                key = (req.tool, req.action,
                       ANY_FILE if wildcard else req.path)
                self._granted.add(key)
            self._results[req.id] = granted
        ev.set()


# 全局单例：工具（agent 线程）和 tui（UI 线程）共享同一份状态
permission_service = PermissionService()


if __name__ == "__main__":
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import time as _time

    # 1) headless：未注册 ask_ui 直接放行，不阻塞
    svc = PermissionService()
    req = PermissionRequest(tool="edit_file", action="write", path="a.txt", diff="...")
    assert svc.request(req) is True
    print("headless 放行  OK")

    # 2) 三档：asker 在回调里同步决策（同线程立即唤醒，单线程可测）
    asked = []

    def ask_allow(r):
        asked.append(r.path)
        svc.allow(r)

    svc.ask_ui = ask_allow
    assert svc.request(PermissionRequest("edit_file", "write", "a.txt", "")) is True

    def ask_deny(r):
        svc.deny(r)

    svc.ask_ui = ask_deny
    assert svc.request(PermissionRequest("edit_file", "write", "a.txt", "")) is False
    print("允许 / 拒绝    OK")

    # 3) allow_for_session：同一文件只问一次，换文件重新问
    svc2 = PermissionService()
    calls = []

    def ask_session(r):
        calls.append(r.path)
        svc2.allow_for_session(r)

    svc2.ask_ui = ask_session
    assert svc2.request(PermissionRequest("edit_file", "write", "b.txt", "")) is True
    assert svc2.request(PermissionRequest("edit_file", "write", "b.txt", "")) is True
    assert calls == ["b.txt"], calls              # 第二次缓存命中，没回调
    assert svc2.request(PermissionRequest("edit_file", "write", "c.txt", "")) is True
    assert calls == ["b.txt", "c.txt"]             # 换文件重新问
    print("会话缓存      OK:", calls)

    # 3b) allow_for_session_all：本会话该工具+动作不限文件免审
    svc4 = PermissionService()
    calls2 = []

    def ask_all(r):
        calls2.append(r.path)
        svc4.allow_for_session_all(r)

    svc4.ask_ui = ask_all
    assert svc4.request(PermissionRequest("edit_file", "write", "x.txt", "")) is True
    assert svc4.request(PermissionRequest("edit_file", "write", "y.txt", "")) is True
    assert calls2 == ["x.txt"], calls2            # 第二个文件也没问
    print("通配缓存      OK:", calls2)

    # 4) cancel_all：另一个线程挂在 Event.wait 时，主线程整体拒绝唤醒
    svc3 = PermissionService()
    svc3.ask_ui = lambda r: None                  # 上屏后不决策：request 线程挂起
    result = {}

    def blocked():
        result["v"] = svc3.request(
            PermissionRequest("edit_file", "write", "d.txt", ""))

    import threading as _th
    t = _th.Thread(target=blocked)
    t.start()
    _time.sleep(0.1)                               # 等它进入 Event.wait
    svc3.cancel_all()
    t.join(timeout=2)
    assert not t.is_alive() and result["v"] is False
    print("cancel_all     OK: 挂起的请求被唤醒并拒绝")

    # 5) 对已不存在的请求响应：忽略不炸
    svc3.deny(PermissionRequest("edit_file", "write", "none", ""))
    print("过期响应忽略  OK")

    print("\n全部测试通过")
