# -*- coding: utf-8 -*-
"""run_command 工具：在持久 cmd 会话中执行命令（黑白名单 + 审批 + 截断）。

两层职责：
- 工具层（本文件）管"该不该跑"：单行校验 -> 超时钳制 -> 黑名单
  （连审批都不给）-> 白名单（免审批直接跑）-> 其余弹审批。
- 持久层（根目录 shell/persistent_shell.py，与 permission/ 同级的
  内部能力包）管"怎么跑"：cmd.exe 单例 + 文件信箱协议 +
  超时杀树重启，详见其模块 docstring。

审批接线复用 permission_service（零改动）：action="run"，path 放命令
摘要、diff 放完整命令文本（TUI 端 PermissionBlock 按 action 分流渲染）。
拒绝 = ToolError -> ERROR 文本 observation，agent 继续——与 edit_file
同一 deny 语义。headless（无 ask_ui，如 CLI 自测）自动放行。

黑白名单只防顺手不防绕行：python -c "import urllib..." 这类拦不住，
最后一道是审批弹窗。会话缓存照常生效：allow_for_session 之后同一条
命令不再问；通配键 (run_command, run, *) 会让所有命令免审。
"""

import re
import sys
from pathlib import Path

# 根进 sys.path：permission/ 与 shell/ 两个内部能力包都在根下
# （直接运行本文件时 cwd 是 tool/，根目录不在 sys.path 里）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from base_tool import Tool, ToolError
from permission.permission import PermissionRequest, permission_service
from shell.persistent_shell import get_shell

DEFAULT_TIMEOUT = 60    # 秒
MAX_TIMEOUT = 600       # 秒，上限钳制
MAX_OUTPUT = 30000      # 输出字符上限，超了掐头去尾各留一半

# 黑名单：连审批都不给（下载 / 裸网络 / 浏览器 / 嵌套 shell / 自毁）。
# 匹配 = 首 token 精确比对 + 全串词边界扫描——后者抓 `echo x & curl ...`
# 这类 & 连接的绕行；边界是"前后不是 [0-9A-Za-z_-]"，防误伤
# my-curl-script / exit_code.py 之类（'-' 和 '_' 都算名字的一部分）
BANNED = {
    # shell 别名 / 自毁
    "doskey", "exit",
    # 下载工具（Windows 自带 curl；certutil / bitsadmin 是出名的下载旁路）
    "curl", "curlie", "wget", "axel", "aria2c", "certutil", "bitsadmin", "ftp",
    # 裸网络
    "nc", "ncat", "netcat", "telnet",
    # 终端浏览器 / HTTP 客户端
    "lynx", "w3m", "links", "httpie", "xh", "http-prompt",
    # 图形浏览器
    "chrome", "firefox", "msedge", "safari",
    # 绕行 shell：嵌套 shell 一放行，黑白名单全部作废
    "powershell", "pwsh", "bash", "sh", "wsl",
    # 系统级破坏
    "shutdown",
}

# 白名单：确认只读的命令，免审批直接执行。
# 前缀 + 边界匹配（前缀后必须是结尾/空格/'-'）：防 "git statusx" 冒充、
# 兼容 "git log -n 5" 带 flag 形态。kill / set / go test 等会改状态
# 或执行代码的命令一律不收
SAFE = [
    # 系统信息类（只读）
    "dir", "ls", "cd", "echo", "ver", "whoami", "hostname",
    "type", "tree", "where", "tasklist", "find", "findstr",
    "date /t", "time /t",
    # git 只读查询
    "git status", "git log", "git diff", "git show", "git branch",
    "git tag", "git remote", "git ls-files", "git ls-remote",
    "git rev-parse", "git config --get", "git config --list",
    "git describe", "git blame", "git grep", "git shortlog",
    # go 工具链里真只读的（run/build/test/install/mod/fmt 已剔除）
    "go version", "go help", "go list", "go env", "go doc", "go vet",
    # 跑测试/脚本用（M2 verify 档需要）。python 能执行任意代码，
    # 这里放行的是"跑起来"这个动作本身——非 readonly 的主 agent
    # 走它仍免审批，剩余风险与 go vet 同级：真正守门的是黑名单
    # （网络/下载/嵌套 shell）+ readonly 档的预算计数
    "python",
]

