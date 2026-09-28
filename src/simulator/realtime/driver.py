from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from simulator.datamodel import Episode, Persona, Scenario, RoleAssignment, TurnRecord
from simulator.backends import ModelBackend
from simulator.runner import run_episode
from simulator.realtime.backend import ClientDrivenBackend, TurnTimeoutError
from simulator.realtime.session import SessionState, assert_transition


@dataclass(frozen=True)
class DriverEvent:
    """SessionDriver 经 outbox 转发给 gRPC 主循环的事件。
    kind="turn" → payload: TurnRecord；kind="closed" → payload: Episode；
    kind="failed" → payload: str（终止原因，如 "timeout"/"role_backend_failure"）。"""

    kind: str
    payload: Any


class SessionDriver:
    """在后台线程里驱动 run_episode，把每轮结果与最终结果经 outbox 队列
    转发出来。client_driven_slot 对应的 backend 换成 ClientDrivenBackend，
    其余角色位使用调用方传入的 server_backends（真实/罐头模型）。"""

    def __init__(
        self,
        scenario: Scenario,
        assignment: RoleAssignment,
        personas: dict[str, Persona],
        *,
        server_backends: dict[str, ModelBackend],
        client_driven_slot: str,
        episode_id: str,
        turn_timeout_s: float,
    ) -> None:
        self._scenario = scenario
        self._assignment = assignment
        self._personas = personas
        self._episode_id = episode_id
        self.client_driven_slot = client_driven_slot
        self._client_backend = ClientDrivenBackend(client_driven_slot, turn_timeout_s)
        self._backends: dict[str, ModelBackend] = dict(server_backends)
        self._backends[client_driven_slot] = self._client_backend
        self._outbox: "queue.Queue[DriverEvent]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.state = SessionState.CREATED
        self._end_requested = threading.Event()

    def start(self) -> None:
        assert_transition(self.state, SessionState.ACTIVE)
        self.state = SessionState.ACTIVE
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit_turn(self, utterance: str, inner_thought: str | None) -> None:
        self._client_backend.submit(utterance, inner_thought)

    def request_end(self, reason: str) -> None:
        """客户端主动终止：置位标志，并投喂一条哨兵话语唤醒可能正阻塞的
        ClientDrivenBackend.respond()，让后台线程尽快检查标志并收尾。"""
        self._end_requested.set()
        self._client_backend.submit("", None)

    def poll_event(self, timeout_s: float) -> DriverEvent | None:
        try:
            return self._outbox.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _stop_predicate(self, turns: list[TurnRecord]) -> bool:
        return self._end_requested.is_set()

    def _on_turn(self, turn: TurnRecord) -> None:
        self._outbox.put(DriverEvent(kind="turn", payload=turn))

    def _run(self) -> None:
        try:
            episode = run_episode(
                self._scenario, self._assignment, self._personas, self._backends,
                self._episode_id,
                stop_predicate=self._stop_predicate,
                on_turn=self._on_turn,
            )
        except TurnTimeoutError:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="timeout"))
            return
        except Exception:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_failure"))
            return

        assert_transition(self.state, SessionState.COMPLETING)
        self.state = SessionState.COMPLETING
        if self._end_requested.is_set():
            episode = Episode(
                id=episode.id, scenario_id=episode.scenario_id,
                role_assignment=episode.role_assignment, turns=episode.turns,
                terminated_reason="client_terminated",
            )
        assert_transition(self.state, SessionState.CLOSED)
        self.state = SessionState.CLOSED
        self._outbox.put(DriverEvent(kind="closed", payload=episode))
