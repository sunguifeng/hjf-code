# ❯ mychat

> **⚠️ 早期开发中：** 个人学习项目，功能可能变化、缺失或不完整，尚未面向生产使用。

一个用 Python 从零实现的终端 AI 编程助手——在终端里对话，AI 可以自主读文件、改代码、跑命令、派出子 agent 干活。后端接豆包 Coding Plan（火山方舟 Anthropic 兼容接口）。

## Overview

mychat 是一个 Textual TUI 应用 + 同步 ReAct agent 循环：模型思考 → 调用工具 → 观察结果 → 继续，直到给出回答。写盘和执行命令前有权限审批，多 agent 并发有汇合屏障，全程日志落盘。

```
python -m mychat
```

## Features

- **交互式 TUI**：基于 [Textual](https://textual.textualize.io/)，`❯` 用户消息、思考过程折叠块（`ctrl+o` 展开）、`esc` 中断生成、diff 彩色渲染
- **ReAct 双模式**：原生 Anthropic function calling 协议（默认）+ 经典 Action/Observation 文本协议（兼容不支持工具调用的模型）
- **工具集**：读写文件、grep、ls、持久 cmd 会话、派生子 agent 等（见下方工具表）
- **多 agent 编排**：explore / verify 两档子 agent 已开放，任务书自包含、报告自动注入主对话
- **权限系统**：写盘前弹审批（允许本次 / 本会话该文件 / 本会话全部 / 拒绝），拒绝时磁盘零改动
- **会话持久化**：SQLite 存对话与会话列表，`/resume` 恢复
- **运行日志**：RotatingFileHandler 2MB × 5 份，跨启动累积，SDK 重试自动落盘
- **编辑后诊断**：Python 文件改完自动跑三层 lint（语法 / 语义 / 格式）

## Installation

要求：Python ≥ 3.14，Windows（子 agent 的持久 shell 会话基于 cmd）。

```bash
git clone <仓库地址>
cd mychat
uv sync
uv run python -m mychat
```

## Configuration

所有配置集中在 `config/settings.json`，分三段：

```json
{
  "log": {
    "level": "INFO",
    "file": "~/.mychat/logs/mychat.log",
    "max_bytes": 2097152,
    "backup_count": 5
  },
  "agent": {
    "max_steps": 20
  },
  "model": {
    "api_key": "在这里填入你的豆包 Coding Plan 专属 Key",
    "base_url": "https://ark.cn-beijing.volces.com/api/coding",
    "name": "glm-5.3",
    "protocol": "anthropic",
    "max_retries": 3,
    "timeout": 180
  }
}
```

注意：不要用 `https://ark.cn-beijing.volces.com/api/v3`，那会按量额外计费。

### 环境变量

环境变量优先于配置文件，临时排查不用改文件：

| 环境变量 | 用途 |
| --- | --- |
| `ARK_API_KEY` | 覆盖 model.api_key |
| `ARK_BASE_URL` | 覆盖 model.base_url |
| `ARK_MODEL` | 覆盖 model.name（默认模型） |
| `MYCHAT_LOG_LEVEL` | 覆盖日志级别（排查时设 `DEBUG`） |
| `MYCHAT_LOG_FILE` | 覆盖日志文件路径 |
| `MYCHAT_AGENTS_DIR` | 覆盖子 agent 配置目录 |

### 重试与超时

超时和重试交给 Anthropic SDK 内置机制（覆盖连接错误 / 超时 / 408 / 409 / 429 / 5xx，429 优先遵循服务端 `Retry-After`，指数退避 + 抖动）。**超时也会被重试**，最坏墙钟 = `timeout × (max_retries + 1)`；默认 180s × 4 ≈ 12 分钟，压着 TUI 汇合屏障（600s）的量级，调这两个值先算这笔账。

## Usage

```bash
uv run python -m mychat
```

### 斜杠命令

| 命令 | 作用 |
| --- | --- |
| `/new` | 新建会话 |
| `/sessions` | 列出历史会话 |
| `/resume` | 恢复某个会话 |
| `/use` | 切换模型 |

### 键盘快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Enter` | 发送消息 |
| `Esc` | 中断当前回复 |
| `Ctrl+O` | 展开 / 收起思考过程 |
| `Ctrl+Q` | 退出 |

### 权限审批

写盘或执行命令时弹出两行式审批块，`v` 可展开看 diff：

| 按键 | 作用 |
| --- | --- |
| `a` | 允许本次 |
| `s` | 本会话此文件（命令为相同命令） |
| `S` | 本会话全部 |
| `v` | 展开 / 收起改动 |
| `d` / `Esc` | 拒绝 |

## AI 工具

| 工具 | 说明 |
| --- | --- |
| `run_command` | 持久 cmd 会话执行命令，输出截断保护 |
| `edit_file` | 先读后改的字符串替换编辑，写盘前过审批，Python 文件附编辑后诊断 |
| `read_file_range` | 按行区间读文件（编辑前必读） |
| `grep` | 内容正则搜索，返回 `文件:行号:行内容` |
| `ls` | 列目录 |
| `spawn_agent` | 派后台子 agent（任务书自包含） |
| `get_time` / `string_length` | 辅助小工具 |
| `read_state` | 读内部状态 |

## 子 agent 档位

档位定义在 `config/agents.json`，`route` 字段是路由判据，主模型据此决定派谁：

| 档位 | 定位 | 状态 |
| --- | --- | --- |
| `explore` | 只读调研 | ✅ 开放 |
| `verify` | 只读验证（可跑白名单命令） | ✅ 开放 |
| `general` | 改写闭环（隔离 git worktree） | 🚧 未开放 |

派出的子 agent 完成后，报告经汇合屏障自动注入主对话，模型不用轮询。

## Architecture

```
mychat/      Textual TUI（消息流 / 审批块 / 会话选择器）
agent/       ReAct 循环、子 agent 注册与调度、运行时、转录
tool/        工具集（base_tool.ToolManager 递归扫描自动注册）
model/       豆包 API 封装（Anthropic 兼容协议、重试、token 归属）
persona/     分层系统提示词装配（USER.md / MEMORY.md / AGENTS.md）
permission/   写盘 / 执行审批
db/          SQLite 会话与用量持久化
config/      settings.json（运行配置）+ agents.json（子 agent 档位）
applog.py    全局日志（轮转、级别过滤、SDK 重试接线）
```

## Development

每个模块自带自测，直跑即验，不调真模型、不碰真实数据文件：

```bash
python applog.py
python model/model.py
python agent/reactAgent.py
python tool/tool_spawn.py
# ...其余各模块同理
```

带参数运行 `model/model.py` 可做一次真调用冒烟：

```bash
python model/model.py 你好
```

## License

本项目**暂未开源**（未附加开源许可证）。

- ✅ 欢迎阅读、下载，供**学习**和**私人使用**
- ✅ 参考代码思路做自己项目的，随意
- ⚠️ **商用前请先联系作者获得许可**（其他闭源分发场景同理）

后续视情况补充正式许可证。
