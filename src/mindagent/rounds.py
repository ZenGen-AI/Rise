"""轮次处理器 start_agent_round(闭环集成设计 §3.2、§8.3)。

流程:读绑定模型 → 把被取代的 pending 旧轮次置 failed → 读当前正式库 → 写 agent_round(pending)
→ 部署 episode → 分析 → 提案 → 验证 → 打包 → 轮次置 executed。

失败分类(与派发器同一约定):
- 确定性错误(配置、树里的数据、解析耗尽、不可重跑的会话失败)与任何 BridgeError(R18:T1 是
  桥接已用尽自己的重试,T2 是确定性故障)→ 轮次 failed,并经 ledger.record_failure 写一条 T3
  失败记录(task_id 为 agent-<model>)。
- 非桥接的瞬态(会话 T1 重跑用尽)与其余异常原样上抛,轮次保持 pending。按 R19,调和循环对
  pending 的轮次在下一个 tick 重新触发 start_agent_round,所以本处理器的重入必须幂等:已提交的
  部署 episode 不重跑,提案按确定性 id 重写。

部署记忆(设计二第 9 节):
- 处理器一开始(写任何东西之前)按排序后的绑定人格 id 逐个拿 deploy/<persona_id>.lock,锁持到处理器
  返回或抛出;拿不到就抛 DeferredError,轮次留在原状态,守护进程退避后重试。
- 轮次产物带 memory_runtime(装配时给的运行时身份)与 episode_digests({部署 id: {persona_id: 起点摘要}}),
  每条 episode 开始前写回一次;pending 轮次重入时继承上一次记下的 episode_digests,已提交的才认得出来。
- 把 pending 轮次写成终态的每条路径(T3、被取代、模型读不出、轮次产物读不出)都先走同一步 _close_round:
  拿该轮次绑定人格的锁(被占抛 DeferredError,不写终态),用起点副本把在途 episode 覆盖回起点、删副本,
  再写终态。覆盖失败时库与副本都保留,轮次照样写 failed(T3,失败点 mindagent.deployment.restore)并记
  失败记录。驱动器自己抛的覆盖失败不再回滚。瞬态失败与其余异常不回滚:轮次留 pending,重入时由驱动器
  "副本在就先覆盖"处理。
- 验证重放在部署库的验证快照上做:基线与候选各从快照恢复一份临时副本,实例只读;轮次结束即删,
  snapshots/<round_id>/ 空了也删。收尾关验证实例失败只记 warning,不抛(终审 M-1)。
- 记忆层的确定性错误不从处理器逃出(终审 I-2):部署驱动器把管理面与副本文件操作的永久失败改抛
  AgentRoundFailedError(失败点 mindagent.deployment.memory),验证快照段同样(mindagent.rounds.verify),都判 T3
  并回滚;验证段(快照与重放)里的瞬态 MemoryFailureError 改抛 DeferredError,轮次留 pending、守护进程退避重试;绑定人格 id 拼不出锁路径(ActioDataError)判 T3、不回滚(失败点 mindagent.rounds.persona)。
  原因只报类名。模型后端的 OSError 不在其内,照旧原样上抛、轮次留 pending。
"""
from __future__ import annotations

import copy
import logging
import re
import shutil
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from actio.errors import ActioDataError
from actio.instance import instantiate
from actio.memory_port import PERSONA_ID_PATTERN, DeploymentMemoryStore, MemoryFailureError
from actio.skills import Skill
from controlplane.deferred import DeferredError
from controlplane.envelope import BigRef, Envelope, InvalidBigRefError, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.lock import FileLockBusyError
from controlplane.models import version_of_file, version_of_id
from controlplane.paths import CONTROL_PLANE_ROOT, agent_round_file, model_file, persona_file, scenario_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from inference_bridge import BridgeError
from mindagent.analysis import analyze
from mindagent.config import AgentConfig, agent_slot_of
from mindagent.deployment import DeploymentFailedError, run_deployment_episodes
from mindagent.errors import AgentRoundFailedError, MindAgentDataError, ProposalParseError, RestoreFailedError
from mindagent.failures import UNREADABLE, fail_once, read_or_none
from mindagent.library import LibraryState, load_library
from mindagent.memory_layout import MemoryLayout
from mindagent.packaging import Thresholds, package, screen, stale_proposals
from mindagent.proposals import BRIDGE_GENERATOR, PROMPT_TEMPLATE_VERSION, RULE_GENERATOR, propose
from mindagent.verify import verify
from simulator.datamodel import Persona
from simulator.store_adapter import read_scenario

