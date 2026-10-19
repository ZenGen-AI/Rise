from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VariantFamily:
    family_id: str
    base_item_id: str
    variant_item_ids: list[str]

    def to_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "base_item_id": self.base_item_id,
            "variant_item_ids": list(self.variant_item_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> VariantFamily:
        return cls(
            family_id=d["family_id"],
            base_item_id=d["base_item_id"],
            variant_item_ids=list(d["variant_item_ids"]),
        )


@dataclass
class SampleOutcome:
    """一道题的一次判定。真实运行器返回它;`extracted` 是抽出的答案字母,不是模型的完整输出。"""

    score: int
    extracted: str
    error_reason: str | None

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or self.score not in (0, 1):
            raise ValueError(f"score must be 0 or 1, got {self.score!r}")

    def to_dict(self) -> dict:
        return {"score": self.score, "extracted": self.extracted, "error_reason": self.error_reason}

    @classmethod
    def from_dict(cls, d: dict) -> SampleOutcome:
        return cls(score=d["score"], extracted=d["extracted"], error_reason=d["error_reason"])


@dataclass
class DiagnosticItem:
    id: str
    l3_task: str
    item_type: str
    scoring_mode: str
    track: str
    reference_answer: str | None
    rubric: dict | None
    family_id: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "l3_task": self.l3_task,
            "item_type": self.item_type,
            "scoring_mode": self.scoring_mode,
            "track": self.track,
            "reference_answer": self.reference_answer,
            "rubric": dict(self.rubric) if self.rubric is not None else None,
            "family_id": self.family_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiagnosticItem:
        return cls(
            id=d["id"],
            l3_task=d["l3_task"],
            item_type=d["item_type"],
            scoring_mode=d["scoring_mode"],
            track=d["track"],
            reference_answer=d["reference_answer"],
            rubric=dict(d["rubric"]) if d["rubric"] is not None else None,
            family_id=d["family_id"],
        )


@dataclass
class DiagnosticTaxonomy:
    version: str
    l1_dimensions: list[str]
    l2_dimensions: dict[str, list[str]]
    l3_tasks: dict[str, list[str]]
    g_probes: list[str]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "l1_dimensions": list(self.l1_dimensions),
            "l2_dimensions": {k: list(v) for k, v in self.l2_dimensions.items()},
            "l3_tasks": {k: list(v) for k, v in self.l3_tasks.items()},
            "g_probes": list(self.g_probes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiagnosticTaxonomy:
        return cls(
            version=d["version"],
            l1_dimensions=list(d["l1_dimensions"]),
            l2_dimensions={k: list(v) for k, v in d["l2_dimensions"].items()},
            l3_tasks={k: list(v) for k, v in d["l3_tasks"].items()},
            g_probes=list(d["g_probes"]),
        )


@dataclass
class ItemResult:
    item_id: str
    raw_outputs: list[str]
    per_sample_scores: list[float]
    pass_rate: float
    variance: float
    track: str

    @property
    def n_samples(self) -> int:
        return len(self.raw_outputs)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "raw_outputs": list(self.raw_outputs),
            "per_sample_scores": list(self.per_sample_scores),
            "pass_rate": self.pass_rate,
            "variance": self.variance,
            "track": self.track,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ItemResult:
        return cls(
            item_id=d["item_id"],
            raw_outputs=list(d["raw_outputs"]),
            per_sample_scores=list(d["per_sample_scores"]),
            pass_rate=d["pass_rate"],
            variance=d["variance"],
            track=d["track"],
        )


@dataclass
class DimensionScore:
    dimension: str
    level: str
    score: float
    ci_low: float
    ci_high: float
    sample_size: int
    track_provenance: str

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "level": self.level,
            "score": self.score,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "sample_size": self.sample_size,
            "track_provenance": self.track_provenance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DimensionScore:
        return cls(
            dimension=d["dimension"],
            level=d["level"],
            score=d["score"],
            ci_low=d["ci_low"],
            ci_high=d["ci_high"],
            sample_size=d["sample_size"],
            track_provenance=d["track_provenance"],
        )


@dataclass
class ErrorProfile:
    l2_dimension: str
    error_type_distribution: dict[str, float]
    track: str

    def to_dict(self) -> dict:
        return {
            "l2_dimension": self.l2_dimension,
            "error_type_distribution": dict(self.error_type_distribution),
            "track": self.track,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ErrorProfile:
        return cls(
            l2_dimension=d["l2_dimension"],
            error_type_distribution=dict(d["error_type_distribution"]),
            track=d["track"],
        )


@dataclass
class RegressionFlags:
    flags: list[dict]

    def to_dict(self) -> dict:
        return {
            "flags": [dict(f) for f in self.flags],
        }

    @classmethod
    def from_dict(cls, d: dict) -> RegressionFlags:
        return cls(
            flags=[dict(f) for f in d["flags"]],
        )


@dataclass
class ConsistencySignals:
    rule_vs_judge_divergence: dict[str, float]
    diagnostic_vs_reward_divergence: float

    def to_dict(self) -> dict:
        return {
            "rule_vs_judge_divergence": dict(self.rule_vs_judge_divergence),
            "diagnostic_vs_reward_divergence": self.diagnostic_vs_reward_divergence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConsistencySignals:
        return cls(
            rule_vs_judge_divergence=dict(d["rule_vs_judge_divergence"]),
            diagnostic_vs_reward_divergence=d["diagnostic_vs_reward_divergence"],
        )


@dataclass
class DiagnosticReport:
    id: str
    candidate_ref: str
    baseline_ref: str
    benchmark_version: str
    run_tier: str
    sampling_k: int
    dimension_scores: list[DimensionScore]
    error_profiles: list[ErrorProfile]
    regression_flags: RegressionFlags
    consistency_signals: ConsistencySignals
    links: list[str]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "candidate_ref": self.candidate_ref,
            "baseline_ref": self.baseline_ref,
            "benchmark_version": self.benchmark_version,
            "run_tier": self.run_tier,
            "sampling_k": self.sampling_k,
            "dimension_scores": [d.to_dict() for d in self.dimension_scores],
            "error_profiles": [ep.to_dict() for ep in self.error_profiles],
            "regression_flags": self.regression_flags.to_dict(),
            "consistency_signals": self.consistency_signals.to_dict(),
            "links": list(self.links),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiagnosticReport:
        return cls(
            id=d["id"],
            candidate_ref=d["candidate_ref"],
            baseline_ref=d["baseline_ref"],
            benchmark_version=d["benchmark_version"],
            run_tier=d["run_tier"],
            sampling_k=d["sampling_k"],
            dimension_scores=[DimensionScore.from_dict(ds) for ds in d["dimension_scores"]],
            error_profiles=[ErrorProfile.from_dict(ep) for ep in d["error_profiles"]],
            regression_flags=RegressionFlags.from_dict(d["regression_flags"]),
            consistency_signals=ConsistencySignals.from_dict(d["consistency_signals"]),
            links=list(d["links"]),
        )
