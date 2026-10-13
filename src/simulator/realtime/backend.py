from __future__ import annotations

import queue

from simulator.datamodel import Persona, Scenario, TurnRecord


class TurnTimeoutError(Exception):
    """等待客户端提交本轮话语超时（§2.6.5 T1 类异常，本模块只报告不重试）。"""


class SessionEndRequested(Exception):
    """respond() 检测到会话被要求结束（优雅结束或异常断连），而非收到真实
    话语。调用方（SessionDriver）捕获后据此收尾，绝不能把这次中断误当成
    一次真实的候选发言——这正是修复"哨兵话语被写成空字符串轮次"缺陷的
    关键：respond() 现在要么返回真实提交，要么抛出本异常，二者不再共用
    同一个"返回值"通道。"""


class ClientDrivenBackend:
    """ModelBackend 的实时实现：respond() 不在本地计算话语，而是阻塞等待
    跨进程客户端通过 submit() 提交的话语（§2.2"角色-模型后端映射由调用方
    指定"的实时落地）。这是 run_episode 轮次循环能在不改动一行代码的情况下
    支持"部分角色由实时客户端驱动"的桥接点——对 run_episode 而言，
    ClientDrivenBackend 只是又一个普通 ModelBackend。"""

    _END = object()  # 唤醒哨兵：与任何真实 (utterance, inner_thought) 提交区分

    def __init__(self, slot_id: str, timeout_s: float) -> None:
        self.slot_id = slot_id
        self._timeout_s = timeout_s
        self._inbox: "queue.Queue[tuple | object]" = queue.Queue()

    def submit(
        self,
        utterance: str,
        inner_thought: str | None,
        *,
        activation_trace: dict | None = None,
    ) -> None:
        """由 gRPC 主循环线程调用：把客户端刚提交的话语放进队列。
        带激活轨迹时排进去的是三元组,respond() 原样返回给 run_episode,
        由它写进该轮的 TurnRecord;不带时仍是二元组,行为与改动前相同。"""
        if activation_trace is None:
            self._inbox.put((utterance, inner_thought))
        else:
            self._inbox.put((utterance, inner_thought, activation_trace))

    def request_end(self) -> None:
        """唤醒可能正阻塞的 respond()，但不作为真实话语处理——respond() 收到
        本哨兵后立即抛 SessionEndRequested，绝不会把它当作候选发言返回。"""
        self._inbox.put(self._END)

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None] | tuple[str, str | None, dict]:
        """由 run_episode 的后台驱动线程调用：阻塞直到 submit()/request_end()
        到来或超时。"""
        try:
            item = self._inbox.get(timeout=self._timeout_s)
        except queue.Empty:
            raise TurnTimeoutError(
                f"no SubmitTurn received for slot {self.slot_id!r} within {self._timeout_s}s"
            )
        if item is self._END:
            raise SessionEndRequested(
                f"session end requested while awaiting slot {self.slot_id!r}"
            )
        return item
