from __future__ import annotations

import queue
import threading
import uuid
from typing import Callable

import grpc

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

_CLIENT_CLOSED = object()


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
        """双向流主循环（select 风格）。一个后台泄流线程把 request_iterator 的
        消息搬进 inbox，迭代器耗尽（客户端正常结束或异常断连）时投递
        _CLIENT_CLOSED 哨兵。主循环交替做两件事：非阻塞轮询 inbox 处理客户端
        消息；抽干驱动器 outbox 转发服务端事件。这样服务端主动产生的事件
        （超时、后端失败）无需等待下一条客户端消息即可推送（§2.6.5），
        TurnUpdate 也不再与客户端消息一一绑定（§2.6.2）。"""
        inbox: "queue.Queue" = queue.Queue()

        def _pump() -> None:
            try:
                for msg in request_iterator:
                    inbox.put(msg)
            except grpc.RpcError:
                # 流被取消/客户端断连：对泄流线程而言这与迭代器正常耗尽是同一
                # 终止条件，哨兵已足以通知主循环，无需向上传播（§2.6.5 客户端
                # 侧异常由服务端自行检测收尾，不需要客户端配合）。
                pass
            finally:
                inbox.put(_CLIENT_CLOSED)

        threading.Thread(target=_pump, daemon=True).start()

        driver: SessionDriver | None = None
        client_closed = False
        try:
            while True:
                try:
                    msg = inbox.get(timeout=0.01)
                except queue.Empty:
                    msg = None

                if msg is _CLIENT_CLOSED:
                    client_closed = True
                elif msg is not None:
                    kind = msg.WhichOneof("payload")
                    if kind == "create_session":
                        session_id = f"sess-{uuid.uuid4().hex[:12]}"
                        driver, ready = self._handle_create(msg.create_session, session_id)
                        yield ready
                    elif kind == "submit_turn" and driver is not None:
                        driver.submit_turn(
                            msg.submit_turn.utterance,
                            msg.submit_turn.inner_thought
                            if msg.submit_turn.HasField("inner_thought") else None,
                        )
                    elif kind == "end_session" and driver is not None:
                        driver.request_end("client_terminated")

                if driver is not None:
                    terminal = False
                    for out in self._drain_ready_events(driver):
                        yield out
                        if out.WhichOneof("payload") == "end_session":
                            terminal = True
                    if terminal:
                        return

                if client_closed:
                    if driver is None:
                        return
                    if driver.state in ("CLOSED", "FAILED"):
                        return
                    driver.request_end("client_terminated")
                    # 继续循环：等待驱动器收尾产生 closed 事件后由 drain 转发并 return
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