_BOUND = r"(?<![0-9A-Za-z_-]){name}(?![0-9A-Za-z_-])"


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在持久的 Windows cmd 会话中执行命令，返回 stdout/stderr/退出码。"
        "会话跨命令持久：环境变量(set)、当前目录(cd)会保留。"
        "只执行单行命令，多条命令请拆成多次调用。"
        "超时默认 60 秒（最大 600），超时会终止整个进程树并重启 shell——"
        "重启后环境变量会重置，工作目录会恢复。"
        "下载文件 / 打开浏览器 / 交互式命令不被允许，需要时请向用户说明意图。"
        "示例：查看项目目录 -> command='dir'；查 git 改动 -> "
        "command='git status'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "单行 cmd 命令（不能含换行）"},
            "timeout": {"type": "integer",
                        "description": "超时秒数，默认 60，最大 600"},
        },
        "required": ["command"],
    }

    # readonly 模式追加说明（verify 档的运行纪律）
    _READONLY_NOTE = (
        "注意：当前为只读验证模式（后台 verify 档）——仅只读白名单内的"
        "命令直接执行，白名单外的命令一律直接拒绝，不会有人来批准。"
        "被拒时不要反复重试，把'该命令被拒'如实写进报告。命令有次数预算，"
        "输出末尾的提示是剩余次数，接近上限时优先完成核心任务。"
    )

    def __init__(self, shell_getter=None, readonly: bool = False,
                 max_calls: int | None = None):
        """可配置实例：默认（ToolManager 扫描出的那个）零参数——主会话
        单例 shell + 审批弹窗。子 agent 档位注入自己的实例：

        :param shell_getter: () -> PersistentShell；默认模块级 get_shell。
            verify/general 档传 agent/shell_pool 的取件函数（隔离环境）
        :param readonly: True = 只走白名单，白名单外直接拒（不弹审批、
            不执行）——后台 verify 档的安全边界，"跑起来"类命令放行、
            改状态的命令一律拒绝
        :param max_calls: 命令次数预算（防失控刷命令）；None 不限。
            每次实际执行前扣减，扣完拒绝并把剩余预算写进拒绝文本
        """
        self._shell_getter = shell_getter or get_shell
        self._readonly = readonly
        self._max_calls = max_calls
        self._calls_left = max_calls if max_calls is not None else float("inf")
        if readonly:
            # description 是实例属性覆盖类属性：子 agent 的工具 schema
            # 里带档位纪律，主 agent 的默认实例不受影响
            self.description = self.description + " " + self._READONLY_NOTE

    def execute(self, command: str, timeout=None) -> str:
        # 1) 基本校验：非空、单行
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command 不能为空")
        if "\n" in command or "\r" in command:
            raise ToolError("一次只能执行一条单行命令，多条命令请拆成多次调用")
        # 2) 超时钳制（clamp：越界压到边界，不报错）
        timeout = _clamp_timeout(timeout)
        # 3) 黑名单：连审批都不给（readonly 与否都一样）
        _check_banned(command)
        # 4) 分流：readonly 只走白名单，白名单外直接拒（无人审批）；
        #    默认模式白名单免审、其余弹审批（拒绝 -> ToolError）
        if not _is_safe(command):
            if self._readonly:
                raise ToolError(
                    f"readonly 模式：命令未在只读白名单，已拒绝执行: "
                    f"{command}\n（后台 verify 档只允许测试/查看类命令，"
                    f"改状态的命令不会被批准；请把这条拒绝如实写进报告）")
            _request_permission(command)
        # 5) 预算检查 + 扣减（真要执行了才扣；被上面拒绝的不扣）
        if self._calls_left <= 0:
            raise ToolError(
                f"命令次数预算已用完（{self._max_calls} 次），不再执行任何命令。"
                f"剩余任务请用只读工具完成，或如实报告未完成的部分。")
        self._calls_left -= 1
        # 6) 执行 + 拼装（自己的 shell，不在主会话排队）
        result = self._shell_getter().run(command, timeout)
        out = _format(result, command, timeout)
        # 剩余预算反馈：不藏私，让模型自己规划（默认不限时不提示）
        if self._readonly and self._calls_left <= 5:
            out += f"\n（命令预算剩余 {self._calls_left} 次）"
        if self._readonly and self._calls_left <= 0:
            out += "\n（预算即将耗尽：剩余任务请用只读工具完成，或如实报告未完成部分）"
        return out


# ---- 模块级辅助 ----

def _clamp_timeout(timeout) -> int:
    """默认 60；非法值回默认；[1, 600] 之外的压到边界。"""
    if timeout is None:
        return DEFAULT_TIMEOUT
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1, min(MAX_TIMEOUT, timeout))


def _check_banned(command: str) -> None:
    """黑名单：首 token 精确比对 + 全串词边界扫描，任一命中抛 ToolError。"""
    c = command.strip().lower()
    tokens = c.split()
    first = tokens[0] if tokens else ""
    for name in BANNED:
        if first == name or re.search(_BOUND.format(name=re.escape(name)), c):
            raise ToolError(
                f"禁止执行 {name}：该命令不允许 agent 直接运行"
                f"（下载 / 网络直连 / 浏览器 / 嵌套 shell / 会破坏会话），"
                f"如确有需要请向用户说明意图，由用户自己执行")


