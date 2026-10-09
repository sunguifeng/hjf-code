# -*- coding: utf-8 -*-
"""
豆包 Coding Plan 大模型调用封装（火山方舟 Anthropic 兼容接口）

对外入口方法：call_model(prompt) / stream_model(prompt) —— 传入提示词，返回模型回复
支持 thinking=True 时输出/返回模型的思考过程
"""

import contextvars
import os
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic

from applog import get_logger

# ---- 配置 ----
# 默认地址（Anthropic 兼容协议）。settings.json 的 model.base_url 可覆盖；
# 注意：不要用 https://ark.cn-beijing.volces.com/api/v3，那会按量额外计费
_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
_DEFAULT_MODEL = "glm-5.3"
# 重试/超时缺省：墙钟预算见 get_client 的注释
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 180.0


def _num(section: dict, key: str, lo: float, hi: float, default):
    """读一个数值配置并夹在 [lo, hi] 内：缺失/非数字/越界都走缺省——
    重试参数配错不该把应用搞挂（和 applog 同一哲学）。"""
    raw = section.get(key, default)
    try:
        v = type(default)(raw)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def _load_model_config(path: Path | None = None):
    """读模型五要素（key / 地址 / 模型名 / 重试 / 超时）：优先级环境变量 >
    config/settings.json 的 model 段 > 各自缺省。key 拿不到（缺失 / 还是
    占位符）直接报错退出——源码里没有兜底 key，发布不泄密。
    返回 (api_key, base_url, model_name, max_retries, timeout)。"""
    cfg = path or Path(__file__).resolve().parent.parent / "config" / "settings.json"
    section: dict = {}
    try:
        import json
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(data.get("model"), dict):
            section = data["model"]
    except (OSError, ValueError):
        pass                     # 坏配置：url/模型名走缺省，key 那关自会拦

    key = os.environ.get("ARK_API_KEY") or str(section.get("api_key", "")).strip()
    if not key or "填入" in key:   # 占位符文本带"填入"二字，没换过视为未配置
        raise SystemExit(
            f"未配置 API key：请在 {cfg} 的 model.api_key 填入真实 key，"
            f"或设置环境变量 ARK_API_KEY。")
    url = os.environ.get("ARK_BASE_URL") or str(section.get("base_url", "")).strip() \
        or _DEFAULT_BASE_URL
    model_name = os.environ.get("ARK_MODEL") or str(section.get("name", "")).strip() \
        or _DEFAULT_MODEL
    retries = _num(section, "max_retries", 0, 10, _DEFAULT_MAX_RETRIES)
    timeout = _num(section, "timeout", 10, 600, _DEFAULT_TIMEOUT)
    return key, url, model_name, retries, timeout


API_KEY, BASE_URL, MODEL, MAX_RETRIES, TIMEOUT = _load_model_config()

# 思考过程占用的 token 预算（API 要求 >= 1024）
THINKING_BUDGET = 2048

_client: Anthropic | None = None

log = get_logger(__name__)

# ---- token 用量归属上下文 ----
# 三元组 (session_id, dialog_id, agent_id)：此后所有模型调用的 usage 都
# 记到这个归属下。ContextVar 随任务/线程拷贝：TUI 发消息时 set，
# asyncio.to_thread 自动带进 agent 工作线程；子 agent 是裸线程（新线程
# 拿到的是空上下文），由 runtime.run_agent 在线程入口显式 set。
# 无上下文时（CLI 直跑、persona 启动摘要）记 'adhoc'，数据不丢。
_usage_ctx = contextvars.ContextVar("usage_ctx", default=None)


def set_usage_context(session_id: str, dialog_id: int | None = None,
                      agent_id: str | None = None):
    """设置此后模型调用的 token 归属（会话 / 对话 / 子 agent）。"""
    return _usage_ctx.set((session_id, dialog_id, agent_id))


def get_usage_context() -> tuple | None:
    """读当前归属。supervisor.spawn 在主 agent 线程里调它，捕获 dialog_id
    存到 handle 上（ContextVar 过不了线程，靠 handle 捎给子 agent）。"""
    return _usage_ctx.get()


def _record_usage(model_name: str, usage) -> None:
    """一次 API 调用一行，落 token_usage 表。usage 是响应里的 usage 对象，
    四类 token 缺哪项记 0。落库失败只告警——统计绝不能影响对话。"""
    session_id, dialog_id, agent_id = _usage_ctx.get() or ("adhoc", None, None)

    def g(key: str) -> int:
        return int(getattr(usage, key, 0) or 0)

    try:
        from db.db import add_token_usage   # 懒加载：model 包不硬依赖 db
        add_token_usage(
            session_id, model_name,
            g("input_tokens"), g("output_tokens"),
            g("cache_read_input_tokens"), g("cache_creation_input_tokens"),
            dialog_id=dialog_id, agent_id=agent_id)
    except Exception:
        log.warning("token 用量落库失败 session=%s dialog=%s model=%s",
                    session_id, dialog_id, model_name, exc_info=True)


