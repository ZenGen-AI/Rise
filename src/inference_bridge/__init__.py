"""外部推理桥接(闭环集成设计 §5.1,R15)。依赖 controlplane;controlplane 不导入本包。"""
from __future__ import annotations

from inference_bridge.base import (
    BRIDGE_COST_TYPE,
    BridgeError,
    BridgeResult,
    InferenceBridge,
    record_bridge_cost,
)
from inference_bridge.cli import DEFAULT_COMMANDS, CliBridge, RunOutcome, Runner, subprocess_runner
from inference_bridge.scripted import ScriptedBridge, ScriptExhaustedError

__all__ = [
    "BRIDGE_COST_TYPE",
    "DEFAULT_COMMANDS",
    "BridgeError",
    "BridgeResult",
    "CliBridge",
    "InferenceBridge",
    "RunOutcome",
    "Runner",
    "ScriptExhaustedError",
    "ScriptedBridge",
    "record_bridge_cost",
    "subprocess_runner",
]
