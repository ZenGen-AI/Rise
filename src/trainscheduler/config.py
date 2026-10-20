"""调度器的两份配置:config/scheduler.yaml 与 config/training_defaults.yaml(设计 §7.2、§11)。

两份都是 type: config 的信封,经 controlplane.config.read_config / write_config 读写。解析严格:
类型错、未知键一律抛 SchedulerConfigError,信息里点名字段。

- scheduler.yaml 的键都可省略,省略的取默认值;文件不在时整份取默认值。
- training_defaults.yaml 两个键(training_config、data_spec)都必填;文件不在抛 SchedulerConfigError。
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, fields

from controlplane.config import read_config, write_config
from controlplane.gitstore import GitStore
from controlplane.paths import scheduler_config_file, training_defaults_file
from mindtrainer.data_spec import (
    DataSpec,
    InvalidDataSpecError,
    check_data_spec_against_config,
    parse_data_spec,
    required_stages,
)
from mindtrainer.training_config import (
    InvalidTrainingConfigError,
    TrainingConfig,
    validate_training_config,
)
from trainscheduler.errors import SchedulerConfigError
from trainplanner.templates import PLANNER_V1_VERSION

SCHEDULER_CONFIG_ID = "scheduler-config"
TRAINING_DEFAULTS_ID = "training-defaults"

# 扰动键语法:对 training_config 的点路径,每段是标识符,例如 opd_coefficient、grpo_config.learning_rate
_SEGMENT = r"[A-Za-z_][A-Za-z0-9_]*"
_PERTURB_KEY = re.compile(rf"^{_SEGMENT}(\.{_SEGMENT})*$")
# 可以往下走一层的 training_config 字段(值本身是映射)
NESTED_FIELDS = ("grpo_config", "reward")
_TOP_FIELDS = tuple(f.name for f in fields(TrainingConfig))


def check_perturbable_key(key: object) -> str | None:
    """扰动键的语法:点路径;首段必须是 TrainingConfig 的字段;多段时首段必须是 grpo_config 或 reward。"""
    if not isinstance(key, str) or not _PERTURB_KEY.match(key):
        return f"perturbable key {key!r} must be a dotted path of identifiers"
    head, _, rest = key.partition(".")
    if head not in _TOP_FIELDS:
        return f"perturbable key {key!r}: {head!r} is not a training_config field"
    if rest and head not in NESTED_FIELDS:
        return f"perturbable key {key!r}: only {list(NESTED_FIELDS)} can be descended into"
    if head in ("mode", "trained_slot", "base_checkpoint_ref", "teacher_model_ref",
                "stage_manifest_refs", "bindings", "scenario_ids"):
        return f"perturbable key {key!r}: {head!r} anchors the run and is never perturbable"
    return None


@dataclass(frozen=True)
class SchedulerConfig:
    confirm_directives: bool = True
    allow_interact: bool = False
    accept_mock_evidence: bool = False
    perturbable: tuple[str, ...] = (
        "opd_coefficient",
        "sft_steps",
        "grpo_steps",
        "grpo_config.learning_rate",
    )
    min_gain: float = 0.01
    cost_gain_window: int = 5
    recent_decision_window: int = 5
    max_plan_attempts: int = 3
    template_version: str = PLANNER_V1_VERSION
    strategy_experience: bool = False

    def to_dict(self) -> dict:
        return {
            "confirm_directives": self.confirm_directives,
            "allow_interact": self.allow_interact,
            "accept_mock_evidence": self.accept_mock_evidence,
            "perturbable": list(self.perturbable),
            "min_gain": self.min_gain,
            "cost_gain_window": self.cost_gain_window,
            "recent_decision_window": self.recent_decision_window,
            "max_plan_attempts": self.max_plan_attempts,
            "template_version": self.template_version,
            "strategy_experience": self.strategy_experience,
        }


DEFAULT_SCHEDULER_CONFIG = SchedulerConfig()
_SCHEDULER_KEYS = tuple(f.name for f in fields(SchedulerConfig))


def _label(key: str) -> str:
    return f"scheduler.{key}"


def _bool(raw: dict, key: str) -> bool:
    v = raw[key]
    if not isinstance(v, bool):
        raise SchedulerConfigError(f"{_label(key)} must be a bool, got {type(v).__name__}")
    return v


def _int(raw: dict, key: str, low: int, high: int) -> int:
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise SchedulerConfigError(f"{_label(key)} must be an int, got {type(v).__name__}")
    if not (low <= v <= high):
        raise SchedulerConfigError(f"{_label(key)} must be in [{low}, {high}], got {v}")
    return v


def parse_scheduler_config(raw: object) -> SchedulerConfig:
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"scheduler config must be a mapping, got {type(raw).__name__}")
    unknown = sorted(repr(k) for k in raw if k not in _SCHEDULER_KEYS)
    if unknown:
        raise SchedulerConfigError(
            f"scheduler config has unknown key(s) {unknown}; allowed {list(_SCHEDULER_KEYS)}"
        )
    values = DEFAULT_SCHEDULER_CONFIG.to_dict()
    values.update(raw)
    perturbable = values["perturbable"]
    if not isinstance(perturbable, list):
        raise SchedulerConfigError(
            f"{_label('perturbable')} must be a list, got {type(perturbable).__name__}"
        )
    for key in perturbable:
        reason = check_perturbable_key(key)
        if reason is not None:
            raise SchedulerConfigError(f"{_label('perturbable')}: {reason}")
    if len(set(perturbable)) != len(perturbable):
        raise SchedulerConfigError(f"{_label('perturbable')} has duplicate keys")
    min_gain = values["min_gain"]
    if isinstance(min_gain, bool) or not isinstance(min_gain, (int, float)) or not math.isfinite(min_gain):
        raise SchedulerConfigError(f"{_label('min_gain')} must be a finite number, got {min_gain!r}")
    template_version = values["template_version"]
    if not isinstance(template_version, str) or not template_version.strip():
        raise SchedulerConfigError(f"{_label('template_version')} must be a non-empty str")
    return SchedulerConfig(
        confirm_directives=_bool(values, "confirm_directives"),
        allow_interact=_bool(values, "allow_interact"),
        accept_mock_evidence=_bool(values, "accept_mock_evidence"),
        perturbable=tuple(perturbable),
        min_gain=float(min_gain),
        cost_gain_window=_int(values, "cost_gain_window", 1, 100),
        recent_decision_window=_int(values, "recent_decision_window", 1, 100),
        max_plan_attempts=_int(values, "max_plan_attempts", 1, 10),
        template_version=template_version,
        strategy_experience=_bool(values, "strategy_experience"),
    )


def load_scheduler_config(store: GitStore) -> SchedulerConfig:
    rel = scheduler_config_file()
    if not store.exists(rel):
        return DEFAULT_SCHEDULER_CONFIG
    return parse_scheduler_config(read_config(store, rel))


# ---- training_defaults.yaml ----

_ZERO_SHA = "0" * 64

DEFAULT_TRAINING_CONFIG: dict = {
    "mode": "batch",
    "trained_slot": "zengen",
    # 没有任何已晋级模型时的初始基座。真实部署前要换成开源底座权重的真实引用与哈希。
    "base_checkpoint_ref": {
        "uri": "file:///compute/checkpoints/initial-base.safetensors",
        "sha256": _ZERO_SHA, "size_bytes": 0, "fetch_hint": None,
    },
    "teacher_model_ref": {
        "uri": "file:///compute/checkpoints/opd-teacher.safetensors",
        "sha256": _ZERO_SHA, "size_bytes": 0, "fetch_hint": None,
    },
    "reward": {
        "components": ["goal"],
        "attribution": "outcome_split",
        "combination": "sum",
        "outcome_rules": {"goal": ["agreed", "同意"]},
        "gamma": 1.0,
        "judge_model_id": None,
        "rubric_version": None,
    },
    "sft_steps": 2,
    "grpo_steps": 2,
    "sft_batch_size": 1,
    "groups_per_step": 2,
    "group_size": 2,
    "grpo_config": {"learning_rate": 1.0e-5},
    "opd_coefficient": 0.1,
    "checkpoint_every_steps": 1,
    "allow_resume": True,
}

DEFAULT_DATA_SPEC: dict = {
    "scenario_count": 4,
    "seed": 0,
    "purpose": "training",
    "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}},
}

_DEFAULTS_KEYS = ("training_config", "data_spec")
# 每轮各自的值,不能写进默认配置:清单 id 由 construct_directive 按新 run id 写
_PER_RUN_KEYS = ("stage_manifest_refs",)


@dataclass(frozen=True)
class TrainingDefaults:
    training_config: dict  # TrainingConfig.to_dict() 的形状,不含 stage_manifest_refs
    data_spec: DataSpec

    def to_dict(self) -> dict:
        return {"training_config": copy.deepcopy(self.training_config), "data_spec": self.data_spec.to_dict()}


def _placeholder_refs(cfg: TrainingConfig) -> dict[str, str]:
    return {stage: f"dsm-run-000-{stage}" for stage in required_stages(cfg)}


def parse_training_defaults(raw: object) -> TrainingDefaults:
    """严格解析。training_config 必须能被 TrainingConfig.from_dict 读出,补上占位清单 id 后过
    validate_training_config,且 data_spec 与它交叉校验通过:默认配置本身就跑不通的话,
    每一条 new 指令都会失败。"""
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"training defaults must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _DEFAULTS_KEYS if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _DEFAULTS_KEYS)
    if missing or unknown:
        raise SchedulerConfigError(
            f"training defaults: missing key(s) {missing}, unknown key(s) {unknown}"
        )
    cfg_raw = raw["training_config"]
    if not isinstance(cfg_raw, dict):
        raise SchedulerConfigError(
            f"training_defaults.training_config must be a mapping, got {type(cfg_raw).__name__}"
        )
    for key in _PER_RUN_KEYS:
        if key in cfg_raw:
            raise SchedulerConfigError(
                f"training_defaults.training_config.{key} must not be set; it is written per run"
            )
    unknown_fields = sorted(repr(k) for k in cfg_raw if k not in _TOP_FIELDS)
    if unknown_fields:
        raise SchedulerConfigError(
            f"training_defaults.training_config has unknown field(s) {unknown_fields}"
        )
    try:
        cfg = TrainingConfig.from_dict(cfg_raw)
    except KeyError as exc:
        raise SchedulerConfigError(f"training_defaults.training_config is missing field {exc}") from exc
    except (TypeError, AttributeError, InvalidTrainingConfigError) as exc:
        raise SchedulerConfigError(f"training_defaults.training_config is malformed: {exc}") from exc
    if cfg.mode != "batch":
        raise SchedulerConfigError(
            f"training_defaults.training_config.mode must be 'batch', got {cfg.mode!r}"
        )
    # 先在没有清单 id 的配置上校验:类型错(sft_steps 写成字符串之类)要在 required_stages 比较数值之前
    # 挡下,不能让 TypeError 漏出去。唯一允许的违规是"缺 stage_manifest_refs",它在下一步补上占位 id。
    reason = validate_training_config(cfg)
    if reason is not None and not reason.startswith("stage_manifest_refs["):
        raise SchedulerConfigError(f"training_defaults.training_config: {reason}")
    probe = TrainingConfig.from_dict({**cfg_raw, "stage_manifest_refs": _placeholder_refs(cfg)})
    reason = validate_training_config(probe)
    if reason is not None:
        raise SchedulerConfigError(f"training_defaults.training_config: {reason}")
    try:
        spec = parse_data_spec(raw["data_spec"])
    except InvalidDataSpecError as exc:
        raise SchedulerConfigError(f"training_defaults.{exc}") from exc
    reason = check_data_spec_against_config(spec, probe, required_stages(probe))
    if reason is not None:
        raise SchedulerConfigError(f"training_defaults: {reason}")
    return TrainingDefaults(training_config=copy.deepcopy(cfg_raw), data_spec=spec)


def load_training_defaults(store: GitStore) -> TrainingDefaults:
    rel = training_defaults_file()
    if not store.exists(rel):
        raise SchedulerConfigError(
            f"{rel} not found; run trainscheduler.config.install_default_configs(store) first"
        )
    return parse_training_defaults(read_config(store, rel))


def default_training_defaults_payload() -> dict:
    return {"training_config": copy.deepcopy(DEFAULT_TRAINING_CONFIG), "data_spec": copy.deepcopy(DEFAULT_DATA_SPEC)}


def install_default_configs(store: GitStore) -> list[str]:
    """把两份默认配置写进树。已存在的文件一律不动(哪怕内容与默认不同):人改过的配置不被覆盖。
    返回本次实际写入的相对路径。"""
    written: list[str] = []
    for rel, config_id, payload in (
        (scheduler_config_file(), SCHEDULER_CONFIG_ID, DEFAULT_SCHEDULER_CONFIG.to_dict()),
        (training_defaults_file(), TRAINING_DEFAULTS_ID, default_training_defaults_payload()),
    ):
        if store.exists(rel):
            continue
        write_config(store, rel, config_id, payload, f"trainscheduler: install default {rel.rsplit('/', 1)[-1]}")
        written.append(rel)
    return written
