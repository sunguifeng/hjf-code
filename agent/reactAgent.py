# -*- coding: utf-8 -*-
"""
ReAct Agent（Reasoning + Acting 循环）

双模式:
- use_native_tools=True（默认）: 标准现代协议（Anthropic tools）——一个循环 +
  两条 append 规则（assistant 原样回填 / user 打包 tool_result 块）+
  stop_reason 终止判断，无需任何文本解析
- use_native_tools=False: 经典文本协议（react_prompt.txt + Action/Observation
  正则解析），用于不支持 function calling 的模型接口

工具来源: tool/ 目录下所有 tool_*.py（ToolManager 启动时自动加载）。
大模型后端复用 model/model.py 的 call_model_messages / call_model。
对外入口方法：react_agent(question) —— 传入问题，返回最终答案
"""

import json
import queue
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applog import get_logger
from model.model import call_model, call_model_messages
from tool.base_tool import ToolManager

_ROOT = Path(__file__).resolve().parent.parent

log = get_logger(__name__)

# 日志里工具 input / observation 的截断长度（防一条大结果刷爆轮转文件）
_LOG_SNIPPET = 500

# 最大循环轮数的默认值/合法范围（config/settings.json 的 agent.max_steps
# 可覆盖；坏配置回退默认——调优参数不值得让应用停摆）
DEFAULT_MAX_STEPS = 10
_MAX_STEPS_CEILING = 50


