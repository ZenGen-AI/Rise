"""RecipeStore:配方的可复现性校验、检索、战绩、生命周期与收录(闭环集成设计 §6)。

依赖方向:本包只导入 controlplane,不导入 diagnostic、mindtrainer、trainscheduler。
"""
from __future__ import annotations
