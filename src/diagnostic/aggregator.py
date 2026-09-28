from __future__ import annotations

from diagnostic.datamodel import DimensionScore, DiagnosticTaxonomy, ItemResult
from diagnostic.stats import aggregate_pass_rates, wilson_interval


def aggregate_l3(
    l3_task: str, item_results: list[ItemResult], track: str
) -> DimensionScore:
    """Pool per-item pass rates/sample sizes into a single L3 DimensionScore with a Wilson CI."""
    rate_and_n = [(r.pass_rate, r.n_samples) for r in item_results]
    score, n = aggregate_pass_rates(rate_and_n)
    lo, hi = wilson_interval(round(score * n), n)
    return DimensionScore(
        dimension=l3_task,
        level="l3",
        score=score,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        track_provenance=track,
    )


def aggregate_l2(
    l2: str,
    l3_scores: list[DimensionScore],
    taxonomy: DiagnosticTaxonomy,
    track: str,
) -> DimensionScore:
    """Roll up the L3 scores belonging to an L2 dimension into a single L2 DimensionScore."""
    members = set(taxonomy.l3_tasks[l2])
    selected = [ds for ds in l3_scores if ds.dimension in members]
    rate_and_n = [(ds.score, ds.sample_size) for ds in selected]
    score, n = aggregate_pass_rates(rate_and_n)
    lo, hi = wilson_interval(round(score * n), n)
    return DimensionScore(
        dimension=l2,
        level="l2",
        score=score,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        track_provenance=track,
    )


def aggregate_l1(
    l1: str,
    l2_scores: list[DimensionScore],
    taxonomy: DiagnosticTaxonomy,
    track: str,
) -> DimensionScore:
    """Roll up the L2 scores belonging to an L1 dimension into a single L1 DimensionScore."""
    members = set(taxonomy.l2_dimensions[l1])
    selected = [ds for ds in l2_scores if ds.dimension in members]
    rate_and_n = [(ds.score, ds.sample_size) for ds in selected]
    score, n = aggregate_pass_rates(rate_and_n)
    lo, hi = wilson_interval(round(score * n), n)
    return DimensionScore(
        dimension=l1,
        level="l1",
        score=score,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        track_provenance=track,
    )


def fuse_tracks(
    static_score: DimensionScore, dynamic_score: DimensionScore
) -> DimensionScore:
    """Sample-size-weighted merge of a static and dynamic DimensionScore for the same dimension."""
    rate_and_n = [
        (static_score.score, static_score.sample_size),
        (dynamic_score.score, dynamic_score.sample_size),
    ]
    score, n = aggregate_pass_rates(rate_and_n)
    lo, hi = wilson_interval(round(score * n), n)
    return DimensionScore(
        dimension=static_score.dimension,
        level=static_score.level,
        score=score,
        ci_low=lo,
        ci_high=hi,
        sample_size=n,
        track_provenance="fused",
    )