def _load_max_steps(path: Path = None) -> int:
    """读 config/settings.json 的 agent.max_steps。
    缺文件/缺段/坏值/超范围一律回退默认。自测用 path 注入隔离。"""
    p = Path(path or _ROOT / "config" / "settings.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get("agent", {}).get("max_steps")
        if isinstance(v, int) and 1 <= v <= _MAX_STEPS_CEILING:
            return v
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return DEFAULT_MAX_STEPS


MAX_STEPS = _load_max_steps()

# 单次模型调用的输出预算（thinking + 正文 + tool_use 共用一栏）。
# 注意 thinking 的 budget_tokens 是"保底预留"不是封顶——思考可以
# 超过它继续吃 max_tokens 的剩余空间（协议设计如此）。8192 实测
# 不够：复杂任务的思考就能吃满导致正文零字被截断；16384 给正文留余量
MAX_TOKENS = 16384

# 原生模式的系统提示词（格式/工具清单/终止都由协议承担，
# 提示词只管协议管不了的事：勤快——不猜、不偷懒提前收工）
NATIVE_SYSTEM_PROMPT = (
    "你是一个可以使用工具的智能助手。"
    "在用户的问题被完全解决之前不要结束回合：需要信息就调用工具核实，"
    "禁止凭猜测作答；工具失败时读取错误信息自行修正或换方案，"
    "而不是放弃任务。"
    "无论用户使用什么语言提问，你的思考过程（thinking）和最终回复都必须使用简体中文。"
)


# ---- 原生 function calling 模式（标准协议） ----

def _inject_user_text(messages: list, text: str) -> None:
    """把一段用户补充并入消息列表（steering）。末条是 user 消息就并进去
    ——方舟不接受连续多条 user 消息（观察打包规则同理）；末条是别的
    才新开一条。user 消息内容是块列表（tool_result 打包）就追加 text 块，
    是纯字符串就拼接。"""
    if messages and messages[-1]["role"] == "user":
        last = messages[-1]
        if isinstance(last["content"], list):
            last["content"].append({"type": "text", "text": text})
        else:
            last["content"] = f"{last['content']}\n\n{text}"
    else:
        messages.append({"role": "user", "content": text})


def _react_native(question: str, verbose: bool, max_steps: int,
                  manager: ToolManager, thinking: bool,
                  history: list | None = None,
                  on_step: Callable | None = None,
                  system_prompt: str | None = None,
                  on_turn: Callable[[list], list] | None = None,
                  inject_queue: "queue.Queue | None" = None) -> str:
    """标准协议 ReAct 循环：一个循环 + 两条 append 规则 + stop_reason 终止判断。

    - Action：assistant 消息原样回填 resp.content（含 tool_use 块）
    - Observation：所有工具结果包成 tool_result 块（tool_use_id 配对），
      一个 user 消息打包——注意不能每个结果单独发一条 user 消息，
      方舟接口不接受连续多条 user 消息
    - 终止：stop_reason != "tool_use"（模型显式宣布收工）
    - 错误也是观察：工具失败的 ERROR 头文本原样回填，模型自主处理
    - history 传入完整对话历史（含最新 user 消息）则多轮续跑，
      否则单问题模式新建首条消息
    - on_step(event, data) 每轮进度回调：thinking / action / observation
    - system_prompt 覆盖默认系统提示词（None 用 NATIVE_SYSTEM_PROMPT）：
      persona 多层装配、子 agent 档位说明都从这进来
    - on_turn(messages) 每轮 LLM 调用前的消息变换钩子，返回处理后的
      messages——子 agent 汇合屏障（等全部后台 agent 完成并把报告
      注入本轮对话）从这进来；不传则零开销
    - inject_queue 生成中途的用户补充队列（steering）：每轮开头排空，
      并入末条 user 消息——正在飞行中的那次 API 调用改不了（协议边界），
      注入最快发生在当前调用返回之后
    """
    tools = manager.get_anthropic_tools()
    # history 逐消息 dict 拷贝（不是 list() 浅拷）：后续往末条 user 消息里
    # 并入补充（steering）改的是本循环的副本——否则会把 TUI 传进来的
    # 原 dict（self.messages 里的活对象）原地改掉，污染持久历史
    messages = ([dict(m) for m in history] if history
                else [{"role": "user", "content": question}])
    carried: list[str] = []   # 续写接力：正文被截断时存下的前几段

    for turn in range(1, max_steps + 1):
        if on_turn:      # 变换钩子：可注入/追加消息（屏障汇合、邮箱投递等）
            messages = on_turn(messages)
        if inject_queue is not None:     # steering：收编用户中途补充的话
            n_injected = 0
            while True:
                try:
                    _inject_user_text(messages, inject_queue.get_nowait())
                    n_injected += 1
                except queue.Empty:
                    break
            if n_injected:
                log.info("turn=%s 注入用户补充 %d 条", turn, n_injected)
        if verbose:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"===== 第 {turn} 轮 {ts} =====", flush=True)

        resp = call_model_messages(system_prompt or NATIVE_SYSTEM_PROMPT,
                                   messages, tools=tools, thinking=thinking,
                                   max_tokens=MAX_TOKENS)
        log.info("turn=%s stop=%s usage=%s", turn, resp.stop_reason,
                 getattr(resp, "usage", None))
        blocks = list(resp.content)
        tool_uses = [b for b in blocks if b.type == "tool_use"]
        think = "".join(b.thinking for b in blocks if b.type == "thinking")
        text = "".join(b.text for b in blocks if b.type == "text")

        if verbose and think.strip():
            print(f"[思考] {think.strip()}\n", flush=True)
        if on_step and think.strip():
            on_step("thinking", think)

        if verbose and text.strip():
            print(f"[模型] {text.strip()}\n", flush=True)

        if resp.stop_reason != "tool_use" or not tool_uses:  # 模型宣布收工
            # 正文被截断 ≠ 收工（stop=max_tokens 且已写出正文）：
            # 回填半截正文（剥掉 thinking 块——续写的锚是正文），
            # 让模型从中断处接着写。已写的字不浪费；续写若再截断
            # 会再次走到这，max_steps 兜底。
            if (resp.stop_reason == "max_tokens" and text.strip()
                    and turn < max_steps):
                carried.append(text)
                log.warning("正文被截断（本段 %s 字），回填续写", len(text))
                messages.append({"role": "assistant",
                                 "content": [b for b in blocks
                                             if b.type == "text"]})
                messages.append({"role": "user",
                                 "content": "你上一条回复因长度限制被截断，"
                                            "从中断处继续写完，不要重复已写内容。"})
                continue
            if not text.strip() and not carried:
                # "未能回答"的病灶现场：模型收工但没给正文（可能只有思考、
                # 空响应、refusal 等）——把响应形态记全，这是排查唯一线索
                brief = [(b.type, len(getattr(b, "text", "")
                                       or getattr(b, "thinking", "") or ""))
                         for b in blocks]
                log.warning("模型收工但无文本: stop=%s blocks=%s question=%.60s",
                            resp.stop_reason, brief, question)
            return ("".join(carried) + text).strip() or f"（未能回答：{question}）"

        # 记 Action：原样回填（含 tool_use 块）
        messages.append({"role": "assistant", "content": resp.content})

        # Act：执行工具，错误也是观察
        results = []
        for b in tool_uses:
            log.info("tool=%s input=%.500s", b.name, dict(b.input or {}))
            observation = manager.execute(b.name, dict(b.input or {}))
            log.debug("tool=%s observation=%.500s", b.name, observation)
            if on_step:
                on_step("action", f"{b.name}({dict(b.input or {})})")
                on_step("observation", observation)
            if verbose:
                print(f"[Action] {b.name}({b.input})\n"
                      f"[Observation] {observation}\n{'-' * 50}", flush=True)
            results.append({
                "type": "tool_result",
                "tool_use_id": b.id,   # ID 配对，观察精确挂回它的动作
                "content": observation,
            })

        # 记 Observation：一个 user 消息打包全部 tool_result 块
        messages.append({"role": "user", "content": results})

    log.warning("达最大循环轮数 %s，未得出答案", max_steps)
    return "（已达最大循环轮数，未能得出答案）"