def get_client() -> Anthropic:
    """懒加载并复用客户端连接。

    重试/超时交给 SDK 内置机制（覆盖连接错误/超时/408/409/429/5xx，
    429 时优先遵循服务端 Retry-After，指数退避+抖动）。
    墙钟预算：超时也会被重试，最坏 timeout × (max_retries + 1)，
    3 × 180s = 12 分钟，压着 TUI 汇合屏障 AGENT_WAIT_TIMEOUT=600s 的
    量级——调这两个值先算这笔账，别手滑配成 10 × 600。"""
    global _client
    if _client is None:
        _client = Anthropic(api_key=API_KEY, base_url=BASE_URL,
                            max_retries=MAX_RETRIES, timeout=TIMEOUT)
    return _client


def _build_kwargs(
    prompt: str, model: str | None, max_tokens: int, thinking: bool
) -> dict:
    """组装请求参数"""
    kwargs: dict = {
        "model": model or MODEL,
        "max_tokens": max(max_tokens, THINKING_BUDGET + 1024) if thinking else max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # 强制思考过程和回复都使用中文
        "system": "无论用户使用什么语言提问，你的思考过程（thinking）和最终回复都必须使用简体中文。",
    }
    if thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    return kwargs


def call_model(
    prompt: str, model: str | None = None, max_tokens: int = 8192, thinking: bool = False
) -> str | tuple[str, str]:
    """
    入口方法：调用大模型。

    :param prompt: 提示词（用户输入）
    :param model: 模型名，不传则使用默认 MODEL
    :param max_tokens: 回复最大 token 数（thinking=True 时会保证大于思考预算）
    :param thinking: 是否开启思考过程
    :return: thinking=False 时返回回复文本；
             thinking=True 时返回 (思考过程, 回复文本)
    """
    message = get_client().messages.create(
        **_build_kwargs(prompt, model, max_tokens, thinking)
    )
    _record_usage(model or MODEL, getattr(message, "usage", None))
    think = "".join(b.thinking for b in message.content if b.type == "thinking")
    text = "".join(b.text for b in message.content if b.type == "text")
    return (think, text) if thinking else text


def call_model_messages(
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str | None = None,
    max_tokens: int = 8192,
    thinking: bool = False,
):
    """
    入口方法：messages 级调用，支持 function calling（Anthropic tools 协议）。

    :param system:   系统提示词
    :param messages: 完整对话消息（含 tool_use / tool_result 内容块），
                     可直接使用 messages.create 的返回内容回填下一轮
    :param tools:    Anthropic tools 格式
                     [{"name": ..., "description": ..., "input_schema": {...}}]
    :param model:    模型名，不传则使用默认 MODEL
    :param max_tokens: 回复最大 token 数（thinking=True 时自动保证大于思考预算）
    :param thinking: 是否开启思考过程（resp.content 里会多出 thinking 内容块）
    :return: 完整 message 对象（resp.content 里含 thinking / text / tool_use 内容块）
    """
    kwargs = {
        "model": model or MODEL,
        "max_tokens": max(max_tokens, THINKING_BUDGET + 1024) if thinking else max_tokens,
        "system": system,
        "messages": messages,
    }
    if thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    if tools:
        kwargs["tools"] = tools
    log.debug("模型请求: messages=%s tools=%s model=%s thinking=%s",
              len(messages), len(tools or []), kwargs["model"], thinking)
    resp = get_client().messages.create(**kwargs)
    _record_usage(kwargs["model"], getattr(resp, "usage", None))
    log.info("模型响应: stop=%s usage=%s model=%s ctx=%s", resp.stop_reason,
             getattr(resp, "usage", None), kwargs["model"], _usage_ctx.get())
    return resp


