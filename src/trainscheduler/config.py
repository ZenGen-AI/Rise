"""调度器的两份配置:config/scheduler.yaml 与 config/training_defaults.yaml(设计 §7.2、§11)。

两份都是 type: config 的信封,经 controlplane.config.read_config / write_config 读写。解析严格:
类型错、未知键一律抛 SchedulerConfigError,信息里点名字段。

- scheduler.yaml 的键都可省略,省略的取默认值;文件不在时整份取默认值。
- training_defaults.yaml 两个键(training_config、data_spec)都必填;文件不在抛 SchedulerConfigError。
  另有两个可选段 interact(interact 训练线的角色位、轮数、情境人格来源)与 opponent(外部对手的端点),
  缺省时为 None,文件与没有这两段时完全相同。
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field, fields

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
                "stage_manifest_refs", "bindings", "scenario_ids", "memory"):
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

    def __post_init__(self) -> None:
        """开关打开而模板不渲染第七段时,直接拒绝。

        这类"配置了但没生效"的静默陷阱最难发现:落账在写、条目在算,规划器却永远看不到。
        宁可启动即报错,也不让功能悄悄空转。
        """
        if not self.strategy_experience:
            return
        from trainplanner.templates import STRATEGY_TEMPLATE_VERSIONS

        if self.template_version not in STRATEGY_TEMPLATE_VERSIONS:
            raise SchedulerConfigError(
                f"strategy_experience is on but template_version is {self.template_version!r}; "
                f"the strategy section is only rendered by {list(STRATEGY_TEMPLATE_VERSIONS)}"
            )

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
# 可选段:缺省为 None,to_dict 也不写出,所以没有它们的文件逐字节不变
_DEFAULTS_OPTIONAL_KEYS = ("interact", "opponent")
# 每轮各自的值,不能写进默认配置:清单 id 由 construct_directive 按新 run id 写
_PER_RUN_KEYS = ("stage_manifest_refs",)


@dataclass(frozen=True)
class InteractDefaults:
    """interact 段的解析结果。来源二选一:点名路给 scenario_ids 与 persona_ids,生成路给 data_spec;
    另一路在这里是空(scenario_ids 为空元组、persona_ids 为空映射、data_spec 为 None)。"""

    trained_slot: str
    slots: tuple[dict, ...]  # 每项取自 slot_id、model_backend,可带 model_ref、driver;不含 persona_id
    max_turns_per_episode: int
    scenario_ids: tuple[str, ...] = ()
    persona_ids: dict[str, str] = field(default_factory=dict)  # 角色位 id -> 人格 id
    data_spec: DataSpec | None = None
    # 起点记忆的种子(None 为空起点)。不放整段 memory 映射:frozen dataclass 里放可变 dict 会破坏冻结与"不别名输入输出"。
    # digests 与 runtime 是派发时算出来的,默认值里只有 seed
    memory_seed: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "trained_slot": self.trained_slot,
            "slots": [dict(s) for s in self.slots],
            "max_turns_per_episode": self.max_turns_per_episode,
            "memory": {"seed": self.memory_seed},
        }
        if self.data_spec is not None:
            out["data_spec"] = self.data_spec.to_dict()
        else:
            out["scenario_ids"] = list(self.scenario_ids)
            out["persona_ids"] = dict(self.persona_ids)
        return out


@dataclass(frozen=True)
class TrainingDefaults:
    training_config: dict  # TrainingConfig.to_dict() 的形状,不含 stage_manifest_refs
    data_spec: DataSpec
    interact: InteractDefaults | None = None  # 可选段;缺省 None
    opponent: dict | None = None  # {base_url, model, api_key_env};只存环境变量名,不存密钥

    def to_dict(self) -> dict:
        out: dict = {"training_config": copy.deepcopy(self.training_config), "data_spec": self.data_spec.to_dict()}
        if self.interact is not None:
            out["interact"] = self.interact.to_dict()
        if self.opponent is not None:
            out["opponent"] = copy.deepcopy(self.opponent)
        return out


def _placeholder_refs(cfg: TrainingConfig) -> dict[str, str]:
    return {stage: f"dsm-run-000-{stage}" for stage in required_stages(cfg)}


_INTERACT_REQUIRED = ("trained_slot", "slots", "max_turns_per_episode", "memory")
_INTERACT_OPTIONAL = ("scenario_ids", "persona_ids", "data_spec")
_MEMORY_KEYS = ("seed",)
_SLOT_REQUIRED = ("slot_id", "model_backend")
_SLOT_OPTIONAL = ("model_ref", "driver")
_OPPONENT_KEYS = ("base_url", "model", "api_key_env")
# trainscheduler 不得导入 simulator,所以这里复制一份;取值与 simulator.datamodel.OPPONENT_DRIVERS(RoleBinding.driver)
# 一致,改那里要一起改。这里只拦明显的拼写错,是否与 model_backend、model_ref 相容仍由展开后的 validate_training_config 判。
_SLOT_DRIVERS = ("vllm", "external_api", "scripted")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _nonempty_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SchedulerConfigError(f"{label} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise SchedulerConfigError(f"{label} must be non-empty")
    return value


def _parse_interact_slots(raw: object, label: str) -> tuple[dict, ...]:
    if not isinstance(raw, list) or not raw:
        raise SchedulerConfigError(f"{label} must be a non-empty list")
    slots: list[dict] = []
    for i, item in enumerate(raw):
        here = f"{label}[{i}]"
        if not isinstance(item, dict):
            raise SchedulerConfigError(f"{here} must be a mapping, got {type(item).__name__}")
        missing = [k for k in _SLOT_REQUIRED if k not in item]
        unknown = sorted(repr(k) for k in item if k not in _SLOT_REQUIRED + _SLOT_OPTIONAL)
        if missing:
            raise SchedulerConfigError(f"{here} is missing key(s) {missing}")
        if unknown:
            raise SchedulerConfigError(f"{here} has unknown key(s) {unknown}")
        for key in item:
            # 取值是否合法(后端名、model_ref 是否必填)留给展开后的 validate_training_config;driver 的取值下面就拦
            _nonempty_str(item[key], f"{here}.{key}")
        if "driver" in item and item["driver"] not in _SLOT_DRIVERS:
            raise SchedulerConfigError(
                f"{here}.driver must be one of {list(_SLOT_DRIVERS)}, got {item['driver']!r}"
            )
        slots.append(dict(item))
    ids = [s["slot_id"] for s in slots]
    duplicated = sorted({s for s in ids if ids.count(s) > 1})
    if duplicated:
        raise SchedulerConfigError(f"{label} has duplicate slot_id(s) {duplicated}")
    return tuple(slots)


def _parse_interact_memory(raw: object, label: str) -> str | None:
    """memory 段只许 seed 一个键(None 为空起点,否则是非空 str)。digests 与 runtime 由派发器写,出现在这里即 unknown。"""
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"{label} must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _MEMORY_KEYS if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _MEMORY_KEYS)
    if missing or unknown:
        raise SchedulerConfigError(f"{label}: missing key(s) {missing}, unknown key(s) {unknown}")
    seed = raw["seed"]
    if seed is not None and not (isinstance(seed, str) and seed.strip()):
        raise SchedulerConfigError(f"{label}.seed must be null or a non-empty str")
    return seed


def _parse_interact(raw: object, groups_per_step: int) -> InteractDefaults:
    """interact 段的严格解析。只看形状与自洽,不读树:点名的 id 在不在树里由装配期检查。"""
    label = "training_defaults.interact"
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"{label} must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _INTERACT_REQUIRED if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _INTERACT_REQUIRED + _INTERACT_OPTIONAL)
    if missing or unknown:
        raise SchedulerConfigError(f"{label}: missing key(s) {missing}, unknown key(s) {unknown}")
    memory_seed = _parse_interact_memory(raw["memory"], f"{label}.memory")
    # 来源:先判成对,再判二选一。顺序决定"只写了一半"报哪条
    has_ids, has_personas, has_spec = "scenario_ids" in raw, "persona_ids" in raw, "data_spec" in raw
    if has_ids != has_personas:
        raise SchedulerConfigError(f"{label}: scenario_ids and persona_ids must be written together")
    if has_ids == has_spec:
        raise SchedulerConfigError(
            f"{label}: exactly one of (scenario_ids with persona_ids) or data_spec must be given"
        )
    slots = _parse_interact_slots(raw["slots"], f"{label}.slots")
    slot_ids = {s["slot_id"] for s in slots}
    trained_slot = _nonempty_str(raw["trained_slot"], f"{label}.trained_slot")
    if trained_slot not in slot_ids:
        raise SchedulerConfigError(
            f"{label}.trained_slot {trained_slot!r} is not among slots {sorted(slot_ids)}"
        )
    turns = raw["max_turns_per_episode"]
    if isinstance(turns, bool) or not isinstance(turns, int):
        raise SchedulerConfigError(f"{label}.max_turns_per_episode must be an int, got {type(turns).__name__}")
    if turns < 1:
        raise SchedulerConfigError(f"{label}.max_turns_per_episode must be >= 1, got {turns}")

    if has_spec:
        try:
            spec = parse_data_spec(raw["data_spec"])
        except InvalidDataSpecError as exc:
            raise SchedulerConfigError(f"{label}.{exc}") from exc
        # 一步抽 groups_per_step 个不同情境;少于它,同一情境在一步里出现两次,两批 rollout 会并成一组
        if spec.scenario_count < groups_per_step:
            raise SchedulerConfigError(
                f"{label}.data_spec.scenario_count {spec.scenario_count} must be at least "
                f"groups_per_step {groups_per_step} (one scenario per group)"
            )
        return InteractDefaults(trained_slot, slots, turns, data_spec=spec, memory_seed=memory_seed)

    ids = raw["scenario_ids"]
    if not isinstance(ids, list) or not ids:
        raise SchedulerConfigError(f"{label}.scenario_ids must be a non-empty list")
    for i, sid in enumerate(ids):
        _nonempty_str(sid, f"{label}.scenario_ids[{i}]")
    if len(set(ids)) != len(ids):
        raise SchedulerConfigError(
            f"{label}.scenario_ids has duplicate entries {sorted({s for s in ids if ids.count(s) > 1})}"
        )
    personas = raw["persona_ids"]
    if not isinstance(personas, dict):
        raise SchedulerConfigError(f"{label}.persona_ids must be a mapping, got {type(personas).__name__}")
    if set(personas) != slot_ids:
        raise SchedulerConfigError(
            f"{label}.persona_ids must name the same slots as slots: "
            f"persona_ids has {sorted(map(repr, personas))}, slots has {sorted(slot_ids)}"
        )
    for slot, pid in personas.items():
        _nonempty_str(pid, f"{label}.persona_ids[{slot!r}]")
    if len(ids) < groups_per_step:
        raise SchedulerConfigError(
            f"{label}.scenario_ids has {len(ids)} entries, must be at least "
            f"groups_per_step {groups_per_step} (one scenario per group)"
        )
    return InteractDefaults(
        trained_slot, slots, turns, scenario_ids=tuple(ids), persona_ids=dict(personas), memory_seed=memory_seed
    )


def _parse_opponent(raw: object) -> dict:
    """外部对手的端点。api_key_env 只存环境变量名;值写成了密钥时报错不回显它。"""
    label = "training_defaults.opponent"
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"{label} must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _OPPONENT_KEYS if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _OPPONENT_KEYS)
    if missing or unknown:
        raise SchedulerConfigError(f"{label}: missing key(s) {missing}, unknown key(s) {unknown}")
    out = {k: _nonempty_str(raw[k], f"{label}.{k}") for k in _OPPONENT_KEYS}
    if not _ENV_NAME.fullmatch(out["api_key_env"]):
        raise SchedulerConfigError(
            f"{label}.api_key_env must be an environment variable name (letters, digits, underscore)"
        )
    return out


def parse_training_defaults(raw: object) -> TrainingDefaults:
    """严格解析。training_config 必须能被 TrainingConfig.from_dict 读出,补上占位清单 id 后过
    validate_training_config,且 data_spec 与它交叉校验通过:默认配置本身就跑不通的话,
    每一条 new 指令都会失败。可选的 interact 与 opponent 段缺省为 None,出现时同样严格解析。"""
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"training defaults must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _DEFAULTS_KEYS if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _DEFAULTS_KEYS + _DEFAULTS_OPTIONAL_KEYS)
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
    interact = _parse_interact(raw["interact"], probe.groups_per_step) if "interact" in raw else None
    opponent = _parse_opponent(raw["opponent"]) if "opponent" in raw else None
    return TrainingDefaults(
        training_config=copy.deepcopy(cfg_raw), data_spec=spec, interact=interact, opponent=opponent
    )


def load_training_defaults(store: GitStore) -> TrainingDefaults:
    rel = training_defaults_file()
    if not store.exists(rel):
        raise SchedulerConfigError(
            f"{rel} not found; run trainscheduler.config.install_default_configs(store) first"
        )
    return parse_training_defaults(read_config(store, rel))


def load_opponent_section(store: GitStore) -> dict | None:
    """只读 training_defaults.yaml 的 opponent 段并严格解析它;没有这一段返回 None。

    给派发期每次重读用:其余段落写坏了不影响这里(它们由 load_training_defaults 在装配期与调度时校验)。
    文件不在、不是 config 信封、载荷不是映射,照样抛错:这些情形下 opponent 段也读不出。"""
    rel = training_defaults_file()
    if not store.exists(rel):
        raise SchedulerConfigError(f"{rel} not found")
    raw = read_config(store, rel)
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"training defaults must be a mapping, got {type(raw).__name__}")
    return _parse_opponent(raw["opponent"]) if "opponent" in raw else None


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