def _is_safe(command: str) -> bool:
    """白名单：前缀匹配且前缀后是结尾/空格/'-'（防冒充、兼容带 flag）。"""
    c = command.strip().lower()
    for prefix in SAFE:
        rest = c[len(prefix):]
        if c.startswith(prefix) and (rest == "" or rest[0] in (" ", "-")):
            return True
    return False


def _request_permission(command: str) -> None:
    """审批：拒绝时抛 ToolError（走 ERROR 观察，agent 继续）。"""
    req = PermissionRequest(tool="run_command", action="run",
                            path=command[:80], diff=command)
    if not permission_service.request(req):
        raise ToolError(f"用户拒绝了本次执行（deny）：{command}，"
                       f"请说明意图或询问用户后重试")


def _truncate(text: str) -> str:
    """超长输出掐头去尾各留一半，中间标注省略字数。"""
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    omitted = len(text) - 2 * half
    return text[:half] + f"\n...（中间省略 {omitted} 字）...\n" + text[-half:]


def _format(result, command: str, timeout: int) -> str:
    """拼装输出：命令回显 + stdout/stderr 分节 + 退出码（或超时说明）。"""
    head = f"命令: {command}\n"
    if result.error:
        return head + (f"[stderr]\n{result.stderr}" if result.stderr else "")
    if result.timed_out:
        parts = [head]
        if result.stdout:
            parts.append("[stdout]\n" + _truncate(result.stdout) + "\n")
        parts.append(f"命令超时（{timeout} 秒），已终止整个进程树并重启 shell。\n"
                     f"注意：环境变量已重置，工作目录已恢复。")
        return "\n".join(parts)
    parts = [head, "[stdout]\n" + _truncate(result.stdout)]
    if result.stderr.strip():
        parts.append("[stderr]\n" + _truncate(result.stderr))
    parts.append(f"Exit code: {result.status}")
    return "\n".join(parts)


