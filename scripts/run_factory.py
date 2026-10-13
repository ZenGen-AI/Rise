#!/usr/bin/env python3
"""组合根入口的薄壳,逻辑在 factory.cli(见那里的说明)。默认不接受 mock 证据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factory.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