ROUND_TYPE = "agent_round"
_log = logging.getLogger(__name__)
_ROUND_FILE = re.compile(r"agent-round-(zengen-v(\d+))\.yaml$")

BackendFactory = Callable[[str, "BigRef | None"], object]
PersonasLoader = Callable[[GitStore, list[str]], dict[str, Persona]]
Handler = Callable[[GitStore, Action], None]
Rollback = Callable[[Envelope], None]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_id_for(model_id: str) -> str:
    return f"agent-round-{model_id}"


def task_id_for(model_id: str) -> str:
    return f"agent-{model_id}"


def _model_version(model_id: str) -> int:
    version = version_of_id(model_id)
    if version is None:
        raise AgentRoundFailedError(f"not a zengen model id: {model_id!r}", point="mindagent.rounds.model")
    return version


def _read_model(store: GitStore, model_id: str) -> tuple[int, str, BigRef | None]:
    """(版本, 产生它的 run, checkpoint_ref)。promote_candidate 写的 links 是 [候选 id, run id]。"""
    version = _model_version(model_id)
    rel = model_file(version)
    if not store.exists(rel):
        raise AgentRoundFailedError(f"model {model_id} not found at {rel}", point="mindagent.rounds.model")
    env = store.read_product(rel)
    if env.status != "effective":
        raise AgentRoundFailedError(f"model {model_id} is {env.status}, not effective", point="mindagent.rounds.model")
    if len(env.links) < 2 or not env.links[1]:
        raise AgentRoundFailedError(f"model {model_id} has no run in its links", point="mindagent.rounds.model")
    raw = env.payload.get("checkpoint_ref")
    if raw is None:
        return version, env.links[1], None
    try:
        return version, env.links[1], BigRef.from_dict(raw)
    except (InvalidBigRefError, KeyError, AttributeError) as exc:
        # 手改进树的坏引用(I8):调用方只接 AgentRoundFailedError,别的异常会从处理器漏出去打崩守护进程
        raise AgentRoundFailedError(
            f"model {model_id} has an unusable checkpoint_ref ({type(exc).__name__}: {exc})",
            point="mindagent.rounds.model",
        ) from exc


def _latest_effective_version(store: GitStore) -> int:
    best = 0
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        version = version_of_file(rel)
        if version is not None and store.read_product(rel).status == "effective":
            best = max(best, version)
    return best


def _fail_round(store: GitStore, env: Envelope, reason: str, *, category: str, point: str) -> None:
    assert_transition("nonreview", env.status, "failed")
    env.status = "failed"
    env.payload["failure"] = {"category": category, "reason": reason, "point": point}
    store.write_product(agent_round_file(env.payload["model_id"]), env, f"mindagent: {env.id} failed ({category})")


def _close_round(store: GitStore, env: Envelope, reason: str, *, category: str, point: str,
                 rollback: Rollback | None, run_id: object = None, retries: int = 0, record: bool = False) -> None:
    """把一个 pending 轮次写成终态的唯一入口(设计二第 9 节"轮次失败收尾"):先 rollback(env)(拿锁、
    在途 episode 覆盖回起点、删副本),再 _fail_round,record 为真时再记一次 T3 失败记录。
    rollback 抛 DeferredError(锁被占)时原样上抛,什么都不写;抛 RestoreFailedError 时库与副本保留,
    轮次改记覆盖失败(T3)并记失败记录。轮次已是终态(同一 tick 里 supersede 刚置过)时什么都不做。"""
    if env.status != "pending":
        return
    if rollback is not None:
        try:
            rollback(env)
        except RestoreFailedError as exc:
            reason = f"{exc.reason} (while failing the round: {reason})"
            category, point, record = "T3", exc.point, True
    _fail_round(store, env, reason, category=category, point=point)
    if record:
        fail_once(store, env.payload.get("run_id") if run_id is None else run_id,
                  task_id=task_id_for(env.payload["model_id"]), category="T3",
                  point=point, retries=max(retries, 0), reason=reason)


