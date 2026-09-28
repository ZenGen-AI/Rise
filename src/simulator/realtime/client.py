from __future__ import annotations

import queue

from simulator.datamodel import Episode, Persona, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, role_assignment_to_proto, turn_record_from_proto,
)
from simulator.realtime.proto import realtime_env_pb2 as pb2


class EnvironmentServiceClient:
    """EnvironmentService 的同步客户端包装器（未来 MindTrainer InteractTrain /
    MindAgent 经验采集直接复用的接口）。用直接 next() 拉取响应，不起消费者
    线程——每次调用严格对应"发一条 ClientMessage → 拉直到下一个边界事件"。
    逐轮累积 TurnRecord，在 EndSession 到达时于本地重建完整 Episode
    （§2.6.2："最终Episode摘要引用"，协议不重复内联整个 Episode）。"""

    def __init__(self, stub) -> None:
        self._stub = stub
        self._send_q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()
        self._resp_iter = stub.Interact(self._request_gen())
        self._session_id = ""
        self._scenario_id = ""
        self._role_assignment: RoleAssignment | None = None
        self._episode_id = ""
        self._turns: list[TurnRecord] = []
        self._closed = False

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
        ready = next(self._resp_iter)
        self._session_id = ready.session_ready.session_id
        self._scenario_id = scenario_id
        self._role_assignment = role_assignment
        self._episode_id = f"ep-{self._session_id}"
        return self._session_id

    def submit_turn(self, utterance: str, inner_thought: str | None = None) -> TurnRecord | None:
        """提交本轮候选话语，拉取直到下一次轮到候选（或会话已结束）。
        返回期间收到的最后一条对手轮 TurnRecord，会话已结束则返回 None。"""
        submit = pb2.SubmitTurn(utterance=utterance)
        if inner_thought is not None:
            submit.inner_thought = inner_thought
        self._send_q.put(pb2.ClientMessage(submit_turn=submit))
        last_opponent_turn: TurnRecord | None = None
        while True:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                turn = turn_record_from_proto(msg.turn_update.turn)
                self._turns.append(turn)
                last_opponent_turn = turn
                if len(self._turns) % len(self._role_assignment.bindings) == 0:
                    return last_opponent_turn
            elif kind == "end_session":
                self._finish(msg.end_session)
                return None

    def drain_until_end(self) -> Episode:
        """不主动提交，持续拉取直到会话自然终止（超时/后端失败/max_turns）。"""
        while not self._closed:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                self._turns.append(turn_record_from_proto(msg.turn_update.turn))
            elif kind == "end_session":
                self._finish(msg.end_session)
        return self._build_episode(self._last_reason)

    def end_session(self, reason: str = "client_terminated") -> Episode:
        self._send_q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason=reason)))
        while not self._closed:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                self._turns.append(turn_record_from_proto(msg.turn_update.turn))
            elif kind == "end_session":
                self._finish(msg.end_session)
        return self._build_episode(self._last_reason)

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
