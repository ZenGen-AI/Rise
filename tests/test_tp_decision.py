import json

import pytest

from trainplanner.decision import DECISION_KEYS, DecisionParseError, PlannerDecision, parse_decision


def _new(**over):
    d = {
        "target_dimension": "tom",
        "action": "new",
        "mode": "batch",
        "source_recipe_id": None,
        "perturbation": {},
        "data_synthesis_hint": "false-belief dialogues",
        "rationale": "tom is the weakest dimension",
    }
    d.update(over)
    return d


def _clone(**over):
    d = _new(action="clone", mode=None, source_recipe_id="recipe-run-001",
             perturbation={"grpo_config.learning_rate": 5e-6})
    d.update(over)
    return d


def _replay(**over):
    d = _new(action="replay", mode=None, source_recipe_id="recipe-run-001")
    d.update(over)
    return d


def _text(d):
    return json.dumps(d)


def test_a_valid_new_decision_parses():
    dec = parse_decision(_text(_new()))
    assert dec == PlannerDecision(
        target_dimension="tom", action="new", mode="batch", source_recipe_id=None,
        perturbation={}, data_synthesis_hint="false-belief dialogues",
        rationale="tom is the weakest dimension",
    )


def test_valid_clone_and_replay_decisions_parse():
    assert parse_decision(_text(_clone())).perturbation == {"grpo_config.learning_rate": 5e-6}
    assert parse_decision(_text(_replay())).source_recipe_id == "recipe-run-001"


def test_to_dict_has_the_seven_keys_and_round_trips():
    dec = parse_decision(_text(_clone()))
    assert tuple(dec.to_dict()) == DECISION_KEYS
    assert PlannerDecision.from_dict(dec.to_dict()) == dec
    assert parse_decision(json.dumps(dec.to_dict())) == dec


def test_decision_payload_drops_only_the_rationale():
    payload = parse_decision(_text(_new())).decision_payload()
    assert set(payload) == set(DECISION_KEYS) - {"rationale"}


def test_surrounding_whitespace_is_allowed():
    assert parse_decision("\n  " + _text(_new()) + "  \n").action == "new"


@pytest.mark.parametrize("opening", ["```json\n", "```\n", "```json  \n"])
def test_one_markdown_fence_around_the_object_is_allowed(opening):
    assert parse_decision(f"  {opening}{_text(_new())}\n```\n").action == "new"


@pytest.mark.parametrize(
    "text, match",
    [
        ("Here you go:\n```json\n" + json.dumps(_new()) + "\n```", "not a single JSON value"),
        ("```json\n" + json.dumps(_new()) + "\n```\nHope this helps", "exactly one fenced block"),
        ("```json\n" + json.dumps(_new()) + "\n```\n```json\n{}\n```", "not a single JSON value"),
        ("```python\n" + json.dumps(_new()) + "\n```", "exactly one fenced block"),
        ("```json\n" + json.dumps(_new()), "exactly one fenced block"),
        ("```json\n```", "output is empty"),
        ("", "output is empty"),
        ("   ", "output is empty"),
        ("Sure! " + json.dumps(_new()), "not a single JSON value"),
        (json.dumps(_new()) + " thanks", "not a single JSON value"),
        (json.dumps(_new()) + json.dumps(_new()), "not a single JSON value"),
        ("[" + json.dumps(_new()) + "]", "must be a JSON object"),
        ('"tom"', "must be a JSON object"),
        ("{'action': 'new'}", "not a single JSON value"),
    ],
)
def test_anything_but_one_json_object_is_rejected(text, match):
    with pytest.raises(DecisionParseError, match=match):
        parse_decision(text)


def test_duplicate_keys_are_rejected():
    text = '{"action": "new", ' + _text(_new())[1:]
    with pytest.raises(DecisionParseError, match="duplicate key 'action'"):
        parse_decision(text)


def test_non_finite_numbers_are_rejected():
    text = _text(_clone()).replace("5e-06", "NaN")
    with pytest.raises(DecisionParseError, match="non-finite number NaN"):
        parse_decision(text)


