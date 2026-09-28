import pytest
from diagnostic.datamodel import DimensionScore, RegressionFlags
from diagnostic.benchmark_version import BenchmarkVersion
from diagnostic.regression import (
    detect_regressions, detect_g_probe_failure, detect_reward_hacking,
    CrossVersionError,
)


def _ds(dim, score, lo, hi, level="l2", track="fused"):
    return DimensionScore(dim, level, score, lo, hi, 40, track)


def test_significant_drop_is_flagged_with_evidence():
    cand = [_ds("empathy", 0.60, 0.52, 0.68)]
    base = [_ds("empathy", 0.85, 0.80, 0.90)]
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert len(rf.flags) == 1
    f = rf.flags[0]
    assert f["dimension"] == "empathy"
    assert f["beyond_ci"] is True
    assert f["baseline_version"] == "zengen-v3"
    assert f["drop"] > 0


def test_drop_within_ci_not_flagged():
    cand = [_ds("empathy", 0.82, 0.75, 0.89)]
    base = [_ds("empathy", 0.85, 0.80, 0.90)]   # 区间重叠
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert rf.flags == []


def test_improvement_not_flagged():
    cand = [_ds("empathy", 0.95, 0.90, 0.98)]
    base = [_ds("empathy", 0.70, 0.62, 0.78)]
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert rf.flags == []


def test_g_probe_failure_flagged_separately():
    # G 组不参与能力分,但失效是 OPD 崩坏信号
    flags = detect_g_probe_failure({"format_compliance": 0.4, "basic_reasoning": 0.95},
                                   threshold=0.7)
    assert any(f["probe"] == "format_compliance" and f["kind"] == "g_probe_failure"
               for f in flags)
    assert all(f["probe"] != "basic_reasoning" for f in flags)


def test_reward_hacking_suspected_when_diag_and_reward_diverge():
    # 训练 reward 高涨但诊断分下降 → 嫌疑
    f = detect_reward_hacking(diag_delta=-0.05, reward_delta=+0.20, threshold=0.15)
    assert f is not None
    assert f["kind"] == "reward_hacking_suspected"


def test_reward_hacking_not_flagged_when_aligned():
    assert detect_reward_hacking(diag_delta=+0.10, reward_delta=+0.12, threshold=0.15) is None


def test_per_track_l3_collision_still_flags_single_track_drop():
    # 同一 dimension + 同一 level(l3),但 static/dynamic 两条 track 各有一条基线/候选。
    # 仅按 dimension 分组会在 dict 中相互覆盖,导致 static 的真实回归被漏检。
    cand = [
        _ds("fbt", 0.40, 0.30, 0.50, level="l3", track="static"),
        _ds("fbt", 0.90, 0.80, 0.96, level="l3", track="dynamic"),
    ]
    base = [
        _ds("fbt", 0.90, 0.80, 0.96, level="l3", track="static"),
        _ds("fbt", 0.40, 0.30, 0.50, level="l3", track="dynamic"),
    ]
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert len(rf.flags) == 1
    f = rf.flags[0]
    assert f["dimension"] == "fbt"
    assert f["track"] == "static"
    assert f["level"] == "l3"
    assert f["drop"] == pytest.approx(0.50)
    assert f["beyond_ci"] is True


def test_cross_version_comparison_refused():
    cand = [_ds("empathy", 0.6, 0.5, 0.7)]
    base = [_ds("empathy", 0.85, 0.8, 0.9)]
    v_a = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    v_b = BenchmarkVersion("tax-v2", "pool-v1", "sv-1", "jv-1", "cv-1")  # taxonomy 变
    with pytest.raises(CrossVersionError):
        detect_regressions(cand, base, baseline_version="zengen-v3",
                           candidate_bench=v_a, baseline_bench=v_b)
