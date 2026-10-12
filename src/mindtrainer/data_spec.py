"""指令里的 data_spec:严格解析、与训练配置的交叉校验、数据生产器协议(设计 §9,R14)。

data_spec 由调度器写进 directive 的 payload,形如:

    {scenario_count: 4, seed: 0, purpose: training,
     stages: {sft: {group_size: 1}, grpo_opd: {group_size: 2}}}

解析在派发器里、碰树之前就跑:字符串当整数、布尔当整数、列表当字典之类的输入在这里
一律变成点名字段的 InvalidDataSpecError,不让 TypeError 或 AttributeError 一路滑进生产器、
再逃出守护循环。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.training_config import STAGES, TrainingConfig

PURPOSES = ("training", "deployment_simulation")
MAX_SCENARIO_COUNT = 64
MAX_GROUP_SIZE = 16
MAX_SEED = 2**31 - 1
_KEYS = frozenset({"scenario_count", "seed", "purpose", "stages"})
_STAGE_KEYS = frozenset({"group_size"})


class InvalidDataSpecError(ValueError):
    """data_spec 缺字段、类型不对或越界。确定性失败:同一份 data_spec 重试还会失败。"""


class DataSynthesisError(ValueError):
    """数据生产器确定性地做不成(例如训练角色位与对手角色位同名)。派发器把它判成 T2。"""


@dataclass(frozen=True)
class StageSpec:
    group_size: int


@dataclass(frozen=True)
class DataSpec:
    scenario_count: int
    seed: int
    purpose: str
    stages: dict[str, StageSpec]

    def to_dict(self) -> dict:
        return {
            "scenario_count": self.scenario_count,
            "seed": self.seed,
            "purpose": self.purpose,
            "stages": {name: {"group_size": s.group_size} for name, s in self.stages.items()},
        }


def _show(value) -> str:
    """错误信息里展示的值(照 training_config._short 的做法):太长就截断;转字符串本身会抛时只写类型。
    超过 4300 位的整数(YAML 的十六进制能写出来)做 repr 或 f-string 都会抛 ValueError,
    若让它从拼错误信息的地方漏出去,就绕过了 InvalidDataSpecError,一路逃出守护循环。"""
    try:
        text = repr(value)
    except Exception:  # 超长整数的 ValueError,或自定义对象的 __repr__ 抛的任何异常
        return f"<{type(value).__name__}>"
    return text if len(text) <= 48 else text[:45] + "..."


def _keys_text(keys) -> str:
    # 键可能是互不可比较的类型(int 和 str 混在一起),也可能是超长整数:排序前先转成安全的展示串
    return str(sorted(_show(k) for k in keys))


def _int_in_range(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataSpecError(f"{label} must be an int, got {type(value).__name__}")
    if not (low <= value <= high):
        raise InvalidDataSpecError(f"{label} must be in [{low}, {high}], got {_show(value)}")
    return value


def _exact_keys(raw: dict, allowed: frozenset, label: str) -> None:
    missing = allowed - set(raw)
    if missing:
        raise InvalidDataSpecError(f"{label} is missing key(s) {_keys_text(missing)}")
    unknown = set(raw) - allowed
    if unknown:
        raise InvalidDataSpecError(
            f"{label} has unknown key(s) {_keys_text(unknown)}; allowed {sorted(allowed)}"
        )


def _parse_stage(name: str, raw) -> StageSpec:
    label = f"data_spec.stages[{name!r}]"
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"{label} must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _STAGE_KEYS, label)
    # grpo_opd 的组少于 2 条时组内零方差、优势恒为 0;sft 每组一条是正常的
    low = 2 if name == "grpo_opd" else 1
    return StageSpec(_int_in_range(raw["group_size"], f"{label}.group_size", low, MAX_GROUP_SIZE))


def parse_data_spec(raw) -> DataSpec:
    """严格解析。任何类型错、范围错、缺键、多键都抛 InvalidDataSpecError(信息里点名字段),
    绝不抛别的异常类型。"""
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"data_spec must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _KEYS, "data_spec")
    scenario_count = _int_in_range(
        raw["scenario_count"], "data_spec.scenario_count", 1, MAX_SCENARIO_COUNT
    )
    seed = _int_in_range(raw["seed"], "data_spec.seed", 0, MAX_SEED)
    purpose = raw["purpose"]
    if not isinstance(purpose, str):
        raise InvalidDataSpecError(
            f"data_spec.purpose must be a str, got {type(purpose).__name__}"
        )
    if purpose not in PURPOSES:
        raise InvalidDataSpecError(
            f"data_spec.purpose must be one of {list(PURPOSES)}, got {purpose!r}"
        )
    stages_raw = raw["stages"]
    if not isinstance(stages_raw, dict):
        raise InvalidDataSpecError(
            f"data_spec.stages must be a dict, got {type(stages_raw).__name__}"
        )
    if not stages_raw:
        raise InvalidDataSpecError("data_spec.stages must name at least one stage")
    unknown = set(stages_raw) - set(STAGES)
    if unknown:
        raise InvalidDataSpecError(
            f"data_spec.stages has unknown stage(s) {_keys_text(unknown)}; allowed {list(STAGES)}"
        )
    stages = {name: _parse_stage(name, stages_raw[name]) for name in STAGES if name in stages_raw}
    return DataSpec(scenario_count=scenario_count, seed=seed, purpose=purpose, stages=stages)


def data_spec_from_directive(directive: Envelope) -> DataSpec | None:
    """payload 里没有 data_spec 键,或值为 null,都表示"这条指令不合成数据"。"""
    raw = directive.payload.get("data_spec")
    if raw is None:
        return None
    return parse_data_spec(raw)


def required_stages(cfg: TrainingConfig) -> list[str]:
    """真正会用到的阶段:配了步数的才算,且与模式有关。stage_manifest_refs 里多写的阶段不读。

    sft 两种模式都从清单读,配了 sft_steps 就要。grpo_opd 只有 batch 模式从清单读;
    interact 模式的 grpo_opd 来自实时会话(_interact_sources 事后整段覆盖),不读清单,
    所以即便指令在 stage_manifest_refs 里为 interact 顺手点了一个 grpo_opd 的清单 id
    (validate_training_config 并不禁止),这里也不该把它当成需要校验的阶段。"""
    wanted = []
    if cfg.sft_steps > 0:
        wanted.append("sft")
    if cfg.mode == "batch" and cfg.grpo_steps > 0:
        wanted.append("grpo_opd")
    return wanted


def manifest_data_run(ref: str, stage: str) -> str | None:
    """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。
    解出数据所在的 run;格式不对返回 None。"""
    head, tail = "dsm-", f"-{stage}"
    if not (ref.startswith(head) and ref.endswith(tail) and len(ref) > len(head) + len(tail)):
        return None
    return ref[len(head):-len(tail)]


def stages_to_synthesize(store: GitStore, run_id: str, cfg: TrainingConfig) -> list[str]:
    """需要合成的阶段:会用到、点了清单、清单的数据所在 run 就是本 run、且清单文件还不在树里。
    清单已在的阶段不重新合成;id 格式不对的阶段不在这里判(派发器会判 failed)。"""
    out = []
    for name in required_stages(cfg):
        ref = cfg.stage_manifest_refs.get(name)
        if not ref or manifest_data_run(ref, name) != run_id:
            continue
        if not store.exists(dataset_manifest_file(run_id, name)):
            out.append(name)
    return out


def check_data_spec_against_config(
    spec: DataSpec, cfg: TrainingConfig, stages: list[str]
) -> str | None:
    """data_spec 能否为这些阶段产出训练用得上的数据。返回第一条原因,合格返回 None。
    这些问题训练时也会以 DegenerateGroupError、NotEnoughGroupsError 暴露,在这里先判,
    是为了一条 episode 都不写就失败,并且原因直接指向 data_spec。"""
    for name in stages:
        if name not in spec.stages:
            return f"data_spec.stages has no entry for stage {name!r}, which must be synthesized"
    if "grpo_opd" in stages:
        size = spec.stages["grpo_opd"].group_size
        if size < cfg.min_group_size:
            return (
                f"data_spec.stages['grpo_opd'].group_size {size} is below "
                f"min_group_size {cfg.min_group_size}"
            )
        if spec.scenario_count < cfg.groups_per_step:
            return (
                f"data_spec.scenario_count {spec.scenario_count} is below "
                f"groups_per_step {cfg.groups_per_step} (one group per scenario)"
            )
    return None


class DataProducer(Protocol):
    def produce(
        self, store: GitStore, run_id: str, cfg: TrainingConfig, data_spec: DataSpec
    ) -> None:
        """写出 stages_to_synthesize(store, run_id, cfg) 列出的各阶段数据集:episode 先写、
        清单最后写,清单 id 为 dsm-{run_id}-{阶段}。确定性做不成抛 DataSynthesisError
        (或 DatasetIntegrityError 等派发器认得的确定性错误);瞬态错误原样上抛。"""
        ...
