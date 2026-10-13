"""组合根(闭环集成设计 §10.2):读配置、按开关装配派发器、处理器、评估器与桥接,调用 run_daemon。

依赖方向:factory 可以导入任何包;任何包都不导入 factory。mindtrainer.wire 与
scripts/run_controlplane.py 保持原样,不传 --enable 时本包的 run 与它们逐字节相同。
"""
from __future__ import annotations
