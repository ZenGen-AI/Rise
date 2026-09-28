from diagnostic.datamodel import ItemResult, VariantFamily
from diagnostic.error_classifier import (
    classify_static_family, classify_dynamic_consistency,
    aggregate_error_profile,
)


def _r(item_id, pass_rate, variance=0.0, track="static", outputs=None):
    n = 4
    return ItemResult(item_id, outputs or ["x"] * n, [pass_rate] * n,
                      pass_rate, variance, track)


def test_surface_match_base_passes_variants_fail():
    fam = VariantFamily("vf1", "base", ["v1", "v2"])
    results = {"base": _r("base", 1.0), "v1": _r("v1", 0.0), "v2": _r("v2", 0.0)}
    assert classify_static_family(fam, results) == "surface_match"


def test_reasoning_break_base_fails():
    fam = VariantFamily("vf1", "base", ["v1"])
    results = {"base": _r("base", 0.0), "v1": _r("v1", 0.0)}
    assert classify_static_family(fam, results) == "reasoning_break"


def test_unstable_high_variance():
    fam = VariantFamily("vf1", "base", ["v1"])
    results = {"base": _r("base", 0.5, variance=0.25), "v1": _r("v1", 0.5, variance=0.25)}
    assert classify_static_family(fam, results) == "unstable"


def test_format_fail_detected_from_outputs():
    fam = VariantFamily("vf1", "base", [])
    results = {"base": _r("base", 0.0, outputs=["<malformed", "<malformed", "<malformed", "<malformed"])}
    assert classify_static_family(fam, results, format_validator=lambda o: o.startswith("<")) == "format_fail"


def test_refusal_detected():
    fam = VariantFamily("vf1", "base", [])
    results = {"base": _r("base", 0.0, outputs=["I can't help", "I refuse", "no", "no"])}
    assert classify_static_family(fam, results,
        refusal_detector=lambda o: "can't" in o or "refuse" in o) == "refusal"


def test_dynamic_consistency_flip_flops():
    utterances = ["I agree completely", "actually I disagree", "yes agree", "no way"]
    assert classify_dynamic_consistency(
        utterances, stance_fn=lambda u: "agree" in u and "disagree" not in u) == "inconsistency"


def test_dynamic_consistency_stable_returns_none():
    utterances = ["I agree", "still agree", "agree"]
    assert classify_dynamic_consistency(
        utterances, stance_fn=lambda u: "agree" in u) is None


def test_aggregate_error_profile_normalizes_distribution():
    labels = ["surface_match", "surface_match", "reasoning_break", "unstable"]
    ep = aggregate_error_profile("belief", labels, track="static")
    assert ep.l2_dimension == "belief"
    assert abs(ep.error_type_distribution["surface_match"] - 0.5) < 1e-9
    assert abs(sum(ep.error_type_distribution.values()) - 1.0) < 1e-9
