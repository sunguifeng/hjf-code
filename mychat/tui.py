# -*- coding: utf-8 -*-
"""tui：Textual 聊天界面。

无 Header/Footer 边框，用户消息 ❯ 前缀，
助手回复 ●，思考过程折叠块（✻ Thought for Ns，ctrl+o 展开），
生成中状态行计时（✻ Thinking… Ns / Baked for Ns），esc 中断当前回复。
生成中途发消息 = 补充进当前任务（steering，下一轮并入对话），不顶掉；
先 esc 再输入才是新任务。

回复由 agent/reactAgent 的 ReAct 循环生成（asyncio.to_thread 丢线程里
跑多轮工具调用，on_step 进度回调经 call_from_thread 回 UI 上屏）；
开屏有蜜蜂横幅自我介绍。

思考过程入库（role='thinking'）但不进 API 历史：messages 内存列表只放
user/assistant，重放历史时 thinking 只上屏不参与对话。

快捷键：esc 中断回复；ctrl+o 展开/收起思考块；ctrl+q 退出
工具观察里长得像 diff 的（edit_file 返回）渲染成 DiffBlock 彩色块，
其余观察（如读文件内容）不上屏；/resume 恢复后历史 diff 不重放（不入库）。
edit 写盘前过审批：agent 线程经 permission_service 挂起等按键，UI 挂
PermissionBlock（选项列表：↑↓ 高亮 · enter 确认 · esc 快捷拒绝，
焦点锁在列表上防误输入）；拒绝走 ToolError → ERROR 观察（agent 不停，
模型自己消化）；headless 自动放行。
命令：  /new 开新会话；/resume 浮层选历史会话恢复；/sessions 列历史；
        /use <uuid> 按完整 uuid 或前缀切换会话
输入 / 时在输入框上方弹出命令提示：↑↓ 高亮，tab/enter 采纳，esc 关闭。
会话标识是唯一 uuid（列表/恢复/切换都用它），摘要以最后一条消息为准。
"""

import asyncio
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from applog import get_logger
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option   # 8.x：Option 不再挂在 OptionList 下
from rich.markup import escape                 # diff 内容按纯文本上色，防代码里的 [] 被当标记

from agent import supervisor               # 子 agent 登记簿（汇合屏障/清场用）
from agent.reactAgent import NATIVE_SYSTEM_PROMPT, react_agent   # ReAct 循环 + 基础提示词
from db import db                          # db/ 文件夹下的 db.db（会话持久化）
from model.model import MODEL, call_model, set_usage_context   # 默认模型名（横幅）；persona 摘要用；token 归属
from persona.loader import build_system_prompt   # 系统提示词分层装配（读侧）
from tool.base_tool import ToolManager     # 启动时加载 tool/ 目录
from permission.permission import permission_service   # edit 写盘前的审批（agent 线程挂起等按键）

_ROOT = Path(__file__).resolve().parent.parent   # 项目根（找 tool/ 目录）

# 单个后台 agent 的最长等待（秒）：到点放行并如实说明，卡死不陪葬主对话
AGENT_WAIT_TIMEOUT = 600

log = get_logger(__name__)


def _summarize_for_persona(text: str, hint: str) -> str:
    """persona 超长层的 LLM 摘要。注入给 loader（loader 本身不依赖
    model 包，保持可独立自测）；返回空/异常由 loader 兜底硬截断。"""
    return call_model(
        f"以下内容是{hint}，原文较长。请提炼为要点列表"
        f"（简体中文、忠实原文、不添加原文没有的信息），"
        f"直接输出要点本身，不要任何前后缀：\n\n{text}")
BANNER_FILE = Path(__file__).resolve().parent / "ui" / "banner.txt"  # 开屏文案（ui 包）

# 命令清单：输入 / 时弹出提示（名称, 说明, 是否带参数）。
# needs_arg 的命令 enter 只补全（补空格）不执行，等参数齐了再回车。
COMMANDS = [
    ("/new", "开新会话（旧的自动保存）", False),
    ("/resume", "弹出会话列表，选择恢复历史会话", False),
    ("/sessions", "列出最近的历史会话", False),
    ("/use", "切换到指定会话：/use <uuid>，支持前缀", True),
    ("/tokens", "查看 token 用量：当前对话 + 本会话累计", False),
]


