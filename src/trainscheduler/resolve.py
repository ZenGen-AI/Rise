"""领域校验与动作解析(设计 §7.2):把一条 PlannerDecision 变成一份完整、通过校验的训练配置。"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from controlplane.gitstore import GitStore
from controlplane.models import MissingCheckpointRefError, latest_promoted_checkpoint
from mindtrainer.data_spec import DataSpec
from mindtrainer.training_config import InvalidTrainingConfigError, TrainingConfig, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import SchedulerConfig, TrainingDefaults
from trainscheduler.errors import ResolveError
from trainscheduler.recipes_port import RecipeNotFoundError, get_recipe

CONSTRUCTIBLE_RECIPE_TYPES = ("batch", "interact")
_MISSING = object()


@dataclass(frozen=True)
class ResolvedAction:
    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    recipe_ref: str  # clone / replay 为配方 id;new 为空串
    training_config: dict  # TrainingConfig.to_dict() 的形状;new 不含 stage_manifest_refs
    data_spec: DataSpec | None  # 只有 new 带
    base_model_id: str | None  # 换上的已晋级基座(zengen-vN);没换为 None


def apply_perturbation(training_config: dict, perturbation: dict) -> dict:
    """把扰动套到 training_config 的深拷贝上。键是点路径;首段必须已在配置里,中间段必须是映射,
    叶子可以是新键(grpo_config 是透传给执行器的字典)。套不上抛 ResolveError。"""
    out = copy.deepcopy(training_config)
    for key in sorted(perturbation):
        parts = key.split(".")
        node = out
        for depth, part in enumerate(parts[:-1]):
            child = node.get(part, _MISSING) if isinstance(node, dict) else _MISSING
            if not isinstance(child, dict):
                where = ".".join(parts[: depth + 1])
                raise ResolveError(f"perturbation {key!r}: training_config.{where} is not a mapping")
            node = child
        if len(parts) == 1 and parts[0] not in out:
            raise ResolveError(f"perturbation {key!r}: training_config has no field {parts[0]!r}")
        node[parts[-1]] = copy.deepcopy(perturbation[key])
    return out


def check_training_config(training_config: dict, where: str) -> TrainingConfig:
    """from_dict + validate_training_config。任何不合格抛 ResolveError(信息带 where)。"""
    try:
        cfg = TrainingConfig.from_dict(training_config)
    except KeyError as exc:
        raise ResolveError(f"{where}: training_config is missing field {exc}") from exc
    except (TypeError, AttributeError, InvalidTrainingConfigError) as exc:
        raise ResolveError(f"{where}: training_config is malformed ({exc})") from exc
    reason = validate_training_config(cfg)
    if reason is not None:
        raise ResolveError(f"{where}: {reason}")
    return cfg


def _recipe_violation(store: GitStore, decision: PlannerDecision, config: SchedulerConfig):
    """返回 (违规原因, 配方信封)。"""
    try:
        recipe = get_recipe(store, decision.source_recipe_id)
    except RecipeNotFoundError:
        return f"recipe {decision.source_recipe_id!r} does not exist", None
    if recipe.status != "effective":
        return f"recipe {recipe.id} is {recipe.status}, only effective recipes can be reused", None
    recipe_type = recipe.payload.get("recipe_type")
    if recipe_type not in CONSTRUCTIBLE_RECIPE_TYPES:
        return f"recipe {recipe.id} is a {recipe_type!r} recipe; only batch and interact recipes can be reused", None
    if recipe_type == "interact" and not config.allow_interact:
        return f"recipe {recipe.id} is an interact recipe and allow_interact is off", None
    if recipe.payload.get("dimension") != decision.target_dimension:
        return (
            f"recipe {recipe.id} targets {recipe.payload.get('dimension')!r}, "
            f"not {decision.target_dimension!r}"
        ), None
    if not isinstance(recipe.payload.get("training_config"), dict):
        return f"recipe {recipe.id} has no training_config", None
    return None, recipe


def validate_decision(store: GitStore, decision: PlannerDecision, *, config: SchedulerConfig) -> str | None:
    """领域有效性。返回第一条违规(非空字符串),全部合格返回 None。只读树。"""
    if decision.action == "new":
        if decision.mode == "interact":
            if not config.allow_interact:
                return "mode 'interact' is not allowed (scheduler.allow_interact is false); use 'batch'"
            return (
                "a new interact round needs scenario_ids and bindings, which training_defaults "
                "does not provide; use mode 'batch' or clone an interact recipe"
            )
        return None
    reason, recipe = _recipe_violation(store, decision, config)
    if reason is not None:
        return reason
    for key in sorted(decision.perturbation):
        if key not in config.perturbable:
            return f"perturbation key {key!r} is not perturbable; allowed {list(config.perturbable)}"
    if decision.perturbation:
        try:
            perturbed = apply_perturbation(recipe.payload["training_config"], decision.perturbation)
            check_training_config(perturbed, f"perturbed {recipe.id}")
        except ResolveError as exc:
            return exc.reason
    return None


def _promoted_base(store: GitStore) -> tuple[dict | None, str | None]:
    try:
        base = latest_promoted_checkpoint(store)
    except MissingCheckpointRefError as exc:
        raise ResolveError(str(exc)) from exc
    if base is None:
        return None, None
    return base.checkpoint_ref.to_dict(), base.model_id


def resolve_action(
    store: GitStore,
    decision: PlannerDecision,
    *,
    config: SchedulerConfig,
    defaults: TrainingDefaults,
) -> ResolvedAction:
    """new:默认配置 + 最新已晋级基座(没有就用默认配置自带的初始基座)+ 默认 data_spec。
    clone:配方的配置 + 扰动 + 最新已晋级基座;复用配方锚定的清单,不带 data_spec。
    replay:配方的配置原样,不做任何替换,不带 data_spec。
    确定性做不成抛 ResolveError。"""
    violation = validate_decision(store, decision, config=config)
    if violation is not None:
        raise ResolveError(violation)
    if decision.action == "new":
        if decision.mode != "batch":
            raise ResolveError(
                f"new {decision.mode!r} rounds cannot be resolved: training_defaults only holds a batch config"
            )
        cfg = copy.deepcopy(defaults.training_config)
        base, model_id = _promoted_base(store)
        if base is not None:
            cfg["base_checkpoint_ref"] = base
        return ResolvedAction(decision.target_dimension, "new", "", cfg, defaults.data_spec, model_id)
    recipe = get_recipe(store, decision.source_recipe_id)
    cfg = copy.deepcopy(recipe.payload["training_config"])
    model_id = None
    if decision.action == "clone":
        cfg = apply_perturbation(cfg, decision.perturbation)
        base, model_id = _promoted_base(store)
        if base is not None:
            cfg["base_checkpoint_ref"] = base
    check_training_config(cfg, f"{decision.action} of {recipe.id}")
    return ResolvedAction(decision.target_dimension, decision.action, recipe.id, cfg, None, model_id)
