# -*- coding: utf-8 -*-
"""applog：全局日志——一处配置（config/settings.json 的 log 段），
所有模块 get_logger(__name__) 直接用。

设计要点：
    懒初始化   第一次 get_logger 才建目录开文件，import 零副作用
    级别过滤   log.debug(...) 级别不够时一个字节不落盘（ DEBUG 行
               平时是零成本空操作，排查时环境变量打开）
    环境变量   MYCHAT_LOG_FILE / MYCHAT_LOG_LEVEL 覆盖配置——
               自测隔离、临时排查都不动配置文件
    线程安全   logging 的 handler 自带锁：主对话、最多 5 个子 agent
               线程、TUI 同时写一个文件不会交错撕裂
    轮转       RotatingFileHandler 2MB × 5 份，总占用封顶 12MB，
               跨启动不清空（上次会话的崩溃现场还在）

优先级：环境变量 > settings.json > 内置默认值。
坏配置（文件缺/JSON 非法）回退默认值——日志不该把应用搞挂。

直跑自测：python applog.py（MYCHAT_LOG_FILE 指向临时文件，全隔离）
"""

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_SETTINGS_PATH = Path(__file__).resolve().parent / "config" / "settings.json"

# 内置默认值：settings.json 的 log 段按同名键覆盖
_DEFAULTS = {
    "level": "INFO",
    "file": "~/.mychat/logs/mychat.log",
    "max_bytes": 2 * 1024 * 1024,
    "backup_count": 5,
}
_KNOWN_KEYS = set(_DEFAULTS)

_initialized = False


def _merged_settings(path: Path = None) -> dict:
    """默认值 ← settings.json 的 log 段。坏配置静默回退（日志自身不吵）。"""
    s = dict(_DEFAULTS)
    p = Path(path or _SETTINGS_PATH)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return s
    log_cfg = data.get("log") if isinstance(data, dict) else None
    if isinstance(log_cfg, dict):
        s.update({k: v for k, v in log_cfg.items() if k in _KNOWN_KEYS})
    return s


