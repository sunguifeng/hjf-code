"""read_file_range 工具：按行区间只读文件内容（自包含，实现逻辑在本文件）。"""

import os
from pathlib import Path

from base_tool import Tool, ToolError
from read_state import file_state   # view 成功后登记读取时间（edit 前置检查用）

# 相对路径的起点：项目根目录（tool/ 的上一级）
_ROOT = Path(__file__).resolve().parent.parent

# 单次读取的行数上限（无字节限制），超过则截断并抛 TooManyLinesError
MAX_LINES = 2000


def _resolve(path: str) -> str:
    """相对路径以项目根目录为起点解析；绝对路径原样使用。"""
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    return str(p)


class TooManyLinesError(Exception):
    """
    要读的行数超过 MAX_LINES，已按上限截断。

    属性:
        next_start: 被截断部分的开始行号（从 0 计），翻页时作为新的 start 传入
        content:    截断后保留的内容（整行，格式原样）
    """

    def __init__(self, next_start: int, content: str):
        self.next_start = next_start
        self.content = content
        super().__init__(
            f"单次最多读取 {MAX_LINES} 行，已按上限截断，"
            f"后续内容从 start={next_start} 开始"
        )


def read_file_range(path: str, start: int, offset: int) -> str:
    """
    只读工具：按行区间读取文件内容，格式原样保留。

    参数:
        path:   文件路径，相对路径以项目根目录为起点（如 'main.py'、
                'agent/reactAgent.py'），也接受绝对路径
        start:  开始行号（从 0 计，0 表示第一行）
        offset: 要读取的行数

    返回:
        str: 对应行区间的原始内容（含原有换行符，\r\n 或 \n 均原样保留）。
             - start 超出文件总行数时返回空字符串 ""
             - offset 超出剩余行数时读到文件末尾为止
             - 行尾无换行符的最后一行也原样返回

    截断:
        无字节限制；offset 超过 MAX_LINES(2000) 时按上限截断，抛出
        TooManyLinesError（异常消息含下一页的开始位置 next_start），
        已读内容放在异常的 content 属性里。
    """
    path = _resolve(path)
    _check_args(path, start, offset)

    # newline="": 关闭换行符统一转换，\r\n / \n / \r 原样读出，保证格式不变
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        file_state.record(path)    # view 成功（能打开读到），刷新读取时间
        # 跳过 start 之前的行（流式，大文件不会撑爆内存）
        for _ in range(start):
            if not f.readline():
                return ""          # start 超出总行数

        lines = []
        for _ in range(min(offset, MAX_LINES)):
            line = f.readline()
            if not line:
                break             # 到文件末尾，正常结束
            lines.append(line)

        result = "".join(lines)
        # 要读的比上限多、且确实读满了上限：说明后面还有内容，提示翻页
        if offset > MAX_LINES and len(lines) == MAX_LINES:
            raise TooManyLinesError(start + len(lines), result)
        return result


def line_count(path: str) -> int:
    """返回文件总行数（只读）。路径解析规则同 read_file_range。"""
    path = _resolve(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {path}")
    if os.path.isdir(path):
        raise ValueError(f"路径是目录不是文件: {path}")

    with open(path, "rb") as f:
        return sum(1 for _ in f)


def _check_args(path: str, start: int, offset: int) -> None:
    """校验参数，给 agent 返回明确的错误信息。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {path}")
    if os.path.isdir(path):
        raise ValueError(f"路径是目录不是文件: {path}")
    if not isinstance(start, int) or isinstance(start, bool):
        raise ValueError(f"start 必须是整数: {start!r}")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValueError(f"offset 必须是整数: {offset!r}")
    if start < 0:
        raise ValueError(f"start 必须从 0 开始（0 表示第一行）: {start}")
    if offset < 0:
        raise ValueError(f"offset 必须是非负整数: {offset}")


class ReadFileRangeTool(Tool):
    name = "read_file_range"
    description = (
        "只读工具：按行区间读取文件内容，格式原样保留。"
        "path 为相对路径时以项目根目录为起点（如 'main.py'、'agent/reactAgent.py'），"
        "也可传绝对路径；"
        "start 为开始行号（从 0 计，0 表示第一行），offset 为要读取的行数；"
        "start 越界返回空字符串，offset 超出剩余行数读到文件末尾为止，"
        "无字节限制但单次最多 2000 行。"
        "offset 超过 2000 时返回错误文本（含下一页的开始位置 start=N），"
        "后续如何访问由模型自行决定。"
        "要编辑文件必须先读本工具读过它（edit_file 的前置检查）。"
        "示例：读 main.py 的前 10 行 -> path='main.py', start=0, offset=10；"
        "读 agent/reactAgent.py 的第 20~39 行 -> path='agent/reactAgent.py', "
        "start=19, offset=20。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "文件路径：相对路径以项目根目录为起点，"
                                    "如 'main.py'，也接受绝对路径"},
            "start": {"type": "integer", "description": "开始行号，从 0 计，0 表示第一行"},
            "offset": {"type": "integer", "description": "要读取的行数"},
        },
        "required": ["path", "start", "offset"],
    }

    def execute(self, path: str, start: int, offset: int) -> str:
        try:
            return read_file_range(path, start, offset)
        except TooManyLinesError as e:
            # 工具特定的业务异常统一转成 ToolError，由 manager 统一处理
            raise ToolError(str(e)) from e
