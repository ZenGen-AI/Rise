"""simple_actio 的唯一错误基类。"""
from __future__ import annotations


class SimpleActioDataError(ValueError):
    """技能、记忆、推理经验、触发条件或激活轨迹的结构不合法。"""
