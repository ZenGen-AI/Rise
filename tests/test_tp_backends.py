import json

import pytest

from inference_bridge import BridgeError, ScriptedBridge
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend, ScriptedDecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.decision import parse_decision
from trainplanner.examples import example_context_dict
from trainplanner.prompt import render_prompt


def _packed(mutate=None):
    d = example_context_dict()
    if mutate is not None:
        mutate(d)
    return render_prompt(PlanningContext.from_dict(d), "{{context}}", "v1")


def _recipe(rid, dimension, *, mean_gain, n_reuse=0, status="effective", recipe_type="batch"):
    return {"id": rid, "recipe_type": recipe_type, "dimension": dimension, "status": status,
            "mean_gain": mean_gain, "n_reuse": n_reuse}


def _heuristic(mutate=None):
    text = HeuristicBackend().decide(_packed(mutate), run_id="run-003", task_id="t")
    json.loads(text)  # 输出本身就是合法 JSON
    return parse_decision(text)


def test_heuristic_clones_the_recipe_of_the_lowest_dimension():
    dec = _heuristic()
    assert (dec.target_dimension, dec.action, dec.source_recipe_id) == ("tom", "clone", "recipe-run-001")
    assert dec.mode is None and dec.perturbation == {}
    assert "tom" in dec.rationale and "0.58" in dec.rationale


def test_heuristic_starts_a_new_batch_when_the_dimension_has_no_recipe():
    dec = _heuristic(lambda d: d["diagnostic"]["l1_scores"].update(empathy=0.1))
    assert dec.target_dimension == "empathy"
    assert dec.action == "clone" and dec.source_recipe_id == "recipe-run-002"

    dec = _heuristic(lambda d: d["diagnostic"]["l1_scores"].update(pragmatics=0.1))
    assert (dec.target_dimension, dec.action, dec.mode, dec.source_recipe_id) == (
        "pragmatics", "new", "batch", None)


def test_heuristic_breaks_score_ties_by_dimension_name():
    dec = _heuristic(lambda d: d["diagnostic"].update(l1_scores={"zeta": 0.2, "alpha": 0.2}))
    assert dec.target_dimension == "alpha"


def test_heuristic_ranks_recipes_by_track_then_reuse_then_id():
    def recipes(d):
        d["recipes"] = [
            _recipe("recipe-c", "tom", mean_gain=None, n_reuse=9),
            _recipe("recipe-b", "tom", mean_gain=0.02, n_reuse=1),
            _recipe("recipe-a", "tom", mean_gain=0.02, n_reuse=1),
            _recipe("recipe-d", "tom", mean_gain=0.02, n_reuse=3),
            _recipe("recipe-e", "tom", mean_gain=-0.01, n_reuse=5),
        ]

    assert _heuristic(recipes).source_recipe_id == "recipe-d"


@pytest.mark.parametrize(
    "recipe",
    [
        _recipe("recipe-x", "tom", mean_gain=0.5, status="deprecated"),
        _recipe("recipe-x", "tom", mean_gain=0.5, recipe_type="agent_rsi"),
        _recipe("recipe-x", "tom", mean_gain=0.5, recipe_type="interact"),
        _recipe("recipe-x", "empathy", mean_gain=0.5),
    ],
)
def test_heuristic_ignores_recipes_it_cannot_clone(recipe):
    dec = _heuristic(lambda d: d.update(recipes=[recipe]))
    assert (dec.action, dec.mode) == ("new", "batch")


def test_heuristic_ignores_the_prompt_text():
    packed = _packed()
    a = HeuristicBackend().decide(packed, run_id="r", task_id="t")
    b = HeuristicBackend().decide(packed.with_feedback(("attempt 1: whatever",)), run_id="r", task_id="t")
    assert a == b
    assert HeuristicBackend().name == "heuristic"


class _RecordingBridge:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def complete(self, prompt, *, run_id, task_id):
        self.calls.append((prompt, run_id, task_id))
        return self.inner.complete(prompt, run_id=run_id, task_id=task_id)


def test_bridge_backend_sends_the_full_prompt_and_returns_the_text():
    bridge = _RecordingBridge(ScriptedBridge(["{\"a\": 1}"]))
    backend = BridgeDecisionBackend(bridge, name="claude_cli")
    packed = _packed().with_feedback(("attempt 1: parse error: output is empty",))
    assert backend.decide(packed, run_id="run-003", task_id="planner-attempt-2") == '{"a": 1}'
    assert bridge.calls == [(packed.prompt, "run-003", "planner-attempt-2")]
    assert backend.name == "claude_cli"


class _FailingBridge:
    def __init__(self, err):
        self.err = err

    def complete(self, prompt, *, run_id, task_id):
        raise self.err


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_bridge_backend_lets_bridge_errors_through_unchanged(category):
    err = BridgeError(category, "cli timed out")
    backend = BridgeDecisionBackend(_FailingBridge(err))
    with pytest.raises(BridgeError) as info:
        backend.decide(_packed(), run_id="run-003", task_id="t")
    assert info.value is err
    assert info.value.category == category
    assert backend.name == "bridge"


def test_bridge_backend_needs_a_name():
    with pytest.raises(ValueError, match="name"):
        BridgeDecisionBackend(ScriptedBridge([]), name="")


def test_scripted_backend_replays_answers_and_records_calls():
    boom = RuntimeError("boom")
    backend = ScriptedDecisionBackend(["one", boom, "two"])
    packed = _packed()
    assert backend.decide(packed, run_id="r", task_id="a") == "one"
    with pytest.raises(RuntimeError) as info:
        backend.decide(packed, run_id="r", task_id="b")
    assert info.value is boom
    assert backend.decide(packed, run_id="r", task_id="c") == "two"
    assert [c[2] for c in backend.calls] == ["a", "b", "c"]
    with pytest.raises(AssertionError, match="exhausted"):
        backend.decide(packed, run_id="r", task_id="d")
