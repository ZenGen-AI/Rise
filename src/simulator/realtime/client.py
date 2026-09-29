from __future__ import annotations

import queue

from simulator.datamodel import Episode, Persona, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, role_assignment_to_proto, turn_record_from_proto,
)
from simulator.realtime.proto import realtime_env_pb2 as pb2


class SessionRejectedError(Exception):
    """服务端在 create_session 阶段就以 EndSession 拒绝了会话（如
    invalid_session）。reason/detail 原样来自 EndSession.reason/failure_detail。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class EnvironmentServiceClient:
    """EnvironmentService 的同步客户端包装器（未来 MindTrainer InteractTrain /
    MindAgent 经验采集直接复用的接口）。用直接 next() 拉取响应，不起消费者
    线程。逐轮累积 TurnRecord，在 EndSession 到达时于本地重建完整 Episode
    （§2.6.2："最终Episode摘要引用"，协议不重复内联整个 Episode）。

    轮次感知：服务端按 bindings[turn_index % n] 轮转发言，所以客户端知道
    "下一轮轮到谁"。核心原语 _await_client_turn 持续读取，直到下一轮属于
    客户端角色位（或会话结束）。因此支持任意数量的角色位、客户端处于任意
    位置——包括"客户端后说"（MindAgent 场景）：create_session 返回时，
    客户端之前的对方开场轮已收进 opening_turns。

    典型用法::

        client.create_session(...)
        for t in client.opening_turns: ...      # 客户端后说时非空
        while not client.closed:
            opponent_turns = client.submit_turn(my_utterance)
        episode = client.drain_until_end()
    """

    def __init__(self, stub) -> None:
        self._stub = stub
        self._send_q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()
        self._resp_iter = stub.Interact(self._request_gen())
        self._session_id = ""
        self._scenario_id = ""
        self._role_assignment: RoleAssignment | None = None
        self._client_slot = ""
        self._episode_id = ""
        self._turns: list[TurnRecord] = []
        self._opening_turns: list[TurnRecord] = []
        self._closed = False
        self._last_reason = ""

    @property
    def closed(self) -> bool:
        """会话是否已收到服务端的 EndSession。"""
        return self._closed

    @property
    def opening_turns(self) -> list[TurnRecord]:
        """create_session 返回前、客户端首次发言之前收到的对方轮次。"""
        return list(self._opening_turns)

    def _request_gen(self):
        while True:
            item = self._send_q.get()
            if item is None:
                return
            yield item

    def create_session(
        self,
        *,
        scenario_id: str,
        role_assignment: RoleAssignment,
        personas: dict[str, Persona],
        purpose: str,
        client_driven_slot: str,
    ) -> str:
        cs = pb2.CreateSession(
            scenario_id=scenario_id,
            role_assignment=role_assignment_to_proto(role_assignment),
            personas={pid: persona_to_proto(p) for pid, p in personas.items()},
            purpose=purpose,
            client_driven_slot=client_driven_slot,
        )
        self._send_q.put(pb2.ClientMessage(create_session=cs))
        first = next(self._resp_iter)
        if first.WhichOneof("payload") == "end_session":
            end = first.end_session
            self._finish(end)
            raise SessionRejectedError(
                end.reason, end.failure_detail if end.HasField("failure_detail") else "",
            )
        self._session_id = first.session_ready.session_id
        self._scenario_id = scenario_id
        self._role_assignment = role_assignment
        self._client_slot = client_driven_slot
        self._episode_id = f"ep-{self._session_id}"
        self._opening_turns = self._await_client_turn()
        return self._session_id

    def submit_turn(self, utterance: str, inner_thought: str | None = None) -> list[TurnRecord]:
        """提交客户端这一轮，返回随后直到"再次轮到客户端"之前收到的全部对方
        轮次（可能为空；会话在此期间结束则包含结束前收到的轮次，此后
        closed 为 True）。不再假定角色数量或客户端位置。"""
        if self._closed:
            raise RuntimeError("session already ended; check client.closed before submitting")
        submit = pb2.SubmitTurn(utterance=utterance)
        if inner_thought is not None:
            submit.inner_thought = inner_thought
        self._send_q.put(pb2.ClientMessage(submit_turn=submit))
        # 服务端会把客户端自己的这一轮也作为 TurnUpdate 回显——先吞掉它，
        # 否则轮次计数会错位，后面的"轮到谁"判断就不成立。
        own_index = len(self._turns)
        while not self._closed and len(self._turns) <= own_index:
            self._pump_one()
        return self._await_client_turn()

    def drain_until_end(self) -> Episode:
        """不主动提交，持续拉取直到会话自然终止（超时/后端失败/max_turns）。"""
        while not self._closed:
            self._pump_one()
        return self._build_episode(self._last_reason)

    def end_session(self, reason: str = "client_terminated") -> Episode:
        self._send_q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason=reason)))
        while not self._closed:
            self._pump_one()
        return self._build_episode(self._last_reason)

    def _pump_one(self) -> TurnRecord | None:
        """读一条服务端消息：turn_update → 累积并返回该轮；end_session →
        收尾并返回 None。"""
        msg = next(self._resp_iter)
        kind = msg.WhichOneof("payload")
        if kind == "turn_update":
            turn = turn_record_from_proto(msg.turn_update.turn)
            self._turns.append(turn)
            return turn
        if kind == "end_session":
            self._finish(msg.end_session)
            return None
        raise RuntimeError(f"unexpected server message {kind!r} mid-session")

    def _await_client_turn(self) -> list[TurnRecord]:
        """持续读取，直到下一轮属于客户端角色位或会话结束；返回期间收到的
        轮次。已达 max_turns 时继续读到 EndSession，让 closed 及时生效。"""
        assert self._role_assignment is not None
        bindings = self._role_assignment.bindings
        max_turns = self._role_assignment.max_turns
        received: list[TurnRecord] = []
        while not self._closed:
            next_index = len(self._turns)
            if (
                next_index < max_turns
                and bindings[next_index % len(bindings)].slot_id == self._client_slot
            ):
                break
            turn = self._pump_one()
            if turn is not None:
                received.append(turn)
        return received

    def _finish(self, end_session_proto) -> None:
        self._closed = True
        self._last_reason = end_session_proto.reason
        self._send_q.put(None)

    def _build_episode(self, reason: str) -> Episode:
        return Episode(
            id=self._episode_id,
            scenario_id=self._scenario_id,
            role_assignment=self._role_assignment,
            turns=list(self._turns),
            terminated_reason=reason,
        )