class ThinkingBlock(Vertical):
    """折叠式思考块：一行状态（✻ Thought for Ns）+ 可展开的暗淡正文。

    生成中头部显示计时；正文随流式追加；点击头部或 ctrl+o 切换展开。
    """

    DEFAULT_CSS = """
    ThinkingBlock { height: auto; }
    .think-head { color: $text-muted; text-style: italic dim; height: auto; }
    .think-body { color: $text-muted; text-style: italic dim;
                  padding: 0 0 0 2; height: auto; }
    """

    def __init__(self):
        # 子控件在 __init__ 直接建好挂进去：mount 后立即可用，
        # 不依赖 compose 时机（流式首块到达时 compose 可能还没跑）
        self.head = Static("✻ Thinking…", classes="think-head")
        self.body = Static("", classes="think-body")
        self.body.display = False               # 默认收起
        super().__init__(self.head, self.body)
        self.head_text = "✻ Thinking…"
        self.expanded = False
        self.finished = False
        self.chunks = []

    def append_chunk(self, chunk: str) -> None:
        self.chunks.append(chunk)
        self.body.update("".join(self.chunks))

    def set_thinking(self, seconds: int) -> None:
        """生成中：头部显示计时。"""
        if not self.finished:
            self.head_text = f"✻ Thinking… {seconds}s"
            self.head.update(self.head_text)

    def finish(self, seconds: float | None) -> None:
        """收尾：头部换成 Thought for Ns（幂等，首次生效）。"""
        if self.finished:
            return
        self.finished = True
        dur = f" for {int(seconds)}s" if seconds is not None else ""
        self.head_text = f"✻ Thought{dur} (ctrl+o to expand)"
        self.head.update(self.head_text)

    def restore(self, content: str) -> None:
        """从库里重放历史思考（无计时时长）。"""
        self.chunks = [content]
        self.body.update(content)
        self.finish(None)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.body.display = self.expanded

    def on_click(self) -> None:
        self.toggle()


class SessionPicker(Vertical):
    """/resume 的会话选择列表：挂在对话流里（消息区正下方、状态行上方），
    跟着内容走而不是屏幕中间弹窗。↑↓ 移动，enter 选中恢复，esc 取消。

    每行以最后一次对话为准：短 uuid · 创建时间 · 最后活跃时间 · 最后一条
    消息摘要（❯ 用户 / ● 助手）。Option 的 id 就是完整 uuid，选中事件
    冒泡给 ChatApp 处理（_close_picker + _switch_session）。
    """

    DEFAULT_CSS = """
    SessionPicker { height: auto; margin: 0 1;
                    border: round $accent; background: $surface; padding: 0 1; }
    .picker-title { color: $text-muted; text-style: dim; height: 1; }
    #picker-list { height: auto; max-height: 12; padding: 0; }
    """

    def __init__(self, sessions: list):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        yield Static("恢复历史会话（↑↓ 选择 · enter 恢复 · esc 取消）",
                     classes="picker-title")
        yield OptionList(*[
            Option(self._fmt(s), id=s["uuid"])
            for s in self.sessions
        ], id="picker-list")

    def on_mount(self) -> None:
        self.query_one("#picker-list", OptionList).focus()

    @staticmethod
    def _fmt(s: dict) -> str:
        """一行会话摘要：短 uuid + 创建/最后时间 + 最后一条消息截断。"""
        short = s["uuid"][:8]
        created = (s["created"] or "")[:16]
        last = (s["last_time"] or "")[:16]
        content = " ".join((s["last_content"] or "").split())
        mark = "❯" if s["last_role"] == "user" else "●"
        # escape：消息内容可能含 [ ]（代码/报错文本），不转义会被当 markup 解析
        return (f"[bold]{short}[/]  创建 {created} · 最后 {last}  "
                f"{mark} {escape(content[:50])}")


def _color_diff_line(line: str) -> str:
    """单行 diff 上色：@@ 块头青、+ 绿、- 红、其余（含 --- +++ 头）暗淡。
    DiffBlock（写完的结果）在用；PermissionBlock 已瘦身为不着色。
    escape 转义：代码里的 [] 不能被当成富文本标记。"""
    if line.startswith("@@"):
        return f"[cyan]{escape(line)}[/]"
    if line.startswith("+") and not line.startswith("+++"):
        return f"[green]{escape(line)}[/]"
    if line.startswith("-") and not line.startswith("---"):
        return f"[red]{escape(line)}[/]"
    return f"[dim]{escape(line)}[/]"


def _diff_stats(diff: str) -> tuple[int, int]:
    """数 diff 的 (+N, -M) 统计（排除 +++/--- 文件头行）。
    写盘审批的摘要行用——默认只给统计，v 键才展开全文。"""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


class DiffBlock(Vertical):
    """编辑工具返回的 diff 渲染块：首行摘要（accent 色）+ 逐行上色的 diff。

    数据来自 agent 的 observation 事件（edit_file 的返回文本），这里只管
    展示：@@ 行青色、删除行红色、新增行绿色、上下文行暗淡。
    v1 不做折叠。注意 escape：代码内容里的 [] 不能被当成富文本标记。
    """

    DEFAULT_CSS = """
    DiffBlock { height: auto; margin: 0 1; padding: 0 1;
                border: round $accent; }
    .diff-head { color: $accent; text-style: bold; height: auto;
                 margin: 0 0 1 0; }
    .diff-body { height: auto; }
    """

    def __init__(self, observation: str):
        lines = observation.splitlines()
        head = lines[0] if lines else ""
        self.head = Static(head, classes="diff-head")
        self.body = Static("\n".join(_color_diff_line(l) for l in lines[1:]),
                           classes="diff-body")
        # 子控件直接挂进去：mount 后立即可用（同 ThinkingBlock 的做法）
        super().__init__(self.head, self.body)


class PermissionAsked(Message):
    """agent 线程 → UI：edit 请求写盘审批。

    seq 是触发请求的回复序号（过期请求在 UI 侧直接拒绝，
    免得 agent 被中断后审批块还在等一个没人管的按键）。"""

    def __init__(self, seq: int, req) -> None:
        super().__init__()
        self.seq = seq
        self.req = req


