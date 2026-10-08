#!/usr/bin/env python3
"""调和循环入口的薄壳,逻辑在 mindtrainer.wire(见那里的说明)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mindtrainer.wire import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
