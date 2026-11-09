"""部署驱动器:SimpleActio 实例占客户端角色位,经实时环境服务跑部署 episode 并落盘(闭环集成设计 §8.3,
设计二第 9 节)。

落盘归驱动器,不归服务端钩子:客户端在会话结束时重建的 Episode(自己轮次是服务端回显,
带激活轨迹;对手轮次的内心独白与轨迹按协议已被抹掉)以 deploy-<model>-<scenario>-<k> 为 id
写进绑定模型所在 run 的 episodes 目录。失败会话整条重跑(补充 E.4)。

部署记忆(设计三 4.3–4.5 节):一个情境一个部署世界(世界 id 就是 scenario_id),跨轮次持久,智能体位与
部署对手位都是其中的主体。每条 episode 开始前先写 `.world` 旁注、再把整个世界 backup_to 起点快照
snapshots/<round_id>/deploy-<episode_id>;每次尝试新开 (世界, 智能体位人格) 端口、新造实例,对手位的端口由服务端
按登记的 round_id 开。尝试失败先关实例与端口、等这个世界的端口全关(服务端在另一条线程上收尾),再用起点快照
覆盖整个世界。成功时等端口全关、落盘 episode、放弃这条 episode 留下的 pending 行(不分主体),最后删快照、再删
旁注:删快照就是提交点。两种 id:服务端与落盘文件名用不带轮次的部署 id(deploy-...,白名单不含 '/'),
实例(事件 key 的前缀)用带轮次前缀的 <round_id>/deploy-...,所以下一轮同名的部署 id 不撞上一轮的 key。
调用方(轮次处理器)持本轮全部情境的世界锁。
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import grpc

from simple_actio.memory_port import DeploymentMemoryStore, MemoryBackend, MemoryFailureError
from simple_actio.trace import normalize_library_version
from controlplane.deferred import DeferredError
from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from mindagent.config import agent_slot_of
from mindagent.errors import AgentRoundFailedError, RestoreFailedError
from mindagent.memory_layout import MemoryLayout, _tag_path
from simulator.datamodel import Episode, Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.store_adapter import read_episode, read_scenario, write_episode

PURPOSE = "deployment_simulation"
# 与 mindtrainer.trajectory_source 同一套判定(本包不导入 mindtrainer;tests/test_interact_parity.py 钉住瞬态集合相等):
ACCEPTED_REASONS = frozenset({"max_turns", "goal_reached"})
TRANSIENT_REASONS = frozenset({
    "timeout", "role_backend_timeout", "role_backend_unavailable", "role_memory_unavailable",
    "persistence_failed",
})
# 准入被拒的退避(终审 I-1),与 mindtrainer.trajectory_source 同一套(tests/test_interact_parity.py 钉住):
# 服务端发完 EndSession 之后还在收尾里 join 在途后端调用(最多 resource_join_timeout_s,缺省 60 秒),
# 其间同一部署 id 的 CreateSession 以 role_backend_unavailable 被拒。被拒且原因是它时,在同一次尝试里
# 0.5 秒起指数退避(单次至多 8 秒)重建,总等待 admission_wait_s(缺省 65 秒),不计入 max_retries_per_episode。
ADMISSION_REFUSED = "role_backend_unavailable"
ADMISSION_WAIT_S = 65.0
ADMISSION_FIRST_BACKOFF_S = 0.5
ADMISSION_MAX_BACKOFF_S = 8.0
# 恢复、开下一次尝试与提交之前等这个世界的端口全关的上限(设计三 4.5 节):服务端在 Interact 的 finally 里
# 另起线程收尾,join 在途后端调用最多 resource_join_timeout_s(缺省 60 秒)后才关端口,再加 10 秒余量。
SESSION_CLOSE_WAIT_S = 70.0


# make_instance(带轮次前缀的 episode id, (情境世界, 智能体位人格) 新开的端口, 智能体位人格 id 即写入方主体)
# -> SimpleActioInstance;每次尝试调一次。
MakeInstance = Callable[[str, MemoryBackend, str], object]


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


def rerun_episode_id(base: str, skill_version: str) -> str:
    """技能版本变了之后重跑一条已提交 episode 用的新逻辑 id:事件 key 随之不同,不撞已 committed 的行。"""
    return f"{base}-s{skill_version}"


def _skill_of(trace: object) -> object:
    if not isinstance(trace, dict):
        return None
    version = trace.get("library_version")
    return version.get("skill") if isinstance(version, dict) else None


def _first_agent_skill(episode: Episode, agent_slot: str) -> object:
    """首个智能体轮次轨迹里的技能版本;没有智能体轮次或轨迹不成形时为 None。"""
    first = next((t for t in episode.turns if t.speaker_slot == agent_slot), None)
    return None if first is None else _skill_of(first.activation_trace)


def _reusable(episode: Episode, agent_slot: str, skill_version: str, recorded_digest: str | None) -> bool:
    """已提交(起点快照已删)的部署 episode 复用的条件(设计三 4.5 节):有智能体轮次、episode_digests 里记过它
    的起点摘要、每个智能体轮次的轨迹都是 dict 且技能版本等于当前的。只比技能版本:一次记忆收录不让已提交的
    episode 重跑。不比部署世界当前摘要:已提交的 episode 写过记忆,摘要本来就变了。也不比首轮的 digest_before:
    三个及以上角色位时,智能体位开口之前别的对手位已经往世界里写过,它不再等于起点摘要;起点摘要记进轮次产物与
    删快照(提交点)在同一个轮次里一一对应,已足以防"同一个 episode id 下世界的起点不一样"。"""
    agent_turns = [t for t in episode.turns if t.speaker_slot == agent_slot]
    if not agent_turns or recorded_digest is None:
        return False
    return all(_skill_of(t.activation_trace) == skill_version for t in agent_turns)


MEMORY_POINT = "mindagent.deployment.memory"
_T = TypeVar("_T")
_log = logging.getLogger(__name__)


def _memory_op(what: str, call: Callable[[], _T]) -> _T:
    """部署记忆管理面与副本文件操作(建副本目录、backup、open、pending、abandon、删副本):永久的
    MemoryFailureError 与 OSError 改抛 AgentRoundFailedError(失败点 MEMORY_POINT,只报类名),轮次处理器据此
    判 T3 并回滚(终审 I-2:坏库或磁盘问题不能从处理器逃出、让守护进程每次重启都在同一处退出)。
    瞬态的 MemoryFailureError 原样上抛:轮次留 pending,下一 tick 重入。覆盖(restore_from)不走这里,见 _restore。"""
    try:
        return call()
    except MemoryFailureError as exc:
        if exc.transient:
            raise
        raise AgentRoundFailedError(f"{what}: {type(exc).__name__}", point=MEMORY_POINT) from exc
    except OSError as exc:
        raise AgentRoundFailedError(f"{what}: {type(exc).__name__}", point=MEMORY_POINT) from exc


def _close_after_failure(what: str, close: Callable[[], None]) -> None:
    """原异常在途时关实例或端口(终审 M-1):close 再失败也不盖掉原异常,只记一条 warning(只报类名)。
    没有原异常在途时用 _memory_op(what, close),close 失败按管理面的同一语义改抛。"""
    try:
        close()
    except Exception as exc:
        _log.warning("%s failed while another exception propagates: %s", what, type(exc).__name__)


def _restore(memory_store: DeploymentMemoryStore, world_id: str, snap: Path,
             expected: str | None, episode_id: str, close_wait_s: float) -> str:
    """先等该世界的端口全关(至多 close_wait_s 秒),再用起点快照覆盖整个世界,返回覆盖后的摘要。等不到、
    覆盖抛错、摘要与 expected 不等都是 RestoreFailedError(设计二第 9 节"覆盖失败"、设计三 4.5 节):原因只报
    类名,库与快照由调用方原样保留,等人来看。"""
    what = f"restore {episode_id} in world {world_id}"
    if not memory_store.wait_closed(world_id, close_wait_s):
        raise RestoreFailedError(f"{what}: memory ports still open after {close_wait_s:g}s",
                                 point="mindagent.deployment.restore")
    try:
        digest = memory_store.restore_from(world_id, snap)
    except (MemoryFailureError, OSError) as exc:
        raise RestoreFailedError(f"{what}: {type(exc).__name__}", point="mindagent.deployment.restore") from exc
    if expected is not None and digest != expected:
        raise RestoreFailedError(f"{what}: digest mismatch", point="mindagent.deployment.restore")
    return digest


def _await_closed(memory_store: DeploymentMemoryStore, world_id: str, close_wait_s: float, before: str) -> None:
    """开一次尝试或提交之前等该世界的端口全关;等不到是瞬态(设计三 4.5 节):DeferredError,轮次留 pending,
    守护进程退避后重入,重入时"快照在就先恢复"。"""
    if not memory_store.wait_closed(world_id, close_wait_s):
        raise DeferredError(f"world {world_id} still has open memory ports after {close_wait_s:g}s before {before}")


def _check_seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number, got {value!r}")
    return float(value)


def _abort_unless_closed(client) -> None:
    """不发 EndSession:服务端按断连判 FAILED,不落盘半截 episode。"""
    if not client.closed:
        client.abort()


def _open_session(client_factory, scenario, assignment, personas, agent_slot, *, episode_id: str,
                  deadline_s: float | None, admission_wait_s: float,
                  sleep: Callable[[float], None], clock: Callable[[], float]):
    """新建客户端并 create_session,返回会话已建成的客户端;建不成的客户端在这里 abort(被拒的本已 closed)。
    创建被拒且原因是 ADMISSION_REFUSED 时退避重建(一条流只承载一次会话,所以每次都新造客户端),
    预算用完仍被拒才往下走。第 10 节:其余创建被拒按终止原因查瞬态集合;传输与协议错误照旧分类。"""
    give_up_at = clock() + admission_wait_s
    backoff = ADMISSION_FIRST_BACKOFF_S
    while True:
        client = client_factory() if deadline_s is None else client_factory(deadline_s=deadline_s)
        try:
            client.create_session(
                scenario_id=scenario.id,
                role_assignment=assignment,
                personas=personas,
                purpose=PURPOSE,
                client_driven_slot=agent_slot,
                episode_id=episode_id,
            )
        except SessionRejectedError as exc:
            _abort_unless_closed(client)
            wait = min(backoff, give_up_at - clock())
            if exc.reason == ADMISSION_REFUSED and wait > 0:
                sleep(wait)
                backoff = min(backoff * 2, ADMISSION_MAX_BACKOFF_S)
                continue
            raise _AttemptFailed(exc.reason, exc.detail, exc.reason in TRANSIENT_REASONS) from exc
        except (grpc.RpcError, RuntimeError) as exc:
            _abort_unless_closed(client)
            raise _classify_client_error(exc) from exc
        except BaseException:
            _abort_unless_closed(client)
            raise
        return client


def _attempt(instance, client_factory, scenario, assignment, personas, agent_slot, *,
             episode_id: str, deadline_s: float | None, admission_wait_s: float = ADMISSION_WAIT_S,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Episode:
    """episode_id 是不带轮次的部署 id,随 CreateSession 交给服务端。deadline_s 为 None 时不传,
    客户端工厂用它自己的现值(替身路径)。准入被拒的退避见 _open_session。"""
    persona = personas[next(b.persona_id for b in assignment.bindings if b.slot_id == agent_slot)]
    client = _open_session(client_factory, scenario, assignment, personas, agent_slot, episode_id=episode_id,
                           deadline_s=deadline_s, admission_wait_s=admission_wait_s, sleep=sleep, clock=clock)
    try:
        while not client.closed:
            # 记忆端口的失败按 transient 分两个终止原因,信息只报类名(第 10 节);策略(SimpleActio 实例)的
            # 其余异常不在任何 except 里:穿过下面的 finally(先 abort)原样上抛
            try:
                utterance, thought, trace = instance.respond(persona, scenario, client.turns, agent_slot)
            except MemoryFailureError as exc:
                raise _AttemptFailed(
                    "role_memory_unavailable" if exc.transient else "role_memory_failure",
                    type(exc).__name__, exc.transient,
                ) from None
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
    make_instance: MakeInstance,
    client_factory: Callable[..., EnvironmentServiceClient],
    *,
    memory_store: DeploymentMemoryStore,
    memory_layout: MemoryLayout,
    run_id: str,
    round_id: str,
    library_version: dict,
    episode_digests: Mapping[str, str],
    on_episode_start: Callable[[str, str], None],
    scenario_ids: list[str] | tuple[str, ...],
    personas: dict[str, Persona],
    bindings: list[RoleBinding] | tuple[RoleBinding, ...],
    model_id: str,
    max_turns: int,
    episodes_per_scenario: int = 1,
    max_retries_per_episode: int = 1,
    stream_deadline_s: float | None = None,
    admission_wait_s: float = ADMISSION_WAIT_S,
    register_episode: Callable[[str, str], None] = lambda eid, rid: None,
    unregister_episode: Callable[[str], None] = lambda eid: None,
    close_wait_s: float = SESSION_CLOSE_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[Episode]:
    """按 scenario_ids 的顺序,每个情境跑 episodes_per_scenario 条部署 episode,返回全部 episode
    (含重入时复用的)。每条 episode 落在 runs/<run_id>/episodes/<部署 id>.yaml。调用方持本轮全部情境的世界锁。

    - 世界:一条 episode 的世界就是它的情境(world = scenario_id)。开 episode 之前,这个世界里有别的轮次留下的
      未提交起点快照(R1 回滚失败,或 R1 的轮次产物读不出)就抛 DeferredError,原因写出那个轮次 id,要人恢复或
      删掉它;任何一份快照的 `.world` 旁注坏了抛 MindAgentDataError(轮次处理器判 T3)。本轮自己的快照不算。
    - 起点快照:快照不在时依次 write_world_tag、memory_store.backup_to(world, snap)、
      on_episode_start(部署 id, 起点摘要):旁注先写,崩在两步之间只留一个孤儿旁注,它既不算在途快照、也不算旧布局。
    - 每次尝试:先等该世界的端口全关,再 register_episode(部署 id, round_id)(服务端据此在同一世界里开对手位的
      端口),memory_store.open(世界, 智能体位人格) 新端口,make_instance(f"{round_id}/{部署 id}", 端口, 智能体位人格)
      新造实例,跑会话,关实例与端口,unregister_episode(部署 id)(finally 里,失败的尝试也撤销)。尝试失败先等
      该世界的端口全关、再用起点快照覆盖整个世界(_restore),下一次尝试面对起点状态;成功时等端口全关再提交。
      等端口全关的上限都是 close_wait_s 秒。
    - 等端口全关超时:要恢复时抛 RestoreFailedError(库与快照都保留,等人看);开一次尝试或提交之前抛
      DeferredError(瞬态,轮次留 pending,重入时"快照在就先恢复")。
    - 提交:某次尝试成功后落盘 episode,放弃这条 episode 留下的 pending 行(列世界的,只放弃本轮本条前缀
      f"{round_id}/{部署 id}#" 的,各按行自带的主体放弃,智能体位与对手位的都算;门② 的行归提案),
      最后删起点快照、再删旁注。删快照就是提交点;崩在它之前,快照还在,这条就算在途。先删快照:崩在两步之间
      留下的是孤儿旁注,反过来会留下没有旁注的快照,启动检查会把它当成旧布局。
    - 重入:起点快照还在的 episode 先覆盖、核对摘要等于 episode_digests 里记的,再按同一 id 重跑,不重新备份。
      起点快照不在、episode 已落盘且可复用(见 _reusable)的直接复用;技能版本变了的改用
      rerun_episode_id(部署 id, 技能版本) 重跑,原 episode 留在树里。episode_digests 的值是本改动之前的形状
      (dict)时当作没记过:已提交的 episode 不复用、按同一 id 重跑。
    - 失败:T1(TRANSIENT_REASONS 与传输层 UNAVAILABLE)整条重跑,上限 max_retries_per_episode;其余失败
      不重跑。两者都以 DeploymentFailedError 结束,部署世界已覆盖回起点、起点快照保留(留给重入或轮次回滚)。
      覆盖失败(等不到端口全关、抛错或摘要对不上)抛 RestoreFailedError,库与快照都保留。
    - 实例自己的异常(记忆端口失败除外)与存储的 OSError:会话先 abort(服务端不落盘),实例已关,
      原异常原样上抛,不重试、不包装、不覆盖,与进程崩溃同效。
    - 部署记忆管理面与快照文件操作(写旁注、建快照目录、backup、open、pending、abandon、删快照与旁注)的永久失败
      (MemoryFailureError 非瞬态、OSError)抛 AgentRoundFailedError(失败点 mindagent.deployment.memory,
      只报类名),轮次处理器判 T3 并回滚(见 _memory_op)。每次尝试收尾关实例与端口也按这一语义;
      有原异常在途(尝试失败、实例自己抛错)时 close 失败不盖掉原异常,只记 warning(终审 M-1)。
    - 创建被拒且原因是 role_backend_unavailable(同 id 的旧会话还在服务端收尾,或名额满)时,先在同一次
      尝试里退避重建(见 ADMISSION_WAIT_S),不计入重试次数、不记 failed_attempts;admission_wait_s 用完仍被拒
      才按上一条算一次 T1 失败。sleep 与 clock 只为测试注入。
    - stream_deadline_s 为 None 时不给客户端工厂传 deadline_s。
    """
    admission_wait_s = _check_seconds("admission_wait_s", admission_wait_s)
    close_wait_s = _check_seconds("close_wait_s", close_wait_s)
    agent_slot = agent_slot_of(bindings)
    persona_id = next(b.persona_id for b in bindings if b.slot_id == agent_slot)
    skill_version = normalize_library_version(library_version)["skill"]
    out: list[Episode] = []
    failed_attempts: list[dict] = []

    def recorded(eid: str) -> str | None:
        digest = episode_digests.get(eid)
        return digest if isinstance(digest, str) else None   # 旧形状 {persona_id: 摘要} 当作没记过

    def committed_reusable(eid: str) -> tuple[bool, Episode | None]:
        """起点快照不在且已落盘时返回 (能否复用, 已落盘的 episode);快照在或没落盘时 (False, None)。"""
        if memory_layout.snapshot(round_id, eid).exists():
            return False, None
        if not store.exists(episode_file(run_id, eid)):
            return False, None
        existing = read_episode(store, run_id, eid)
        return _reusable(existing, agent_slot, skill_version, recorded(eid)), existing

    for scenario_id in scenario_ids:
        world = scenario_id
        scenario = None
        for k in range(1, episodes_per_scenario + 1):
            base = deployment_episode_id(model_id, scenario_id, k)
            eid = base
            reusable, existing = committed_reusable(base)
            if reusable:
                out.append(existing)
                continue
            if existing is not None and _first_agent_skill(existing, agent_slot) != skill_version:
                eid = rerun_episode_id(base, skill_version)
                reusable, again = committed_reusable(eid)
                if reusable:
                    out.append(again)
                    continue
            # 设计三 4.3 节:回滚一个世界会退掉快照之后的全部写入,别的轮次的快照在,本轮就不能往这个世界写
            others = [p for p in memory_layout.uncommitted_snapshots(world) if p.parent.name != round_id]
            if others:
                raise DeferredError(f"world {world} has an uncommitted start snapshot of round "
                                    f"{others[0].parent.name}; restore or delete it by hand")
            if scenario is None:
                scenario = read_scenario(store, scenario_id)
            snap = memory_layout.snapshot(round_id, eid)
            if snap.exists():
                # 在途时崩溃(不论 episode 文件有没有落盘):先覆盖回起点、核对摘要,不重新备份
                expected = recorded(eid)
                start = _restore(memory_store, world, snap, expected, eid, close_wait_s)
                if expected is None:  # 崩在 backup 与写回轮次产物之间(或产物是旧形状)
                    on_episode_start(eid, start)
            else:
                _memory_op(f"snapshot dir for {eid}", lambda: snap.parent.mkdir(parents=True, exist_ok=True))
                _memory_op(f"world tag for {eid}", lambda: memory_layout.write_world_tag(snap, world))
                start = _memory_op(f"backup {eid} of world {world}", lambda: memory_store.backup_to(world, snap))
                on_episode_start(eid, start)
            budget = 1 + max_retries_per_episode
            raw = None
            for attempt in range(1, budget + 1):
                assignment = RoleAssignment(
                    scenario_id=scenario_id,
                    bindings=[RoleBinding.from_dict(b.to_dict()) for b in bindings],
                    max_turns=max_turns,
                )
                failure: _AttemptFailed | None = None
                _await_closed(memory_store, world, close_wait_s, f"attempt {attempt} of {eid}")
                register_episode(eid, round_id)
                try:
                    port = _memory_op(f"open {persona_id} in world {world}",
                                      lambda: memory_store.open(world, persona_id))
                    # 先关实例与端口:restore_from 要求该世界没有打开的端口。close 失败(终审 M-1)没有原异常在途时
                    # 按 _memory_op 改抛,有原异常在途时不盖它、只记 warning
                    try:
                        instance = make_instance(f"{round_id}/{eid}", port, persona_id)
                        try:
                            raw = _attempt(instance, client_factory, scenario, assignment, personas, agent_slot,
                                           episode_id=eid, deadline_s=stream_deadline_s,
                                           admission_wait_s=admission_wait_s, sleep=sleep, clock=clock)
                        except BaseException:
                            _close_after_failure(f"close instance for {eid}", instance.close)
                            raise
                        _memory_op(f"close instance for {eid}", instance.close)
                    except _AttemptFailed as exc:
                        failure = exc
                        failed_attempts.append({
                            "episode_id": eid, "attempt": attempt,
                            "reason": exc.reason, "detail": exc.detail,
                        })
                    except BaseException:
                        # 兜住 make_instance 自己抛出、端口没交给实例的情况;close 幂等
                        _close_after_failure(f"close port of {persona_id}", port.close)
                        raise
                    _memory_op(f"close port of {persona_id}", port.close)
                finally:
                    unregister_episode(eid)
                if failure is None:
                    break
                # 服务端的对手端口在另一条线程上关:_restore 先等这个世界的端口全关
                _restore(memory_store, world, snap, start, eid, close_wait_s)
                if not failure.transient or attempt == budget:
                    raise DeploymentFailedError(
                        episode_id=eid, reason=failure.reason, detail=failure.detail,
                        attempts=attempt, transient=failure.transient,
                        failed_attempts=failed_attempts,
                    ) from failure
            # 只有某次尝试成功才走到这里:等端口全关、落盘、放弃本条的 pending 行、删起点快照(提交点)与旁注
            _await_closed(memory_store, world, close_wait_s, f"committing {eid}")
            episode = Episode(
                id=eid, scenario_id=raw.scenario_id, role_assignment=raw.role_assignment,
                turns=raw.turns, terminated_reason=raw.terminated_reason,
            )
            write_episode(store, run_id, episode)
            prefix = f"{round_id}/{eid}#"
            pending = _memory_op(f"pending events of world {world}", lambda: memory_store.pending_events(world))
            for p in pending:
                if p.key.startswith(prefix):
                    _memory_op(f"abandon {p.key}", lambda p=p: memory_store.abandon_event(world, p.holder, p.key))
            _memory_op(f"commit {eid}", snap.unlink)
            _memory_op(f"drop world tag of {eid}", lambda: _tag_path(snap).unlink(missing_ok=True))
            out.append(episode)
    return out
