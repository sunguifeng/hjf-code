# -*- coding: utf-8 -*-
"""持久 cmd.exe 会话：跨命令保留环境变量与当前目录。

    (命令) 1> out 2> err < NUL     ← 执行 + 输出分流 + 交互命令吃 EOF 即退
    @cd > cwd                      ← 执行后的工作目录（目录持久就靠它）
    @echo %ERRORLEVEL% > status     ← 非空即完成信号

cmd 逐行解释 stdin，天然隔离每条命令。
完成判定 = status.txt 非空；10ms 轮询，见到信号后等 20ms 再读、
读到纯数字才算真完成（缓解"文件已建但内容未写完"的竞态）。

编码：命令行与文件信箱两侧统一用 OEM 代码页（"oem" 编解码器，
中文系统 = GBK）。实测 chcp 65001 + 管道 stdin + 多字节命令会直接
杀死 cmd.exe（Windows 的坑），所以不动代码页——两侧同编同解，
中文系统下中文命令与输出天然正常；OEM 之外的字符（emoji 等）不支持。

超时处理：taskkill /F /T 杀整棵进程树（孙进程不会漏网），然后重启
shell——cwd 恢复到最近一次成功命令的目录（cd 每条命令都落盘，总有
最新值），环境变量丢失是重启的固有代价（只在 cmd 的内存里）；
退出码不伪造（Windows 没有信号语义），超时直接置 None，
由工具层输出超时说明。

stdout 是与 shell 的控制通道：正常命令输出全走文件信箱不进管道，
但 banner / chcp 回显 / 漏网重定向会写它——开守护排水线程持续丢弃，
防止 64KB 管道写满把 shell 堵死。

位置：根目录 shell/ 包（与 permission/ 同级，同属内部能力，
不在 tool/ 下、天然不会被 ToolManager 扫描）。
直跑自测：python shell/persistent_shell.py
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 项目根目录（shell/ 的上一级）：shell 的默认起点
_ROOT = Path(__file__).resolve().parent.parent

POLL_INTERVAL = 0.01   # 完成信号轮询步长（10ms）
SETTLE = 0.02          # 见到信号后的落盘等待（20ms）


@dataclass
class ShellResult:
    """一条命令的执行结果。status 为 None 表示没有退出码（超时/意外）。"""
    stdout: str = ""
    stderr: str = ""
    status: Optional[int] = None
    cwd: str = ""
    timed_out: bool = False
    error: bool = False      # True = shell 意外退出等异常（stderr 里带说明）


class PersistentShell:
    """cmd.exe 持久会话。一条 shell = 串行执行，run() 全程持锁。"""

    def __init__(self, cwd=None):
        self._cwd = cwd or str(_ROOT)
        self._proc = None
        self._lock = threading.Lock()
        self._closed = False
        self._start()

    # ---- 生命周期 ----

    @property
    def alive(self) -> bool:
        return (not self._closed and self._proc is not None
                and self._proc.poll() is None)

    def _start(self):
        """起一个新 cmd.exe：/Q 关回显、/K 常驻；排水线程防管道写满堵死。"""
        env = {**os.environ, "GIT_EDITOR": "true", "EDITOR": "true"}
        self._proc = subprocess.Popen(
            ["cmd.exe", "/Q", "/K"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,      # 控制通道合流，由排水线程统一丢弃
            cwd=self._cwd, env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        pump = threading.Thread(target=self._pump_stdout,
                                 args=(self._proc,), daemon=True)
        pump.start()
        # 初始化一行：显式落位（重启场景即恢复 cwd）。
        # 不做 chcp 65001：管道 stdin + 多字节命令会杀死 cmd.exe，
        # 命令行与文件信箱两侧统一用 OEM 代码页（见模块 docstring）
        self._send(f'@cd /d "{self._cwd}"')

    def _pump_stdout(self, proc):
        """后台排水：持续丢弃 shell 控制通道的输出，防 64KB 管道写满堵死。
        进程被杀 / 管道关闭时自然退出（读返回空或抛异常）。"""
        try:
            while proc.stdout.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def close(self):
        """进程退出时调用：杀整树。已关闭时幂等。"""
        with self._lock:
            self._closed = True
            self._kill_tree()

    def  _kill_tree(self):
        """taskkill /F /T 杀整棵进程树 + 关旧管道句柄。"""
        if self._proc is not None and self._proc.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                capture_output=True)
        if self._proc is not None:
            for s in (self._proc.stdin, self._proc.stdout):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    # ---- 执行 ----

    def run(self, command: str, timeout: float) -> ShellResult:
        """执行一条单行命令并等它结束。线程安全（跨命令串行）。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("persistent shell 已 close，不能再执行")
            mail = Path(tempfile.mkdtemp(prefix="mychat-cmd-"))
            out_f = mail / "out.txt"
            err_f = mail / "err.txt"
            status_f = mail / "status.txt"
            cwd_f = mail / "cwd.txt"
            try:
                # 四个信箱先建成空文件：完成判定是"status 从空变非空"，
                # 先建空文件才能区分"没执行"和"还没执行完"
                for f in (out_f, err_f, status_f, cwd_f):
                    f.touch()
                # 三行协议（cmd 逐行解释）。顺序关键：status 必须在 cd
                # 之前——成功的 cd 会把 ERRORLEVEL 清零（自测用例 2 抓出
                # 的坑），先抓退出码再落目录：
                # 1) 真正执行，stdout/stderr 分流进文件；( ) 把整条命令
                #    （含 && / |）归到重定向下；< NUL 让交互命令立即吃 EOF
                # 2) 独立行解析，此时上一行已执行完，%ERRORLEVEL% 是真实
                #    退出码；status 非空即完成信号
                # 3) cd 无参数 = 打印当前目录 -> 落盘（持久目录的实现）
                self._send(f'({command}) 1> "{out_f}" 2> "{err_f}" < NUL')
                self._send(f'@echo %ERRORLEVEL% > "{status_f}"')
                self._send(f'@cd > "{cwd_f}"')
                return self._wait(command, timeout, out_f, err_f,
                                   cwd_f, status_f)
            finally:
                # 信箱用完即删；被杀进程可能还短暂攥着句柄，
                # 删不掉就留给系统临时目录清理
                shutil.rmtree(mail, ignore_errors=True)

    def _send(self, line: str):
        self._proc.stdin.write((line + "\n").encode("oem"))
        self._proc.stdin.flush()

    def _wait(self, command, timeout, out_f, err_f, cwd_f, status_f):
        deadline = time.monotonic() + timeout
        while True:
            if status_f.stat().st_size > 0:
                # 见到信号：等落盘再读，读到纯数字才算真完成
                time.sleep(SETTLE)
                text = self._read(status_f).strip()
                if text.lstrip("-").isdigit():
                    self._wait_cwd(cwd_f)  # cd 行在 status 之后，等它落盘
                    return self._collect(text, out_f, err_f, cwd_f)
            if time.monotonic() >= deadline:
                return self._kill_and_restart(command, out_f)
            if self._proc.poll() is not None:
                # shell 意外退出（如漏网的 exit）：重启 + 报错返回
                self._start()
                return ShellResult(
                    stderr=(f"shell 在执行中意外退出"
                            f"（命令: {command[:80]}），已重启"),
                    cwd=self._cwd, error=True)
            time.sleep(POLL_INTERVAL)

    def _wait_cwd(self, cwd_f: Path, timeout: float = 0.2):
        """等 cd 行落盘（它在 status 之后执行，见到信号时可能还没写）。
        超时则放弃——_collect 里有回退（沿用上一条命令的目录）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cwd_f.stat().st_size > 0:
                return
            time.sleep(POLL_INTERVAL)

    def _collect(self, status_text, out_f, err_f, cwd_f):
        out = self._read(out_f)
        err = self._read(err_f)
        cwd = self._read(cwd_f).strip() or self._cwd
        self._cwd = cwd     # 记下来：下次超时重启从这里恢复
        return ShellResult(stdout=out, stderr=err,
                           status=int(status_text), cwd=cwd)

    def _kill_and_restart(self, command, out_f):
        """超时：带回已产生的输出，杀整树，重启（_start 内含 cwd 恢复）。"""
        partial = self._read(out_f)
        self._kill_tree()
        self._start()
        return ShellResult(stdout=partial, status=None,
                           cwd=self._cwd, timed_out=True)

    @staticmethod
    def _read(f: Path) -> str:
        try:
            return f.read_text(encoding="oem", errors="replace")
        except OSError:
            return ""


# ---- 模块级单例（与 permission_service 同风格）----

_shell = None
_shell_lock = threading.Lock()


def get_shell() -> PersistentShell:
    """懒创建的模块级单例：agent 不跑命令就不起 cmd.exe；死了自动重建。"""
    global _shell
    with _shell_lock:
        if _shell is None or not _shell.alive:
            _shell = PersistentShell()
        return _shell


def close_shell() -> None:
    """进程退出时调用：杀掉整树（若有）。没起过则什么都不做。"""
    global _shell
    with _shell_lock:
        if _shell is not None:
            _shell.close()


if __name__ == "__main__":
    # 自测：真起一个 cmd.exe 跑全部分支
    sh = PersistentShell()

    # 1) 基本执行 + 退出码 0
    r = sh.run("echo hello", 10)
    assert r.status == 0 and "hello" in r.stdout, r
    print("echo           OK:", repr(r.stdout.strip()))

    # 2) 非零退出码
    r = sh.run("cmd /c exit 3", 10)
    assert r.status == 3, r
    print("退出码 3       OK")

    # 3) 环境变量持久（set 后跨命令可读）
    sh.run("set MYVAR=abc", 10)
    r = sh.run("echo %MYVAR%", 10)
    assert r.status == 0 and "abc" in r.stdout, r
    print("环境变量持久   OK:", repr(r.stdout.strip()))

    # 4) 当前目录持久（cd 后 cd 打印新目录）
    sh.run("cd tool", 10)
    r = sh.run("cd", 10)
    assert r.stdout.strip().endswith("tool"), r
    print("目录持久       OK:", r.stdout.strip())

    # 5) 中文输出（chcp 65001 生效）
    r = sh.run("echo 你好世界", 10)
    assert "你好世界" in r.stdout, r
    print("中文           OK:", repr(r.stdout.strip()))

    # 6) stderr 分流（stdout 不混入）
    r = sh.run('python -c "import sys; sys.stderr.write(\'ERR\')"', 30)
    assert "ERR" in r.stderr and "ERR" not in r.stdout, r
    print("stderr 分流    OK")

    # 7) 超时：3 秒杀整树 + 重启 + shell 仍可用
    t0 = time.monotonic()
    r = sh.run("ping -n 100 127.0.0.1", 3)
    assert r.timed_out and r.status is None, r
    assert time.monotonic() - t0 < 20, "超时应约 3 秒返回"
    assert r.stdout, "超时应带回已产生的部分输出"
    r = sh.run("echo alive", 10)
    assert r.status == 0 and "alive" in r.stdout, r
    print("超时杀树重启   OK: %.1fs 返回，重启后可用" % (time.monotonic() - t0))

    # 8) 重启后 cwd 恢复（回到最近一次成功命令的目录 = tool）
    r = sh.run("cd", 10)
    assert r.stdout.strip().endswith("tool"), r
    print("重启目录恢复   OK:", r.stdout.strip())

    sh.close()
    assert not sh.alive, "close 后应不再存活"
    print("close          OK")

    # 9) get_shell 单例：两次调用同一实例
    a, b = get_shell(), get_shell()
    assert a is b
    a.close()
    print("get_shell 单例 OK")

    print("\n全部测试通过")
