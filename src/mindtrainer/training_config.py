from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field

from controlplane.envelope import SHA256_HEX, BigRef, Envelope, InvalidBigRefError
from simulator.datamodel import RoleBinding

MODES = ("batch", "interact")
STAGES = ("sft", "grpo_opd")
ATTRIBUTIONS = ("outcome_split", "judge")
COMBINATIONS = ("sum", "product")


class InvalidTrainingConfigError(ValueError):
    """训练配置缺字段或违反校验规则。"""


def _expect(value, kind: type, label: str):
    """from_dict 用:容器类型不对就点名字段地抛 TypeError,而不是把字符串悄悄拆成字符、
    把空列表悄悄当成空字典。load_config_from_directive 会把它包成 InvalidTrainingConfigError。"""
    if not isinstance(value, kind):
        raise TypeError(f"{label} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _ref_from_dict(name: str, value) -> BigRef:
    try:
        return BigRef.from_dict(_expect(value, dict, name))
    except InvalidBigRefError as exc:
        # 点名是哪个引用:BigRef 自己只知道字段,不知道它在配置里叫什么。仍是 TypeError 子类,
        # load_config_from_directive 照旧把它包成 InvalidTrainingConfigError。
        raise InvalidBigRefError(f"{name}: {exc}") from exc


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
        rules = _expect(d.get("outcome_rules", {}), dict, "reward.outcome_rules")
        return cls(
            components=list(_expect(d["components"], list, "reward.components")),
            attribution=d["attribution"],
            combination=d["combination"],
            outcome_rules={
                k: list(_expect(v, list, f"reward.outcome_rules[{k!r}]")) for k, v in rules.items()
            },
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
            base_checkpoint_ref=_ref_from_dict("base_checkpoint_ref", d["base_checkpoint_ref"]),
            teacher_model_ref=_ref_from_dict("teacher_model_ref", d["teacher_model_ref"]),
            reward=RewardConfig.from_dict(_expect(d["reward"], dict, "reward")),
            sft_steps=d["sft_steps"],
            grpo_steps=d["grpo_steps"],
            sft_batch_size=d["sft_batch_size"],
            groups_per_step=d["groups_per_step"],
            group_size=d["group_size"],
            grpo_config=copy.deepcopy(d["grpo_config"]),
            opd_coefficient=d["opd_coefficient"],
            checkpoint_every_steps=d["checkpoint_every_steps"],
            allow_resume=d["allow_resume"],
            stage_manifest_refs=dict(
                _expect(d.get("stage_manifest_refs", {}), dict, "stage_manifest_refs")
            ),
            scenario_ids=list(_expect(d.get("scenario_ids", []), list, "scenario_ids")),
            bindings=[
                RoleBinding.from_dict(_expect(b, dict, f"bindings[{i}]"))
                for i, b in enumerate(_expect(d.get("bindings", []), list, "bindings"))
            ],
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


def _short(value) -> str:
    """错误信息里展示的值:太长就截断;转字符串本身会抛(例如超过位数上限的大整数)时只写类型,
    免得拼错误信息的时候又抛出一个裸异常。"""
    try:
        text = repr(value)
    except Exception:  # 自定义对象的 __repr__ 可以抛任何异常
        return f"<{type(value).__name__}>"
    return text if len(text) <= 48 else text[:45] + "..."


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:  # 大整数转不成浮点数
        return False


def _canonical_json(value) -> bytes:
    """config_hash 与类型校验共用的序列化:键排序、无空白、不转义非 ASCII,再编码成 UTF-8。
    两边走同一个函数,所以"校验放行却算不出哈希"不可能发生。键类型互不可比较、字符串里有孤立代理字符
    (编码成 UTF-8 才失败)这类只有排序或编码才暴露的毛病,校验阶段就能拦下。"""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def _check_ref(name: str, ref) -> str | None:
    # 字段本身由 BigRef 在构造时校验(I8),这里只需确认它确实是 BigRef。
    if not isinstance(ref, BigRef):
        return f"{name} must be a BigRef, got {type(ref).__name__}"
    return None


def _check_reward_types(r) -> str | None:
    if not isinstance(r, RewardConfig):
        return f"reward must be a RewardConfig, got {type(r).__name__}"
    if not isinstance(r.components, list) or not all(isinstance(c, str) for c in r.components):
        return f"reward.components must be a list of str, got {_short(r.components)}"
    if not isinstance(r.outcome_rules, dict):
        return f"reward.outcome_rules must be a dict, got {type(r.outcome_rules).__name__}"
    for key, words in r.outcome_rules.items():
        if not isinstance(key, str):
            return f"reward.outcome_rules keys must be str, got {type(key).__name__}"
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            return f"reward.outcome_rules[{key!r}] must be a list of str, got {_short(words)}"
    if isinstance(r.gamma, bool) or not isinstance(r.gamma, (int, float)):
        return f"reward.gamma must be an int or float, got {type(r.gamma).__name__}"
    for name in ("judge_model_id", "rubric_version"):
        value = getattr(r, name)
        if value is not None and not isinstance(value, str):
            return f"reward.{name} must be a str or None, got {type(value).__name__}"
    return None


def _check_bindings(bindings) -> str | None:
    if not isinstance(bindings, list):
        return f"bindings must be a list, got {type(bindings).__name__}"
    for i, b in enumerate(bindings):
        if not isinstance(b, RoleBinding):
            return f"bindings[{i}] must be a RoleBinding, got {type(b).__name__}"
        for name in ("slot_id", "persona_id", "model_backend"):
            value = getattr(b, name)
            if not isinstance(value, str):
                return f"bindings[{i}].{name} must be a str, got {type(value).__name__}"
        if b.model_ref is not None and not isinstance(b.model_ref, str):
            return f"bindings[{i}].model_ref must be a str or None, got {type(b.model_ref).__name__}"
    return None


def _check_types(cfg: TrainingConfig) -> str | None:
    """类型校验,在任何比较之前跑一遍。对 YAML 或 from_dict 能产出的值(内建的 str、int、float、bool、None、list、dict)
    永不抛异常;字段里若被塞进 __deepcopy__ 之类会抛任意异常的自定义对象,to_dict 的深拷贝仍可能穿透。错误类型绝不让后面的代码看到:
    字符串当列表、浮点数当整数、None 当对象之类的输入会在这里被挡下,返回点名字段的可读原因
    (原因里含字段名和 "must be"),而不是让 '<' 之类的运算在后面抛裸 TypeError,也不是静默通过。
    覆盖嵌套对象(reward、bindings、两个 BigRef)和各容器的元素。
    config_hash 序列化失败时也靠它点名真正出问题的字段。"""
    if not isinstance(cfg, TrainingConfig):
        return f"config must be a TrainingConfig, got {type(cfg).__name__}"
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
    reason = _check_reward_types(cfg.reward)
    if reason is not None:
        return reason
    if not isinstance(cfg.grpo_config, dict):
        return f"grpo_config must be a dict, got {type(cfg.grpo_config).__name__}"
    try:
        _canonical_json(cfg.grpo_config)
    except (TypeError, ValueError, RecursionError) as exc:
        return f"grpo_config must be JSON serializable: {exc}"
    for name, ref in (
        ("base_checkpoint_ref", cfg.base_checkpoint_ref),
        ("teacher_model_ref", cfg.teacher_model_ref),
    ):
        reason = _check_ref(name, ref)
        if reason is not None:
            return reason
    if not isinstance(cfg.scenario_ids, list) or not all(
        isinstance(s, str) and s for s in cfg.scenario_ids
    ):
        return "scenario_ids must be a list of non-empty str"
    if not isinstance(cfg.stage_manifest_refs, dict):
        return f"stage_manifest_refs must be a dict, got {type(cfg.stage_manifest_refs).__name__}"
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in cfg.stage_manifest_refs.items()):
        return "stage_manifest_refs must be a dict of str to str"
    reason = _check_bindings(cfg.bindings)
    if reason is not None:
        return reason
    try:
        _canonical_json(cfg.to_dict())
    except (TypeError, ValueError, RecursionError) as exc:
        # 兜底:前面的逐项检查都过了,却仍序列化不了(例如 BigRef.fetch_hint 里夹带了不可序列化的值)
        return f"config must be JSON serializable: {exc}"
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
            return f"reward.gamma must be in (0, 1], got {_short(r.gamma)}"
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


def _validate_common(cfg: TrainingConfig) -> str | None:
    """两种模式共有的校验:类型、模式名、步数与批量、清单点名、奖励。interact 专用的情境与绑定检查不在这里。"""
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
    if not (_is_finite_number(cfg.opd_coefficient) and cfg.opd_coefficient >= 0):
        return f"opd_coefficient must be a finite number >= 0, got {_short(cfg.opd_coefficient)}"
    if cfg.max_retries_per_episode < 0:
        return f"max_retries_per_episode must be >= 0, got {cfg.max_retries_per_episode}"
    if cfg.max_resume_attempts < 0:
        return f"max_resume_attempts must be >= 0, got {cfg.max_resume_attempts}"
    unknown = sorted(set(cfg.stage_manifest_refs) - set(STAGES))
    if unknown:
        return f"stage_manifest_refs has unknown stage(s) {unknown}; allowed {list(STAGES)}"
    if cfg.sft_steps > 0 and not cfg.stage_manifest_refs.get("sft"):
        return "stage_manifest_refs['sft'] is required when sft_steps > 0"
    if cfg.mode == "batch" and cfg.grpo_steps > 0 and not cfg.stage_manifest_refs.get("grpo_opd"):
        return "stage_manifest_refs['grpo_opd'] is required for batch mode when grpo_steps > 0"
    return _validate_reward(cfg.reward)


def validate_training_config(cfg: TrainingConfig) -> str | None:
    """返回第一条违规的可读原因;全部合法返回 None。纯函数。

    类型检查在任何比较之前跑:任何字段(含嵌套的 reward、bindings、两个 BigRef 与各容器的元素,
    甚至传进来的根本不是 TrainingConfig)的错误类型都绝不抛异常,一律返回可读原因
    (原因里含字段名和 "must be"),不能以裸 TypeError 或静默通过的形式逃出去。"""
    reason = _validate_common(cfg)
    if reason is not None:
        return reason
    if cfg.mode == "interact":
        return _validate_interact(cfg)
    return None


def validate_pending_interact(cfg: TrainingConfig) -> str | None:
    """指令构造期的宽口径,只给 trainscheduler.directive.check_directive 用,且只在指令带 data_spec 时用。

    生成路的 interact 指令在构造时 scenario_ids 与 bindings 都还是空列表,要到派发时才由合成出的数据回写。
    这种配置(mode 为 interact 且两者同为空)只跳过 _validate_interact 里的检查(情境、绑定,以及同在其中的
    max_turns_per_episode >= 1),_validate_common 的类型、步数、批量、清单点名、奖励检查照常全跑;
    回写之后派发器再按 validate_training_config 全量复检。
    其余任何配置(含只空了一个的半成品)与 validate_training_config 的结论完全相同。"""
    if (
        isinstance(cfg, TrainingConfig)
        and cfg.mode == "interact"
        and cfg.scenario_ids == []
        and cfg.bindings == []
    ):
        return _validate_common(cfg)
    return validate_training_config(cfg)


def ensure_valid_config(cfg: TrainingConfig) -> TrainingConfig:
    reason = validate_training_config(cfg)
    if reason is not None:
        raise InvalidTrainingConfigError(reason)
    return cfg


def config_hash(cfg: TrainingConfig) -> str:
    """配置的 sha256:规范化 JSON(键排序、无空白),不依赖字典顺序或 YAML 排版。

    这一步在 validate_training_config 之前就会被下半段编排器调用,所以序列化不了的配置在这里必须
    抛 InvalidTrainingConfigError(确定性失败),不能是裸 TypeError 或 AttributeError(会被当成
    瞬态失败重试)。校验与哈希共用 _canonical_json,所以(只要 to_dict 本身不抛校验之外的异常,配置来自 YAML 或 from_dict 时恒成立)校验放行的配置一定算得出哈希,算不出哈希的配置校验一定拒绝,异常信息就是校验给出的原因,点名真正出问题的字段。只是没过校验、
    但仍能序列化的配置照常算出哈希:编排器要靠它建 training_run 记录,配置失败才会留下 failed 的痕迹。"""
    try:
        payload = _canonical_json(cfg.to_dict())
    except (TypeError, ValueError, AttributeError, RecursionError) as exc:
        reason = _check_types(cfg)
        raise InvalidTrainingConfigError(
            reason or f"config must be JSON serializable: {exc}"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


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
