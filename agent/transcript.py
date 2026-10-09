# -*- coding: utf-8 -*-
"""transcript：子 agent 对话流 JSONL（一个 agent 一个文件，逐行追加）。

与 SQLite 的分工：agents 表管"低频·强一致"（登记 + 最终报告），
JSONL 管"高频·追加式"（每轮 LLM 响应、每批工具结果）——每 agent
独立文件，多 agent 并发写零竞争，坏了也只丢最后一行。

运行时只写不读：子 agent 的消息流在它线程局部 messages 里，
文件只是同步落盘的影子。读只有两个场景：M4 复活重放、人工审计。

行格式（ts=Unix 秒）：
    {"ts": 1727260004, "type": "thinking",   "content": "…"}
    {"ts": 1727260005, "type": "action",      "content": "grep(...)"}
    {"ts": 1727260010, "type": "observation", "content": "…"}
    {"ts": 1727260012, "type": "user",        "content": "任务书"}
    {"ts": 1727260060, "type": "final",       "content": "最终报告"}

直跑自测：python agent/transcript.py（临时目录，不碰真实 .agents/）
"""

import json
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 转录目录：<项目根>/.agents/transcripts/（建议加进 .gitignore）
TRANSCRIPT_DIR = _ROOT / ".agents" / "transcripts"


def path_for(agent_id: str) -> Path:
    """agent_id 对应的转录文件路径（不主动创建，append 时才建）。"""
    return TRANSCRIPT_DIR / f"{agent_id}.jsonl"


def append(agent_id: str, record: dict) -> None:
    """追加一行（自动补 ts）。任何写失败都吞掉——转录是影子不是命脉，
    不能因为它把 agent 线程砸了。"""
    try:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        line = {"ts": round(time.time(), 3), **record}
        with path_for(agent_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_all(agent_id: str) -> list[dict]:
    """整文件读回（复活重放/审计用）。坏行跳过——只丢那一行，不整体失败。
    文件不存在返回 []（没跑过的 agent 一样能'复活'，只是没有记忆）。"""
    fp = path_for(agent_id)
    if not fp.is_file():
        return []
    out = []
    with fp.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue        # 坏行（如断电写了一半）：跳过
    return out


if __name__ == "__main__":
    # 自测：临时目录全隔离
    import tempfile

    old_dir = TRANSCRIPT_DIR
    TRANSCRIPT_DIR = Path(tempfile.mkdtemp()) / "transcripts"

    # 1) append + read_all 往返
    append("agent-test01", {"type": "user", "content": "调研任务书"})
    append("agent-test01", {"type": "action", "content": "grep('permission')"})
    rows = read_all("agent-test01")
    assert len(rows) == 2
    assert rows[0]["type"] == "user" and rows[1]["content"] == "grep('permission')"
    assert all(isinstance(r["ts"], float) and r["ts"] > 0 for r in rows), \
        "ts 应自动补全"
    print("追加/读回     OK: ts 自动补全")

    # 2) 中文不转义（文件里应是人类可读的中文）
    append("agent-test01", {"type": "final", "content": "报告：审批走三道闸"})
    raw = path_for("agent-test01").read_text(encoding="utf-8")
    assert "报告：审批走三道闸" in raw, "ensure_ascii=False 应保留中文原文"
    print("中文可读     OK: JSONL 里保留原文")

    # 3) 每行独立 JSON（逐行 json.loads 成功）
    for line in raw.strip().splitlines():
        json.loads(line)
    print("行独立       OK: 每行独立 JSON")

    # 4) 不存在的 agent → []（复活路径的前置约定）
    assert read_all("agent-never") == []
    print("无文件       OK: 返回 []")

    # 5) 坏行容错：手写一行残缺 JSON，只丢那一行
    with path_for("agent-test01").open("a", encoding="utf-8") as f:
        f.write('{"ts": 1, "type": "断电写了一半\n')
    rows = read_all("agent-test01")
    assert len(rows) == 3, "坏行应被跳过"
    print("坏行容错     OK: 只丢残缺行")

    # 6) append 的 OSError 吞掉（目录只读等场景不炸线程）
    TRANSCRIPT_DIR = Path("Z:/no/such/drive/mychat")
    append("agent-x", {"type": "user", "content": "写到不存在的盘"})
    print("写失败静默   OK: 异常被吞")

    TRANSCRIPT_DIR = old_dir
    print("\n全部测试通过")
