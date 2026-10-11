"""桥接的公共类型:结果、错误、协议,以及每次成功调用后的统一记账。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost

BRIDGE_COST_TYPE = "external_inference"
_CATEGORIES = ("T1", "T2")


def _check_segment(name: str, value: str) -> None:
    """run_id、task_id 会拼进日志路径与成本记录:必须是单个非空路径段。两个桥接都在调用前检查。"""
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or value in (".", ".."):
        raise ValueError(f"{name} must be a non-empty path segment, got {value!r}")


@dataclass(frozen=True)
class BridgeResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str
    log_ref: BigRef | None


class BridgeError(Exception):
    """桥接调用失败。category:"T1" 瞬态(重试已耗尽)| "T2" 确定性(重试无益)。
    升级为 T3 是调用方的事(设计 R18:无论 T1 还是 T2,调用方一律升级为 T3)。
    reason、str(exc) 与 __cause__ / __context__ 上都不挂带凭证的原异常:CliBridge 在 except 块外才抛它。"""

    def __init__(
        self, category: str, reason: str = "", *, attempts: int = 0, log_ref: BigRef | None = None
    ) -> None:
        if category not in _CATEGORIES:
            raise ValueError(f"BridgeError category must be one of {_CATEGORIES}, got {category!r}")
        super().__init__(f"{category}: {reason}")
        self.category = category
        self.reason = reason
        self.attempts = attempts
        self.log_ref = log_ref


@runtime_checkable
class InferenceBridge(Protocol):
    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult: ...


def record_bridge_cost(
    store: GitStore | None, result: BridgeResult, *, run_id: str, task_id: str, calls: int
) -> None:
    """成功调用后记一条 external_inference。token 数只有输入、输出都已知时才记(否则省略键);
    同一 (task_id, external_inference) 只记一次,由 ledger.record_cost 保证。store 为 None 时不记。"""
    if store is None:
        return
    tokens = None
    if result.input_tokens is not None and result.output_tokens is not None:
        tokens = result.input_tokens + result.output_tokens
    record_cost(store, run_id, task_id=task_id, cost_type=BRIDGE_COST_TYPE, tokens=tokens, calls=calls)
