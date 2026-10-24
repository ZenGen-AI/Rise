"""部署驱动器:Actio 实例占客户端角色位,经实时环境服务跑部署 episode 并落盘(闭环集成设计 §8.3)。

落盘归驱动器,不归服务端钩子:客户端在会话结束时重建的 Episode(自己轮次是服务端回显,
带激活轨迹;对手轮次的内心独白与轨迹按协议已被抹掉)以 deploy-<model>-<scenario>-<k> 为 id
写进绑定模型所在 run 的 episodes 目录。失败会话整条重跑(补充 E.4)。
"""
from __future__ import annotations

from collections.abc import Callable

import grpc

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from mindagent.config import agent_slot_of
from simulator.datamodel import Episode, Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.store_adapter import read_episode, read_scenario, write_episode

PURPOSE = "deployment_simulation"
# 与 mindtrainer.trajectory_source 同一套判定(本包不导入 mindtrainer):
ACCEPTED_REASONS = frozenset({"max_turns", "goal_reached"})
TRANSIENT_REASONS = frozenset({"timeout", "role_backend_timeout", "persistence_failed"})


class DeploymentFailedError(Exception):
    """某条部署 episode 失败且不再重跑(不可重跑的原因,或瞬态重跑用尽)。
    transient 为真表示最后一次失败是 T1(重跑用尽);failed_attempts 是本次调用的全部失败尝试。"""

    def __init__(
        self, *, episode_id: str, reason: str, detail: str, attempts: int,
        transient: bool, failed_attempts: list[dict],
    ) -> None:
        tail = f": {detail}" if detail else ""
        super().__init__(f"deployment episode {episode_id} failed after {attempts} attempt(s): {reason}{tail}")
        self.episode_id = episode_id
        self.reason = reason
        self.detail = detail
        self.attempts = attempts
        self.transient = transient
        self.failed_attempts = [dict(a) for a in failed_attempts]