class PermissionBlock(Vertical):
    """写盘/执行前的审批块：默认两行（摘要 + 按键提示），小而不花。

    瘦身原则（单行摘要风）：
        - 不铺框：无边框无底色，缩进区分层级
        - 不刷色：diff 不着色（+/- 前缀本身可读），标题用默认前景色
        - 不默认展开：写盘审批只给统计（+N -M），v 键才看改动
        - 不列选项：单行按键提示替代 OptionList（省 6 行，免焦点管理）
    按键决策：a 允许本次 / s 本会话此文件 / S 本会话全部 /
    v 看改动（run 审批命令本来就一行，默认给看，v 无操作）/
    d 或 esc 拒绝。焦点锁进本块：审批期间敲键进不了输入框。
    决策后收拢成一行结果留在对话流（沿用原格式）。
    """

    DEFAULT_CSS = """
    PermissionBlock { height: auto; margin: 0 2; padding: 0; }
    .perm-head { height: auto; }
    .perm-body { height: auto; margin: 0 0 0 2; }
    .perm-keys { height: auto; margin: 0 0 0 2; color: $text-muted; }
    """
    can_focus = True          # 焦点锁进本块：审批期间按键进不了输入框
    BINDINGS = [
        ("escape", "deny", "拒绝"), ("d", "deny", "拒绝"),
        ("a", "allow", "允许本次"), ("s", "session", "本会话此文件"),
        ("S", "session_all", "本会话全部"), ("v", "view", "看改动/收起"),
    ]

    def __init__(self, req, on_decided):
        self.req = req
        self._on_decided = on_decided
        self._done = False                   # 防重复决策（连按两次）
        if req.action == "run":
            # 命令执行审批：命令本身就是要审的内容，只一行，默认给看
            head = f"⚠ {escape(req.tool)} 执行命令"
            body_text = f"$ {escape(req.diff)}"
            keys = ("[a]允许  [s]本会话相同命令  [S]本会话所有命令  "
                    "[d/esc]拒绝")
        else:
            # 写盘审批：默认只给统计，v 键才展开改动（不着色）
            added, removed = _diff_stats(req.diff)
            head = (f"⚠ {escape(req.tool)} 写入 {escape(req.path)}"
                    f"（+{added} -{removed}）")
            body_text = escape(req.diff)
            keys = ("[a]允许  [s]本会话该文件  [S]本会话全部  "
                    "[v]看改动  [d/esc]拒绝")
        self.head = Static(head, classes="perm-head")
        self.body = Static(body_text, classes="perm-body")
        self.keys = Static(keys, classes="perm-keys")
        self.body.display = (req.action == "run")   # diff 默认收起
        super().__init__(self.head, self.body, self.keys)

    def on_mount(self) -> None:
        self.focus()      # 焦点锁进本块：审批期间无法往输入框打字

    # ---- 按键决策 ----

    def action_allow(self) -> None:
        self._decide("allow", "✓ 已允许")

    def action_session(self) -> None:
        self._decide("session", "✓ 本会话允许")

    def action_session_all(self) -> None:
        verb = "所有命令执行" if self.req.action == "run" else "所有文件修改"
        self._decide("session_all", f"✓ 本会话允许{verb}")

    def action_deny(self) -> None:
        self._decide("deny", "✗ 已拒绝")

    def action_view(self) -> None:
        """v：写盘审批展开/收起改动；run 审批命令本来就显示着，忽略。"""
        if self.req.action != "run":
            self.body.display = not self.body.display

    def _decide(self, kind: str, mark: str) -> None:
        """决策完成：调用 service 对应档位，收拢成一行结果（只生效一次）。"""
        if self._done:
            return
        self._done = True
        if kind == "allow":
            permission_service.allow(self.req)
        elif kind == "session":
            permission_service.allow_for_session(self.req)
        elif kind == "session_all":
            permission_service.allow_for_session_all(self.req)
        else:
            permission_service.deny(self.req)
        self.body.display = False
        self.keys.display = False
        self.head.update(f"{mark} · {self.req.tool} · {self.req.path}")
        self._on_decided()


def _looks_like_diff(text: str) -> bool:
    """判断一条 observation 是不是 edit_file 的 diff 输出：
    得同时有 @@ 块头和 --- / +++ 文件头（read_file_range 等其它工具的
    文件内容不会两个特征都有）。"""
    lines = text.splitlines()
    return (any(l.startswith("@@") for l in lines)
            and any(l.startswith(("--- ", "+++ ")) for l in lines))


