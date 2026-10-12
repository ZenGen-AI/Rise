"""TrainPlanner:上下文打包、决策后端、输出解析三者分离(设计 §7.1)。

只依赖 controlplane 与 inference_bridge;不读树里的 run、配方或诊断,那是 TrainScheduler 的事。
"""
from __future__ import annotations
