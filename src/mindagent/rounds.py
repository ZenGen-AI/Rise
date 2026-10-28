"""轮次处理器 start_agent_round(闭环集成设计 §3.2、§8.3)。

流程:读绑定模型 → 把被取代的 pending 旧轮次置 failed → 读当前正式库 → 写 agent_round(pending)
→ 部署 episode → 分析 → 提案 → 验证 → 打包 → 轮次置 executed。

失败分类(与派发器同一约定):
- 确定性错误(配置、树里的数据、解析耗尽、不可重跑的会话失败)与任何 BridgeError(R18:T1 是
  桥接已用尽自己的重试,T2 是确定性故障)→ 轮次 failed,并经 ledger.record_failure 写一条 T3
  失败记录(task_id 为 agent-<model>)。
- 非桥接的瞬态(会话 T1 重跑用尽)与其余异常原样上抛,轮次保持 pending。按 R19,调和循环对
  pending 的轮次在下一个 tick 重新触发 start_agent_round,所以本处理器的重入必须幂等:已落盘的
  部署 episode 不重跑,提案按确定性 id 重写。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone

from actio.errors import ActioDataError
from actio.instance import instantiate
from actio.skills import Skill
from controlplane.envelope import BigRef, Envelope, InvalidBigRefError, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.models import version_of_file, version_of_id
from controlplane.paths import CONTROL_PLANE_ROOT, agent_round_file, model_file, persona_file, scenario_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from inference_bridge import BridgeError
from mindagent.analysis import analyze
from mindagent.config import AgentConfig
from mindagent.deployment import DeploymentFailedError, run_deployment_episodes
from mindagent.errors import AgentRoundFailedError, MindAgentDataError, ProposalParseError
from mindagent.failures import UNREADABLE, fail_once, read_or_none
from mindagent.library import LibraryState, load_library
from mindagent.packaging import Thresholds, package, screen, stale_proposals
from mindagent.proposals import BRIDGE_GENERATOR, PROMPT_TEMPLATE_VERSION, RULE_GENERATOR, propose
from mindagent.verify import verify
from simulator.datamodel import Persona
from simulator.store_adapter import read_scenario

ROUND_TYPE = "agent_round"
_ROUND_FILE = re.compile(r"agent-round-(zengen-v(\d+))\.yaml$")

BackendFactory = Callable[[str, "BigRef | None"], object]
PersonasLoader = Callable[[GitStore, list[str]], dict[str, Persona]]
Handler = Callable[[GitStore, Action], None]


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


def supersede_stale_rounds(store: GitStore, latest_version: int) -> list[str]:
    """把绑定模型版本低于 latest_version、仍为 pending 的轮次置 failed(superseded)。返回被置的轮次 id。"""
    out: list[str] = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/agent_rounds"):
        match = _ROUND_FILE.search(rel)
        if match is None or int(match.group(2)) >= latest_version:
            continue
        env = read_or_none(store, rel)
        if env is None:
            continue  # 读不出:留给它自己那个模型的 start_agent_round 判损坏(计划 8 视它为可触发)
        if env.status == "pending":
            _fail_round(store, env, f"superseded by zengen-v{latest_version}",
                        category="superseded", point="mindagent.rounds.supersede")
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
                     lib: LibraryState, config: AgentConfig, generator: str) -> dict:
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


def make_start_round_handler(
    *,
    config: AgentConfig,
    client_factory: Callable[[], object],
    backend_factory: BackendFactory,
    personas_loader: PersonasLoader,
    bridge=None,
) -> Handler:
    """backend_factory(model_id, checkpoint_ref) 每次调用返回一个新的模型后端(普通或带指导语的);
    personas_loader(store, persona_ids) 返回 {persona_id: Persona}。bridge 缺省走规则模板。"""
    generator = RULE_GENERATOR if bridge is None else BRIDGE_GENERATOR

    def handle(store: GitStore, action: Action) -> None:
        model_id = action.target_id
        rel = agent_round_file(model_id)
        existing = read_or_none(store, rel)
        if existing is None and store.exists(rel):
            _fail_unreadable(store, model_id)
            return
        if existing is not None and existing.status != "pending":
            return  # executed 或 failed:终态,空操作
        latest = _latest_effective_version(store)
        supersede_stale_rounds(store, latest)

        try:
            version, run_id, ckpt = _read_model(store, model_id)
        except AgentRoundFailedError as exc:
            _fail_round(store, _existing_or_new(store, model_id, ""), exc.reason, category="T3", point=exc.point)
            return  # 没有 run 可挂失败记录;原因写在轮次产物里
        if version < latest:
            _fail_round(store, _existing_or_new(store, model_id, run_id), f"superseded by zengen-v{latest}",
                        category="superseded", point="mindagent.rounds.supersede")
            return

        lib: LibraryState | None = None
        try:
            lib = load_library(store)
            payload = _binding_payload(model_id, version, run_id, ckpt, lib, config, generator)
            env = _round_envelope(model_id, payload)
            if existing is not None:
                env.created_at = existing.created_at
            store.write_product(rel, env, f"mindagent: {env.id} pending")
            _check_inputs(store, config)
            _run(store, env, lib, run_id, model_id, ckpt)
        except DeploymentFailedError as exc:
            if exc.transient:
                raise
            _record(store, rel, run_id, model_id, f"deployment: {exc}", "mindagent.deployment", exc.attempts - 1)
        except ProposalParseError as exc:
            _record(store, rel, run_id, model_id, str(exc), "mindagent.proposals", exc.attempts - 1)
        except BridgeError as exc:
            # R18:桥接已经自己退避重试过,T1 也升级为 T3,不留 pending 让守护循环无限空转外部推理
            # retries 是已重试次数(计划 1):attempts 是总尝试次数
            _record(store, rel, run_id, model_id, f"bridge {exc.category}: {exc.reason}",
                    "mindagent.proposals", max(exc.attempts - 1, 0))
        except AgentRoundFailedError as exc:
            _record(store, rel, run_id, model_id, exc.reason, exc.point, 0)
        except (MindAgentDataError, ActioDataError) as exc:
            _record(store, rel, run_id, model_id, f"{type(exc).__name__}: {exc}", "mindagent.rounds", 0)

    def _run(store: GitStore, env: Envelope, lib: LibraryState, run_id: str, model_id: str,
             ckpt: BigRef | None) -> None:
        def instance_with(extra: tuple[Skill, ...] = ()):
            return instantiate(
                backend_factory(model_id, ckpt), skills=lib.skills + tuple(extra), memories=(),
                library_version=lib.version, profile={"model_id": model_id, "round_id": env.id},
            )

        persona_ids = sorted({b.persona_id for b in config.bindings})
        personas = personas_loader(store, persona_ids)
        episodes = run_deployment_episodes(
            store, instance_with(), client_factory, run_id=run_id,
            scenario_ids=config.scenario_ids, personas=personas, bindings=config.bindings,
            model_id=model_id, max_turns=config.max_turns,
            episodes_per_scenario=config.episodes_per_scenario,
            max_retries_per_episode=config.max_retries_per_episode,
        )
        findings = analyze(episodes, min_support=config.min_support, min_ratio=config.min_ratio, skills=lib.skills)
        drafts = propose(findings, bridge=bridge, run_id=run_id, task_id=f"{task_id_for(model_id)}-propose",
                         max_parse_retries=config.max_parse_retries)
        scenarios = {sid: read_scenario(store, sid) for sid in config.scenario_ids}
        verifications = [
            verify(d, findings, episodes, base_instance_factory=instance_with,
                   personas=personas, scenarios=scenarios)
            for d in drafts
        ]
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

    def _record(store: GitStore, rel: str, run_id: str, model_id: str, reason: str, point: str, retries: int) -> None:
        # 读库失败时轮次产物还没写:先补一份最小的 pending 产物再置 failed
        _fail_round(store, _existing_or_new(store, model_id, run_id), reason, category="T3", point=point)
        fail_once(store, run_id, task_id=task_id_for(model_id), category="T3",
                  point=point, retries=max(retries, 0), reason=reason)

    def _fail_unreadable(store: GitStore, model_id: str) -> None:
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
        _fail_round(store, env, reason, category="T3", point=point)
        fail_once(store, run_id, task_id=task_id_for(model_id), category="T3",
                  point=point, retries=0, reason=reason)

    return handle
