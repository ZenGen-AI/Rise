"""上下文第七段与 PLANNER_V2:向后兼容、渲染稳定(设计 5、6.2)。"""
import pytest

from trainplanner.context import InvalidContextError, PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, render_prompt
from trainplanner.templates import PLANNER_V1, PLANNER_V2, PLANNER_V2_VERSION


def test_six_key_context_still_parses():
    """旧产物没有第七键。示例字典自己已带这一键,所以要真的删掉再解析。"""
    raw = example_context_dict()
    del raw["strategy_experience"]
    ctx = PlanningContext.from_dict(raw)
    assert ctx.strategy_experience == []


@pytest.mark.parametrize("bad", ["abc", {"a": "b"}, None, 5, 0, ""])
def test_a_non_list_seventh_value_is_rejected(bad):
    """非 list 的第七段要被拒,不许静默强转:字符串会拆成字符,None 会变成空表。"""
    raw = example_context_dict()
    raw["strategy_experience"] = bad
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


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


def test_v2_renders_the_entry_between_the_cost_section_and_the_answer():
    """第七段真的出现在提示里,位置在成本段之后、作答要求之前。"""
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2 · 证据 2 条"]
    text = render_prompt(PlanningContext.from_dict(raw), PLANNER_V2, PLANNER_V2_VERSION).text
    assert "opd_coefficient=0.2" in text
    assert text.index("## Cost and gain") < text.index("opd_coefficient=0.2") < text.index("## Your answer")


def test_v1_rendering_is_unchanged_by_the_new_field():
    ctx = PlanningContext.from_dict(example_context_dict())
    before = render_prompt(ctx, PLANNER_V1, "v1").text
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2"]
    after = render_prompt(PlanningContext.from_dict(raw), PLANNER_V1, "v1").text
    assert before == after