class ChatApp(App):
    CSS = """
    #feed { height: 1fr; scrollbar-size: 0 0; }
    #chat { height: auto; padding: 1 1 0 1; }   /* 顶部空一行 */
    Markdown.assistant { color: $text; }        /* 助手回复正常色（不吃红） */
    /* 标题默认边距是 上2行下1行，空行全堆在标题四周；压成 上1下0 */
    Markdown.assistant MarkdownHeader { margin: 1 0 0 0; }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    #input-row { height: auto; padding: 0 1; margin: 0 1;
                 border: round black; }      /* 输入框包黑色边框 */
    #prompt-sym { width: 2; color: $accent; }
    #input { border: none; padding: 0; height: 1;
             background: $background; }
    #hint { height: 1; color: $text-muted; text-style: dim; padding: 0 1; }
    .tool-line { color: $text-muted; height: auto; padding: 0 0 0 2; }
    .banner { border: round $accent; padding: 0 1; margin: 0 0 2 0; }
    #cmd-hints { display: none; height: auto; margin: 0 1;
                 border: round $accent; background: $surface; }
    .hint-opt { height: 1; color: $text-muted; padding: 0 1; }
    .hint-opt.hint-sel { color: $text; background: $boost; text-style: bold; }
    """
    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("escape", "interrupt", "中断回复"),
        ("ctrl+o", "toggle_thinking", "展开/收起思考"),
        # ↑↓ 不加 priority：会话列表（OptionList）要自己处理上下移动；
        # 输入框场景下按键冒泡到 App 才触发提示导航
        ("up", "hint_prev", "提示上一条"),
        ("down", "hint_next", "提示下一条"),
        # tab 被 App 级焦点遍历占用，必须 priority 才抢得到
        Binding("tab", "hint_accept", "采纳命令提示", priority=True),
    ]
    AUTO_FOCUS = "Input"

    def __init__(self):
        super().__init__()
        self._agent_fn = react_agent   # 自测可替换的假 agent
        self.tool_manager = None       # on_mount 时加载 tool/ 目录
        self._system_prompt = None     # on_mount 时 persona 装配（基础 + 用户/项目层）
        self._reply_seq = 0            # 回复序号：过期线程回调据此丢弃
        self._think = None             # 当前回复的思考折叠块
        self.session_id = None
        self._dialog_id = None       # 当前对话号（最新 user 消息 id；token 归属用）
        # API 历史：只含 user/assistant（thinking 不进对话上下文）
        self.messages = []
        self.generating = False       # 是否正在生成回复（计时/中断用）
        self.t0 = 0.0
        self.reply_worker = None
        self._hint_cmds = []       # 当前匹配的命令提示（输入 / 开头时）
        self._hint_index = 0       # 提示列表高亮项
        self._picker = None        # /resume 挂起的会话选择列表
        self._perm_block = None    # 挂起的审批块（edit 写盘前）
        self._reported_agents = set()   # 已汇报过报告的子 agent id（屏障去重）
        self._inject = queue.Queue()    # 生成中途的用户补充（reply() 每轮换新）

    # ---- 布局 ----

    def compose(self) -> ComposeResult:
        # 整个 feed（消息 + 状态行 + 输入框 + 提示）在一个滚动容器里：
        # 内容不足一屏时顶对齐、输入框紧跟最后一条消息，
        # 内容超屏后自动滚底，输入框贴着屏幕底。
        with VerticalScroll(id="feed"):
            yield Vertical(id="chat")
            yield Vertical(id="cmd-hints")        # 命令提示浮层（默认隐藏）
            yield Static("", id="status")
            yield Horizontal(
                Static("❯", id="prompt-sym"),
                Input(placeholder="输入消息回车发送；/new 新会话  /resume 恢复历史  /use <uuid> 切换",
                      id="input"),
                id="input-row",
            )
            yield Static(" esc 中断 · ctrl+o 展开思考 · ctrl+q 退出", id="hint")

    def on_mount(self) -> None:
        db.init_db()
        self.tool_manager = ToolManager(_ROOT / "tool")
        # persona 装配：基础身份 + 用户级（~/.mychat）+ 项目级（AGENTS.md 等）。
        # 超长层走 LLM 摘要（有缓存，源文件没变不重摘）；无 summarizer 时
        # loader 内部硬截断兜底。启动跑一次，之后每轮照用
        self._system_prompt = build_system_prompt(
            NATIVE_SYSTEM_PROMPT, _ROOT, summarizer=_summarize_for_persona)
        # 注册审批回调：此后 edit 写盘前 agent 线程会挂起等用户按键
        permission_service.ask_ui = self._ask_permission
        self.mount_banner()
        # 每次启动都是全新会话；历史会话仍留在库里（/sessions 查看 /use 切换）
        self.session_id = db.new_session()
        supervisor.set_parent(self.session_id)   # 后台子 agent 挂靠本会话
        self.refresh_status()
        self.set_interval(0.5, self._tick)      # 生成中：计时刷新

    def on_resize(self) -> None:
        # 终端缩放后内容可能超屏（或缩出大片空白），滚底保证输入框可见
        self.feed().scroll_end(animate=False)

    def _tick(self) -> None:
        if not self.generating:
            return
        n = int(time.monotonic() - self.t0)
        running = sum(1 for h in supervisor.running_handles()
                      if h.parent_id == self.session_id)
        extra = f" · 后台 {running} 个运行中" if running else ""
        self.query_one("#status", Static).update(
            f"✻ Thinking… {n}s (esc 中断){extra}")
        for block in self.query(ThinkingBlock):
            block.set_thinking(n)

    # ---- 开屏横幅 ----

    def mount_banner(self) -> None:
        """开屏自我介绍：文案在 banner.txt（纯文本，无富文本标记，改文案不用动代码），
        代码负责上色：前 14 列是蜜蜂画（第 3 行翅膀青、条纹黄），右边是文案。"""
        content = BANNER_FILE.read_text(encoding="utf-8").format(model=MODEL)
        rows = []
        for i, line in enumerate(content.splitlines()):
            if not line.strip():
                continue
            art, text = line[:14], line[14:]
            if i == 2:   # 蜜蜂身体行：翅膀青、条纹黄
                art_html = (f"[bright_cyan]{art[:2]}[/][bold yellow]{art[2:9]}[/]"
                            f"[bright_cyan]{art[9:]}[/]")
            else:        # 其余行：翅膀/天线青，眼睛黄
                style = "dim" if i == 0 else ("bold yellow" if i == 1 else "bright_cyan")
                art_html = f"[{style}]{art}[/]"
            if text:
                art_html += f"[{'bold' if i == 0 else 'dim'}]{text}[/]"
            rows.append(art_html)
        # 蜜蜂+介绍装进边框；框下两个空行用 CSS margin（内容里的 \n 会被框包住）
        self.chat().mount(Static("\n".join(rows), classes="banner"))

    # ---- agent 进度回调 ----

    def _on_agent_step(self, seq: int, event: str, data) -> None:
        """agent 线程的进度回调：经 call_from_thread 回 UI 线程上屏。
        过期回复（seq 不匹配 / 已被中断）的更新直接丢弃。"""
        def apply() -> None:
            if seq != self._reply_seq or not self.generating:
                return
            if event == "thinking" and data.strip():
                if self._think is not None:
                    self._think.append_chunk(data + "\n")
            elif event == "action":
                # escape：工具参数里可能带代码片段（含 [ ]），不转义会被
                # rich 当 markup 解析而抛 MarkupError
                self.chat().mount(Static(f"  ⚙ {escape(data)}", classes="tool-line"))
                self.query_one("#status", Static).update(f"⚙ {escape(data)}")
            elif event == "observation" and _looks_like_diff(data):
                # 只渲染长得像 diff 的观察（edit_file 的返回）；
                # read_file_range 的文件内容等其它观察照旧不上屏
                self.chat().mount(DiffBlock(data))
            self.feed().scroll_end(animate=False)
        self.call_from_thread(apply)

    # ---- 审批（edit 写盘前） ----

    def _ask_permission(self, req) -> None:
        """permission_service 的回调，agent 线程调用：把请求投给 UI。
        只投递不等待——挂起等待在 service 的 Event.wait 里，按键后唤醒。
        post_message 线程安全；seq 在此刻读取，过期请求在处理侧拒绝。"""
        self.post_message(PermissionAsked(self._reply_seq, req))

    def on_permission_asked(self, msg: PermissionAsked) -> None:
        """挂审批块（焦点移过去，a / s / d 生效）。过期请求直接拒绝。"""
        if msg.seq != self._reply_seq or not self.generating:
            permission_service.deny(msg.req)
            return
        block = PermissionBlock(msg.req, self._permission_decided)
        self._perm_block = block
        self.feed().mount(block, before=self.query_one("#status"))
        # 焦点由块自己 on_mount 锁进选项列表（此时不再额外 focus 块）
        self.feed().scroll_end(animate=False)

    def _permission_decided(self) -> None:
        """审批块决策完成：焦点回输入框（块自己收拢成一行结果留在流里）。"""
        self._perm_block = None
        self.query_one("#input", Input).focus()
        self.refresh_status()

    # ---- 消息 widget ----

    def new_message(self, cls: str, first_text: str) -> Markdown:
        """往容器挂一个带角色类的 Markdown 消息块（user/assistant），
        返回它供流式 append。"""
        box = Markdown(first_text, classes=cls)
        self.chat().mount(box)
        return box

    def mount_message(self, role: str, content: str) -> None:
        """按角色完整挂一条历史消息（重启恢复 / /use 切换用）。"""
        if role == "user":
            self.new_message("user", f"\n❯ {content}\n")
        elif role == "thinking":
            block = ThinkingBlock()
            self.chat().mount(block)
            block.restore(content)
        else:
            self.new_message("assistant", f"\n● {content}\n")

    # ---- 事件 ----

    async def on_input_changed(self, event: Input.Changed) -> None:
        """输入变化：/ 开头且未输参数时刷新命令提示，否则隐藏。"""
        self._refresh_hints(event.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        # 命令提示展开时：enter 采纳高亮项；已输完整且无参命令才照常执行
        if self._hint_cmds:
            name, _, needs_arg = self._hint_cmds[self._hint_index]
            if text != name or needs_arg:
                self._apply_hint()
                return
            self._hide_hints()
        event.input.value = ""
        self._hide_hints()
        if not text:
            return
        if text.startswith("/"):
            self.run_command(text)
            return

        # 正在生成中：不顶掉当前任务（steering）——这条消息作为补充并入，
        # 下一轮开头送进 agent 循环；token 归属还是当前 dialog。
        # esc 仍然是打断：先 esc 再输入 = 新任务。
        if self.generating:
            self.new_message("user", f"\n❯ {text}（已并入当前任务）\n\n")
            self.messages.append({"role": "user", "content": text})
            db.add_message(self.session_id, "user", text)
            log.info("用户补充入队: %.60s", text)
            self._inject.put(text)
            self.feed().scroll_end(animate=False)
            return

        # 用户消息：上屏（❯ 前缀）、进 API 历史、入库。
        # 后台报告兜底：空闲期间收尾的子 agent，报告并入本条消息文本
        # （正常流程由屏障在回合内汇合，这里只捞轮外落下的）
        blocks = self._collect_agent_blocks()
        if blocks:
            blocks.append("（以上是后台子 agent 的报告，已随本条消息送达，"
                          "请结合用户问题答复。）")
            text = text + "\n\n" + "\n".join(blocks)
        self.new_message("user", f"\n❯ {text}\n\n")
        self.messages.append({"role": "user", "content": text})
        # 对话号 = 本条 user 消息 id：本轮问答的全部模型调用（主循环 +
        # persona 摘要 + 子 agent）都记在它名下
        self._dialog_id = db.add_message(self.session_id, "user", text)
        if sum(m["role"] == "user" for m in self.messages) == 1:
            db.set_title(self.session_id, text[:30])
        self.refresh_status()
        self.feed().scroll_end(animate=False)

        # 回复交给 worker：exclusive=True 让新输入打断上一条未完成的回复
        self.reply_worker = self.run_worker(self.reply(), exclusive=True)

    async def reply(self) -> None:
        """跑 agent（ReAct 多轮工具循环在线程里执行）：思考/工具进度上屏，
        最终答案红色 ● 块显示、入库；错误上屏不崩溃。"""
        self.generating = True
        self.t0 = time.monotonic()
        self._reply_seq += 1
        seq = self._reply_seq
        # token 归属：设在本协程上下文里，asyncio.to_thread 会拷给
        # agent 工作线程；子 agent 由 runtime 在自己线程里另行 set
        set_usage_context(self.session_id, dialog_id=self._dialog_id)
        think = ThinkingBlock()
        self._think = think
        self.chat().mount(think)
        # 本轮专属补充队列：每轮换新，上一轮被 esc 打断时滞留的补充
        # 不带到这轮（它们已进 self.messages，下一轮历史自然带着）
        self._inject = queue.Queue()
        question = self.messages[-1]["content"] if self.messages else ""
        try:
            answer = await asyncio.to_thread(
                self._agent_fn, question,
                verbose=False,
                history=list(self.messages),
                tool_manager=self.tool_manager,
                on_step=lambda ev, data: self._on_agent_step(seq, ev, data),
                system_prompt=self._system_prompt,
                on_turn=self._agent_barrier,   # 每轮开头：等子 agent 汇合
                inject_queue=self._inject,     # 生成中途的用户补充从这进
            )
        except asyncio.CancelledError:
            # esc / 新消息打断：线程杀不掉，跑完后的过期回调被 seq 丢弃
            think.finish(time.monotonic() - self.t0)
            self.generating = False
            self.refresh_status("⏸ 已中断（后台 agent 可能仍在跑完当前轮）")
            return
        except anthropic.APIError as e:
            log.exception("API 出错（会话 %s）", self.session_id)
            self.new_message("assistant",
                             f"\n● ⚠ API 出错 {type(e).__name__}: {str(e)[:200]}\n")
            self.generating = False
            self.refresh_status("API 错误，稍后重试")
            return
        except Exception as e:      # 其余错误也不让 TUI 崩
            log.exception("reply 未知错误（会话 %s）", self.session_id)
            self.new_message("assistant", f"\n● ⚠ 出错 {type(e).__name__}: {e}\n")
            self.generating = False
            self.refresh_status("未知错误")
            return

        elapsed = time.monotonic() - self.t0
        full = (answer or "").strip() or "（agent 没有给出答案）"
        think.finish(elapsed)
        if think.chunks:
            db.add_message(self.session_id, "thinking", "".join(think.chunks))
        self.messages.append({"role": "assistant", "content": full})
        db.add_message(self.session_id, "assistant", full)
        self.new_message("assistant", f"\n● {full}\n")
        self.generating = False
        self.feed().scroll_end(animate=False)
        self.refresh_status(f"✻ Baked for {elapsed:.0f}s")

    # ---- 多 agent 汇合屏障 ----

    def _agent_barrier(self, messages: list) -> list:
        """on_turn 钩子（每轮 LLM 调用前跑一次）：等本会话后台子 agent
        全部收尾，把报告注入当前对话。确定性由协议保证——模型刚发过
        spawn（stop_reason == tool_use），下一轮必然经过这里，不依赖
        模型"记得来收"。注入并进最后一条 user 消息：方舟接口不接受
        连续两条 user。"""
        running = [h for h in supervisor.running_handles()
                   if h.parent_id == self.session_id]
        still = []
        t0 = time.monotonic()
        for h in running:
            if not h.done_event.wait(AGENT_WAIT_TIMEOUT):
                still.append(h)   # 超时放行：卡死的子 agent 不陪葬主对话
        if running:
            log.info("屏障汇合: 等待 %s 个子 agent %.1fs，超时 %s 个",
                     len(running), time.monotonic() - t0, len(still))
        blocks = self._collect_agent_blocks()
        blocks += [f'<agent-report id="{h.id}" type="{h.spec.name}" status="running">'
                   f'\n仍在运行，已超过 {AGENT_WAIT_TIMEOUT}s 未完成；本轮先不等它，'
                   f'完成后其报告会在后续轮次自动送达。\n</agent-report>'
                   for h in still]
        if not blocks:
            return messages
        blocks.append("（以上是后台子 agent 的报告/状态，请结合用户问题向用户汇总；"
                      "未送达的结果不要编造。）")
        text = "\n".join(blocks)
        last = messages[-1] if messages else None
        if last and last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, list):       # tool_result 打包消息：加文本块
                content.append({"type": "text", "text": "\n" + text})
            else:                                # 纯文本 user 消息：拼尾部
                last["content"] = f"{content}\n\n{text}"
        else:
            messages.append({"role": "user", "content": text})
        return messages

    def _collect_agent_blocks(self) -> list[str]:
        """收集本会话已收尾、尚未汇报过的子 agent 报告（收即标记去重）。
        running 的不收——屏障负责等它们；超时未完成的也不收，等真报告。"""
        blocks = []
        for h in supervisor.handles_of(self.session_id):
            if h.id in self._reported_agents or h.status == "running":
                continue
            self._reported_agents.add(h.id)
            blocks.append(f'<agent-report id="{h.id}" '
                          f'type="{h.spec.name}" status="{h.status}">\n'
                          f'{h.report}\n</agent-report>')
        return blocks

    # ---- 快捷键动作 ----

    def action_quit(self) -> None:
        """ctrl+q：先唤醒挂在审批等待上的 agent 线程（一律拒绝），
        再收尾后台子 agent（状态如实落库、等在屏障上的主对话放行），
        最后杀掉持久 cmd 会话（超时/失控命令一起收走），退出。"""
        permission_service.cancel_all()
        supervisor.cancel_all()
        try:
            from agent.shell_pool import close_all_agent_shells
            close_all_agent_shells()
        except Exception:
            log.exception("退出时收子 agent shell 失败")
        # 顺序固定：先放审批，再清 agent，最后杀 shell——防止线程
        # 还卡在某个 Event 上等一个永不到来的答复
        try:
            from shell.persistent_shell import close_shell
            close_shell()
        except Exception:
            log.exception("退出时收主 shell 失败")
        log.info("应用退出，清场完成（会话 %s）", self.session_id)
        self.exit()

    def action_interrupt(self) -> None:
        """esc：命令提示/会话列表开着先关它们，否则中断当前回复。"""
        if self._hint_cmds:
            self._hide_hints()
            return
        if self._picker is not None:
            self._close_picker()
            return
        if self.reply_worker is not None and self.reply_worker.is_running:
            self.reply_worker.cancel()

    def action_toggle_thinking(self) -> None:
        """ctrl+o：全部思考块展开/收起。"""
        for block in self.query(ThinkingBlock):
            block.toggle()

    # ---- 命令提示 ----

    def _refresh_hints(self, value: str) -> None:
        """按输入框内容过滤命令清单并渲染提示浮层（非命令则隐藏）。"""
        if value.startswith("/") and " " not in value:
            self._hint_cmds = [c for c in COMMANDS if c[0].startswith(value)]
        else:
            self._hint_cmds = []
        if not 0 <= self._hint_index < len(self._hint_cmds):
            self._hint_index = 0
        box = self.query_one("#cmd-hints")
        box.display = bool(self._hint_cmds)
        box.remove_children()
        for i, (name, desc, _) in enumerate(self._hint_cmds):
            sel = " hint-sel" if i == self._hint_index else ""
            box.mount(Static(f"{name:<12} {desc}", classes="hint-opt" + sel))

    def _hide_hints(self) -> None:
        self._refresh_hints("")

    def _apply_hint(self) -> None:
        """把高亮命令填进输入框（带参命令补空格，方便直接接参数）。"""
        if not self._hint_cmds:
            return
        name, _, needs_arg = self._hint_cmds[self._hint_index]
        inp = self.query_one("#input", Input)
        inp.value = name + (" " if needs_arg else "")
        inp.cursor_position = len(inp.value)
        self._refresh_hints(inp.value)
        inp.focus()

    def action_hint_prev(self) -> None:
        """↑：提示浮层里上移高亮（没开时是空操作）。"""
        if self._hint_cmds:
            self._hint_index = (self._hint_index - 1) % len(self._hint_cmds)
            self._refresh_hints(self.query_one("#input", Input).value)

    def action_hint_next(self) -> None:
        """↓：提示浮层里下移高亮。"""
        if self._hint_cmds:
            self._hint_index = (self._hint_index + 1) % len(self._hint_cmds)
            self._refresh_hints(self.query_one("#input", Input).value)

    def action_hint_accept(self) -> None:
        """tab：采纳高亮命令。"""
        self._apply_hint()

    # ---- 命令 ----

    def run_command(self, text: str) -> None:
        cmd, *args = text.split()
        # 选择列表开着时，任何其它命令都先收起它
        if self._picker is not None and cmd != "/resume":
            self._close_picker()
        if cmd == "/new":
            old = self.session_id
            self.session_id = db.new_session()
            supervisor.set_parent(self.session_id)
            self._dialog_id = None
            self.messages = []
            self.chat().remove_children()
            self.new_message("assistant",
                             f"*（已开新会话 {self.session_id[:8]}，"
                             f"旧会话 {old[:8]} 已保存，/use {old} 可切回）*\n")
            self.refresh_status()
            self.feed().scroll_end(animate=False)
        elif cmd == "/resume":
            rows = db.list_sessions()
            if not rows:
                self.new_message("assistant", "*（暂无历史会话可恢复）*\n")
            elif self._picker is None:      # 已开着就不重复挂
                self._picker = SessionPicker(rows)
                self.feed().mount(self._picker, before=self.query_one("#status"))
                self.feed().scroll_end(animate=False)
        elif cmd == "/sessions":
            rows = db.list_sessions()
            if not rows:
                self.new_message("assistant", "*（暂无历史会话）*\n")
            else:
                lines = ["**历史会话（按最后活跃排序）：**\n"]
                for r in rows:
                    mark = " ←当前" if r["uuid"] == self.session_id else ""
                    content = " ".join((r["last_content"] or "").split())[:30]
                    lines.append(f"- [{r['uuid'][:8]}] 最后 {r['last_time'][:16]} "
                                 f"{content}{mark}\n")
                self.new_message("assistant", "\n" + "".join(lines)
                                 + "\n/resume 选择恢复 或 /use <uuid> 切换\n")
        elif cmd == "/use" and args:
            uid = db.resolve_session(args[0])
            if uid is None:
                self.new_message("assistant",
                                 f"*（找不到会话 {args[0]}，/resume 选择）*\n")
                return
            self._switch_session(uid)
        elif cmd == "/tokens":
            self._show_token_usage()
        else:
            self.new_message("assistant", f"*（未知命令 {text}）*\n")

    def on_option_list_option_selected(self,
                                       event: OptionList.OptionSelected) -> None:
        """会话选择列表：enter 恢复选中的会话（事件从 SessionPicker 冒泡上来）。"""
        if self._picker is None:
            return
        self._close_picker()
        self._switch_session(event.option_id)

    def _close_picker(self) -> None:
        """收起会话选择列表，焦点回输入框。"""
        if self._picker is not None:
            self._picker.remove()
            self._picker = None
        self.query_one("#input", Input).focus()

    def _show_token_usage(self) -> None:
        """token 用量报表（/tokens）：当前对话（进行中）+ 本会话历次对话，
        纯文字版。数据来自 token_usage 表的两级汇总；子 agent 的消耗已含
        在内（想细分可按 agent_id 拆，暂不上屏）。"""
        def fmt(d: dict) -> str:
            return (f"输入 {d['input_tokens']:,} · 输出 {d['output_tokens']:,}"
                    f" · 缓存读 {d['cache_read_tokens']:,}"
                    f" · 缓存写 {d['cache_write_tokens']:,}")

        lines = ["**token 用量**\n"]
        if self._dialog_id is not None:
            cur = db.dialog_usage(self._dialog_id)
            if cur["total"]["calls"]:
                lines.append(f"- **当前对话 #{self._dialog_id}**（进行中）："
                             f"{cur['total']['calls']} 次调用，{fmt(cur['total'])}\n")
                for r in cur["by_model"]:
                    lines.append(f"  - {r['model']}: {r['calls']} 次，{fmt(r)}\n")
            else:
                lines.append("- 当前对话：还没有模型调用\n")
        else:
            lines.append("- 当前对话：（还没发过消息）\n")

        sess = db.session_usage(self.session_id)
        lines.append(f"\n**本会话合计**：{sess['total']['calls']} 次调用，"
                     f"{fmt(sess['total'])}\n")
        for d in sess["dialogs"]:
            q = " ".join((d["question"] or "").split())
            total_tok = sum(d[k] or 0 for k in db.USAGE_TOKEN_COLS)
            mark = " ←进行中" if d["dialog_id"] == self._dialog_id else ""
            lines.append(f"- #{d['dialog_id']} \"{q}\"：{d['calls']} 次，"
                         f"总量 {total_tok:,}{mark}\n")
        self.new_message("assistant", "\n" + "".join(lines) + "\n")

    def _switch_session(self, uid: str | None) -> None:
        """切到指定会话：清屏重放历史（/use 与 /resume 选择器共用）。
        uid 为 None 是选择器取消，不动会话，只把焦点还给输入框。"""
        self.query_one("#input", Input).focus()
        if uid is None:
            return
        self.session_id = uid
        supervisor.set_parent(self.session_id)
        self._dialog_id = None       # 对话号属旧会话，切换即作废
        self.messages = []
        self.chat().remove_children()
        for m in db.load_messages(uid):
            self.mount_message(m["role"], m["content"])
            if m["role"] in ("user", "assistant"):
                self.messages.append({"role": m["role"], "content": m["content"]})
        self.refresh_status()
        self.feed().scroll_end(animate=False)

    # ---- 小工具 ----

    def chat(self) -> Vertical:
        return self.query_one("#chat", Vertical)

    def feed(self) -> VerticalScroll:
        """整个对话流（含输入框）：滚它让输入框保持可见。"""
        return self.query_one("#feed", VerticalScroll)

    def refresh_status(self, extra: str = "") -> None:
        text = f"⏸ 会话 {self.session_id[:8]} · 模型 {MODEL} · agent 模式"
        if extra:
            text = f"{extra} · {text}"
        self.query_one("#status", Static).update(text)
