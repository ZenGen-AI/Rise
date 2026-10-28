from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable, Protocol

import grpc

from controlplane.gitstore import GitStore
from mindtrainer.datamodel import TrajectoryBatch
from mindtrainer.training_config import TrainingConfig, ensure_valid_config
from simulator.backends import ModelBackend
from simulator.dataset import DatasetManifest, verify_manifest
from simulator.datamodel import Episode, RoleAssignment
from simulator.realtime.backend_timeout import RoleMemoryUnavailableError
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.store_adapter import read_persona, read_scenario


# 每条 rollout 进出一次的策略上下文:交出 ModelBackend,respond 返回二元组或三元组。
# 参数是这条 rollout 的逻辑 episode id(见 logical_episode_id)。
PolicyForRollout = Callable[[str], AbstractContextManager[ModelBackend]]

# 设计源 6.4 节"客户端每轮等待 90 秒":按 max_turns_per_episode 乘出整条会话的流 deadline。
PER_TURN_DEADLINE_S = 90.0


def logical_episode_id(run_id: str, step: int, scenario_id: str, k: int) -> str:
    """一条 rollout 的逻辑 episode id:`{run_id}-grpo{step}-{scenario_id}-r{k}`。
    只用白名单 [A-Za-z0-9._-] 内的字符(取决于 run_id 与 scenario_id 本身)。"""
    return f"{run_id}-grpo{step}-{scenario_id}-r{k}"


class TrajectorySource(Protocol):
    """下游(奖励管线、分组)只依赖这一个方法,不关心轨迹来自哪里。"""

    def collect(self) -> TrajectoryBatch: ...


def batch_for_step(items: list, step: int, size: int) -> list:
    """按固定顺序轮转取:[items[(step*size + i) % n] for i in range(size)]。

    每个 step 用的数据是 step 的纯函数,续训等价才可测。回绕时同一批内样本可能重复
    (size > len(items) 时)。返回新列表,改它不会影响 items。"""
    if not items:
        raise ValueError("items must be non-empty")
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    if step < 0:
        raise ValueError(f"step must be >= 0, got {step}")
    n = len(items)
    return [items[(step * size + i) % n] for i in range(size)]


class BatchTrajectorySource:
    """BatchTrain 的轨迹源:读带 sha256 清单的数据集。

    直接调 simulator.dataset.verify_manifest:逐条读 episode,用规范化 JSON 重算
    sha256 比对清单,再重算整体 manifest_hash,不依赖 YAML 排版。任何不一致
    (清单被改、episode 被改或丢失、清单为空)抛 DatasetIntegrityError,不静默跳过。

    本类不判断组大小:SFT 数据集每组一条是正常的。GRPO 阶段启动前的组大小校验
    属于 grouping.validate_groups。每次 collect() 都重新读盘并重新校验,
    所以返回的 episode 彼此独立,调用方改动不会影响下一次 collect()。
    """

    def __init__(self, store: GitStore, manifest: DatasetManifest) -> None:
        self._store = store
        self._manifest = manifest

    def collect(self) -> TrajectoryBatch:
        episodes = verify_manifest(self._store, self._manifest)
        return TrajectoryBatch(
            episodes=episodes,
            metadata={
                "manifest_id": self._manifest.id,
                "manifest_hash": self._manifest.manifest_hash,
                "episode_count": len(episodes),
            },
        )


# 只有这两种终止原因的 episode 进 batch。超时、后端失败、断连产生的半截 episode 一律不进。
_ACCEPTED_REASONS = frozenset({"max_turns", "goal_reached"})
# T1 瞬态:服务端报告的两种超时、对手 API 暂时不可用(role_backend_unavailable:429、5xx、连不上)、
# 对手位记忆暂时不可用(role_memory_unavailable),加上传输层 UNAVAILABLE(见 _classify_client_error)。
# role_memory_failure 是确定性的(T2),不在这里。
# persistence_failed:会话跑完了,但服务端落盘钩子失败,树里没有这条 episode。整条重跑。
# mindagent.deployment.TRANSIENT_REASONS 是这个集合的复制(mindagent 不导入 mindtrainer),
# tests/test_interact_parity.py 钉住两边相等。
_TRANSIENT_REASONS = frozenset({
    "timeout", "role_backend_timeout", "role_backend_unavailable", "role_memory_unavailable",
    "persistence_failed",
})


class RolloutFailedError(Exception):
    """某条 rollout 失败且不再重试(不可重试的原因,或瞬态重试用尽)。

    failed_attempts 带着截至失败时本次 collect 的全部失败尝试(含更早 rollout 的瞬态
    失败):失败是数据,调用方应把它记下来,不要丢。"""

    def __init__(
        self,
        *,
        scenario_id: str,
        rollout: int,
        reason: str,
        detail: str,
        attempts: int,
        failed_attempts: list[dict],
    ) -> None:
        tail = f": {detail}" if detail else ""
        super().__init__(
            f"rollout {rollout} of scenario {scenario_id!r} failed after "
            f"{attempts} attempt(s): {reason}{tail}"
        )
        self.scenario_id = scenario_id
        self.rollout = rollout
        self.reason = reason
        self.detail = detail
        self.attempts = attempts
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


