from __future__ import annotations

import queue

from simulator.datamodel import Persona, Scenario, TurnRecord


class TurnTimeoutError(Exception):
    """等待客户端提交本轮话语超时（§2.6.5 T1 类异常，本模块只报告不重试）。"""


class ClientDrivenBackend:
    """ModelBackend 的实时实现：respond() 不在本地计算话语，而是阻塞等待
    跨进程客户端通过 submit() 提交的话语（§2.2"角色-模型后端映射由调用方
    指定"的实时落地）。这是 run_episode 轮次循环能在不改动一行代码的情况下
    支持"部分角色由实时客户端驱动"的桥接点——对 run_episode 而言，
    ClientDrivenBackend 只是又一个普通 ModelBackend。"""

    def __init__(self, slot_id: str, timeout_s: float) -> None:
        self.slot_id = slot_id
        self._timeout_s = timeout_s
        self._inbox: "queue.Queue[tuple[str, str | None]]" = queue.Queue()

    def submit(self, utterance: str, inner_thought: str | None) -> None:
        """由 gRPC 主循环线程调用：把客户端刚提交的话语放进队列。"""
        self._inbox.put((utterance, inner_thought))

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        """由 run_episode 的后台驱动线程调用：阻塞直到 submit() 到来或超时。"""
        try:
            return self._inbox.get(timeout=self._timeout_s)
        except queue.Empty:
            raise TurnTimeoutError(
                f"no SubmitTurn received for slot {self.slot_id!r} within {self._timeout_s}s"
            )
