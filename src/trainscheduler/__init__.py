"""TrainScheduler:装配规划上下文、领域校验、动作解析、构造指令、配方提名(闭环集成设计 §7.2、§6.3)。

依赖方向:controlplane、recipestore、trainplanner、mindtrainer.training_config、mindtrainer.data_spec。
controlplane 不导入本包;本包不导入 diagnostic、simulator。
"""
from __future__ import annotations