class InteractTrajectorySource:
    """InteractTrain 的轨迹源:作为客户端驱动 Plan 4 的实时环境服务。

    - 一条 gRPC 流只承载一次会话,所以每条 rollout 用 client_factory(deadline_s=...) 新建客户端。
    - policy_for_rollout 每条 rollout(含每次重试)进出一次,交出 simulator.backends.ModelBackend,
      respond 返回二元组 (utterance, inner_thought) 或三元组 (…, activation_trace);三元组的轨迹随
      submit_turn 上行。先进上下文、再造客户端、再 create_session;失败时先 abort 客户端、再退出上下文。
    - 每条 rollout 用逻辑 episode id `{run_id}-grpo{step}-{scenario_id}-r{k}`(step 是本次 collect 的
      GRPO step,k 是组内序号),随 create_session 上行,服务端按它落盘。同一条 rollout 的重试、失败后
      重做同一步、续训从同一步接着走,id 都不变(服务端按同名覆盖)。
    - 每个 scenario 连续 rollout group_size 条,共享同一个 RoleAssignment 的内容,
      所以 group_id 相同。每次 collect() 取 groups_per_step 个 scenario,按 scenario_ids
      轮转(batch_for_step)。V1 顺序执行。
    - episode 由服务端的 on_session_closed 钩子落盘(调用方配置);本类不写 episode,
      只在 metadata 里记 episode id 列表。服务端的 scenario_resolver 必须读与本类
      同一个 ControlPlane,否则两边对 scenario 的理解会分歧(接线计划负责)。

    失败处理(补充 E.4:失败会话整条重跑,不续接):
    - 只有 terminated_reason ∈ {max_turns, goal_reached} 的 episode 进 batch。
    - T1 瞬态(timeout、role_backend_timeout、role_backend_unavailable、role_memory_unavailable、persistence_failed、
      传输层 UNAVAILABLE;创建被拒时同样按 reason 判,上面这些原因的拒绝也重跑;训练位的
      RoleMemoryUnavailableError 记为 role_memory_unavailable、detail 只写类名)原样重跑,
      上限 max_retries_per_episode,每次失败记入 metadata["failed_attempts"]。
    - 其余(invalid_session、role_backend_rejected、role_memory_failure、role_backend_failure 等)不重试,抛 RolloutFailedError。
      role_backend_rejected 是对手 API 明确拒绝(4xx,429 除外),确定性,T2:重试无益,编排器据此写 failed。
      role_memory_failure 是对手位记忆的确定性失败,同样是 T2,编排器同样据此写 failed。
      role_backend_failure 在服务端同时覆盖"后端确定性异常"和"工厂/解析器故障",
      光看 reason 分不出 T1 还是 T2,所以保守地不重试。
    - 策略自己抛的异常不重试、不包装(含训练位的 RoleMemoryFailureError,确定性,由编排器与派发器
      分类;回复长度或轨迹类型不对的 TypeError 同理):会话先 abort()(服务端按断连判 FAILED、不落盘),
      原异常原样向上抛——那是被训模型的 bug,不是瞬态服务问题。
    - 失败的 collect() 里已经成功的 rollout 已被服务端经 on_session_closed 落盘,下次
      collect() 会对同一批 scenario 产出新的 episode,所以 ControlPlane 里会留下不属于
      任何 batch 的完整 episode;metadata["episode_ids"] 是唯一权威索引,下游不能按
      scenario 扫描取数据。
    - 批内的 episode 是策略视角:对手的 inner_thought 等不会流给客户端,所以它与服务端
      落盘的版本内容不同、episode_digest 也不同(episode_group_id 相同);权威版本按
      metadata["episode_ids"] 到 ControlPlane 取,两者不能互相核对摘要。
    """

    def __init__(
        self,
        client_factory: Callable[..., EnvironmentServiceClient],
        policy_for_rollout: PolicyForRollout,
        store: GitStore,
        config: TrainingConfig,
        *,
        run_id: str,
        start_step: int = 0,
    ) -> None:
        ensure_valid_config(config)
        if config.mode != "interact":
            raise ValueError(f"InteractTrajectorySource needs mode='interact', got {config.mode!r}")
        if start_step < 0:
            raise ValueError(f"start_step must be >= 0, got {start_step}")
        self._client_factory = client_factory
        self._policy_for_rollout = policy_for_rollout
        self._run_id = run_id
        self._store = store
        self._cfg = config
        # 已完成的 GRPO step 数:续训时由编排器传入,让 scenario 轮转接着走
        self._next_step = start_step
        self._trained_persona_id = next(
            b.persona_id for b in config.bindings if b.slot_id == config.trained_slot
        )

    def collect(self) -> TrajectoryBatch:
        cfg = self._cfg
        scenario_ids = batch_for_step(cfg.scenario_ids, self._next_step, cfg.groups_per_step)
        episodes: list[Episode] = []
        failed_attempts: list[dict] = []
        for scenario_id in scenario_ids:
            scenario = read_scenario(self._store, scenario_id)
            personas = {
                b.persona_id: read_persona(self._store, b.persona_id) for b in cfg.bindings
            }
            for rollout in range(cfg.group_size):
                episodes.append(
                    self._rollout(scenario, personas, rollout, failed_attempts)
                )
        self._next_step += 1  # 失败(抛异常)时不前进,下次 collect 重做同一批
        return TrajectoryBatch(
            episodes=episodes,
            metadata={
                "episode_ids": [e.id for e in episodes],
                "failed_attempts": failed_attempts,
            },
        )

    def _assignment(self, scenario_id: str) -> RoleAssignment:
        # 每次尝试都新建一份,免得同组 episode 共享同一个可变对象
        return RoleAssignment.from_dict(
            {
                "scenario_id": scenario_id,
                "bindings": [b.to_dict() for b in self._cfg.bindings],
                "max_turns": self._cfg.max_turns_per_episode,
            }
        )

    def _rollout(self, scenario, personas, rollout: int, failed_attempts: list[dict]) -> Episode:
        budget = 1 + self._cfg.max_retries_per_episode
        episode_id = logical_episode_id(self._run_id, self._next_step, scenario.id, rollout)
        for attempt in range(1, budget + 1):
            try:
                return self._attempt(scenario, personas, episode_id)
            except _AttemptFailed as failure:
                failed_attempts.append(
                    {
                        "scenario_id": scenario.id,
                        "rollout": rollout,
                        "attempt": attempt,
                        "reason": failure.reason,
                        "detail": failure.detail,
                    }
                )
                if not failure.transient or attempt == budget:
                    raise RolloutFailedError(
                        scenario_id=scenario.id,
                        rollout=rollout,
                        reason=failure.reason,
                        detail=failure.detail,
                        attempts=attempt,
                        failed_attempts=failed_attempts,
                    ) from failure
        raise AssertionError("unreachable: the loop always returns or raises")

    def _attempt(self, scenario, personas, episode_id: str) -> Episode:
        slot = self._cfg.trained_slot
        # 上下文进入时抛的异常(工厂侧的确定性错误)不包装,原样上抛
        with self._policy_for_rollout(episode_id) as policy:
            client = self._client_factory(
                deadline_s=self._cfg.max_turns_per_episode * PER_TURN_DEADLINE_S
            )
            try:
                try:
                    client.create_session(
                        scenario_id=scenario.id,
                        role_assignment=self._assignment(scenario.id),
                        personas=personas,
                        purpose="training",
                        client_driven_slot=slot,
                        episode_id=episode_id,
                    )
                except SessionRejectedError as exc:
                    raise _AttemptFailed(exc.reason, exc.detail, exc.reason in _TRANSIENT_REASONS) from exc
                except (grpc.RpcError, RuntimeError) as exc:
                    raise _classify_client_error(exc) from exc
                persona = personas[self._trained_persona_id]
                while not client.closed:
                    try:
                        reply = policy.respond(persona, scenario, client.turns, slot)
                    except RoleMemoryUnavailableError as exc:
                        # 训练位记忆暂时不可用:整条重跑。detail 只写类名。
                        raise _AttemptFailed("role_memory_unavailable", type(exc).__name__, True) from exc
                    # 其余策略异常(含 RoleMemoryFailureError)不在任何 except 里:
                    # 穿过下面的 finally(先 abort)原样上抛
                    utterance, thought, trace = _unpack_reply(reply, slot)
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
                    # 不发 EndSession:服务端按断连判 FAILED,不经 on_session_closed 落盘。
                    # 若改发 end_session,半截 episode 会被当成正常结束写进 ControlPlane。
                    client.abort()
        if episode.terminated_reason in _ACCEPTED_REASONS:
            return episode
        raise _AttemptFailed(
            episode.terminated_reason, "", episode.terminated_reason in _TRANSIENT_REASONS
        )


def _unpack_reply(reply: object, slot: str) -> tuple[str, str | None, dict | None]:
    """策略回复 -> (utterance, inner_thought, activation_trace)。与 simulator.runner._unpack_reply 同一判法:
    长度不是 2 或 3、或轨迹不是 dict 都是策略的 bug,抛 TypeError,不重试。"""
    if not isinstance(reply, (tuple, list)) or len(reply) not in (2, 3):
        raise TypeError(f"policy for slot {slot!r} must return a 2- or 3-tuple, got {reply!r}")
    trace = reply[2] if len(reply) == 3 else None
    if trace is not None and not isinstance(trace, dict):
        raise TypeError(f"policy for slot {slot!r} returned a non-dict activation_trace: {trace!r}")
    return reply[0], reply[1], trace
