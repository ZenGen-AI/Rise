"""上下文第七段与 PLANNER_V2:向后兼容、渲染稳定(设计 5、6.2)。"""
import json

import pytest

from trainplanner.context import InvalidContextError, PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, render_prompt
from trainplanner.templates import PLANNER_V1, PLANNER_V2, PLANNER_V2_VERSION


def test_six_key_context_still_parses():
    ctx = PlanningContext.from_dict(example_context_dict())
    assert ctx.strategy_experience == []


def test_seventh_key_round_trips():
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2 · 证据 2 条"]
    ctx = PlanningContext.from_dict(raw)
    assert ctx.to_dict()["strategy_experience"] == raw["strategy_experience"]


def test_unknown_keys_are_still_rejected():
    raw = example_context_dict()
    raw["strategy_experiance"] = []
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


def test_entries_must_be_non_empty_strings():
    raw = example_context_dict()
    raw["strategy_experience"] = [""]
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


def test_v2_declares_the_placeholder_and_v1_does_not():
    assert "{{strategy_experience}}" in PLANNER_V2
    assert "{{strategy_experience}}" not in PLANNER_V1
    assert "strategy_experience" in PLACEHOLDERS
    assert PLANNER_V2_VERSION == "v2"


def test_empty_section_renders_as_an_empty_list():
    ctx = PlanningContext.from_dict(example_context_dict())
    packed = render_prompt(ctx, PLANNER_V2, PLANNER_V2_VERSION)
    assert "[]" in packed.text


def test_v1_rendering_is_unchanged_by_the_new_field():
    ctx = PlanningContext.from_dict(example_context_dict())
    before = render_prompt(ctx, PLANNER_V1, "v1").text
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2"]
    after = render_prompt(PlanningContext.from_dict(raw), PLANNER_V1, "v1").text
    assert before == after
