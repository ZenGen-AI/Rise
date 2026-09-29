from __future__ import annotations

import threading

from simulator.backends import ModelBackend
from simulator.datamodel import Persona, Scenario, TurnRecord


class RoleBackendTimeoutError(Exception):
    """服务端角色后端（对手模型/外部 API）在期限内未返回（§2.6.5 明确列举的
    "角色绑定的外部API模型调用超时"）。这是 T1 类瞬态失败，与后端抛出的
    确定性异常（T2，映射为 role_backend_failure）必须区分开，故单独成类。"""


class TimeoutBackend:
    """给任意 ModelBackend 套一层调用期限：每次 respond() 在一个 daemon 线程里
    执行被包装的后端，超过 timeout_s 未返回即抛 RoleBackendTimeoutError。

    这是 §2.6.5 "角色后端调用超时" 的落地——一个挂死的对手模型不应把整个会话
    拖成永久挂起。

    每次调用起独立的 daemon 线程（而不是 ThreadPoolExecutor）：executor 的
    工作线程不是 daemon，挂死的后端调用会在解释器退出时被 join，从而拖住
    进程退出。超时后底层线程无法被安全强杀，会跑到自然结束、结果被丢弃；
    因为是 daemon，它不会阻止进程退出。一次调用一个线程的开销相对于模型
    推理延迟可以忽略。"""

    def __init__(self, inner: ModelBackend, timeout_s: float) -> None:
        self._inner = inner
        self._timeout_s = timeout_s

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        outcome: dict = {}

        def _call() -> None:
            try:
                outcome["value"] = self._inner.respond(
                    persona, scenario, history, speaker_slot,
                )
            except BaseException as exc:  # 跨线程搬运：原样在调用方线程重抛
                outcome["error"] = exc

        worker = threading.Thread(target=_call, daemon=True, name="timeout-backend-call")
        worker.start()
        worker.join(self._timeout_s)
        if worker.is_alive():
            raise RoleBackendTimeoutError(
                f"role backend for slot {speaker_slot!r} did not respond within {self._timeout_s}s"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]