def supersede_stale_rounds(store: GitStore, latest_version: int, *, rollback: Rollback | None = None) -> list[str]:
    """把绑定模型版本低于 latest_version、仍为 pending 的轮次置 failed(superseded),置之前先 rollback
    (见 _close_round)。返回被置的轮次 id。"""
    out: list[str] = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/agent_rounds"):
        match = _ROUND_FILE.search(rel)
        if match is None or int(match.group(2)) >= latest_version:
            continue
        env = read_or_none(store, rel)
        if env is None:
            continue  # 读不出:留给它自己那个模型的 start_agent_round 判损坏(计划 8 视它为可触发)
        if env.status == "pending":
            _close_round(store, env, f"superseded by zengen-v{latest_version}",
                         category="superseded", point="mindagent.rounds.supersede", rollback=rollback)
            out.append(env.id)
    return out


def _existing_or_new(store: GitStore, model_id: str, run_id: str) -> Envelope:
    """已有的轮次产物;不存在或读不出时新建一份最小的 pending 产物(随后由调用方置 failed)。"""
    env = read_or_none(store, agent_round_file(model_id))
    return env if env is not None else _round_envelope(
        model_id, {"model_id": model_id, "run_id": run_id, "failure": None})


def _round_envelope(model_id: str, payload: dict) -> Envelope:
    return Envelope(
        id=round_id_for(model_id), type=ROUND_TYPE, version=1, created_at=_now(),
        created_by=OwnerRef(), status="pending", schema_version=1,
        links=[model_id, payload["run_id"]], payload=payload,
    )


def _binding_payload(model_id: str, version: int, run_id: str, ckpt: BigRef | None,
                     lib: LibraryState, config: AgentConfig, generator: str, memory_runtime: dict) -> dict:
    payload = {
        "model_id": model_id,
        "model_version": version,
        "run_id": run_id,
        "library_version": dict(lib.version),
        "scenario_ids": list(config.scenario_ids),
        "persona_ids": [b.persona_id for b in config.bindings],
        "bindings": [b.to_dict() for b in config.bindings],
        "analyzer_config": {"min_support": config.min_support, "min_ratio": config.min_ratio},
        "verifier_config": {
            "method": "replay_behavior",
            "min_fired_on_motivating": config.min_fired_on_motivating,
            "max_fired_elsewhere": config.max_fired_elsewhere,
        },
        "generator_version": generator,
        "uses_bridge": generator == BRIDGE_GENERATOR,
        "memory_runtime": copy.deepcopy(memory_runtime),
        "episode_digests": {},
        "episode_ids": [],
        "findings": [],
        "proposals": [],
        "dropped": [],
        "failure": None,
    }
    if ckpt is not None:
        payload["checkpoint_ref"] = ckpt.to_dict()
    if generator == BRIDGE_GENERATOR:
        payload["prompt_template_version"] = PROMPT_TEMPLATE_VERSION
    return payload


def _check_inputs(store: GitStore, config: AgentConfig) -> None:
    for sid in config.scenario_ids:
        if not store.exists(scenario_file(sid)):
            raise AgentRoundFailedError(f"scenario {sid!r} not in the tree", point="mindagent.rounds.inputs")
    for b in config.bindings:
        if not store.exists(persona_file(b.persona_id)):
            raise AgentRoundFailedError(f"persona {b.persona_id!r} not in the tree", point="mindagent.rounds.inputs")