class _AttemptFailed(Exception):
    def __init__(self, reason: str, detail: str, transient: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.transient = transient


def _classify_client_error(exc: Exception) -> _AttemptFailed:
    """客户端调用抛出的传输/协议错误 -> 一次失败的尝试。只有 UNAVAILABLE 算瞬态。"""
    if isinstance(exc, grpc.RpcError):
        code = exc.code() if callable(getattr(exc, "code", None)) else None
        detail = exc.details() if callable(getattr(exc, "details", None)) else str(exc)
        if code == grpc.StatusCode.UNAVAILABLE:
            return _AttemptFailed("transport_unavailable", detail or "", True)
        name = code.name.lower() if code is not None else "unknown"
        return _AttemptFailed(f"transport_{name}", detail or "", False)
    return _AttemptFailed("client_protocol_error", str(exc), False)


def deployment_episode_id(model_id: str, scenario_id: str, k: int) -> str:
    return f"deploy-{model_id}-{scenario_id}-{k}"


def _reusable(episode: Episode, agent_slot: str, library_version: dict) -> bool:
    """已落盘的部署 episode 只有在它的每个 agent 轮次都带着与当前实例相同库版本的轨迹时才复用;
    库在轮次中途被收录改过,就按同一个 id 重新生成并覆盖。"""
    agent_turns = [t for t in episode.turns if t.speaker_slot == agent_slot]
    return bool(agent_turns) and all(
        isinstance(t.activation_trace, dict)
        and t.activation_trace.get("library_version") == library_version
        for t in agent_turns
    )


def _attempt(instance, client_factory, scenario, assignment, personas, agent_slot) -> Episode:
    persona = personas[next(b.persona_id for b in assignment.bindings if b.slot_id == agent_slot)]
    client = client_factory()
    try:
        try:
            client.create_session(
                scenario_id=scenario.id,
                role_assignment=assignment,
                personas=personas,
                purpose=PURPOSE,
                client_driven_slot=agent_slot,
            )
        except SessionRejectedError as exc:
            raise _AttemptFailed(exc.reason, exc.detail, False) from exc
        except (grpc.RpcError, RuntimeError) as exc:
            raise _classify_client_error(exc) from exc
        while not client.closed:
            # 策略(Actio 实例)的异常不在任何 except 里:穿过下面的 finally(先 abort)原样上抛
            utterance, thought, trace = instance.respond(persona, scenario, client.turns, agent_slot)
            try:
                client.submit_turn(utterance, thought, activation_trace=trace)
            except (grpc.RpcError, RuntimeError) as exc:
                raise _classify_client_error(exc) from exc
        try:
            episode = client.drain_until_end()
        except (grpc.RpcError, RuntimeError) as exc:
            raise _classify_client_error(exc) from exc
    finally:
        if not client.closed:
            # 不发 EndSession:服务端按断连判 FAILED,不落盘半截 episode
            client.abort()
    if episode.terminated_reason in ACCEPTED_REASONS:
        return episode
    raise _AttemptFailed(
        episode.terminated_reason, "", episode.terminated_reason in TRANSIENT_REASONS
    )


def run_deployment_episodes(
    store: GitStore,
    instance,
    client_factory: Callable[[], EnvironmentServiceClient],
    *,
    run_id: str,
    scenario_ids: list[str] | tuple[str, ...],
    personas: dict[str, Persona],
    bindings: list[RoleBinding] | tuple[RoleBinding, ...],
    model_id: str,
    max_turns: int,
    episodes_per_scenario: int = 1,
    max_retries_per_episode: int = 1,
) -> list[Episode]:
    """按 scenario_ids 的顺序,每个情境跑 episodes_per_scenario 条部署 episode,返回全部 episode
    (含重入时复用的)。每条 episode 落在 runs/<run_id>/episodes/deploy-<model>-<scenario>-<k>.yaml。

    - 已落盘且可复用(见 _reusable)的 episode 不重跑。
    - T1(timeout、role_backend_timeout、传输层 UNAVAILABLE)整条重跑,上限 max_retries_per_episode;
      其余失败不重跑。两者都以 DeploymentFailedError 结束,已成功的 episode 留在树里供重入复用。
    - 实例自己的异常:会话先 abort(服务端不落盘),原异常原样上抛,不重试、不包装。
    """
    agent_slot = agent_slot_of(bindings)
    out: list[Episode] = []
    failed_attempts: list[dict] = []
    for scenario_id in scenario_ids:
        scenario = None
        for k in range(1, episodes_per_scenario + 1):
            episode_id = deployment_episode_id(model_id, scenario_id, k)
            if store.exists(episode_file(run_id, episode_id)):
                existing = read_episode(store, run_id, episode_id)
                if _reusable(existing, agent_slot, instance.library_version):
                    out.append(existing)
                    continue
            if scenario is None:
                scenario = read_scenario(store, scenario_id)
            budget = 1 + max_retries_per_episode
            for attempt in range(1, budget + 1):
                assignment = RoleAssignment(
                    scenario_id=scenario_id,
                    bindings=[RoleBinding.from_dict(b.to_dict()) for b in bindings],
                    max_turns=max_turns,
                )
                try:
                    raw = _attempt(instance, client_factory, scenario, assignment, personas, agent_slot)
                except _AttemptFailed as failure:
                    failed_attempts.append({
                        "episode_id": episode_id, "attempt": attempt,
                        "reason": failure.reason, "detail": failure.detail,
                    })
                    if not failure.transient or attempt == budget:
                        raise DeploymentFailedError(
                            episode_id=episode_id, reason=failure.reason, detail=failure.detail,
                            attempts=attempt, transient=failure.transient,
                            failed_attempts=failed_attempts,
                        ) from failure
                    continue
                episode = Episode(
                    id=episode_id, scenario_id=raw.scenario_id, role_assignment=raw.role_assignment,
                    turns=raw.turns, terminated_reason=raw.terminated_reason,
                )
                write_episode(store, run_id, episode)
                out.append(episode)
                break
    return out
