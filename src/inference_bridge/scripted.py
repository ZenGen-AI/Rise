"""测试替身:按顺序吐罐头回答,脚本里放 BridgeError 即在该次调用抛出。"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from controlplane.gitstore import GitStore
from inference_bridge.base import BridgeError, BridgeResult, _check_segment, record_bridge_cost


class ScriptExhaustedError(Exception):
    """脚本用完了还被调用。故意不是 BridgeError:这是测试写错了,不是桥接失败,不该被调用方当 T1/T2 吞掉。"""


class ScriptedBridge:
    def __init__(
        self,
        script: Iterable[str | BridgeResult | BridgeError],
        *,
        model: str = "scripted",
        store: GitStore | None = None,
    ) -> None:
        items = list(script)
        for item in items:
            if not isinstance(item, (str, BridgeResult, BridgeError)):
                raise TypeError(f"script items must be str, BridgeResult or BridgeError, got {item!r}")
        self._script: deque[str | BridgeResult | BridgeError] = deque(items)
        self._model = model
        self._store = store
        self.calls: list[tuple[str, str, str]] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult:
        _check_segment("run_id", run_id)
        _check_segment("task_id", task_id)
        self.calls.append((prompt, run_id, task_id))
        if not self._script:
            raise ScriptExhaustedError(f"ScriptedBridge ran out of answers at call {len(self.calls)}")
        item = self._script.popleft()
        if isinstance(item, BridgeError):
            raise item
        if isinstance(item, str):
            item = BridgeResult(text=item, input_tokens=None, output_tokens=None, model=self._model, log_ref=None)
        record_bridge_cost(self._store, item, run_id=run_id, task_id=task_id, calls=1)
        return item
