from __future__ import annotations

from diagnostic.datamodel import (
    ConsistencySignals,
    DimensionScore,
    DiagnosticReport,
    ErrorProfile,
    RegressionFlags,
)


def build_report(
    *,
    report_id: str,
    candidate_ref: str,
    baseline_ref: str,
    benchmark_version: str,
    run_tier: str,
    sampling_k: int,
    dimension_scores: list[DimensionScore],
    error_profiles: list[ErrorProfile],
    regression_flags: RegressionFlags,
    consistency_signals: ConsistencySignals,
    run_id: str,
) -> DiagnosticReport:
    return DiagnosticReport(
        id=report_id,
        candidate_ref=candidate_ref,
        baseline_ref=baseline_ref,
        benchmark_version=benchmark_version,
        run_tier=run_tier,
        sampling_k=sampling_k,
        dimension_scores=dimension_scores,
        error_profiles=error_profiles,
        regression_flags=regression_flags,
        consistency_signals=consistency_signals,
        links=[run_id],
    )
