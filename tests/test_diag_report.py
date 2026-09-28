from diagnostic.datamodel import (
    DimensionScore, ErrorProfile, RegressionFlags, ConsistencySignals,
)
from diagnostic.report import build_report


def test_build_report_assembles_all_sections():
    dims = [DimensionScore("empathy", "l1", 0.8, 0.72, 0.87, 100, "fused")]
    errs = [ErrorProfile("belief", {"surface_match": 1.0}, "static")]
    rf = RegressionFlags(flags=[])
    cs = ConsistencySignals({"belief": 0.02}, 0.1)
    report = build_report(
        report_id="diag-1", candidate_ref="cand-1", baseline_ref="zengen-v3",
        benchmark_version="bench-v1", run_tier="full", sampling_k=4,
        dimension_scores=dims, error_profiles=errs,
        regression_flags=rf, consistency_signals=cs, run_id="run-1",
    )
    assert report.id == "diag-1"
    assert report.benchmark_version == "bench-v1"
    assert report.dimension_scores == dims
    assert "run-1" in report.links
