# -*- coding: utf-8 -*-
"""shell_pool：子 agent 的持久 cmd.exe 按需发放（进程级单例登记簿）。

主 agent 的命令照旧走 shell/persistent_shell.get_shell() 单例（零改动），
这里只给子 agent 每人一条独立 shell：环境变量/当前目录只污染自己，
命令不排主队列。verify 档跑测试、general 档在 worktree 里干活都从这取。

不是池——没有预热，起一个就是一条线程 + 一个 cmd.exe 进程；
成本就是真起进程的那几十毫秒。死壳自动重建（alive 检查），
close 时 taskkill /F /T 杀整树（沿用 PersistentShell 自带实现）。

直跑自测：python agent/shell_pool.py（真起 cmd.exe，跑完清场）
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger
from shell.persistent_shell import PersistentShell

log = get_logger(__name__)

# 进程级单例状态（测试用 _reset 清场）
_shells: dict[str, PersistentShell] = {}
_lock = threading.Lock()


def get_agent_shell(agent_id: str, cwd: str | None = None) -> PersistentShell:
    """取（或懒起）某个子 agent 的专属 shell。死壳自动重建。"""
    with _lock:
        sh = _shells.get(agent_id)
        if sh is None or not sh.alive:
            log.info("懒起新 shell: %s%s", agent_id,
                     "（死壳重建）" if sh is not None else "")
            sh = PersistentShell(cwd=cwd)
            _shells[agent_id] = sh
        return sh


def close_agent_shell(agent_id: str) -> None:
    """收掉一个子 agent 的 shell（没起过则什么都不做）。"""
    with _lock:
        sh = _shells.pop(agent_id, None)
    if sh is not None:
        sh.close()      # taskkill /F /T 杀整树


def close_all_agent_shells() -> None:
    """进程退出清场用：全部收掉。"""
    with _lock:
        shells = list(_shells.values())
        _shells.clear()
    for sh in shells:
        sh.close()


def _reset() -> None:
    """自测清场专用。"""
    global _shells
    _shells = {}


if __name__ == "__main__":
    # 自测：真起 cmd.exe 验证隔离性（cwd=临时目录，不碰项目根）
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import tempfile
    import time

    tmp = Path(tempfile.mkdtemp(prefix="mychat-shellpool-"))

    # 1) 两个 agent 各自的 shell 互不相干：环境变量隔离
    a = get_agent_shell("agent-aaa")
    b = get_agent_shell("agent-bbb")
    assert a is not b
    a.run("set AAA_ONLY=from_a", 10)
    r = a.run("echo %AAA_ONLY%", 10)
    assert "from_a" in r.stdout, r
    r = b.run("echo %AAA_ONLY%", 10)
    assert "from_a" not in r.stdout, "b 的 shell 不应看到 a 的环境变量"
    print("环境隔离       OK: %AAA_ONLY% 只在 a 里")

    # 2) 目录隔离：a cd 走了，b 的目录不动
    a.run("cd ..", 10)
    ra, rb = a.run("cd", 10), b.run("cd", 10)
    assert ra.cwd != rb.cwd, "各自 cwd 应独立"
    print("目录隔离       OK: a=%s b=%s" % (Path(ra.cwd).name, Path(rb.cwd).name))

    # 3) 同 id 幂等：两次取到同一实例
    assert get_agent_shell("agent-aaa") is a
    print("同 id 幂等     OK")

    # 4) cwd 参数生效 + 带参懒起
    c = get_agent_shell("agent-ccc", cwd=str(tmp))
    r = c.run("cd", 10)
    assert Path(r.cwd).resolve() == tmp.resolve(), r
    print("cwd 参数       OK: 起壳落位临时目录")

    # 5) close 单个：进程收走、字典清空、未起过的幂等
    pid = a._proc.pid
    close_agent_shell("agent-aaa")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and a.alive:
        time.sleep(0.05)
    assert not a.alive, "close 后 shell 应已终止（pid=%s）" % pid
    close_agent_shell("agent-aaa")     # 再关一次不炸
    print("close 单个     OK: 整树收走 + 幂等")

    # 6) 死壳自动重建：被外部 kill 后 get 能复活
    a2 = get_agent_shell("agent-aaa", cwd=str(tmp))
    assert a2.alive and a2 is not a
    r = a2.run("echo revived", 10)
    assert "revived" in r.stdout, r
    print("死壳重建       OK: kill 后自动复活")

    # 7) close_all：全部收走、字典清空
    close_all_agent_shells()
    close_all_agent_shells()          # 幂等
    _reset()
    print("close_all      OK: 全部收走 + 幂等")

    print("\n全部测试通过")
