"""领域校验与动作解析(设计 §7.2):把一条 PlannerDecision 变成一份完整、通过校验的训练配置。"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from controlplane.gitstore import GitStore
from controlplane.models import MissingCheckpointRefError, latest_promoted_checkpoint
from mindtrainer.data_spec import DataSpec
from mindtrainer.training_config import InvalidTrainingConfigError, TrainingConfig, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import InteractDefaults, SchedulerConfig, TrainingDefaults
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


def validate_decision(
    store: GitStore,
    decision: PlannerDecision,
    *,
    config: SchedulerConfig,
    defaults: TrainingDefaults | None = None,
) -> str | None:
    """领域有效性。返回第一条违规(非空字符串),全部合格返回 None。只读树。
    new 加 interact 要 allow_interact 开着,且 defaults 带 interact 段;没传 defaults 等于没有该段。"""
    if decision.action == "new":
        if decision.mode == "interact":
            if not config.allow_interact:
                return "mode 'interact' is not allowed (scheduler.allow_interact is false); use 'batch'"
            if defaults is None or defaults.interact is None:
                return (
                    "a new interact round needs the interact section in training_defaults.yaml, "
                    "which is missing; use mode 'batch' or clone an interact recipe"
                )
        return None
    reason, recipe = _recipe_violation(store, decision, config)
    if reason is not None:
        return reason
    if decision.action == "replay" and decision.perturbation:
        return "replay takes the recipe verbatim; perturbation must be empty"
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


def _interact_config(cfg: dict, section: InteractDefaults) -> tuple[dict, DataSpec | None]:
    """以默认 batch 配置 cfg(调用方已深拷贝)为底,按 interact 段改写成 interact 配置,返回 (配置, data_spec)。

    点名路:情境取自段,每个角色位的 persona_id 取自 persona_ids,sft_steps 强制为 0
    (没有 data_spec 可合成 SFT 数据,也不做冷启动),data_spec 为 None。
    生成路:scenario_ids 留空,由派发器生成后回写;bindings 照点名路按段里 slots 的顺序展开,但 persona_id 一律为空串,
    由派发器配上生成出的人格后回写(角色位布局只能在这里留下,派发器读不到调度器的 interact 段)。
    sft_steps 保持默认,data_spec 取自段。
    两条路都写未解析的 memory(只带段里的 seed,digests 与 runtime 为空),起点记忆由派发器解出。
    角色位的值照段里写的搬过来,不替人改正:训练位必须是 self_play 且不带 model_ref,写错由展开后的校验报出。"""
    cfg["mode"] = "interact"
    cfg["trained_slot"] = section.trained_slot
    cfg["max_turns_per_episode"] = section.max_turns_per_episode
    # 未解析的起点记忆:只带种子,digests 与 runtime 由派发器解出后写进训练记录(指令不回写)
    cfg["memory"] = {"seed": section.memory_seed, "digests": {}, "runtime": {}}
    if section.data_spec is not None:
        cfg["scenario_ids"] = []
        cfg["bindings"] = _bindings(section, {slot["slot_id"]: "" for slot in section.slots})
        return cfg, section.data_spec
    cfg["sft_steps"] = 0
    cfg["scenario_ids"] = list(section.scenario_ids)
    cfg["bindings"] = _bindings(section, section.persona_ids)
    return cfg, None


def _bindings(section: InteractDefaults, persona_ids: dict[str, str]) -> list[dict]:
    """每个角色位一条绑定,按段里 slots 的顺序;driver、model_ref 只在角色位有时才带。"""
    return [{"persona_id": persona_ids[slot["slot_id"]], **copy.deepcopy(slot)} for slot in section.slots]


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
    """new batch:默认配置 + 最新已晋级基座(没有就用默认配置自带的初始基座)+ 默认 data_spec。
    new interact:默认配置为底,按 interact 段改写(见 _interact_config);基座规则同 batch。
    clone:配方的配置 + 扰动 + 最新已晋级基座;复用配方锚定的清单,不带 data_spec。
    replay:配方的配置原样,不做任何替换,不带 data_spec。
    确定性做不成抛 ResolveError。"""
    violation = validate_decision(store, decision, config=config, defaults=defaults)
    if violation is not None:
        raise ResolveError(violation)
    if decision.action == "new":
        cfg = copy.deepcopy(defaults.training_config)
        data_spec = defaults.data_spec
        if decision.mode == "interact":
            cfg, data_spec = _interact_config(cfg, defaults.interact)
        elif decision.mode != "batch":
            raise ResolveError(f"new {decision.mode!r} rounds cannot be resolved: only batch and interact exist")
        base, model_id = _promoted_base(store)
        if base is not None:
            cfg["base_checkpoint_ref"] = base
        if decision.mode == "interact" and data_spec is None:
            # 点名路的配置已完整,当场过严格校验;生成路的 scenario_ids 与每条 persona_id 要等派发器回写,由指令构造的宽口径校验
            check_training_config(cfg, "new interact")
        return ResolvedAction(decision.target_dimension, "new", "", cfg, data_spec, model_id)
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