@pytest.mark.parametrize("key", DECISION_KEYS)
def test_every_key_is_required(key):
    d = _new()
    del d[key]
    with pytest.raises(DecisionParseError, match=f"missing keys: \\['{key}'\\]"):
        parse_decision(_text(d))


def test_unknown_keys_are_rejected():
    with pytest.raises(DecisionParseError, match=r"unknown keys: \['confidence'\]"):
        parse_decision(_text(_new(confidence=0.9)))


@pytest.mark.parametrize(
    "decision, match",
    [
        (_new(mode=None), "action 'new' requires mode"),
        (_new(mode="agent_rsi"), "mode must be one of"),
        (_new(source_recipe_id="recipe-run-001"), "action 'new' must not name a source_recipe_id"),
        (_new(perturbation={"sft_steps": 1}), "action 'new' must not carry a perturbation"),
        (_clone(source_recipe_id=None), "action 'clone' requires source_recipe_id"),
        (_clone(mode="batch"), "action 'clone' must leave mode null"),
        (_replay(source_recipe_id=None), "action 'replay' requires source_recipe_id"),
        (_replay(perturbation={"sft_steps": 1}), "action 'replay' must not carry a perturbation"),
        (_replay(mode="interact"), "action 'replay' must leave mode null"),
        (_new(action="retrain"), "action must be one of"),
        (_new(rationale=""), "rationale must be a non-empty string"),
        (_new(rationale="   "), "rationale must be a non-empty string"),
        (_new(rationale=None), "rationale must be a non-empty string"),
        (_new(target_dimension=""), "target_dimension must be a non-empty string"),
        (_new(target_dimension=3), "target_dimension must be a non-empty string"),
        (_clone(source_recipe_id=""), "source_recipe_id must be a non-empty string or null"),
        (_new(perturbation=None), "perturbation must be a JSON object"),
        (_clone(perturbation={"sft_steps": [1, 2]}), "must be a string, number or boolean"),
        (_clone(perturbation={"sft_steps": None}), "must be a string, number or boolean"),
        (_clone(perturbation={"": 1}), "perturbation keys must be non-empty"),
        (_new(data_synthesis_hint=None), "data_synthesis_hint must be a string"),
    ],
)
def test_the_rules_are_enforced(decision, match):
    with pytest.raises(DecisionParseError, match=match):
        parse_decision(_text(decision))


def test_an_empty_hint_is_allowed():
    assert parse_decision(_text(_new(data_synthesis_hint=""))).data_synthesis_hint == ""


def test_the_error_carries_a_readable_reason():
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_clone(source_recipe_id=None)))
    assert info.value.reason == "action 'clone' requires source_recipe_id"
    assert str(info.value) == info.value.reason


def test_non_text_output_is_rejected():
    with pytest.raises(DecisionParseError, match="output must be text"):
        parse_decision(None)


def test_a_huge_integer_perturbation_value_is_a_scalar():
    text = _text(_clone()).replace("5e-06", "1" + "0" * 399)  # 400 位整数,转 float 会 OverflowError
    assert parse_decision(text).perturbation == {"grpo_config.learning_rate": 10**399}


@pytest.mark.parametrize(
    "text",
    [
        "1" * 5000,  # 超过 4300 位:json.loads 抛普通 ValueError,不是 JSONDecodeError
        "[" * 100000,  # 3.11–3.13 抛 RecursionError,3.14 抛 JSONDecodeError
    ],
    ids=["5000-digit-integer", "deep-open-brackets"],
)
def test_pathological_json_becomes_a_parse_error(text):
    with pytest.raises(DecisionParseError, match="not a single JSON value"):
        parse_decision(text)


def test_from_dict_rejects_a_non_string_perturbation_key():
    with pytest.raises(DecisionParseError, match="perturbation keys must be non-empty strings"):
        PlannerDecision.from_dict(_clone(perturbation={1: 2}))


def test_echoed_values_are_clipped():
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_new(action="x" * 500)))
    reason = info.value.reason
    assert reason.endswith("…") and len(reason) < 200
    assert "'" + "x" * 79 + "…" in reason
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_clone(perturbation={"k" * 300: ["v" * 300]})))
    assert len(info.value.reason) < 250 and info.value.reason.count("…") == 2
