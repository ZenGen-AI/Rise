from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.someval_adapter import (
    SoMEvalRunner, MockSoMEvalRunner, run_static_items,
)


def _items():
    return [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, None),
        DiagnosticItem("i2", "norm_conflict", "mcq", "rule", "static", "A", None, None),
    ]


def test_mock_runner_returns_fixed_raw_outputs():
    runner = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "A", "B"],
        "i2": ["A", "A", "A", "A"],
    })
    out = runner.evaluate([_items()[0], _items()[1]], candidate_ref="cand-1", k=4)
    assert out["i1"] == ["B", "B", "A", "B"]
    assert out["i2"] == ["A", "A", "A", "A"]


def test_run_static_items_converts_to_item_result_with_track_static():
    runner = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "A", "B"],
        "i2": ["A", "A", "A", "A"],
    })
    results = run_static_items(_items(), runner, candidate_ref="cand-1", k=4)
    by_id = {r.item_id: r for r in results}
    assert by_id["i1"].track == "static"
    assert by_id["i1"].pass_rate == 0.75
    assert by_id["i1"].per_sample_scores == [1.0, 1.0, 0.0, 1.0]
    assert by_id["i2"].pass_rate == 1.0
    assert all(isinstance(r, ItemResult) for r in results)


def test_run_static_items_is_deterministic():
    runner = MockSoMEvalRunner(fixed={"i1": ["B", "A"], "i2": ["A", "B"]})
    a = run_static_items(_items(), runner, candidate_ref="cand-1", k=2)
    b = run_static_items(_items(), runner, candidate_ref="cand-1", k=2)
    assert [r.to_dict() for r in a] == [r.to_dict() for r in b]


def test_mock_runner_missing_item_raises_loud():
    import pytest
    runner = MockSoMEvalRunner(fixed={"i1": ["B"]})
    with pytest.raises(KeyError):
        runner.evaluate(_items(), candidate_ref="cand-1", k=1)
