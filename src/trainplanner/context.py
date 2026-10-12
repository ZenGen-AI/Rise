"""规划上下文:TrainScheduler 的 assemble_context 产出、TrainPlanner 消费的纯数据。

字段名与类型是计划 5 的契约。所有结构都是冻结数据类,构造时校验类型;to_dict / from_dict
互逆,from_dict 对缺键与多键一律拒绝,不做任何补默认值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CANDIDATE_TERMINAL_STATES = ("effective", "rejected")
PLANNABLE_GRADES = ("mock", "real")
# 与 recipestore 的配方类型集合(计划 3 的 RECIPE_TYPES,validate_reproducibility 按它分派)必须一致,
# 改一处要同步另一处。两个包互不导入,所以各留一份字面量。
RECIPE_TYPES = ("batch", "interact", "agent_rsi")
# 决策动作集合的唯一定义。decision.parse_decision 从这里导入,不另写字面量。
DECISION_ACTIONS = ("new", "clone", "replay")


class InvalidContextError(ValueError):
    """规划上下文的形状不对:缺键、多键、类型不符或取值越界。"""


def _exact_keys(d: object, keys: tuple[str, ...], where: str) -> dict:
    if not isinstance(d, dict):
        raise InvalidContextError(f"{where}: expected a mapping, got {type(d).__name__}")
    missing = sorted(set(keys) - set(d))
    unknown = sorted(str(k) for k in set(d) - set(keys))
    parts = []
    if missing:
        parts.append(f"missing keys {missing}")
    if unknown:
        parts.append(f"unknown keys {unknown}")
    if parts:
        raise InvalidContextError(f"{where}: " + ", ".join(parts))
    return d


def _check_str(v: object, where: str) -> None:
    if not isinstance(v, str) or not v.strip():
        raise InvalidContextError(f"{where}: expected a non-empty string, got {v!r}")


def _check_opt_str(v: object, where: str) -> None:
    if v is not None:
        _check_str(v, where)


def _check_num(v: object, where: str) -> None:
    # 整数一定有限,不进 isfinite:超大整数(如 10**400)转 float 会抛 OverflowError
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InvalidContextError(f"{where}: expected a finite number, got {v!r}")
    if isinstance(v, float) and not math.isfinite(v):
        raise InvalidContextError(f"{where}: expected a finite number, got {v!r}")


def _check_opt_num(v: object, where: str) -> None:
    if v is not None:
        _check_num(v, where)


def _check_opt_nonneg_num(v: object, where: str) -> None:
    if v is not None:
        _check_num(v, where)
        if v < 0:
            raise InvalidContextError(f"{where}: expected a non-negative number, got {v!r}")


def _check_count(v: object, where: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise InvalidContextError(f"{where}: expected a non-negative integer, got {v!r}")


def _check_opt_count(v: object, where: str) -> None:
    if v is not None:
        _check_count(v, where)


def _check_choice(v: object, choices: tuple[str, ...], where: str) -> None:
    if v not in choices:
        raise InvalidContextError(f"{where}: expected one of {list(choices)}, got {v!r}")


def _check_list_of(v: object, cls: type, where: str) -> None:
    if not isinstance(v, list) or not all(isinstance(x, cls) for x in v):
        raise InvalidContextError(f"{where}: expected a list of {cls.__name__}")


def _check_score_map(v: object, where: str) -> None:
    if not isinstance(v, dict):
        raise InvalidContextError(f"{where}: expected a mapping, got {type(v).__name__}")
    for k, s in v.items():
        _check_str(k, f"{where} key")
        _check_num(s, f"{where}[{k}]")


def _list_field(d: dict, key: str, where: str) -> list:
    v = d[key]
    if not isinstance(v, list):
        raise InvalidContextError(f"{where}.{key}: expected a list, got {type(v).__name__}")
    return v


@dataclass(frozen=True)
class CandidateSummary:
    """源 run 的候选 checkpoint。只有到达终态的候选才会触发规划(R5)。"""

    id: str
    status: str  # "effective" | "rejected"
    reject_reason: str | None  # effective 时必须为 None

    _KEYS = ("id", "status", "reject_reason")

    def __post_init__(self) -> None:
        _check_str(self.id, "candidate.id")
        _check_choice(self.status, CANDIDATE_TERMINAL_STATES, "candidate.status")
        _check_opt_str(self.reject_reason, "candidate.reject_reason")
        if self.status == "effective" and self.reject_reason is not None:
            raise InvalidContextError("candidate.reject_reason: must be None when status is 'effective'")

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "reject_reason": self.reject_reason}

    @classmethod
    def from_dict(cls, d: dict) -> CandidateSummary:
        _exact_keys(d, cls._KEYS, "candidate")
        return cls(id=d["id"], status=d["status"], reject_reason=d["reject_reason"])


@dataclass(frozen=True)
class RegressionMark:
    """一条退化标记的摘要。kind 照抄诊断报告;subject 是维度(social_regression)、探针名
    (g_probe_failure)或 "reward"(reward_hacking_suspected);magnitude 是下降量、探针分数或 reward_delta - diag_delta。"""

    kind: str
    subject: str
    magnitude: float

    _KEYS = ("kind", "subject", "magnitude")

    def __post_init__(self) -> None:
        _check_str(self.kind, "regression_flag.kind")
        _check_str(self.subject, "regression_flag.subject")
        _check_num(self.magnitude, "regression_flag.magnitude")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "magnitude": self.magnitude}

    @classmethod
    def from_dict(cls, d: dict) -> RegressionMark:
        _exact_keys(d, cls._KEYS, "regression_flag")
        return cls(kind=d["kind"], subject=d["subject"], magnitude=d["magnitude"])


@dataclass(frozen=True)
class ErrorProfileSummary:
    """一个二级维度的错误类型分布,照抄诊断报告的 ErrorProfile(去掉 track)。"""

    l2_dimension: str
    error_type_distribution: dict[str, float]

    _KEYS = ("l2_dimension", "error_type_distribution")

    def __post_init__(self) -> None:
        _check_str(self.l2_dimension, "error_profile.l2_dimension")
        _check_score_map(self.error_type_distribution, "error_profile.error_type_distribution")

    def to_dict(self) -> dict:
        return {
            "l2_dimension": self.l2_dimension,
            "error_type_distribution": dict(self.error_type_distribution),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ErrorProfileSummary:
        _exact_keys(d, cls._KEYS, "error_profile")
        dist = d["error_type_distribution"]
        if not isinstance(dist, dict):
            raise InvalidContextError(
                f"error_profile.error_type_distribution: expected a mapping, got {type(dist).__name__}"
            )
        return cls(
            l2_dimension=d["l2_dimension"],
            error_type_distribution=dict(d["error_type_distribution"]),
        )


@dataclass(frozen=True)
class DiagnosticSummary:
    """源 run 诊断报告里规划用得上的部分。l1_scores 至少一项:没有一级分数就无从规划。"""

    l1_scores: dict[str, float]
    regression_flags: list[RegressionMark]
    error_profiles: list[ErrorProfileSummary]
    benchmark_version: str
    grade: str  # "mock" | "real";"stub" 永远不是可接受证据,进不了规划

    _KEYS = ("l1_scores", "regression_flags", "error_profiles", "benchmark_version", "grade")

    def __post_init__(self) -> None:
        _check_score_map(self.l1_scores, "diagnostic.l1_scores")
        if not self.l1_scores:
            raise InvalidContextError("diagnostic.l1_scores: must name at least one dimension")
        _check_list_of(self.regression_flags, RegressionMark, "diagnostic.regression_flags")
        _check_list_of(self.error_profiles, ErrorProfileSummary, "diagnostic.error_profiles")
        _check_str(self.benchmark_version, "diagnostic.benchmark_version")
        _check_choice(self.grade, PLANNABLE_GRADES, "diagnostic.grade")

    def to_dict(self) -> dict:
        return {
            "l1_scores": dict(self.l1_scores),
            "regression_flags": [f.to_dict() for f in self.regression_flags],
            "error_profiles": [p.to_dict() for p in self.error_profiles],
            "benchmark_version": self.benchmark_version,
            "grade": self.grade,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiagnosticSummary:
        _exact_keys(d, cls._KEYS, "diagnostic")
        if not isinstance(d["l1_scores"], dict):
            raise InvalidContextError("diagnostic.l1_scores: expected a mapping")
        return cls(
            l1_scores=dict(d["l1_scores"]),
            regression_flags=[
                RegressionMark.from_dict(x) for x in _list_field(d, "regression_flags", "diagnostic")
            ],
            error_profiles=[
                ErrorProfileSummary.from_dict(x)
                for x in _list_field(d, "error_profiles", "diagnostic")
            ],
            benchmark_version=d["benchmark_version"],
            grade=d["grade"],
        )


@dataclass(frozen=True)
class RecipeSummary:
    """RecipeView 去掉 payload 的摘要。mean_gain 为 None 表示还没有可比的战绩。"""

    id: str
    recipe_type: str  # "batch" | "interact" | "agent_rsi"
    dimension: str
    status: str
    mean_gain: float | None
    n_reuse: int

    _KEYS = ("id", "recipe_type", "dimension", "status", "mean_gain", "n_reuse")

    def __post_init__(self) -> None:
        _check_str(self.id, "recipe.id")
        _check_choice(self.recipe_type, RECIPE_TYPES, "recipe.recipe_type")
        _check_str(self.dimension, "recipe.dimension")
        _check_str(self.status, "recipe.status")
        _check_opt_num(self.mean_gain, "recipe.mean_gain")
        _check_count(self.n_reuse, "recipe.n_reuse")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "recipe_type": self.recipe_type,
            "dimension": self.dimension,
            "status": self.status,
            "mean_gain": self.mean_gain,
            "n_reuse": self.n_reuse,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecipeSummary:
        _exact_keys(d, cls._KEYS, "recipe")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class DecisionOutcome:
    """一条历史决策与它产生的那一轮的结果。candidate_status 为 None 表示那一轮还没有候选。"""

    decision_id: str
    next_run_id: str
    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    source_recipe_id: str | None
    candidate_status: str | None
    gain: float | None

    _KEYS = (
        "decision_id", "next_run_id", "target_dimension", "action",
        "source_recipe_id", "candidate_status", "gain",
    )

    def __post_init__(self) -> None:
        _check_str(self.decision_id, "recent_decision.decision_id")
        _check_str(self.next_run_id, "recent_decision.next_run_id")
        _check_str(self.target_dimension, "recent_decision.target_dimension")
        _check_choice(self.action, DECISION_ACTIONS, "recent_decision.action")
        _check_opt_str(self.source_recipe_id, "recent_decision.source_recipe_id")
        _check_opt_str(self.candidate_status, "recent_decision.candidate_status")
        _check_opt_num(self.gain, "recent_decision.gain")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict) -> DecisionOutcome:
        _exact_keys(d, cls._KEYS, "recent_decision")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class CostGainEntry:
    """近 K 轮里一轮的成本与增益。成本为 None 表示没有记录,不是 0:没记录不能读成免费。
    键始终在场,值可以是 None;渲染进提示时写成 "not recorded"。"""

    run_id: str
    target_dimension: str
    gpu_hours: float | None
    tokens: int | None
    gain: float | None

    _KEYS = ("run_id", "target_dimension", "gpu_hours", "tokens", "gain")

    def __post_init__(self) -> None:
        _check_str(self.run_id, "cost_gain.run_id")
        _check_str(self.target_dimension, "cost_gain.target_dimension")
        _check_opt_nonneg_num(self.gpu_hours, "cost_gain.gpu_hours")
        _check_opt_count(self.tokens, "cost_gain.tokens")
        _check_opt_num(self.gain, "cost_gain.gain")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict) -> CostGainEntry:
        _exact_keys(d, cls._KEYS, "cost_gain")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class PlanningContext:
    """一次规划的全部输入。run_id 是候选到达终态、触发这次规划的源 run。"""

    run_id: str
    candidate: CandidateSummary
    diagnostic: DiagnosticSummary
    recipes: list[RecipeSummary] = field(default_factory=list)
    recent_decisions: list[DecisionOutcome] = field(default_factory=list)
    cost_gain: list[CostGainEntry] = field(default_factory=list)

    _KEYS = ("run_id", "candidate", "diagnostic", "recipes", "recent_decisions", "cost_gain")

    def __post_init__(self) -> None:
        _check_str(self.run_id, "run_id")
        if not isinstance(self.candidate, CandidateSummary):
            raise InvalidContextError("candidate: expected a CandidateSummary")
        if not isinstance(self.diagnostic, DiagnosticSummary):
            raise InvalidContextError("diagnostic: expected a DiagnosticSummary")
        _check_list_of(self.recipes, RecipeSummary, "recipes")
        _check_list_of(self.recent_decisions, DecisionOutcome, "recent_decisions")
        _check_list_of(self.cost_gain, CostGainEntry, "cost_gain")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "candidate": self.candidate.to_dict(),
            "diagnostic": self.diagnostic.to_dict(),
            "recipes": [r.to_dict() for r in self.recipes],
            "recent_decisions": [r.to_dict() for r in self.recent_decisions],
            "cost_gain": [c.to_dict() for c in self.cost_gain],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlanningContext:
        _exact_keys(d, cls._KEYS, "context")
        return cls(
            run_id=d["run_id"],
            candidate=CandidateSummary.from_dict(d["candidate"]),
            diagnostic=DiagnosticSummary.from_dict(d["diagnostic"]),
            recipes=[RecipeSummary.from_dict(x) for x in _list_field(d, "recipes", "context")],
            recent_decisions=[
                DecisionOutcome.from_dict(x) for x in _list_field(d, "recent_decisions", "context")
            ],
            cost_gain=[CostGainEntry.from_dict(x) for x in _list_field(d, "cost_gain", "context")],
        )
