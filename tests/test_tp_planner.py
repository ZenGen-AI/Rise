import json

import pytest

from inference_bridge import BridgeError, ScriptedBridge
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend, ScriptedDecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.planner import PlannerFailedError, PlannerResult, attempt_task_id, plan
from trainplanner.prompt import PromptRenderError
from trainplanner.templates import PLANNER_V1, PLANNER_V1_VERSION

GOOD = json.dumps({
    "target_dimension": "tom", "action": "new", "mode": "batch", "source_recipe_id": None,
    "perturbation": {}, "data_synthesis_hint": "", "rationale": "tom is lowest",
})
CLONE = json.dumps({
    "target_dimension": "tom", "action": "clone", "mode": None, "source_recipe_id": "recipe-run-001",
    "perturbation": {"grpo_config.learning_rate": 1e-5}, "data_synthesis_hint": "", "rationale": "reuse",
})


@pytest.fixture
def ctx():
    return PlanningContext.from_dict(example_context_dict())


def _plan(ctx, backend, **kw):
    return plan(ctx, PLANNER_V1, PLANNER_V1_VERSION, backend, run_id="run-003", **kw)


def test_a_good_first_answer_is_returned(ctx):
    backend = ScriptedDecisionBackend([GOOD], name="claude_cli")
    result = _plan(ctx, backend)
    assert isinstance(result, PlannerResult)
    assert (result.attempts, result.backend, result.template_version) == (1, "claude_cli", "v1")
    assert result.decision.action == "new"
    assert result.rationale == "tom is lowest"
    assert result.rejected == ()
    prompt, run_id, task_id = backend.calls[0]
    assert (run_id, task_id) == ("run-003", "planner-attempt-1")
    assert "## Rejected attempts" not in prompt


def test_record_fields_match_the_planner_decision_payload(ctx):
    fields = _plan(ctx, ScriptedDecisionBackend([CLONE])).record_fields()
    assert fields == {
        "template_version": "v1",
        "backend": "scripted",
        "decision": {
            "target_dimension": "tom", "action": "clone", "mode": None,
            "source_recipe_id": "recipe-run-001", "perturbation": {"grpo_config.learning_rate": 1e-5},
            "data_synthesis_hint": "",
        },
        "rationale": "reuse",
    }


def test_a_parse_failure_is_fed_back_and_resampled(ctx):
    backend = ScriptedDecisionBackend(["not json", GOOD])
    result = _plan(ctx, backend)
    assert result.attempts == 2
    assert result.rejected == (
        "attempt 1: parse error: output is not a single JSON value: Expecting value at char 0",
    )
    second_prompt = backend.calls[1][0]
    assert second_prompt.endswith(
        "## Rejected attempts\n"
        "Your previous output was rejected for the reasons below. "
        "Fix them and output only one JSON object.\n"
        "- attempt 1: parse error: output is not a single JSON value: Expecting value at char 0\n"
    )
    assert [c[2] for c in backend.calls] == [attempt_task_id(1), attempt_task_id(2)]


def test_a_validator_violation_is_fed_back_and_resampled(ctx):
    seen = []

    def validator(decision):
        seen.append(decision.action)
        return "recipe recipe-run-001 is not effective" if decision.action == "clone" else None

    backend = ScriptedDecisionBackend([CLONE, GOOD])
    result = _plan(ctx, backend, validator=validator)
    assert seen == ["clone", "new"]
    assert result.attempts == 2 and result.decision.action == "new"
    assert "- attempt 1: invalid decision: recipe recipe-run-001 is not effective\n" in backend.calls[1][0]


