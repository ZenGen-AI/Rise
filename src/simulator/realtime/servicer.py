from __future__ import annotations

import uuid
from typing import Callable

from simulator.datamodel import Persona, RoleAssignment, Scenario, RoleSlot
from simulator.backends import ModelBackend
from simulator.realtime.codec import (
    persona_from_proto, role_assignment_from_proto, turn_record_to_proto,
)
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc

BackendsFactory = Callable[
    [Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]
]


class EnvironmentServicer(pb2_grpc.EnvironmentServiceServicer):
    """gRPC 双向流服务实现（§2.6.2）。每个 Interact 调用对应一个会话：
    先收 CreateSession 起 SessionDriver，随后把 SubmitTurn 转发给驱动器、
    把驱动器 outbox 里的事件转成 ServerMessage yield 出去。会话间除各自
    的 SessionDriver 实例外不共享任何可变状态（§2.6.4）。"""

    def __init__(
        self,
        backends_factory: BackendsFactory,
        *,
        turn_timeout_s: float = 30.0,
        on_session_closed=None,
    ) -> None:
        self._backends_factory = backends_factory
        self._turn_timeout_s = turn_timeout_s
        self._on_session_closed = on_session_closed

    def Interact(self, request_iterator, context):
        driver: SessionDriver | None = None
        session_id = ""
        try:
            for msg in request_iterator:
                kind = msg.WhichOneof("payload")
                if kind == "create_session":
                    session_id = f"sess-{uuid.uuid4().hex[:12]}"
                    driver, ready = self._handle_create(msg.create_session, session_id)
                    yield ready
                elif kind == "submit_turn":
                    driver.submit_turn(
                        msg.submit_turn.utterance,
                        msg.submit_turn.inner_thought
                        if msg.submit_turn.HasField("inner_thought") else None,
                    )
                elif kind == "end_session":
                    driver.request_end("client_terminated")

                if driver is not None:
                    yield from self._drain_ready_events(driver)
        finally:
            if driver is not None and driver.state not in ("CLOSED", "FAILED"):
                driver.request_end("client_terminated")

    def _handle_create(self, create_session, session_id: str):
        role_assignment = role_assignment_from_proto(create_session.role_assignment)
        personas = {
            pid: persona_from_proto(p) for pid, p in create_session.personas.items()
        }
        scenario = Scenario(
            id=create_session.scenario_id,
            background="",
            role_slots=[RoleSlot(slot_id=s, description="") for s in role_assignment.slot_ids()],
            conflict="", norms=[], purpose=create_session.purpose,
        )
        server_backends = self._backends_factory(
            scenario, role_assignment, personas, create_session.client_driven_slot,
        )
        driver = SessionDriver(
            scenario, role_assignment, personas,
            server_backends=server_backends,
            client_driven_slot=create_session.client_driven_slot,
            episode_id=f"ep-{session_id}",
            turn_timeout_s=self._turn_timeout_s,
        )
        driver.start()
        ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id=session_id))
        return driver, ready

    def _drain_ready_events(self, driver: SessionDriver):
        """把驱动器 outbox 里已就绪的事件全部转发出去（非阻塞轮询，
        一次 poll_event(0) 抽干当前积压，避免每条 ClientMessage 只对应
        恰好一条 ServerMessage 的强绑定——TurnUpdate 需要在轮次完成后
        立即推送，可能一次提交触发多条服务端消息）。"""
        while True:
            event = driver.poll_event(timeout_s=0.01)
            if event is None:
                return
            if event.kind == "turn":
                yield pb2.ServerMessage(
                    turn_update=pb2.TurnUpdate(turn=turn_record_to_proto(event.payload))
                )
            elif event.kind == "closed":
                episode = event.payload
                if self._on_session_closed is not None:
                    self._on_session_closed(episode)
                yield pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=episode.terminated_reason, episode_id=episode.id,
                    )
                )
                return
            elif event.kind == "failed":
                yield pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=event.payload, episode_id="", failure_detail=event.payload,
                    )
                )
                return