def stream_model(
    prompt: str, model: str | None = None, max_tokens: int = 8192, thinking: bool = True
) -> Iterator[tuple[str, str]]:
    """
    流式版本：返回生成器，逐段产出 (片段类型, 内容)。

    片段类型为 "thinking"（思考过程）或 "text"（正文回复）。

    用法：
        for kind, chunk in stream_model("你好"):
            print(chunk, end="", flush=True)
    """
    with get_client().messages.stream(
        **_build_kwargs(prompt, model, max_tokens, thinking)
    ) as stream:
        for event in stream:
            if event.type != "content_block_delta":
                continue
            delta = event.delta
            if delta.type == "thinking_delta":
                yield "thinking", delta.thinking
            elif delta.type == "text_delta":
                yield "text", delta.text
        # 流式收尾后 usage 在 final message 上（消费端提前 break 时
        # 取不到，这行不执行——漏记好过报错）
        try:
            _record_usage(model or MODEL,
                          stream.get_final_message().usage)
        except Exception:
            log.debug("流式 usage 采集失败", exc_info=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:      # 带参数：真调一次模型（流式演示）
        question = " ".join(sys.argv[1:])
        print(f"模型: {MODEL}\n{'-' * 50}")
        current = None
        for kind, chunk in stream_model(question):
            if kind != current:  # 思考/正文切换时打印分隔标题
                current = kind
                print("\n\n[思考过程]" if kind == "thinking" else "\n\n[回答]",
                      flush=True)
            print(chunk, end="", flush=True)
        print()
        sys.exit(0)

    # ---- 自测：临时 settings 文件覆盖 _load_model_config 各分支，不调真模型 ----
    from applog import isolate_for_selftest
    isolate_for_selftest()      # 自测日志直接打控制台，不落任何文件

    import json
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mychat-model-cfg-"))
    settings = tmp / "settings.json"

    # 清掉干扰的环境变量（测试完还原）
    _env_keys = ("ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL")
    _saved_env = {k: os.environ.pop(k, None) for k in _env_keys}

    def write_cfg(section: dict | None) -> None:
        if section is None:                       # 坏 JSON
            settings.write_text("{ 坏掉的", encoding="utf-8")
        else:
            settings.write_text(json.dumps({"model": section}),
                                encoding="utf-8")

    # 1) 正常读取：五要素全部来自配置文件（重试/超时也在）
    write_cfg({"api_key": "ark-test-key", "base_url": "https://x/api",
               "name": "test-model", "max_retries": 5, "timeout": 60})
    key, url, name, retries, timeout = _load_model_config(settings)
    assert (key, url, name) == ("ark-test-key", "https://x/api", "test-model")
    assert (retries, timeout) == (5, 60.0)
    print("正常读取       OK:", name, url, f"retries={retries} timeout={timeout}")

    # 2) 环境变量优先于配置文件（重试/超时无环境变量开关，只认配置）
    os.environ["ARK_API_KEY"] = "ark-env-key"
    os.environ["ARK_BASE_URL"] = "https://y/api"
    os.environ["ARK_MODEL"] = "env-model"
    key, url, name, retries, timeout = _load_model_config(settings)
    assert (key, url, name) == ("ark-env-key", "https://y/api", "env-model")
    assert (retries, timeout) == (5, 60.0), "环境变量不该动重试参数"
    print("环境变量覆盖   OK:", name)
    del os.environ["ARK_API_KEY"], os.environ["ARK_BASE_URL"], os.environ["ARK_MODEL"]

    # 3) url / 模型名 / 重试 / 超时缺失都走缺省，key 在就行
    write_cfg({"api_key": "ark-test-key"})
    key, url, name, retries, timeout = _load_model_config(settings)
    assert key == "ark-test-key"
    assert url == _DEFAULT_BASE_URL and name == _DEFAULT_MODEL
    assert (retries, timeout) == (_DEFAULT_MAX_RETRIES, _DEFAULT_TIMEOUT)
    print("缺省兜底       OK:", name, url, f"retries={retries} timeout={timeout}")

    # 4) 重试参数坏值（非数字/越界/负数）走缺省，不炸
    for case, section in (
            ("非数字", {"api_key": "k", "max_retries": "很多", "timeout": "很久"}),
            ("越界", {"api_key": "k", "max_retries": 99, "timeout": 9999}),
            ("负数", {"api_key": "k", "max_retries": -1, "timeout": -5})):
        write_cfg(section)
        _, _, _, retries, timeout = _load_model_config(settings)
        assert (retries, timeout) == (_DEFAULT_MAX_RETRIES, _DEFAULT_TIMEOUT), case
    print("坏值兜底       OK: 非数字/越界/负数全走缺省")

    # 5) key 是占位符 / 缺失 / 配置文件坏 -> SystemExit
    for case, section in (("占位符", {"api_key": "在这里填入你的key"}),
                         ("缺key", {"base_url": "https://x"}),
                         ("坏JSON", None)):
        write_cfg(section)
        try:
            _load_model_config(settings)
        except SystemExit as e:
            assert "未配置 API key" in str(e), (case, e)
            print(f"{case}拦截       OK")
        else:
            raise AssertionError(f"{case}应报错退出")

    # 6) 空 JSON / 没有 model 段 -> 同样拦下
    settings.write_text("{}", encoding="utf-8")
    try:
        _load_model_config(settings)
    except SystemExit:
        print("空配置拦截     OK")
    else:
        raise AssertionError("空配置应报错退出")

    # 还原环境变量，清临时目录
    for k, v in _saved_env.items():
        if v is not None:
            os.environ[k] = v
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    # 真实配置冒烟：本项目的 settings.json 能被加载出五要素
    key, url, name, retries, timeout = _load_model_config()
    assert key.startswith("ark-") and url and name
    assert 0 <= retries <= 10 and 10 <= timeout <= 600
    print(f"真实配置冒烟   OK: {name} @ {url} retries={retries} timeout={timeout}")
    print("\n全部测试通过")