class _PersonaLocks:
    """一次处理器调用里拿到的人格库锁。同一个人格只拿一次:flock 按打开的文件算,同一进程再开一次去拿
    也会被拒。被占就抛 DeferredError(原因只有锁文件路径)。"""

    def __init__(self, layout: MemoryLayout) -> None:
        self._layout = layout
        self._stack = ExitStack()
        self._held: set[str] = set()

    def __enter__(self) -> "_PersonaLocks":
        return self

    def __exit__(self, *exc) -> bool:
        return self._stack.__exit__(*exc)

    def hold(self, persona_ids) -> None:
        for pid in sorted(set(persona_ids) - self._held):
            try:
                self._stack.enter_context(self._layout.persona_lock(pid))
            except FileLockBusyError as exc:
                raise DeferredError(f"persona library locked: {exc}") from exc
            self._held.add(pid)


_T = TypeVar("_T")


def _verify_op(call: Callable[[], _T]) -> _T:
    """验证快照与它的临时副本(建目录、backup、造临时库、restore、open):永久的 MemoryFailureError 与 OSError
    判 T3(终审 I-2),失败点 mindagent.rounds.verify,只报类名;瞬态的原样上抛,由验证段外层改抛 DeferredError。
    只包这些调用:验证重放里模型后端抛的 OSError 照旧原样上抛(与部署 episode 同一约定)。"""
    try:
        return call()
    except MemoryFailureError as exc:
        if exc.transient:
            raise
        raise AgentRoundFailedError(f"verify snapshot: {type(exc).__name__}", point="mindagent.rounds.verify") from exc
    except OSError as exc:
        raise AgentRoundFailedError(f"verify snapshot: {type(exc).__name__}", point="mindagent.rounds.verify") from exc


def _rmdir_if_empty(path: Path) -> None:
    """轮次结束时 snapshots/<round_id>/ 已空就删掉(终审 M-3:每轮留一个空目录);不空(在途副本)或删不了都不管。"""
    try:
        path.rmdir()
    except OSError:
        pass


def _round_personas(env: Envelope, fallback: list[str]) -> list[str]:
    """轮次绑定的人格:载荷的 persona_ids;最小产物(没有这个字段)用当前配置的。不合法的 id 不会有库
    也不会有副本,跳过。"""
    ids = env.payload.get("persona_ids")
    if not isinstance(ids, list):
        ids = fallback
    return sorted({p for p in ids if isinstance(p, str) and PERSONA_ID_PATTERN.fullmatch(p)})


