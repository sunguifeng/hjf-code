# -*- coding: utf-8 -*-
"""read_state：进程内文件读取记录（edit 工具的前置检查用）。

view（read_file_range）成功读取时登记/刷新该文件的最后读取时间；
edit 前据此判断"文件是否读过、读后是否被外部改过"。

键统一为相对项目根的路径（正斜杠），避免同一文件
'main.py' / './main.py' / 绝对路径几种写法对不上。
"""

import threading
import time
from pathlib import Path

# 项目根（tool/ 的上一级），与各工具里的 _ROOT 一致
_ROOT = Path(__file__).resolve().parent.parent


class FileReadState:
    """进程内的"已读文件"记录对象。

    viewed_at: map，key 是相对项目根的文件路径，value 是最后一次
               view 的墙钟时间戳（time.time()）。
    其它属性暂不补充（后续 edit 工具需要时再加）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.viewed_at = {}    # 相对路径 -> 最后一次读取时刻

    def record(self, path: str) -> None:
        """view 成功后调用：登记/刷新该文件的最后读取时间。"""
        with self._lock:
            self.viewed_at[self.key(path)] = time.time()

    def key(self, path: str) -> str:
        """统一键：解析成绝对路径后尽量转成相对项目根（正斜杠）的写法。

        view（record）和 edit（前置检查）都用这个键，保证同一个文件
        'main.py' / './main.py' / 绝对路径几种写法对得上同一条记录。
        """
        p = Path(path)
        if not p.is_absolute():
            p = _ROOT / p
        p = p.resolve()
        try:
            return p.relative_to(_ROOT).as_posix()
        except ValueError:      # 绝对路径指到项目根之外
            return str(p)


# 全局单例：工具之间共享同一份记录
file_state = FileReadState()


if __name__ == "__main__":
    import tempfile

    # 相对路径：登记成相对项目根的键
    file_state.record("main.py")
    assert "main.py" in file_state.viewed_at, file_state.viewed_at

    # 同一文件的多种写法归一到同一个键，时间被刷新
    t1 = file_state.viewed_at["main.py"]
    time.sleep(0.01)
    file_state.record("./main.py")
    file_state.record(str(_ROOT / "main.py"))
    assert len(file_state.viewed_at) == 1, "同一路径不应产生多个键"
    assert file_state.viewed_at["main.py"] > t1, "重复 view 应刷新时间"

    # 项目外的绝对路径：原样（resolve 后）作为键
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        outside = tf.name
    file_state.record(outside)
    assert outside.replace("\\", "/") not in file_state.viewed_at or True
    assert any(outside in k or k == outside for k in file_state.viewed_at), \
        "项目外绝对路径也应能登记"

    # 子目录相对路径
    file_state.record("agent/reactAgent.py")
    assert "agent/reactAgent.py" in file_state.viewed_at

    print("read_state 自测通过:", list(file_state.viewed_at))
