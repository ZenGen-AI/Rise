from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from simulator.datamodel import Episode, Persona, Scenario, RoleAssignment, TurnRecord
from simulator.backends import ModelBackend
from simulator.runner import run_episode
from simulator.realtime.backend import ClientDrivenBackend, SessionEndRequested, TurnTimeoutError
from simulator.realtime.backend_timeout import (
    RoleBackendRejectedError, RoleBackendTimeoutError, RoleBackendUnavailableError,
    RoleMemoryFailureError, RoleMemoryUnavailableError,
)
from simulator.realtime.session import SessionState, assert_transition


@dataclass(frozen=True)
class DriverEvent:
    """SessionDriver 经 outbox 转发给 gRPC 主循环的事件。
    kind="turn" → payload: TurnRecord；kind="closed" → payload: Episode；
    kind="failed" → payload: str（终止原因，如 "timeout"/"role_backend_failure"/
    "role_backend_timeout"/"role_backend_unavailable"/"role_backend_rejected"/
    "role_memory_unavailable"/"role_memory_failure"/"client_disconnected"）。"""

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
        self._turns: list[TurnRecord] = []
        self._thread: threading.Thread | None = None
        self.state = SessionState.CREATED
        self._end_requested = threading.Event()
        self._abort_reason: str | None = None

    def start(self) -> None:
        assert_transition(self.state, SessionState.ACTIVE)
        self.state = SessionState.ACTIVE
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit_turn(
        self,
        utterance: str,
        inner_thought: str | None,
        *,
        activation_trace: dict | None = None,
    ) -> None:
        self._client_backend.submit(utterance, inner_thought, activation_trace=activation_trace)

    def request_end(self, reason: str) -> None:
        """客户端主动、优雅地结束会话（收到 EndSession 消息）：唤醒可能正
        阻塞的 ClientDrivenBackend.respond()，让后台线程尽快收尾为
        terminated_reason="client_terminated" 的正常 closed 事件。"""
        self._end_requested.set()
        self._client_backend.request_end()

    def request_abort(self, reason: str) -> None:
        """服务端检测到客户端异常（断连/取消），非客户端主动优雅结束：
        唤醒可能正阻塞的 respond()，但收尾为 failed 事件而非 closed——
        §2.6.5 要求客户端侧异常由服务端自行判定为失败，不能与"客户端主动
        结束"混同，也不应经 on_session_closed 被当成正常完成的 episode
        持久化（closed 与 failed 事件的持久化路径本就不同，见 servicer.py）。"""
        if self._abort_reason is None:  # 首个原因为准：后续（如 finally 兜底）不得覆盖
            self._abort_reason = reason
        self._end_requested.set()
        self._client_backend.request_end()

    def poll_event(self, timeout_s: float) -> DriverEvent | None:
        try:
            return self._outbox.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _stop_predicate(self, turns: list[TurnRecord]) -> bool:
        return self._end_requested.is_set()

    def _on_turn(self, turn: TurnRecord) -> None:
        self._turns.append(turn)
        self._outbox.put(DriverEvent(kind="turn", payload=turn))

    def _run(self) -> None:
        try:
            episode = run_episode(
                self._scenario, self._assignment, self._personas, self._backends,
                self._episode_id,
                stop_predicate=self._stop_predicate,
                on_turn=self._on_turn,
            )
        except SessionEndRequested:
            # respond() 在等待候选下一轮话语时被要求结束——绝不能把唤醒哨兵
            # 当成一次真实发言：Episode 只用 self._turns 里已经真实完成的轮次
            # 重建（run_episode 自己的局部 turns 列表在异常传播时被丢弃，
            # 但每轮完成时 _on_turn 已经把它同步进了 self._turns）。
            if self._abort_reason is not None:
                assert_transition(self.state, SessionState.FAILED)
                self.state = SessionState.FAILED
                self._outbox.put(DriverEvent(kind="failed", payload=self._abort_reason))
                return
            assert_transition(self.state, SessionState.COMPLETING)
            self.state = SessionState.COMPLETING
            episode = Episode(
                id=self._episode_id, scenario_id=self._scenario.id,
                role_assignment=self._assignment, turns=list(self._turns),
                terminated_reason="client_terminated",
            )
            assert_transition(self.state, SessionState.CLOSED)
            self.state = SessionState.CLOSED
            self._outbox.put(DriverEvent(kind="closed", payload=episode))
            return
        except TurnTimeoutError:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="timeout"))
            return
        except RoleBackendTimeoutError:
            # 服务端角色后端超时（T1 瞬态）：与后端抛确定性异常（T2，下方
            # role_backend_failure）区分开，让调用方能按 §2.6.5 决定是否换种子重跑。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_timeout"))
            return
        except RoleBackendUnavailableError:
            # 对端暂时不可用（429、5xx、连不上，T1）：与超时同理，调用方可以按条重跑。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_unavailable"))
            return
        except RoleBackendRejectedError:
            # 对端明确拒绝（4xx，T2）：重试无益。单独的终止原因，让轨迹源与编排器能确定它是确定性的，
            # 而不是和下方说不清的 role_backend_failure 混在一起。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_rejected"))
            return
        except RoleMemoryUnavailableError:
            # 角色位的记忆暂时不可用（Starling 抽取/嵌入端点超时、连不上，T1）：调用方可以按条重跑。
            # simulator 不认 simple_actio.MemoryFailureError（依赖方向不允许），组合根的转换后端改抛本基类的子类，
            # 这里按 isinstance 落进来；必须在下方通用 Exception 之前，否则会被吞成 role_backend_failure。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_memory_unavailable"))
            return
        except RoleMemoryFailureError:
            # 角色位的记忆确定性失败（T2）：同样的输入再来还是同样的结果，重试无益。单独的终止原因，
            # 不并入 role_backend_failure（后者在服务端同时覆盖工厂/解析器故障，会被译成延后反复重试）。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_memory_failure"))
            return
        except Exception:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_failure"))
            return

        assert_transition(self.state, SessionState.COMPLETING)
        self.state = SessionState.COMPLETING
        if self._abort_reason is not None:
            # 运气不好：request_abort 恰好在最后一轮之后才生效（run_episode
            # 已经正常返回），仍必须收尾为 failed，不能被当成正常完成。
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload=self._abort_reason))
            return
        if self._end_requested.is_set():
            episode = Episode(
                id=episode.id, scenario_id=episode.scenario_id,
                role_assignment=episode.role_assignment, turns=episode.turns,
                terminated_reason="client_terminated",
            )
        assert_transition(self.state, SessionState.CLOSED)
        self.state = SessionState.CLOSED
        self._outbox.put(DriverEvent(kind="closed", payload=episode))
