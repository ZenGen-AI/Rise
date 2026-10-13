"""六个处理器键的真实构造器表:{处理器键: (BuildContext) -> Handler}。

任务 104 先放一张空表,闭环开关在没有显式 builders 时一律报
"no handler builder for [...]" 并以退出码 2 退出;任务 107 按计划 3、5、7 的处理器填满它。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

DEFAULT_BUILDERS: Mapping[str, Callable] = {}
