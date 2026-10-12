"""构造训练指令(设计 §4.2、§9):runs/<new_run_id>/directive.yaml。"""
from __future__ import annotations

import copy

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from mindtrainer.data_spec import (
    InvalidDataSpecError,
    check_data_spec_against_config,
    parse_data_spec,
    required_stages,
)
from mindtrainer.training_config import (
    InvalidTrainingConfigError,
    TrainingConfig,
    load_config_from_directive,
    validate_training_config,
)
from trainscheduler.assemble import directive_file
from trainscheduler.errors import DirectiveConstructionError
from trainscheduler.resolve import ResolvedAction

NOW = "2026-10-09T00:00:00Z"


def manifest_id(run_id: str, stage: str) -> str:
    """与 simulator.dataset.build_manifest 的 id 规则一致:dsm-<数据所在的 run>-<阶段>。"""
    return f"dsm-{run_id}-{stage}"


def directive_payload(resolved: ResolvedAction, *, new_run_id: str, planner_decision_id: str) -> dict:
    cfg = copy.deepcopy(resolved.training_config)
    payload: dict = {
        "target_dimension": resolved.target_dimension,
        "recipe_ref": resolved.recipe_ref,
        "training_config": cfg,
        "planner_decision_ref": planner_decision_id,
    }
    if resolved.action == "new":
        stages = required_stages(TrainingConfig.from_dict(cfg))
        cfg["stage_manifest_refs"] = {stage: manifest_id(new_run_id, stage) for stage in stages}
        payload["data_spec"] = resolved.data_spec.to_dict()
    return payload


def _envelope(new_run_id: str, payload: dict, status: str, links: list[str]) -> Envelope:
    return Envelope(
        id=new_run_id, type="directive", version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=CURRENT_SCHEMA_VERSION, links=links, payload=payload,
    )


def check_directive(env: Envelope) -> None:
    """指令必须能被 MindTrainer 派发器读回并通过校验;new 的 data_spec 必须够合成它点名的阶段。"""
    try:
        cfg = load_config_from_directive(env)
    except InvalidTrainingConfigError as exc:
        raise DirectiveConstructionError(str(exc)) from exc
    reason = validate_training_config(cfg)
    if reason is not None:
        raise DirectiveConstructionError(f"directive {env.id}: {reason}")
    if "data_spec" in env.payload:
        try:
            spec = parse_data_spec(env.payload["data_spec"])
        except InvalidDataSpecError as exc:
            raise DirectiveConstructionError(f"directive {env.id}: {exc}") from exc
        reason = check_data_spec_against_config(spec, cfg, required_stages(cfg))
        if reason is not None:
            raise DirectiveConstructionError(f"directive {env.id}: {reason}")


def construct_directive(
    store: GitStore,
    resolved: ResolvedAction,
    *,
    new_run_id: str,
    confirm: bool,
    planner_decision_id: str,
) -> str:
    """写指令并返回相对路径。confirm 为真写 awaiting_confirmation,否则 pending。
    校验不过抛 DirectiveConstructionError,树里什么都不写。目标位置已有指令:由同一决策写的
    视为已完成(原样返回路径),别人写的拒绝覆盖。"""
    if not isinstance(confirm, bool):
        raise TypeError(f"confirm must be a bool, got {type(confirm).__name__}")
    path = directive_file(new_run_id)
    if store.exists(path):
        existing = store.read_product(path)
        if existing.payload.get("planner_decision_ref") == planner_decision_id:
            return path
        raise DirectiveConstructionError(
            f"{path} already exists and was not written by {planner_decision_id}; refusing to overwrite"
        )
    payload = directive_payload(resolved, new_run_id=new_run_id, planner_decision_id=planner_decision_id)
    links = [planner_decision_id] + ([resolved.recipe_ref] if resolved.recipe_ref else [])
    env = _envelope(new_run_id, payload, "awaiting_confirmation" if confirm else "pending", links)
    check_directive(env)
    store.write_product(path, env, f"trainscheduler: directive {new_run_id} ({resolved.action})")
    return path