# ---- 经典文本协议模式（fallback） ----
# 工具注册表：name -> (描述, 函数)，函数签名为 (输入字符串) -> 结果字符串
TOOLS: dict[str, tuple[str, Callable[[str], str]]] = {}


def tool(description: str):
    """装饰器：注册一个文本模式工具"""

    def register(func: Callable[[str], str]) -> Callable[[str], str]:
        TOOLS[func.__name__] = (description, func)
        return func

    return register



# ---- 提示词模板：从同目录的 react_prompt.txt 加载 ----
PROMPT_FILE = Path(__file__).resolve().parent / "react_prompt.txt"
PROMPT_TEMPLATE = PROMPT_FILE.read_text(encoding="utf-8")

TOOL_BLOCK = "\n".join(f"- {name}: {desc}" for name, (desc, _) in TOOLS.items())


def _parse_action(reply: str) -> tuple[str, str] | None:
    """从模型回复中解析 (Action, Action Input)，没有动作则返回 None"""
    m = re.search(r"Action:\s*(\S+?)\s*\nAction\s*Input:\s*(.*?)(?:\n|$)", reply, re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def _strip_fake_observation(reply: str) -> str:
    """删除模型越格式自己编造的 Observation 行（真实观察只能由系统执行工具后填入）"""
    return re.sub(r"^\s*Observation:.*(?:\n|$)", "", reply, flags=re.M | re.I)


def _parse_final_answer(reply: str, question: str) -> str | None:
    """提取 Final Answer；没有则把问题重发给模型的回复作为兜底"""
    m = re.search(r"Final Answer:\s*(.*)", reply, re.S)
    return m.group(1).strip() if m else (reply.strip() or f"（未能回答：{question}）")


def _react_text(question: str, verbose: bool) -> str:
    """经典文本协议：解析 Action/Observation，历史记录累积带入每一轮"""
    history = f"问题: {question}"
    for _ in range(MAX_STEPS):
        reply = call_model(PROMPT_TEMPLATE.format(tools=TOOL_BLOCK, history=history))

        action = _parse_action(reply)
        if action is None:  # 没有动作了，视为给出最终答案
            return _parse_final_answer(reply, question)

        name, action_input = action
        if verbose:
            print(f"[模型] {reply.strip()}\n", flush=True)

        if name not in TOOLS:
            observation = f"错误：不存在工具 {name!r}，可用工具：{', '.join(TOOLS)}"
        else:
            observation = TOOLS[name][1](action_input)

        if verbose:
            print(f"[Observation] {observation}\n{'-' * 50}", flush=True)
        # 工具调用结果（Observation）累积进历史记录，带入下一轮循环
        history += (
            f"\nThought: {_strip_fake_observation(reply).strip()}"
            f"\nAction: {name}\nAction Input: {action_input}"
            f"\nObservation: {observation}"
        )

    log.warning("达最大循环轮数 %s，未得出答案", max_steps)
    return "（已达最大循环轮数，未能得出答案）"


# ---- 对外入口 ----

def react_agent(question: str, verbose: bool = True, use_native_tools: bool = True,
                max_steps: int = MAX_STEPS, thinking: bool = True,
                tool_manager: ToolManager | None = None,
                history: list | None = None,
                on_step: Callable | None = None,
                system_prompt: str | None = None,
                on_turn: Callable[[list], list] | None = None,
                inject_queue: "queue.Queue | None" = None) -> str:
    """
    入口方法：运行 ReAct 循环。

    :param question: 用户问题
    :param verbose: 是否打印每轮思考/动作/观察过程
    :param use_native_tools: True=原生 function calling（默认）；False=经典文本协议
    :param max_steps: 最大循环轮数
    :param thinking: 是否开启并打印模型思考过程（原生模式生效）
    :param tool_manager: 工具管理器，不传则加载 tool/ 目录
    :param history: 完整对话历史（含最新 user 消息），
                    多轮续聊用；None 则单问题模式
    :param on_step: 进度回调 on_step(event, data)，
                    event 为 thinking / action / observation
    :param system_prompt: 覆盖默认系统提示词；None 用 NATIVE_SYSTEM_PROMPT。
                    persona 多层装配、子 agent 档位说明从这进来
    :param on_turn: 每轮 LLM 调用前的消息变换钩子 on_turn(messages) -> messages，
                    可追加/注入消息（子 agent 汇合屏障等）；None 则不干预
    :param inject_queue: 生成中途的用户补充队列（steering），详见 _react_native
    :return: 最终答案
    """
    if use_native_tools:
        manager = tool_manager or ToolManager(_ROOT / "tool")
        return _react_native(question, verbose, max_steps, manager, thinking,
                             history, on_step, system_prompt, on_turn,
                             inject_queue)
    return _react_text(question, verbose)


if __name__ == "__main__":
    # 直跑带参数 = 真模型演示；不带参数 = 打桩自测（不调真模型）
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"问题: {question}\n{'=' * 50}")
        print(f"\n[最终答案] {react_agent(question)}")
        sys.exit()

    # ---- 自测：假的 call_model_messages 验证续写分支 ----
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    from types import SimpleNamespace

    def _resp(stop, blocks):
        return SimpleNamespace(stop_reason=stop, content=blocks,
                               usage=SimpleNamespace(input_tokens=1,
                                                     output_tokens=1))

    def _b(kind, val):
        return SimpleNamespace(type=kind, **{"text": val} if kind == "text"
                                             else {"thinking": val})

    real_call = call_model_messages

    def stub_turns(resps):
        """按序吐 resps；记下每次调用收到的 messages / max_tokens。"""
        seen = []
        seq = list(resps)

        def _call(system, messages, tools=None, thinking=False, **kw):
            seen.append({"messages": [dict(m) for m in messages],
                         "max_tokens": kw.get("max_tokens"),
                         "n_messages": len(messages)})
            return seq.pop(0)
        return _call, seen

    # 1) 正文截断 → 回填续写：最终答案 = 前段 + 续段
    globals()["call_model_messages"] = stub_turns([
        _resp("max_tokens", [_b("thinking", "想了很多"), _b("text", "前半段")]),
        _resp("end_turn", [_b("text", "后半段")]),
    ])[0]
    out = react_agent("写计算器页面", verbose=False,
                      tool_manager=ToolManager(_ROOT / "tool"))
    assert out == "前半段后半段", out
    print("截断续写     OK: 前半段 + 后半段")

    # 2) 续写轮的消息形态：半截 assistant（剥 thinking）+ 续写 user 指令
    stub, seen = stub_turns([
        _resp("max_tokens", [_b("thinking", "草稿"), _b("text", "第一段")]),
        _resp("end_turn", [_b("text", "第二段")]),
    ])
    globals()["call_model_messages"] = stub
    react_agent("x", verbose=False,
                tool_manager=ToolManager(_ROOT / "tool"))
    second = seen[1]["messages"]
    a = second[-2]
    assert a["role"] == "assistant" and a["content"][0].text == "第一段"
    assert len(a["content"]) == 1, "thinking 块应被剥掉"
    u = second[-1]
    assert u["role"] == "user" and "从中断处继续写完" in u["content"]
    print("回填形态     OK: assistant 只含 text 块 + user 续写指令")

    # 3) 连续截断接力：A 截 → B 截 → C 完，拼成 ABC
    globals()["call_model_messages"] = stub_turns([
        _resp("max_tokens", [_b("text", "A")]),
        _resp("max_tokens", [_b("text", "B")]),
        _resp("end_turn", [_b("text", "C")]),
    ])[0]
    out = react_agent("x", verbose=False, max_steps=5,
                      tool_manager=ToolManager(_ROOT / "tool"))
    assert out == "ABC", out
    print("连续截断     OK: ABC 拼接")

    # 4) thinking 截断、正文空（本次事故形态）→ 维持原"未能回答"
    globals()["call_model_messages"] = stub_turns([
        _resp("max_tokens", [_b("thinking", "想" * 20000)]),
    ])[0]
    out = react_agent("帮我给计算器写一个页面", verbose=False,
                      tool_manager=ToolManager(_ROOT / "tool"))
    assert "未能回答" in out, out
    print("空正文截断   OK: 维持未能回答（留给后续方案）")

    # 5) max_tokens 参数真的传了 16384
    stub, seen = stub_turns([_resp("end_turn", [_b("text", "ok")])])
    globals()["call_model_messages"] = stub
    react_agent("x", verbose=False,
                tool_manager=ToolManager(_ROOT / "tool"))
    assert seen[0]["max_tokens"] == MAX_TOKENS, seen[0]["max_tokens"]
    print("输出预算     OK: max_tokens=%s" % MAX_TOKENS)

    # 6) 循环上限进配置：合法覆盖 / 缺段 / 坏类型 / 超范围 / 坏 JSON 全回退
    import tempfile as _tf
    cfg = Path(_tf.mkdtemp(prefix="mychat-steps-")) / "settings.json"
    cfg.write_text(json.dumps({"agent": {"max_steps": 3}}), encoding="utf-8")
    assert _load_max_steps(cfg) == 3, "合法配置应生效"
    cfg.write_text(json.dumps({"log": {"level": "DEBUG"}}), encoding="utf-8")
    assert _load_max_steps(cfg) == DEFAULT_MAX_STEPS, "缺 agent 段回退默认"
    cfg.write_text(json.dumps({"agent": {"max_steps": "十"}}), encoding="utf-8")
    assert _load_max_steps(cfg) == DEFAULT_MAX_STEPS, "坏类型回退默认"
    cfg.write_text(json.dumps({"agent": {"max_steps": 0}}), encoding="utf-8")
    assert _load_max_steps(cfg) == DEFAULT_MAX_STEPS, "超范围回退默认"
    cfg.write_text("不是json", encoding="utf-8")
    assert _load_max_steps(cfg) == DEFAULT_MAX_STEPS, "坏 JSON 回退默认"
    assert MAX_STEPS == _load_max_steps(), "真实仓库配置应已生效"
    print("循环上限配置  OK: 覆盖/各类回退/真实配置生效")

    # 7) steering：生成中途的补充指令，下一轮开头并入末条 user 消息
    stub, seen = stub_turns([
        _resp("max_tokens", [_b("thinking", "草稿"), _b("text", "第一段")]),
        _resp("end_turn", [_b("text", "第二段")]),
    ])
    globals()["call_model_messages"] = stub
    inject = queue.Queue()
    react_agent("x", verbose=False,
                tool_manager=ToolManager(_ROOT / "tool"),
                on_step=lambda ev, data: (
                    inject.put("补充：改成先看 config") if ev == "thinking" else None),
                inject_queue=inject)
    # turn1 的 thinking 回调把补充塞进队列；turn2 开头排空，
    # 并入 turn1 追加的续写指令（末条 user，字符串内容）——两条都在
    second_last = seen[1]["messages"][-1]
    assert second_last["role"] == "user"
    assert "从中断处继续写完" in second_last["content"], second_last
    assert "补充：改成先看 config" in second_last["content"], second_last
    print("steering 注入  OK: 补充并入末条 user 消息，模型下一轮可见")

    # 7b) 合并形态三连：块列表追加 text 块 / 字符串拼接 / 末条非 user 新开
    m1 = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "obs"}]}]
    _inject_user_text(m1, "补充")
    assert m1[-1]["content"][1] == {"type": "text", "text": "补充"}, m1
    m2 = [{"role": "user", "content": "原话"}]
    _inject_user_text(m2, "补充")
    assert m2[-1]["content"] == "原话\n\n补充", m2
    m3 = [{"role": "assistant", "content": "hi"}]
    _inject_user_text(m3, "补充")
    assert m3[-1] == {"role": "user", "content": "补充"}, m3
    print("合并形态     OK: 列表追加 / 字符串拼接 / 新开一条")

    # 7c) 历史隔离：带 history 跑一轮并注入补充，外部列表里的原 dict 不许被动过
    #     （TUI 传的是 self.messages 的浅拷贝，改到共享 dict 会污染持久历史）
    stub, seen2 = stub_turns([
        _resp("end_turn", [_b("text", "done")]),
    ])
    globals()["call_model_messages"] = stub
    ext_history = [{"role": "user", "content": "外部原话"}]
    inj = queue.Queue()
    inj.put("中途补充")
    react_agent("x", history=ext_history, verbose=False,
                tool_manager=ToolManager(_ROOT / "tool"), inject_queue=inj)
    assert ext_history[-1]["content"] == "外部原话", "外部 history 被原地改写了！"
    assert len(ext_history) == 1, "外部 history 被追加了消息！"
    assert "中途补充" in seen2[0]["messages"][0]["content"], "循环副本里应并入补充"
    print("历史隔离     OK: 注入只改循环副本，外部 history 原封不动")

    globals()["call_model_messages"] = real_call
    print("\n全部测试通过")