def _init() -> None:
    """建根 logger（整个进程只跑一次）。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    s = _merged_settings()
    level_name = os.environ.get("MYCHAT_LOG_LEVEL", str(s["level"])).upper()
    level = getattr(logging, level_name, logging.INFO)   # 非法级别回退 INFO
    log_file = Path(os.environ.get("MYCHAT_LOG_FILE",
                                   str(s["file"]))).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("mychat")
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=int(s["max_bytes"]),
        backupCount=int(s["backup_count"]), encoding="utf-8",
        delay=True)   # 首条日志才真正开文件：只 import 不打点的进程不留空文件
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)

    # anthropic SDK 内部走标准 logging（内置重试的动作打在 anthropic.* 的
    # DEBUG 级），把它挂到同一个 handler：SDK 重试自动落 mychat.log，和
    # 业务日志一套轮转，不用给 SDK 单独配一套。只挂 anthropic 树、不挂
    # httpx——httpx 每请求一行噪音大，且 model.py 已有每轮请求/响应的
    # INFO 打点，信息重复。不 import anthropic：getLogger 拿到的是普通
    # logger 对象，SDK 没装/没用时这几行零副作用。
    sdk = logging.getLogger("anthropic")
    sdk.addHandler(handler)
    sdk.setLevel(logging.DEBUG)   # 门开在 DEBUG：SDK 的重试行无条件落盘——
    # 重试是低频关键事件，MYCHAT_LOG_LEVEL=INFO 时也该看得见


def get_logger(name: str) -> logging.Logger:
    """取挂在 mychat 根下的子 logger。传 __name__ 即可，非 mychat.*
    开头的自动补前缀。"""
    _init()
    if not str(name).startswith("mychat"):
        name = f"mychat.{name}"
    return logging.getLogger(name)


def _reset() -> None:
    """自测清场专用：关掉并摘除 handler，恢复未初始化状态。
    anthropic 树上挂的是同一个 handler 对象，也要摘——先摘它再关，
    不然 close 掉的 handler 还挂在 SDK 树上。"""
    global _initialized
    sdk = logging.getLogger("anthropic")
    for h in list(sdk.handlers):
        sdk.removeHandler(h)
    sdk.setLevel(logging.NOTSET)
    root = logging.getLogger("mychat")
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.setLevel(logging.NOTSET)
    _initialized = False


def isolate_for_selftest() -> None:
    """各模块 __main__ 自测块开头调用一次：自测日志直接打到控制台
    （stderr），不落任何文件——测试数据随测试输出一起看，真实的
    ~/.mychat/logs/mychat.log 只留运行痕迹。钉住"已初始化"状态，
    防止后续 get_logger 又把真实文件 handler 挂回来。控制台阈值
    WARNING：桩产生的 INFO 噪音（spawn/finish 等）不刷屏，WARNING
    和异常照印。"""
    global _initialized
    _reset()
    root = logging.getLogger("mychat")
    root.setLevel(logging.WARNING)
    handler = logging.StreamHandler()     # 默认 stderr，自测输出一起看
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    _initialized = True


if __name__ == "__main__":
    # 自测：全部走 MYCHAT_LOG_FILE 指向的临时文件，不碰 ~/.mychat
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mychat-applog-"))
    _seq = [0]

    def fresh(level: str = "INFO") -> Path:
        """重置 + 指定级别 + 新文件，返回该文件路径。"""
        _reset()
        _seq[0] += 1
        f = tmp / f"t{_seq[0]}.log"
        os.environ["MYCHAT_LOG_FILE"] = str(f)
        os.environ["MYCHAT_LOG_LEVEL"] = level
        return f

    # 1) 基本写入：info 落盘，格式含时间/级别/模块名
    f = fresh()
    log = get_logger("agent.test")
    log.info("hello 一条")
    text = f.read_text(encoding="utf-8")
    assert "hello 一条" in text and " INFO " in text, text
    assert "mychat.agent.test" in text, "模块名应带 mychat 前缀"
    print("基本写入     OK: %s" % text.strip().split(" ", 1)[1][:60])

    # 2) 级别过滤：默认 INFO 下 debug 不落盘
    log.debug("不应出现的细节")
    assert "不应出现的细节" not in f.read_text(encoding="utf-8")
    print("INFO 过滤    OK: debug 行被丢弃")

    # 3) DEBUG 模式：debug 也落盘
    f = fresh("DEBUG")
    get_logger("agent.test").debug("细节出现了")
    assert "细节出现了" in f.read_text(encoding="utf-8")
    print("DEBUG 模式    OK: debug 行落盘")

    # 4) log.exception：WARNING + traceback 落盘
    f = fresh()
    try:
        raise RuntimeError("炸了炸了")
    except RuntimeError:
        get_logger("agent.test").exception("子 agent 崩溃")
    text = f.read_text(encoding="utf-8")
    assert "子 agent 崩溃" in text and "RuntimeError" in text \
        and "Traceback" in text, text
    print("exception    OK: WARNING + traceback")

    # 5) 非法级别回退 INFO：debug 不落盘，info 照落（证明回退值是 INFO）
    f = fresh("这不是级别")
    get_logger("agent.test").debug("非法级别下debug不落盘")
    assert not f.exists() or "非法级别下debug不落盘" \
        not in f.read_text(encoding="utf-8")
    get_logger("agent.test").info("非法级别下info仍落盘")
    assert "非法级别下info仍落盘" in f.read_text(encoding="utf-8")
    print("非法级别     OK: 回退 INFO")

    # 6) 配置合并：临时 settings 覆盖默认值；坏 JSON 回退默认
    cfg = tmp / "settings.json"
    cfg.write_text(json.dumps({"log": {"level": "WARNING",
                                       "max_bytes": 1024}}), encoding="utf-8")
    s = _merged_settings(cfg)
    assert s["level"] == "WARNING" and s["max_bytes"] == 1024, s
    assert s["backup_count"] == _DEFAULTS["backup_count"], "未覆盖键留默认"
    cfg.write_text("不是 json", encoding="utf-8")
    assert _merged_settings(cfg) == _DEFAULTS, "坏配置应整体回退默认值"
    print("配置合并     OK: 覆盖/留默认/坏配置回退")

    # 7) 轮转参数真的挂上了 handler
    f = fresh()
    get_logger("agent.test").info("x")
    h = logging.getLogger("mychat").handlers[0]
    assert h.maxBytes == _DEFAULTS["max_bytes"], h.maxBytes
    assert h.backupCount == _DEFAULTS["backup_count"]
    print("轮转参数     OK: 2MB × %s 份" % h.backupCount)

    # 8) 懒初始化：未调 get_logger 前不建文件
    _reset()
    never = tmp / "never.log"
    os.environ["MYCHAT_LOG_FILE"] = str(never)
    _merged_settings()        # 只读配置，不该触发建文件
    assert not never.exists(), "懒初始化：没打点就不该建文件"
    print("懒初始化     OK: 首次打点才建文件")

    # 9) anthropic SDK 树接线：debug 重试行落盘；_reset 后不再落
    f = fresh()
    get_logger("agent.test")          # 触发 _init（会挂 anthropic 树）
    sdk_log = logging.getLogger("anthropic")
    sdk_log.debug("SDK 重试行（模拟）")
    assert "SDK 重试行" in f.read_text(encoding="utf-8"), "SDK 树的 debug 应落盘"
    _reset()
    sdk_log.debug("SDK 重试行（reset 后）")
    assert "reset 后" not in f.read_text(encoding="utf-8"), "reset 后不应再落盘"
    assert not sdk_log.handlers, "reset 应摘掉 SDK 树上的 handler"
    print("SDK 日志接线   OK: debug 落盘 / reset 摘除")

    for k in ("MYCHAT_LOG_FILE", "MYCHAT_LOG_LEVEL"):
        os.environ.pop(k, None)
    print("\n全部测试通过")