def make_start_round_handler(
    *,
    config: AgentConfig,
    client_factory: Callable[..., object],
    backend_factory: BackendFactory,
    personas_loader: PersonasLoader,
    memory_store: DeploymentMemoryStore,
    memory_layout: MemoryLayout,
    memory_runtime: dict,
    scratch_store_factory: Callable[[Path], DeploymentMemoryStore],
    bridge=None,
    stream_deadline_s: float | None = None,
) -> Handler:
    """backend_factory(model_id, checkpoint_ref) 每次调用返回一个新的模型后端(普通或带指导语的);
    personas_loader(store, persona_ids) 返回 {persona_id: Persona}。bridge 缺省走规则模板。
    memory_store 与 memory_layout 是部署记忆(与门② 共用同一份);memory_runtime 是记忆运行时身份,
    装配时深拷贝一次;scratch_store_factory(root) 给验证重放造临时副本库。stream_deadline_s 原样交给
    部署驱动器,None 时客户端工厂用它自己的现值。"""
    generator = RULE_GENERATOR if bridge is None else BRIDGE_GENERATOR
    runtime = copy.deepcopy(memory_runtime)
    bound_personas = sorted({b.persona_id for b in config.bindings})

    def handle(store: GitStore, action: Action) -> None:
        model_id = action.target_id
        rel = agent_round_file(model_id)
        existing = read_or_none(store, rel)
        if existing is not None and existing.status != "pending":
            return  # executed 或 failed:终态,空操作
        with _PersonaLocks(memory_layout) as locks:
            try:
                locks.hold(bound_personas)  # 写任何东西之前:被占就延后,树里什么都不写
            except ActioDataError as exc:
                # 人格 id 不合 PERSONA_ID_PATTERN,拼不出锁路径(装配期本已拦下,这里兜底):不能从处理器逃出,
                # 否则守护进程每次重启都在同一处退出(终审 I-2)。一个锁都没拿到,所以不回滚:T3、只报类名。
                _fail_bad_persona(store, model_id, existing, type(exc).__name__)
                return
            _handle(store, model_id, rel, existing, _rollback_with(locks))

    def _fail_bad_persona(store: GitStore, model_id: str, existing: Envelope | None, cls: str) -> None:
        try:
            _, run_id, _ = _read_model(store, model_id)
        except (AgentRoundFailedError, *UNREADABLE):
            run_id = ""
        env = existing if existing is not None else _round_envelope(
            model_id, {"model_id": model_id, "run_id": run_id, "failure": None})
        _close_round(store, env, f"bound persona id refused by the memory layout: {cls}", category="T3",
                     point="mindagent.rounds.persona", rollback=None, run_id=run_id, record=True)

    def _rollback_with(locks: _PersonaLocks) -> Rollback:
        def rollback(env: Envelope) -> None:
            """该轮次每个还在的起点副本覆盖回它的人格库,再删副本(第 9 节"轮次失败收尾")。先拿该轮次
            绑定人格的锁(被占抛 DeferredError)。驱动器每次尝试的 finally 已关掉实例与端口;restore_from
            遇到仍打开的端口会拒绝覆盖,这里把它与文件系统错误一起改抛 RestoreFailedError,库与副本都不动。
            副本按最长的人格 id 后缀认领:人格 id 里允许 '-',`...-x-pa` 不能被当成 `pa` 的副本。"""
            personas = _round_personas(env, bound_personas)
            locks.hold(personas)
            by_length = sorted(personas, key=len, reverse=True)
            for snap in memory_layout.round_snapshots(env.id):
                pid = next((p for p in by_length if snap.name.endswith(f"-{p}{memory_layout.suffix}")), None)
                if pid is None:
                    continue
                try:
                    memory_store.restore_from(pid, snap)
                    snap.unlink()  # 删不掉的副本同样按覆盖失败处理:库与副本都留着等人,不从处理器逃出
                except (MemoryFailureError, OSError) as exc:
                    raise RestoreFailedError(f"rollback {snap.name} for {pid}: {type(exc).__name__}",
                                             point="mindagent.deployment.restore") from exc

        return rollback

    def _handle(store: GitStore, model_id: str, rel: str, existing: Envelope | None, rollback: Rollback) -> None:
        if existing is None and store.exists(rel):
            _fail_unreadable(store, model_id, rollback)
            return
        latest = _latest_effective_version(store)
        supersede_stale_rounds(store, latest, rollback=rollback)

        try:
            version, run_id, ckpt = _read_model(store, model_id)
        except AgentRoundFailedError as exc:
            _close_round(store, _existing_or_new(store, model_id, ""), exc.reason, category="T3",
                         point=exc.point, rollback=rollback)
            return  # 没有 run 可挂失败记录;原因写在轮次产物里
        if version < latest:
            _close_round(store, _existing_or_new(store, model_id, run_id), f"superseded by zengen-v{latest}",
                         category="superseded", point="mindagent.rounds.supersede", rollback=rollback)
            return

        def record(reason: str, point: str, retries: int, *, roll_back: bool = True) -> None:
            # 读库失败时轮次产物还没写:先补一份最小的 pending 产物再置 failed
            _close_round(store, _existing_or_new(store, model_id, run_id), reason, category="T3", point=point,
                         rollback=rollback if roll_back else None, run_id=run_id, retries=retries, record=True)

        lib: LibraryState | None = None
        try:
            lib = load_library(store)
            payload = _binding_payload(model_id, version, run_id, ckpt, lib, config, generator, runtime)
            env = _round_envelope(model_id, payload)
            if existing is not None:
                env.created_at = existing.created_at
                # 不继承的话重入时认不出已提交的 episode,会按同一 id 重跑而撞上它自己 committed 的 key
                env.payload["episode_digests"] = copy.deepcopy(existing.payload.get("episode_digests", {}))
            store.write_product(rel, env, f"mindagent: {env.id} pending")
            _check_inputs(store, config)
            _run(store, env, lib, run_id, model_id, ckpt)
        except DeploymentFailedError as exc:
            if exc.transient:
                raise  # 瞬态:不回滚,轮次留 pending,重入时驱动器"副本在就先覆盖"
            record(f"deployment: {exc}", "mindagent.deployment", exc.attempts - 1)
        except ProposalParseError as exc:
            record(str(exc), "mindagent.proposals", exc.attempts - 1)
        except BridgeError as exc:
            # R18:桥接已经自己退避重试过,T1 也升级为 T3,不留 pending 让守护循环无限空转外部推理
            # retries 是已重试次数(计划 1):attempts 是总尝试次数
            record(f"bridge {exc.category}: {exc.reason}", "mindagent.proposals", max(exc.attempts - 1, 0))
        except AgentRoundFailedError as exc:
            # 驱动器自己抛的覆盖失败:库与副本都保留,不再回滚
            record(exc.reason, exc.point, 0, roll_back=not isinstance(exc, RestoreFailedError))
        except (MindAgentDataError, ActioDataError) as exc:
            record(f"{type(exc).__name__}: {exc}", "mindagent.rounds", 0)

    def _run(store: GitStore, env: Envelope, lib: LibraryState, run_id: str, model_id: str,
             ckpt: BigRef | None) -> None:
        interlocutors = {b.slot_id: b.persona_id for b in config.bindings}
        profile = {"model_id": model_id, "round_id": env.id}

        def make_instance(episode_id: str, memory):
            return instantiate(
                backend_factory(model_id, ckpt), skills=lib.skills, memory=memory, episode_id=episode_id,
                interlocutors=interlocutors, library_version=lib.version, profile=profile,
            )

        def on_episode_start(eid: str, by_persona: dict[str, str]) -> None:
            env.payload["episode_digests"][eid] = dict(by_persona)
            store.write_product(agent_round_file(model_id), env, f"mindagent: {env.id} start {eid}")

        persona_ids = sorted({b.persona_id for b in config.bindings})
        personas = personas_loader(store, persona_ids)
        episodes = run_deployment_episodes(
            store, make_instance, client_factory, memory_store=memory_store, memory_layout=memory_layout,
            run_id=run_id, round_id=env.id, library_version=lib.version,
            episode_digests=env.payload["episode_digests"], on_episode_start=on_episode_start,
            scenario_ids=config.scenario_ids, personas=personas, bindings=config.bindings,
            model_id=model_id, max_turns=config.max_turns,
            episodes_per_scenario=config.episodes_per_scenario,
            max_retries_per_episode=config.max_retries_per_episode,
            stream_deadline_s=stream_deadline_s,
        )
        findings = analyze(episodes, min_support=config.min_support, min_ratio=config.min_ratio, skills=lib.skills)
        drafts = propose(findings, bridge=bridge, run_id=run_id, task_id=f"{task_id_for(model_id)}-propose",
                         max_parse_retries=config.max_parse_retries)
        scenarios = {sid: read_scenario(store, sid) for sid in config.scenario_ids}

        # 验证快照(第 9 节"验证重放"):部署跑完后把智能体位人格的部署库备份一份,基线与候选各从它恢复
        # 一份临时副本、只读重放;部署库本身不被验证碰到。
        agent_pid = next(b.persona_id for b in config.bindings if b.slot_id == agent_slot_of(config.bindings))
        verify_snap = memory_layout.verify_snapshot(env.id, agent_pid)
        scratch_root = memory_layout.verify_scratch(env.id)
        built: list = []

        def instance_with(extra: tuple[Skill, ...] = ()):
            # episode_id 在只读重放下不产生事件 key;给它只是让"给 memory 时要 episode_id"的校验不分叉
            scratch = _verify_op(lambda: scratch_store_factory(scratch_root / f"{len(built)}"))
            _verify_op(lambda: scratch.restore_from(agent_pid, verify_snap))
            port = _verify_op(lambda: scratch.open(agent_pid))
            inst = instantiate(
                backend_factory(model_id, ckpt), skills=lib.skills + tuple(extra),
                memory=port, episode_id=f"{env.id}/verify", interlocutors=interlocutors,
                replay_read_only=True, library_version=lib.version, profile=profile,
            )
            built.append(inst)
            return inst

        try:
            try:
                _verify_op(lambda: verify_snap.parent.mkdir(parents=True, exist_ok=True))
                _verify_op(lambda: memory_store.backup_to(agent_pid, verify_snap))
                verifications = [
                    verify(d, findings, episodes, base_instance_factory=instance_with,
                           personas=personas, scenarios=scenarios)
                    for d in drafts
                ]
            except MemoryFailureError as exc:
                # 终审 I-2:重放里实例的 recall、digest(以及 _verify_op 原样放过的瞬态)抛的记忆异常不能越过
                # 处理器、让守护进程以 traceback 退出。瞬态改抛 DeferredError(轮次留 pending,不写树,退避重试);
                # 其余判 T3(失败点 mindagent.rounds.verify)。都只报类名。下面的 finally 照样关实例、删快照与临时目录
                cls = type(exc).__name__
                if exc.transient:
                    raise DeferredError(f"verify replay: {cls}") from exc
                raise AgentRoundFailedError(f"verify replay: {cls}", point="mindagent.rounds.verify") from exc
        finally:
            for inst in built:
                try:
                    inst.close()
                except Exception as exc:
                    # 终审 M-1:验证实例只读、临时副本随后整个删掉,关不上不影响轮次结果,也不能盖掉在途的异常
                    _log.warning("verify replay: closing a replay instance failed: %s", type(exc).__name__)
            verify_snap.unlink(missing_ok=True)
            shutil.rmtree(scratch_root, ignore_errors=True)
            _rmdir_if_empty(verify_snap.parent)
        thresholds = Thresholds(config.min_fired_on_motivating, config.max_fired_elsewhere)
        written = package(store, env.id, findings, drafts, verifications, thresholds=thresholds,
                          run_id=run_id, bound_model_id=model_id)
        _, dropped = screen(drafts, verifications, thresholds)
        dropped += stale_proposals(store, env.id, len(written))
        env.payload.update({
            "episode_ids": [e.id for e in episodes],
            "findings": [f.to_dict() for f in findings],
            "proposals": [p.id for p in written],
            "dropped": dropped,
        })
        assert_transition("nonreview", env.status, "executed")
        env.status = "executed"
        store.write_product(agent_round_file(model_id), env, f"mindagent: {env.id} executed")

    def _fail_unreadable(store: GitStore, model_id: str, rollback: Rollback) -> None:
        """轮次产物在但读不出(计划 8 Ruling 20:视图按"没有轮次"再次触发):用新信封整份重写为
        failed(T3),按 fail_once 的守卫记一次失败记录。run id 取绑定模型产物里的那个;模型本身也读
        不出或不合法时 run id 为空,只在轮次产物上留原因。"""
        try:
            _, run_id, _ = _read_model(store, model_id)
        except (AgentRoundFailedError, *UNREADABLE):
            run_id = ""
        reason = "unreadable round product"
        point = "mindagent.rounds.read"
        env = _round_envelope(model_id, {"model_id": model_id, "run_id": run_id, "failure": None})
        _close_round(store, env, reason, category="T3", point=point, rollback=rollback, run_id=run_id, record=True)

    return handle
