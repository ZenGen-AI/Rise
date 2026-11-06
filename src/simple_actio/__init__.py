"""SimpleActio:带技能库、记忆库与推理经验的智能体实例执行引擎。

只依赖 simulator.datamodel(闭环集成设计 §8.1、§11)。不导入 controlplane、
simulator.backends 或任何传输层;读写技能库与记忆库产物是调用方(MindAgent)的事。
"""
from __future__ import annotations
