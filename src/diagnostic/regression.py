"""Regression and OPD-failure detector (§2.5.6).

This module only produces flags + evidence. It makes NO promotion decisions —
promotion/gating logic lives elsewhere. Detects:
  - Statistically significant per-dimension score drops vs a baseline
    (CI-gated: only flagged when candidate/baseline CIs are disjoint).
  - G-probe failures (OPD/format-compliance style guardrail probes) that
    fall below a threshold — these are monitoring-only signals, not part
    of the capability score.
  - Reward-hacking suspicion when training reward rises while the
    diagnostic score falls or diverges beyond a threshold.

Cross-version comparisons are refused (hard constraint, §2.5.8) via
`CrossVersionError` when the candidate and baseline benchmark versions are
not comparable.
"""

from __future__ import annotations

from diagnostic.benchmark_version import BenchmarkVersion, is_comparable
from diagnostic.datamodel import DimensionScore, RegressionFlags
from diagnostic.stats import difference_is_significant


class CrossVersionError(Exception):
    pass


def detect_regressions(
    candidate_scores: list[DimensionScore],
    baseline_scores: list[DimensionScore],
    *,
    baseline_version: str,
    candidate_bench: BenchmarkVersion | None = None,
    baseline_bench: BenchmarkVersion | None = None,
) -> RegressionFlags:
    """Compare candidate vs baseline DimensionScores, paired by
    (dimension, level, track_provenance).

    Flags a regression only when the candidate score is lower AND the two
    confidence intervals are disjoint (statistically significant drop).
    Drops within overlapping CIs, or improvements, are not flagged.

    Raises CrossVersionError if both benchmark versions are given and are
    not comparable (cross-version scores are not directly comparable).
    """
    if candidate_bench is not None and baseline_bench is not None:
        if not is_comparable(candidate_bench, baseline_bench):
            raise CrossVersionError(
                "candidate and baseline benchmark versions are not comparable"
            )

    baseline_by_key = {
        (b.dimension, b.level, b.track_provenance): b for b in baseline_scores
    }

    flags: list[dict] = []
    for cand in candidate_scores:
        base = baseline_by_key.get((cand.dimension, cand.level, cand.track_provenance))
        if base is None:
            continue
        if cand.score < base.score and difference_is_significant(
            (cand.ci_low, cand.ci_high), (base.ci_low, base.ci_high)
        ):
            flags.append(
                {
                    "dimension": cand.dimension,
                    "kind": "social_regression",
                    "drop": base.score - cand.score,
                    "baseline_version": baseline_version,
                    "beyond_ci": True,
                    "level": cand.level,
                    "track": cand.track_provenance,
                }
            )

    return RegressionFlags(flags=flags)


def detect_g_probe_failure(g_scores: dict, threshold: float = 0.7) -> list[dict]:
    """Flag G-probe (OPD guardrail) scores that fall below threshold.

    G-probes are monitoring-only and do not contribute to capability
    scoring, but a failure is a signal of OPD breakdown.
    """
    flags: list[dict] = []
    for probe, score in g_scores.items():
        if score < threshold:
            flags.append({"probe": probe, "kind": "g_probe_failure", "score": score})
    return flags


def detect_reward_hacking(
    diag_delta: float, reward_delta: float, threshold: float = 0.15
) -> dict | None:
    """Flag suspected reward hacking when reward rises while the
    diagnostic score falls or diverges beyond threshold.
    """
    if reward_delta - diag_delta > threshold:
        return {
            "kind": "reward_hacking_suspected",
            "diag_delta": diag_delta,
            "reward_delta": reward_delta,
        }
    return None