def test_parse_failures_and_violations_share_one_budget(ctx):
    backend = ScriptedDecisionBackend(["oops", CLONE, "{}", GOOD])
    with pytest.raises(PlannerFailedError) as info:
        _plan(ctx, backend, validator=lambda d: "no clones today")
    err = info.value
    assert err.attempts == 3
    assert err.reason == "attempt 3: parse error: missing keys: " + str([
        "target_dimension", "action", "mode", "source_recipe_id",
        "perturbation", "data_synthesis_hint", "rationale",
    ])
    assert [r.split(":")[1].strip() for r in err.reasons] == ["parse error", "invalid decision", "parse error"]
    assert len(backend.calls) == 3  # 第四个罐头回答没被取走
    third_prompt = backend.calls[2][0]
    assert "- attempt 1: parse error:" in third_prompt
    assert "- attempt 2: invalid decision: no clones today" in third_prompt


def test_zero_retries_means_one_attempt(ctx):
    with pytest.raises(PlannerFailedError) as info:
        _plan(ctx, ScriptedDecisionBackend(["nope", GOOD]), max_parse_retries=0)
    assert info.value.attempts == 1
    assert str(info.value).startswith("planner gave up after 1 attempts: attempt 1: parse error")


def test_more_retries_allow_more_attempts(ctx):
    result = _plan(ctx, ScriptedDecisionBackend(["a", "b", "c", "d", GOOD]), max_parse_retries=4)
    assert result.attempts == 5


@pytest.mark.parametrize("bad", [-1, 1.5, True, None])
def test_the_retry_limit_must_be_a_non_negative_integer(ctx, bad):
    with pytest.raises(ValueError, match="max_parse_retries"):
        _plan(ctx, ScriptedDecisionBackend([GOOD]), max_parse_retries=bad)


def test_run_id_is_required(ctx):
    with pytest.raises(ValueError, match="run_id"):
        plan(ctx, PLANNER_V1, "v1", ScriptedDecisionBackend([GOOD]), run_id="")


class _GarbageThenFailBridge:
    """第一次返回不合规文本,第二次抛 BridgeError,之后若还被调用就说明 plan 吞了异常。"""

    def __init__(self, err):
        self.err = err
        self.calls = 0

    def complete(self, prompt, *, run_id, task_id):
        self.calls += 1
        if self.calls == 1:
            return ScriptedBridge(["garbage"]).complete(prompt, run_id=run_id, task_id=task_id)
        if self.calls == 2:
            raise self.err
        raise AssertionError("plan kept sampling after a BridgeError")


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_a_bridge_error_propagates_unchanged_and_ends_the_attempts(ctx, category):
    # T2 也原样上抛,不当成解析失败去重采样(见本任务第二条 Ruling)
    err = BridgeError(category, "cli exited 1")
    bridge = _GarbageThenFailBridge(err)
    with pytest.raises(BridgeError) as info:
        _plan(ctx, BridgeDecisionBackend(bridge))
    assert info.value is err
    assert info.value.category == category
    assert bridge.calls == 2


def test_a_template_error_is_not_retried(ctx):
    backend = ScriptedDecisionBackend([GOOD])
    with pytest.raises(PromptRenderError):
        plan(ctx, "{{budget}}", "v1", backend, run_id="run-003")
    assert backend.calls == []


@pytest.mark.parametrize("bad", ["", "   ", 3])
def test_a_validator_must_return_none_or_a_reason(ctx, bad):
    with pytest.raises(TypeError, match="validator"):
        _plan(ctx, ScriptedDecisionBackend([GOOD]), validator=lambda d: bad)


def test_the_heuristic_backend_plans_through_the_same_path(ctx):
    result = _plan(ctx, HeuristicBackend())
    assert (result.backend, result.attempts) == ("heuristic", 1)
    assert (result.decision.action, result.decision.source_recipe_id) == ("clone", "recipe-run-001")


def test_the_bridge_backend_plans_end_to_end_with_a_fenced_answer(ctx):
    bridge = ScriptedBridge(["```json\n" + GOOD + "\n```"])
    result = _plan(ctx, BridgeDecisionBackend(bridge, name="claude_cli"))
    assert (result.backend, result.decision.mode) == ("claude_cli", "batch")


def test_the_planner_does_not_mutate_the_context(ctx):
    before = ctx.to_dict()
    _plan(ctx, ScriptedDecisionBackend(["x", GOOD]))
    assert ctx.to_dict() == before
