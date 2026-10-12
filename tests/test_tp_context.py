import json

import pytest

from trainplanner.context import (
    CandidateSummary,
    CostGainEntry,
    DecisionOutcome,
    DiagnosticSummary,
    ErrorProfileSummary,
    InvalidContextError,
    PlanningContext,
    RecipeSummary,
    RegressionMark,
)
from trainplanner.examples import example_context_dict


def test_the_example_round_trips_exactly():
    d = example_context_dict()
    ctx = PlanningContext.from_dict(d)
    assert ctx.to_dict() == d
    assert json.loads(json.dumps(ctx.to_dict())) == d  # 纯 JSON 可序列化


def test_from_dict_builds_the_typed_substructures():
    ctx = PlanningContext.from_dict(example_context_dict())
    assert isinstance(ctx.candidate, CandidateSummary)
    assert isinstance(ctx.diagnostic, DiagnosticSummary)
    assert isinstance(ctx.diagnostic.regression_flags[0], RegressionMark)
    assert isinstance(ctx.diagnostic.error_profiles[0], ErrorProfileSummary)
    assert isinstance(ctx.recipes[0], RecipeSummary)
    assert isinstance(ctx.recent_decisions[0], DecisionOutcome)
    assert isinstance(ctx.cost_gain[0], CostGainEntry)
    assert ctx.diagnostic.l1_scores["tom"] == 0.58


def test_example_context_dict_returns_a_fresh_copy():
    a = example_context_dict()
    a["diagnostic"]["l1_scores"]["tom"] = 0.0
    assert example_context_dict()["diagnostic"]["l1_scores"]["tom"] == 0.58


def test_the_list_sections_default_to_empty():
    d = example_context_dict()
    ctx = PlanningContext(
        run_id="run-1",
        candidate=CandidateSummary.from_dict(d["candidate"]),
        diagnostic=DiagnosticSummary.from_dict(d["diagnostic"]),
    )
    assert ctx.to_dict()["recipes"] == []
    assert ctx.to_dict()["recent_decisions"] == []
    assert ctx.to_dict()["cost_gain"] == []


@pytest.mark.parametrize(
    "path",
    [
        ("run_id",),
        ("candidate", "reject_reason"),
        ("diagnostic", "grade"),
        ("diagnostic", "benchmark_version"),
        ("recipes", 0, "n_reuse"),
        ("recent_decisions", 0, "gain"),
        ("cost_gain", 1, "tokens"),
        ("diagnostic", "regression_flags", 0, "magnitude"),
        ("diagnostic", "error_profiles", 0, "l2_dimension"),
    ],
)
def test_a_missing_key_is_rejected_not_defaulted(path):
    d = example_context_dict()
    node = d
    for p in path[:-1]:
        node = node[p]
    del node[path[-1]]
    with pytest.raises(InvalidContextError, match=rf"missing keys \['{path[-1]}'\]$") as info:
        PlanningContext.from_dict(d)
    assert "unknown keys" not in str(info.value)  # 只拼非空的那一半


@pytest.mark.parametrize(
    "path",
    [(), ("candidate",), ("diagnostic",), ("recipes", 0), ("cost_gain", 0)],
)
def test_an_unknown_key_is_rejected(path):
    d = example_context_dict()
    node = d
    for p in path:
        node = node[p]
    node["surprise"] = 1
    with pytest.raises(InvalidContextError, match=r"unknown keys \['surprise'\]$") as info:
        PlanningContext.from_dict(d)
    assert "missing keys" not in str(info.value)


def test_missing_and_unknown_keys_are_reported_together():
    d = example_context_dict()
    del d["candidate"]["status"]
    d["candidate"]["surprise"] = 1
    with pytest.raises(
        InvalidContextError, match=r"^candidate: missing keys \['status'\], unknown keys \['surprise'\]$"
    ):
        PlanningContext.from_dict(d)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d["candidate"].update(status="pending_review"), "candidate.status"),
        (lambda d: d["diagnostic"].update(grade="stub"), "diagnostic.grade"),
        (lambda d: d["diagnostic"].update(l1_scores={}), "at least one dimension"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom="high"), "finite number"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom=True), "finite number"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom=float("nan")), "finite number"),
        (lambda d: d["recipes"][0].update(recipe_type="weird"), "recipe.recipe_type"),
        (lambda d: d["recipes"][0].update(n_reuse=-1), "non-negative integer"),
        (lambda d: d["recipes"][0].update(mean_gain="0.1"), "finite number"),
        (lambda d: d["recent_decisions"][0].update(action="retry"), "recent_decision.action"),
        (lambda d: d["cost_gain"][0].update(tokens=1.5), "non-negative integer"),
        (lambda d: d.update(run_id=""), "non-empty string"),
        (lambda d: d.update(run_id="   "), "non-empty string"),
        (lambda d: d.update(recipes={}), "expected a list"),
        (lambda d: d.update(candidate=[]), "expected a mapping"),
        (lambda d: d["candidate"].update(reject_reason="looked fine"), "must be None when status is 'effective'"),
        (
            lambda d: d["diagnostic"]["error_profiles"][0].update(error_type_distribution=[["literal_reading", 1.0]]),
            "error_profile.error_type_distribution: expected a mapping, got list",
        ),
        (
            lambda d: d["diagnostic"]["error_profiles"][0].update(error_type_distribution=5),
            "error_profile.error_type_distribution: expected a mapping, got int",
        ),
    ],
)
def test_wrong_types_and_values_are_rejected(mutate, match):
    d = example_context_dict()
    mutate(d)
    with pytest.raises(InvalidContextError, match=match):
        PlanningContext.from_dict(d)


def test_direct_construction_is_validated_too():
    with pytest.raises(InvalidContextError, match="candidate.status"):
        CandidateSummary(id="cand-run-1", status="approved", reject_reason=None)


def test_unrecorded_costs_stay_none_and_keep_their_keys():
    d = example_context_dict()
    entry = PlanningContext.from_dict(d).cost_gain[0]
    assert (entry.gpu_hours, entry.tokens) == (None, None)
    assert PlanningContext.from_dict(d).to_dict()["cost_gain"][0] == {
        "run_id": "run-002", "target_dimension": "empathy",
        "gpu_hours": None, "tokens": None, "gain": None,
    }


@pytest.mark.parametrize(
    "field, value, match",
    [
        ("gpu_hours", True, "finite number"),
        ("gpu_hours", float("inf"), "finite number"),
        ("gpu_hours", "1.5", "finite number"),
        ("gpu_hours", -0.5, "non-negative number"),
        ("tokens", -1, "non-negative integer"),
        ("tokens", True, "non-negative integer"),
    ],
)
def test_a_recorded_cost_is_still_validated(field, value, match):
    d = example_context_dict()
    d["cost_gain"][1][field] = value
    with pytest.raises(InvalidContextError, match=match):
        PlanningContext.from_dict(d)


def test_a_huge_integer_is_a_finite_number():
    d = example_context_dict()
    d["diagnostic"]["l1_scores"]["tom"] = 10**400  # 转 float 会 OverflowError;整数不该走那条路
    assert PlanningContext.from_dict(d).diagnostic.l1_scores["tom"] == 10**400


def test_a_rejected_candidate_carries_its_reason():
    d = example_context_dict()
    d["candidate"] = {"id": "cand-run-003", "status": "rejected", "reject_reason": "tom dropped"}
    assert PlanningContext.from_dict(d).candidate.reject_reason == "tom dropped"


def test_the_context_is_frozen():
    ctx = PlanningContext.from_dict(example_context_dict())
    with pytest.raises(AttributeError):
        ctx.run_id = "run-9"
