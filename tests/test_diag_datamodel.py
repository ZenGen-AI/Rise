from diagnostic.datamodel import (
    VariantFamily, DiagnosticItem, DiagnosticTaxonomy,
    ItemResult, ErrorProfile, DimensionScore,
    RegressionFlags, ConsistencySignals, DiagnosticReport,
)


def test_variant_family_round_trip():
    vf = VariantFamily(
        family_id="vf-001",
        base_item_id="item-001",
        variant_item_ids=["item-001-v1", "item-001-v2"],
    )
    assert VariantFamily.from_dict(vf.to_dict()) == vf


def test_diagnostic_item_round_trip_with_track_and_scoring_mode():
    item = DiagnosticItem(
        id="item-001",
        l3_task="false_belief_transfer",
        item_type="mcq",
        scoring_mode="rule",
        track="static",
        reference_answer="B",
        rubric=None,
        family_id="vf-001",
    )
    assert DiagnosticItem.from_dict(item.to_dict()) == item


def test_diagnostic_item_judge_open_ended():
    item = DiagnosticItem(
        id="item-002",
        l3_task="persuasion_maintenance",
        item_type="open",
        scoring_mode="judge",
        track="dynamic",
        reference_answer=None,
        rubric={"criteria": ["goal_completion", "relationship", "social_norm"]},
        family_id=None,
    )
    assert DiagnosticItem.from_dict(item.to_dict()) == item


def test_taxonomy_round_trip_with_g_probes():
    tax = DiagnosticTaxonomy(
        version="tax-v1",
        l1_dimensions=["mental_representation", "social_interaction", "norm_internalization"],
        l2_dimensions={"mental_representation": ["belief", "intention"]},
        l3_tasks={"belief": ["false_belief_transfer"]},
        g_probes=["basic_reasoning", "instruction_following", "format_compliance"],
    )
    assert DiagnosticTaxonomy.from_dict(tax.to_dict()) == tax


def test_item_result_carries_k_samples_pass_rate_variance():
    r = ItemResult(
        item_id="item-001",
        raw_outputs=["B", "B", "A", "B"],
        per_sample_scores=[1.0, 1.0, 0.0, 1.0],
        pass_rate=0.75,
        variance=0.1875,
        track="static",
    )
    assert ItemResult.from_dict(r.to_dict()) == r
    assert r.n_samples == 4


def test_dimension_score_requires_track_provenance():
    ds = DimensionScore(
        dimension="mental_representation",
        level="l1",
        score=0.82,
        ci_low=0.75,
        ci_high=0.89,
        sample_size=120,
        track_provenance="fused",
    )
    assert DimensionScore.from_dict(ds.to_dict()) == ds


def test_error_profile_round_trip():
    ep = ErrorProfile(
        l2_dimension="belief",
        error_type_distribution={
            "reasoning_break": 0.3, "surface_match": 0.5,
            "format_fail": 0.1, "refusal": 0.05, "unstable": 0.05,
        },
        track="static",
    )
    assert ErrorProfile.from_dict(ep.to_dict()) == ep


def test_regression_flags_each_flag_carries_evidence():
    rf = RegressionFlags(flags=[
        {"dimension": "empathy", "kind": "social_regression",
         "drop": 0.06, "baseline_version": "zengen-v3", "beyond_ci": True},
    ])
    assert RegressionFlags.from_dict(rf.to_dict()) == rf


def test_consistency_signals_round_trip():
    cs = ConsistencySignals(
        rule_vs_judge_divergence={"belief": 0.02},
        diagnostic_vs_reward_divergence=0.15,
    )
    assert ConsistencySignals.from_dict(cs.to_dict()) == cs


def test_diagnostic_report_full_assembly():
    report = DiagnosticReport(
        id="diag-run-001",
        candidate_ref="cand-run-001",
        baseline_ref="zengen-v3",
        benchmark_version="bench-v1",
        run_tier="full",
        sampling_k=4,
        dimension_scores=[],
        error_profiles=[],
        regression_flags=RegressionFlags(flags=[]),
        consistency_signals=ConsistencySignals(
            rule_vs_judge_divergence={}, diagnostic_vs_reward_divergence=0.0),
        links=["run-001"],
    )
    assert DiagnosticReport.from_dict(report.to_dict()) == report
