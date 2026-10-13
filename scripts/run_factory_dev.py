#!/usr/bin/env python3
"""开发入口:与 run_factory.py 相同,但 run / reconcile 自动加 --allow-mock-evidence(R17)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factory.cli import dev_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(dev_main())
