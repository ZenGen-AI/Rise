"""已判好的静态题(SoMEval 打分):SampleOutcome 往返、转 ItemResult、编排器不重打分、报告带 someval 块。"""
from __future__ import annotations

import pytest

from controlplane.gitstore import GitStore
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, SampleOutcome
from diagnostic.orchestrator import DiagnosticConfig, run_diagnostic
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import (
    MockSoMEvalRunner,
    run_static_items_scored,
)
from diagnostic.store_adapter import read_report
from diagnostic.taxonomy_loader import ItemPool


class ScoredRunner:
    """只实现 evaluate_scored 的运行器;evaluate 被调用就断言失败,证明 someval 题走了新路径。"""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def evaluate(self, items, candidate_ref, k):
        raise AssertionError("someval items must not go through evaluate()")

    def evaluate_scored(self, items, candidate_ref, k):
        self.calls.append((candidate_ref, k))
        return {item.id: self.outcomes[item.id] for item in items}


def _outcome(score, extracted="", reason=None):
    return SampleOutcome(score=score, extracted=extracted, error_reason=reason)


def _taxonomy():
    return DiagnosticTaxonomy(
        "tax-v1", ["d1"], {"d1": ["d1.1"]}, {"d1.1": ["d1.1.1"]}, [])


def _pool(items, families=()):
    return ItemPool("pool-v1", list(items), list(families))


def _someval_item(item_id, l3="d1.1.1"):
    return DiagnosticItem(item_id, l3, "mcq", "someval", "static", None, None, None)


def test_sample_outcome_round_trips():
    outcome = _outcome(1, "C", None)
    assert SampleOutcome.from_dict(outcome.to_dict()) == outcome


@pytest.mark.parametrize("score", [0.5, 2, -1, True])
def test_a_score_that_is_not_zero_or_one_is_refused(score):
    with pytest.raises(ValueError):
        SampleOutcome(score=score, extracted="", error_reason=None)


def test_scored_results_carry_the_extracted_letter_not_the_full_output():
    item = _someval_item("q1")
    runner = ScoredRunner({"q1": [_outcome(1, "C")]})
    results = run_static_items_scored([item], runner, "cand-1", 1)
    assert results[0].raw_outputs == ["C"]
    assert results[0].per_sample_scores == [1]
    assert results[0].pass_rate == 1.0
    assert results[0].track == "static"


def test_a_scored_runner_is_called_once_with_the_candidate_and_k():
    runner = ScoredRunner({"q1": [_outcome(0, "A", "wrong_answer")]})
    run_static_items_scored([_someval_item("q1")], runner, "cand-7", 1)
    assert runner.calls == [("cand-7", 1)]


def test_a_pool_with_someval_items_refuses_a_runner_that_cannot_score_them():
    pool = _pool([_someval_item("q1")])
    runner = MockSoMEvalRunner(fixed={"q1": ["C"]})
    cfg = DiagnosticConfig("run-1", "cand-1", "cand-1", "none", "full", 1, "bench-v1")
    with pytest.raises(ValueError, match="evaluate_scored"):
        run_diagnostic(cfg, _taxonomy(), pool, runner, MockJudgeBackend("judge", {}), _store(),
                       baseline_scores=[], dynamic_specs=[], backends="real")


def test_the_orchestrator_does_not_rescore_a_someval_item():
    """抽出的字母恰好等于参考答案,但 SoMEval 判它错:编排器必须信 SoMEval。"""
    item = _someval_item("q1")
    runner = ScoredRunner({"q1": [_outcome(0, "C", "wrong_answer")]})
    report = run_diagnostic(
        DiagnosticConfig("run-1", "cand-1", "cand-1", "none", "full", 1, "bench-v1"),
        _taxonomy(), _pool([item]), runner, MockJudgeBackend("judge", {}), _store(),
        baseline_scores=[], dynamic_specs=[], backends="real",
    )
    l3 = [d for d in report.dimension_scores if d.level == "l3"][0]
    assert l3.score == 0.0


def test_rule_items_and_someval_items_can_share_a_run():
    rule_item = DiagnosticItem("r1", "d1.1.1", "mcq", "rule", "static", "B", None, None)
    scored_item = _someval_item("q1")
    pool = _pool([rule_item, scored_item])

    class BothRunners(ScoredRunner):
        def evaluate(self, items, candidate_ref, k):
            return {item.id: ["B"] * k for item in items}

    runner = BothRunners({"q1": [_outcome(1, "C")]})
    report = run_diagnostic(
        DiagnosticConfig("run-1", "cand-1", "cand-1", "none", "full", 1, "bench-v1"),
        _taxonomy(), pool, runner, MockJudgeBackend("judge", {}), _store(),
        baseline_scores=[], dynamic_specs=[], backends="real",
    )
    l3 = [d for d in report.dimension_scores if d.level == "l3"][0]
    assert l3.sample_size == 2 and l3.score == 1.0


def test_the_report_payload_carries_the_someval_block_verbatim():
    store = _store()
    item = _someval_item("q1")
    stats = {"protocol": "cot", "samples": 3481, "content_none": 3}
    run_diagnostic(
        DiagnosticConfig("run-9", "cand-1", "cand-1", "none", "full", 1, "bench-v1"),
        _taxonomy(), _pool([item]), ScoredRunner({"q1": [_outcome(1, "C")]}),
        MockJudgeBackend("judge", {}), store,
        baseline_scores=[], dynamic_specs=[], backends="real", someval=stats,
    )
    payload = store.read_product("control-plane/runs/run-9/diagnostic_report.yaml").payload
    assert payload["someval"] == stats
    assert "someval" not in read_report(store, "run-9").to_dict()


def test_without_the_block_the_payload_has_no_someval_key():
    store = _store()
    item = _someval_item("q1")
    run_diagnostic(
        DiagnosticConfig("run-1", "cand-1", "cand-1", "none", "full", 1, "bench-v1"),
        _taxonomy(), _pool([item]), ScoredRunner({"q1": [_outcome(1, "C")]}),
        MockJudgeBackend("judge", {}), store,
        baseline_scores=[], dynamic_specs=[], backends="real",
    )
    assert "someval" not in store.read_product("control-plane/runs/run-1/diagnostic_report.yaml").payload


def test_a_report_with_a_someval_block_but_no_backends_is_still_a_stub():
    from controlplane.evidence import diagnostic_grade

    assert diagnostic_grade({"someval": {"samples": 1}, "dimension_scores": [{"dimension": "d1"}]}) == "stub"


def _store():
    import tempfile

    store = GitStore(tempfile.mkdtemp())
    store.init()
    return store