if __name__ == "__main__":
    # 自测：黑白名单与审批用桩验证；执行分支真起 cmd.exe
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件
    # （本模块不直接打点，但自测经 base_tool/permission 间接产生日志）

    import tempfile

    tool = RunCommandTool()

    # 9) 多行命令 -> 拒绝（提示拆分）
    try:
        tool.execute("echo a\necho b")
    except ToolError as e:
        assert "拆成多次调用" in str(e), e
        print("多行拒绝       OK")
    else:
        raise AssertionError("多行命令应被拒绝")

    # 10) 黑名单：首 token
    for banned in ("curl http://x", "powershell -c dir", "exit"):
        try:
            tool.execute(banned)
        except ToolError as e:
            assert "禁止执行" in str(e), e
        else:
            raise AssertionError(f"{banned!r} 应被黑名单拦截")
    print("黑名单首词     OK: curl / powershell / exit")

    # 11) 黑名单：全串词边界（& 连接的绕行）
    try:
        tool.execute("echo x & curl http://x")
    except ToolError as e:
        assert "禁止执行" in str(e), e
        print("黑名单绕行     OK: & curl 被拦")
    else:
        raise AssertionError("& 连接的 curl 应被拦截")

    # 11b) 词边界不误伤：名字里含黑名单词的正常命令能通过黑名单
    #      （_ 和 - 都算名字的一部分）
    r = tool.execute("type shell\\__init__.py", 10)        # 'sh' 在 shell 里
    assert "Exit code: 0" in r, r
    print("词边界不误伤   OK: 'shell' 未触发 sh")

    # 12) 白名单免审：git status 不弹审批直接跑
    calls = []
    permission_service.ask_ui = lambda req: calls.append(req)
    out = tool.execute("git status", 30)
    assert calls == [], "白名单命令不应弹审批"
    assert "Exit code: 0" in out, out
    print("白名单免审     OK: git status")

    # 12b) 带 flag 的白名单形态同样免审（前缀后是 '-'）。
    #      仓库可能还没有提交（git log 报 128），这里只验证免审 + 正常
    #      执行带回退出码，不要求命令本身成功
    out = tool.execute("git log -1", 30)
    assert calls == [] and "Exit code:" in out, out
    print("带 flag 免审   OK: git log -1")

    # 13) 边界防冒充：git statusx 不在白名单，弹审批
    def _ask_allow(req):
        calls.append(req)
        permission_service.allow(req)

    permission_service.ask_ui = _ask_allow
    out = tool.execute("git statusx", 30)
    assert len(calls) == 1, calls
    assert calls[0].tool == "run_command" and calls[0].action == "run"
    assert "Exit code:" in out, out
    print("边界防冒充     OK: git statusx 弹审后执行（问了 1 次）")

    # 14) 审批拒绝：ToolError，shell 零执行（探测文件不存在）。
    #     注意探测命令不能是白名单内的（echo 免审，deny 无从发生），
    #     用 copy NUL：非白名单 -> 弹审批，放行时才会产生文件
    probe = Path(tempfile.gettempdir()) / "mychat_deny_probe.txt"
    if probe.exists():
        probe.unlink()
    permission_service.ask_ui = lambda req: permission_service.deny(req)
    try:
        tool.execute(f'copy NUL "{probe}"', 10)
    except ToolError as e:
        assert "拒绝" in str(e), e
    else:
        raise AssertionError("拒绝时应抛 ToolError")
    assert not probe.exists(), "拒绝时命令必须零执行（探测文件不应存在）"
    print("审批拒绝       OK: 探测文件未产生（shell 零执行）")

    # 15) 超长输出截断
    permission_service.ask_ui = None      # headless 自动放行
    out = tool.execute('python -c "print(\'x\'*100000)"', 60)
    assert "省略" in out and len(out) <= MAX_OUTPUT + 200, len(out)
    print("超长截断       OK: %d 字符（上限 %d）" % (len(out), MAX_OUTPUT))

    # 16) 超时钳制（直接测函数）
    assert _clamp_timeout(None) == DEFAULT_TIMEOUT
    assert _clamp_timeout(0) == 1
    assert _clamp_timeout(-5) == 1
    assert _clamp_timeout(9999) == MAX_TIMEOUT
    assert _clamp_timeout(30) == 30
    assert _clamp_timeout("abc") == DEFAULT_TIMEOUT
    print("超时钳制       OK: None->60 / 0->1 / 9999->600")

    # ---- readonly 变体（verify 档）：stub shell 验证，不起真 cmd.exe ----
    from shell.persistent_shell import ShellResult

    class _StubShell:
        def __init__(self):
            self.commands = []

        def run(self, command, timeout):
            self.commands.append(command)
            return ShellResult(stdout=f"stub:{command}", status=0)

    stub = _StubShell()
    calls.clear()
    ro = RunCommandTool(shell_getter=lambda: stub, readonly=True, max_calls=3)

    # 17) 白名单命令放行：零审批、落到注入的 shell
    out = ro.execute("echo hi", 10)
    assert calls == [] and stub.commands == ["echo hi"], (calls, stub.commands)
    assert "stub:echo hi" in out, out
    print("readonly 放行 OK: 白名单零审批 + 自己的 shell")

    # 18) 白名单外：直接拒，不弹审批、shell 零执行
    try:
        ro.execute("copy NUL x.txt", 10)
    except ToolError as e:
        assert "readonly 模式" in str(e) and "如实写进报告" in str(e), e
    else:
        raise AssertionError("readonly 白名单外应被拒")
    assert calls == [] and "copy NUL" not in stub.commands, "readonly 拒绝必须零执行零审批"
    print("readonly 拒绝 OK: 零审批 + 零执行")

    # 19) 黑名单在 readonly 下同样拦截（比白名单更早）
    try:
        ro.execute("curl http://x", 10)
    except ToolError as e:
        assert "禁止执行" in str(e), e
    else:
        raise AssertionError("readonly 黑名单应拦截")
    print("readonly 黑名单 OK")

    # 20) 预算：echo hi 已扣 1 次；dir 扣完剩 1、ver 扣完剩 0、再派即拒
    out = ro.execute("dir", 10)
    assert "剩余 1 次" in out, out
    out = ro.execute("ver", 10)
    assert "剩余 0 次" in out and "即将耗尽" in out, out
    try:
        ro.execute("echo no", 10)
    except ToolError as e:
        assert "预算已用完" in str(e), e
    else:
        raise AssertionError("预算耗尽应拒绝")
    assert len(stub.commands) == 3, "被拒的命令不应落到 shell"
    print("readonly 预算  OK: 3 次用完即拒，拒绝零执行")

    # 21) readonly 实例的 description 带档位纪律（子 agent 的工具
    #     schema 可见），默认实例不受影响
    assert "只读验证模式" in ro.description
    assert "只读验证模式" not in RunCommandTool().description
    print("档位描述注入  OK: 实例级覆盖，默认实例不变")

    # 还原 headless，避免污染后续（同进程内）使用
    permission_service._granted.clear()
    print("\n全部测试通过")
