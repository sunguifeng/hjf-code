# -*- coding: utf-8 -*-
"""mychat 入口（纯交互式）。

    python -m mychat
"""

import sys
from pathlib import Path
from mychat.tui import ChatApp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    ChatApp().run()


if __name__ == "__main__":
    main()
