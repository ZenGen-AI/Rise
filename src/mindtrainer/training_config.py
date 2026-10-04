from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from controlplane.envelope import BigRef, Envelope
from simulator.datamodel import RoleBinding

MODES = ("batch", "interact")
STAGES = ("sft", "grpo_opd")
ATTRIBUTIONS = ("outcome_split", "judge")
COMBINATIONS = ("sum", "product")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InvalidTrainingConfigError(ValueError):
    """训练配置缺字段或违反校验规则。"""


@dataclass(frozen=True)
class RewardConfig:
    """奖励配置(设计 §4)。不含任何凭证字段:judge 只存模型标识与 rubric 版本。"""

    components: list[str]
    attribution: str  # "outcome_split" | "judge"
    combination: str  # "sum" | "product"
    # outcome_split 专用:分量 -> 命中关键词列表
    outcome_rules: dict[str, list[str]] = field(default_factory=dict)
    gamma: float = 1.0  # outcome_split 专用:1.0 各训练轮同分;<1 越靠近结尾越高
    # judge 专用
    judge_model_id: str | None = None
    rubric_version: str | None = None  # judge 的 rubric prompt 版本,必填

    def to_dict(self) -> dict:
        return {
            "components": list(self.components),
            "attribution": self.attribution,
            "combination": self.combination,
            "outcome_rules": {k: list(v) for k, v in self.outcome_rules.items()},
            "gamma": self.gamma,
            "judge_model_id": self.judge_model_id,
            "rubric_version": self.rubric_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RewardConfig":
        return cls(
            components=list(d["components"]),
            attribution=d["attribution"],
            combination=d["combination"],
            outcome_rules={k: list(v) for k, v in d.get("outcome_rules", {}).items()},
            gamma=d.get("gamma", 1.0),
            judge_model_id=d.get("judge_model_id"),
            rubric_version=d.get("rubric_version"),
        )


@dataclass(frozen=True)
class TrainingConfig:
    """训练配置(设计 §2.1,spec §5.1 要求精确锚定的项)。内联在 directive payload 的
    "training_config" 键下;RecipeStore 落地后改成 recipe_ref 引用,结构不变。"""

    mode: str  # "batch" | "interact"
    trained_slot: str  # 被训练的角色位;两种模式都必须明确
    base_checkpoint_ref: BigRef  # 起点权重
    teacher_model_ref: BigRef  # OPD 教师;必须是带 sha256 的 BigRef,不是字符串
    reward: RewardConfig
    sft_steps: int  # 0 = 跳过冷启动
    grpo_steps: int
    sft_batch_size: int
    groups_per_step: int  # 每个 GRPO step 吃几个完整组
    group_size: int  # 每组样本数;interact 下即每个 scenario 连续 rollout 的条数
    grpo_config: dict  # 学习率等,透传给执行器
    opd_coefficient: float  # >= 0,spec §3.2 明确要求可调
    checkpoint_every_steps: int
    allow_resume: bool
    # 以下均有默认值
    stage_manifest_refs: dict[str, str] = field(default_factory=dict)  # {"sft": id, "grpo_opd": id}
    scenario_ids: list[str] = field(default_factory=list)  # interact 专用
    bindings: list[RoleBinding] = field(default_factory=list)  # interact 专用
    max_turns_per_episode: int = 0  # interact 专用
    max_retries_per_episode: int = 2  # interact 专用
    min_group_size: int = 2
    max_resume_attempts: int = 3

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "trained_slot": self.trained_slot,
            "base_checkpoint_ref": self.base_checkpoint_ref.to_dict(),
            "teacher_model_ref": self.teacher_model_ref.to_dict(),
            "reward": self.reward.to_dict(),
            "sft_steps": self.sft_steps,
            "grpo_steps": self.grpo_steps,
            "sft_batch_size": self.sft_batch_size,
            "groups_per_step": self.groups_per_step,
            "group_size": self.group_size,
            "grpo_config": copy.deepcopy(self.grpo_config),
            "opd_coefficient": self.opd_coefficient,
            "checkpoint_every_steps": self.checkpoint_every_steps,
            "allow_resume": self.allow_resume,
            "stage_manifest_refs": dict(self.stage_manifest_refs),
            "scenario_ids": list(self.scenario_ids),
            "bindings": [b.to_dict() for b in self.bindings],
            "max_turns_per_episode": self.max_turns_per_episode,
            "max_retries_per_episode": self.max_retries_per_episode,
            "min_group_size": self.min_group_size,
            "max_resume_attempts": self.max_resume_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingConfig":
        return cls(
            mode=d["mode"],
            trained_slot=d["trained_slot"],
            base_checkpoint_ref=BigRef.from_dict(d["base_checkpoint_ref"]),
            teacher_model_ref=BigRef.from_dict(d["teacher_model_ref"]),
            reward=RewardConfig.from_dict(d["reward"]),
            sft_steps=d["sft_steps"],
            grpo_steps=d["grpo_steps"],
            sft_batch_size=d["sft_batch_size"],
            groups_per_step=d["groups_per_step"],
            group_size=d["group_size"],
            grpo_config=copy.deepcopy(d["grpo_config"]),
            opd_coefficient=d["opd_coefficient"],
            checkpoint_every_steps=d["checkpoint_every_steps"],
            allow_resume=d["allow_resume"],
            stage_manifest_refs=dict(d.get("stage_manifest_refs", {})),
            scenario_ids=list(d.get("scenario_ids", [])),
            bindings=[RoleBinding.from_dict(b) for b in d.get("bindings", [])],
            max_turns_per_episode=d.get("max_turns_per_episode", 0),
            max_retries_per_episode=d.get("max_retries_per_episode", 2),
            min_group_size=d.get("min_group_size", 2),
            max_resume_attempts=d.get("max_resume_attempts", 3),
        )


_INT_FIELDS = (
    "sft_steps", "grpo_steps", "sft_batch_size", "groups_per_step", "group_size",
    "checkpoint_every_steps", "max_turns_per_episode", "max_retries_per_episode",
    "min_group_size", "max_resume_attempts",
)


def _check_types(cfg: TrainingConfig) -> str | None:
    """类型校验,在任何比较之前跑一遍。错误类型绝不让后面的代码看到:
    字符串当真值、浮点数当整数之类的输入会在这里被挡下,返回点名字段的可读原因,
    而不是让 '<' 之类的运算在后面抛裸 TypeError。"""
    if not isinstance(cfg.mode, str):
        return f"mode must be a str, got {type(cfg.mode).__name__}"
    if not isinstance(cfg.trained_slot, str):
        return f"trained_slot must be a str, got {type(cfg.trained_slot).__name__}"
    for name in _INT_FIELDS:
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an int, got {type(value).__name__}"
    if not isinstance(cfg.allow_resume, bool):
        return f"allow_resume must be a bool, got {type(cfg.allow_resume).__name__}"
    if isinstance(cfg.opd_coefficient, bool) or not isinstance(cfg.opd_coefficient, (int, float)):
        return f"opd_coefficient must be an int or float, got {type(cfg.opd_coefficient).__name__}"
    if isinstance(cfg.reward.gamma, bool) or not isinstance(cfg.reward.gamma, (int, float)):
        return f"reward.gamma must be an int or float, got {type(cfg.reward.gamma).__name__}"
    if not isinstance(cfg.grpo_config, dict):
        return f"grpo_config must be a dict, got {type(cfg.grpo_config).__name__}"
    try:
        json.dumps(cfg.grpo_config)
    except TypeError as exc:
        return f"grpo_config must be JSON serializable: {exc}"
    for name, ref in (
        ("base_checkpoint_ref", cfg.base_checkpoint_ref),
        ("teacher_model_ref", cfg.teacher_model_ref),
    ):
        if not isinstance(ref.sha256, str):
            return f"{name}.sha256 must be a str, got {type(ref.sha256).__name__}"
    if not isinstance(cfg.scenario_ids, list) or not all(
        isinstance(s, str) and s for s in cfg.scenario_ids
    ):
        return "scenario_ids must be a list of non-empty str"
    if not isinstance(cfg.stage_manifest_refs, dict):
        return f"stage_manifest_refs must be a dict, got {type(cfg.stage_manifest_refs).__name__}"
    return None


def _validate_reward(r: RewardConfig) -> str | None:
    if not r.components or len(set(r.components)) != len(r.components) or not all(r.components):
        return "reward.components must be non-empty and unique"
    if r.attribution not in ATTRIBUTIONS:
        return f"reward.attribution must be one of {list(ATTRIBUTIONS)}, got {r.attribution!r}"
    if r.combination not in COMBINATIONS:
        return f"reward.combination must be one of {list(COMBINATIONS)}, got {r.combination!r}"
    if r.attribution == "outcome_split":
        if set(r.outcome_rules) != set(r.components):
            return (
                f"reward.outcome_rules keys must equal components "
                f"{sorted(r.components)}, got {sorted(r.outcome_rules)}"
            )
        for name, words in r.outcome_rules.items():
            if not words or not all(isinstance(w, str) and w for w in words):
                return f"reward.outcome_rules[{name!r}] needs a non-empty keyword list"
        if not (0 < r.gamma <= 1):  # NaN 也会落到这里
            return f"reward.gamma must be in (0, 1], got {r.gamma}"
    if r.attribution == "judge":
        if not r.judge_model_id:
            return "reward.judge_model_id is required for judge attribution"
        if not r.rubric_version:
            return "reward.rubric_version is required for judge attribution"
    return None


def _validate_interact(cfg: TrainingConfig) -> str | None:
    if not cfg.scenario_ids or len(set(cfg.scenario_ids)) != len(cfg.scenario_ids):
        return "scenario_ids must be non-empty and unique"
    if cfg.groups_per_step > len(cfg.scenario_ids):
        # 同一 scenario 在一步里出现两次,两批 rollout 会因 group_id 相同被并成一组
        return (
            f"groups_per_step {cfg.groups_per_step} exceeds number of scenario_ids "
            f"{len(cfg.scenario_ids)}"
        )
    if cfg.max_turns_per_episode < 1:
        return f"max_turns_per_episode must be >= 1, got {cfg.max_turns_per_episode}"
    if not cfg.bindings:
        return "bindings must be non-empty"
    slots = [b.slot_id for b in cfg.bindings]
    duplicated = sorted({s for s in slots if slots.count(s) > 1})
    if duplicated:
        return f"duplicate slot_id in bindings: {duplicated}"
    by_slot = {b.slot_id: b for b in cfg.bindings}
    trained = by_slot.get(cfg.trained_slot)
    if trained is None:
        return f"trained_slot {cfg.trained_slot!r} is not among bound slots {sorted(by_slot)}"
    if trained.model_backend != "self_play":
        return f"trained slot {cfg.trained_slot!r} must be self_play, got {trained.model_backend!r}"
    if trained.model_ref is not None:
        return f"trained slot {cfg.trained_slot!r} must not carry model_ref {trained.model_ref!r}"
    for b in cfg.bindings:
        if b.slot_id == cfg.trained_slot:
            continue
        if b.model_backend == "self_play":
            return f"non-trained slot {b.slot_id!r} must not be self_play"
        if b.model_backend not in ("frozen_reference", "external_api"):
            return (
                f"non-trained slot {b.slot_id!r} model_backend must be "
                f"frozen_reference or external_api, got {b.model_backend!r}"
            )
        if not b.model_ref:
            # 事后追查 reward hacking 要靠它:不记对手是谁,就无法判断是不是练偏了
            return f"non-trained slot {b.slot_id!r} needs a non-empty model_ref"
    return None


def validate_training_config(cfg: TrainingConfig) -> str | None:
    """返回第一条违规的可读原因;全部合法返回 None。纯函数。

    类型检查在任何比较之前跑:任何字段的错误类型都绝不抛异常,一律返回可读原因
    (原因里含字段名和 "must be"),不能以裸 TypeError 或静默通过的形式逃出去。"""
    type_reason = _check_types(cfg)
    if type_reason is not None:
        return type_reason
    if cfg.mode not in MODES:
        return f"mode must be one of {list(MODES)}, got {cfg.mode!r}"
    if not cfg.trained_slot:
        return "trained_slot must be non-empty"
    if cfg.sft_steps < 0:
        return f"sft_steps must be >= 0, got {cfg.sft_steps}"
    if cfg.grpo_steps < 0:
        return f"grpo_steps must be >= 0, got {cfg.grpo_steps}"
    if cfg.sft_steps + cfg.grpo_steps < 1:
        return "need at least one training step (sft_steps + grpo_steps >= 1)"
    if cfg.sft_batch_size < 1:
        return f"sft_batch_size must be >= 1, got {cfg.sft_batch_size}"
    if cfg.groups_per_step < 1:
        return f"groups_per_step must be >= 1, got {cfg.groups_per_step}"
    if cfg.min_group_size < 2:
        # 一组只有 1 条时优势恒为 0,训练会静默地什么都学不到
        return f"min_group_size must be >= 2, got {cfg.min_group_size}"
    if cfg.group_size < cfg.min_group_size:
        return f"group_size {cfg.group_size} is below min_group_size {cfg.min_group_size}"
    if cfg.checkpoint_every_steps < 1:
        return f"checkpoint_every_steps must be >= 1, got {cfg.checkpoint_every_steps}"
    if not (math.isfinite(cfg.opd_coefficient) and cfg.opd_coefficient >= 0):
        return f"opd_coefficient must be a finite number >= 0, got {cfg.opd_coefficient}"
    if cfg.max_retries_per_episode < 0:
        return f"max_retries_per_episode must be >= 0, got {cfg.max_retries_per_episode}"
    if cfg.max_resume_attempts < 0:
        return f"max_resume_attempts must be >= 0, got {cfg.max_resume_attempts}"
    for name, ref in (
        ("base_checkpoint_ref", cfg.base_checkpoint_ref),
        ("teacher_model_ref", cfg.teacher_model_ref),
    ):
        if not _SHA256.match(ref.sha256):
            return f"{name}.sha256 must be 64 lowercase hex chars, got {ref.sha256!r}"
    unknown = sorted(set(cfg.stage_manifest_refs) - set(STAGES))
    if unknown:
        return f"stage_manifest_refs has unknown stage(s) {unknown}; allowed {list(STAGES)}"
    if cfg.sft_steps > 0 and not cfg.stage_manifest_refs.get("sft"):
        return "stage_manifest_refs['sft'] is required when sft_steps > 0"
    if cfg.mode == "batch" and cfg.grpo_steps > 0 and not cfg.stage_manifest_refs.get("grpo_opd"):
        return "stage_manifest_refs['grpo_opd'] is required for batch mode when grpo_steps > 0"
    reason = _validate_reward(cfg.reward)
    if reason is not None:
        return reason
    if cfg.mode == "interact":
        return _validate_interact(cfg)
    return None


def ensure_valid_config(cfg: TrainingConfig) -> TrainingConfig:
    reason = validate_training_config(cfg)
    if reason is not None:
        raise InvalidTrainingConfigError(reason)
    return cfg


def config_hash(cfg: TrainingConfig) -> str:
    """配置的 sha256:规范化 JSON(键排序、无空白),不依赖字典顺序或 YAML 排版。

    这一步在 validate_training_config 之前就会被下半段编排器调用,所以
    grpo_config 之类带不可序列化值的配置在这里必须抛 InvalidTrainingConfigError
    (确定性失败),不能是裸 TypeError(会被当成瞬态失败重试)。"""
    try:
        text = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError as exc:
        raise InvalidTrainingConfigError(f"grpo_config must be JSON serializable: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def total_steps(cfg: TrainingConfig) -> int:
    return cfg.sft_steps + cfg.grpo_steps


def phase_at(step: int, cfg: TrainingConfig) -> str:
    """全局 step -> 阶段。阶段不另存,由它推出,避免两处不一致。
    sft_steps == 0 时从第一步起就是 grpo_opd。越界抛 ValueError,免得差一错误悄悄滑过。"""
    if not (0 <= step < total_steps(cfg)):
        raise ValueError(f"step {step} out of range [0, {total_steps(cfg)})")
    return "sft" if step < cfg.sft_steps else "grpo_opd"


def load_config_from_directive(env: Envelope) -> TrainingConfig:
    """从 directive 产物读出训练配置。缺键、缺字段或 training_config 本身不是 dict
    一律抛 InvalidTrainingConfigError;不替调用方校验,校验见
    validate_training_config / ensure_valid_config。"""
    raw = env.payload.get("training_config")
    if raw is None:
        raise InvalidTrainingConfigError(
            f"directive {env.id} has no 'training_config' in its payload"
        )
    if not isinstance(raw, dict):
        raise InvalidTrainingConfigError(
            f"directive {env.id}: training_config must be a dict, got {type(raw).__name__}"
        )
    try:
        return TrainingConfig.from_dict(raw)
    except KeyError as exc:
        raise InvalidTrainingConfigError(
            f"directive {env.id}: training_config is missing field {exc}"
        ) from exc
    except (TypeError, AttributeError) as exc:
        raise InvalidTrainingConfigError(
            f"directive {env.id}: training_config is malformed "
            f"({type(exc).__name__}: {exc})"
        ) from exc
