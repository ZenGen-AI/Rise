import pytest
from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.scorer import (
    RuleScorer, JudgeBackend, MockJudgeBackend, LLMJudgeScorer,
    SelfEvalBiasError,
)


def _mcq():
    return DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, None)


def _open():
    return DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic",
                          None, {"criteria": ["goal_completion"], "prompt_version": "judge-v1"}, None)


def test_rule_scorer_exact_match():
    s = RuleScorer()
    assert s.score(_mcq(), "B") == 1.0
    assert s.score(_mcq(), "A") == 0.0


def test_rule_scorer_normalizes_whitespace_and_case():
    s = RuleScorer()
    assert s.score(_mcq(), "  b ") == 1.0


def test_rule_scorer_score_samples_returns_item_result():
    s = RuleScorer()
    r = s.score_samples(_mcq(), ["B", "B", "A", "B"])
    assert isinstance(r, ItemResult)
    assert r.pass_rate == 0.75
    assert r.track == "static"


def test_mock_judge_returns_fixed_scores():
    judge = MockJudgeBackend(model_id="judge-model-x",
                             fixed={("i2", 0): 0.8, ("i2", 1): 0.6})
    scorer = LLMJudgeScorer(judge, candidate_model_id="cand-model")
    r = scorer.score_samples(_open(), ["resp0", "resp1"])
    assert r.per_sample_scores == [0.8, 0.6]
    assert r.pass_rate == 0.7
    assert r.track == "dynamic"


def test_judge_must_not_be_candidate_or_near_relative():
    judge = MockJudgeBackend(model_id="cand-model-sft", fixed={})
    with pytest.raises(SelfEvalBiasError):
        LLMJudgeScorer(judge, candidate_model_id="cand-model")


def test_judge_backend_is_protocol_swappable():
    class _Stub:
        model_id = "external-judge"
        def judge(self, item, response, sample_index): return 0.5
    scorer = LLMJudgeScorer(_Stub(), candidate_model_id="cand-model")
    r = scorer.score_samples(_open(), ["x", "y"])
    assert r.per_sample_scores == [0.5, 0.5]
