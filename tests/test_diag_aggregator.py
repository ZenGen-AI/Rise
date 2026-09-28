from diagnostic.datamodel import ItemResult, DimensionScore, DiagnosticTaxonomy
from diagnostic.aggregator import (
    aggregate_l3, aggregate_l2, aggregate_l1, fuse_tracks,
)


def _r(item_id, pass_rate, track="static", n=4):
    return ItemResult(item_id, ["x"] * n, [pass_rate] * n, pass_rate, 0.0, track)


def _tax():
    return DiagnosticTaxonomy(
        "tax-v1",
        ["mental_representation"],
        {"mental_representation": ["belief", "intention"]},
        {"belief": ["false_belief_transfer"], "intention": ["intent_infer"]},
        ["basic_reasoning"],
    )


def test_aggregate_l3_pools_item_samples_with_ci():
    results = [_r("i1", 0.75), _r("i2", 0.75)]  # 各 3/4,合并 6/8
    ds = aggregate_l3("false_belief_transfer", results, track="static")
    assert ds.level == "l3"
    assert ds.dimension == "false_belief_transfer"
    assert ds.sample_size == 8
    assert abs(ds.score - 0.75) < 1e-9
    assert ds.ci_low < ds.score < ds.ci_high
    assert ds.track_provenance == "static"


def test_aggregate_l2_rolls_up_l3():
    l3s = [
        DimensionScore("false_belief_transfer", "l3", 0.75, 0.5, 0.9, 8, "static"),
        DimensionScore("intent_infer", "l3", 0.5, 0.3, 0.7, 8, "static"),
    ]
    # l2 belief 只含 false_belief_transfer
    ds = aggregate_l2("belief", l3s, _tax(), track="static")
    assert ds.level == "l2"
    assert ds.dimension == "belief"
    assert abs(ds.score - 0.75) < 1e-9
    assert ds.track_provenance == "static"


def test_aggregate_l1_rolls_up_l2():
    l2s = [
        DimensionScore("belief", "l2", 0.75, 0.5, 0.9, 8, "static"),
        DimensionScore("intention", "l2", 0.5, 0.3, 0.7, 8, "static"),
    ]
    ds = aggregate_l1("mental_representation", l2s, _tax(), track="static")
    assert ds.level == "l1"
    assert ds.sample_size == 16


def test_fuse_tracks_marks_provenance_fused():
    static_ds = DimensionScore("belief", "l2", 0.9, 0.8, 0.95, 40, "static")
    dynamic_ds = DimensionScore("belief", "l2", 0.5, 0.35, 0.65, 20, "dynamic")
    fused = fuse_tracks(static_ds, dynamic_ds)
    assert fused.track_provenance == "fused"
    # 融合是样本量加权,动态低分不被静态高分掩盖(分数在两者之间且低于纯静态)
    assert dynamic_ds.score < fused.score < static_ds.score
    assert fused.sample_size == 60
