"""MindAgent:部署驱动、经验分析、技能提案、提案验证、打包,以及轮次与收录两个处理器
(闭环集成设计 §8.3)。依赖 controlplane、actio、inference_bridge 与 simulator 的实时客户端;
controlplane 不导入本包。"""
from __future__ import annotations
