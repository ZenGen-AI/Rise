import json

import pytest

from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, PackedContext, PromptRenderError, check_template, render_prompt


@pytest.fixture
def ctx():
    return PlanningContext.from_dict(example_context_dict())


def test_placeholders_are_replaced_and_the_version_is_recorded(ctx):
    packed = render_prompt(ctx, "run={{run_id}}\n{{ diagnostic }}", "v7")
    assert isinstance(packed, PackedContext)
    assert packed.template_version == "v7"
    assert packed.context is ctx
    assert packed.placeholders == ("diagnostic", "run_id")
    head, body = packed.text.split("\n", 1)
    assert head == "run=run-003"
    assert json.loads(body) == example_context_dict()["diagnostic"]


def test_every_placeholder_renders(ctx):
    template = "\n---\n".join("{{" + name + "}}" for name in PLACEHOLDERS)
    parts = render_prompt(ctx, template, "v1").text.split("\n---\n")
    d = example_context_dict()
    for entry in d["cost_gain"]:  # 未记录的成本渲染成 "not recorded"(见下一条测试)
        for k in ("gpu_hours", "tokens"):
            if entry[k] is None:
                entry[k] = "not recorded"
    assert parts[0] == "run-003"
    for name, part in zip(PLACEHOLDERS[1:-1], parts[1:-1]):
        assert json.loads(part) == d[name]
    assert json.loads(parts[-1]) == d


def test_structured_values_render_as_stable_sorted_json(ctx):
    text = render_prompt(ctx, "{{recipes}}", "v1").text
    assert text == json.dumps(example_context_dict()["recipes"], indent=2, sort_keys=True, ensure_ascii=False)
    assert render_prompt(ctx, "{{recipes}}", "v1").text == text


def test_unrecorded_costs_render_as_not_recorded_never_as_zero_or_null(ctx):
    for template in ("{{cost_gain}}", "{{context}}"):
        rendered = json.loads(render_prompt(ctx, template, "v1").text)
        entries = rendered if template == "{{cost_gain}}" else rendered["cost_gain"]
        assert entries[0]["gpu_hours"] == "not recorded"
        assert entries[0]["tokens"] == "not recorded"
        assert entries[1]["gpu_hours"] == 2.0 and entries[1]["tokens"] == 12000
        assert entries[0]["gain"] is None  # 只有成本改写,增益的 null 照旧
    text = render_prompt(ctx, "{{cost_gain}}", "v1").text
    assert '"gpu_hours": 0' not in text and '"tokens": 0' not in text
    assert '"gpu_hours": null' not in text and '"tokens": null' not in text
    assert ctx.cost_gain[0].gpu_hours is None  # 渲染不改上下文


def test_a_repeated_placeholder_is_replaced_everywhere(ctx):
    assert render_prompt(ctx, "{{run_id}} and {{run_id}}", "v1").text == "run-003 and run-003"


@pytest.mark.parametrize(
    "template, match",
    [
        ("{{run_id}} {{budget}}", r"unknown placeholders \['budget'\]"),
        ("{{Run_Id}}", "unknown placeholders"),
        ("{{run_id}} {{ }}", "line 1: '\\{\\{' does not open a valid placeholder"),
        ("{{run_id}}\n{{run-id}}", "line 2: '\\{\\{' does not open a valid placeholder"),
        ("{{run_id}} {{ run_id", "does not open a valid placeholder"),
        ("{{{run_id}}}", "does not open a valid placeholder"),
        ("no placeholders at all", "references no placeholder"),
        ("{run_id}", "references no placeholder"),
    ],
)
def test_bad_templates_raise_instead_of_leaving_blanks(ctx, template, match):
    with pytest.raises(PromptRenderError, match=match):
        render_prompt(ctx, template, "v1")


def test_a_lone_closing_brace_pair_is_plain_text(ctx):
    assert render_prompt(ctx, "{{run_id}} }} end", "v1").text == "run-003 }} end"


def test_substituted_values_are_not_rescanned():
    # 两个占位符:按名字依次 str.replace 的错误实现会把 reject_reason 里的 {{cost_gain}} 再替换一次
    d = example_context_dict()
    d["candidate"]["reject_reason"] = "contains {{cost_gain}} literally"
    d["candidate"]["status"] = "rejected"
    ctx = PlanningContext.from_dict(d)
    text = render_prompt(ctx, "{{candidate}}\n{{cost_gain}}", "v1").text
    assert "contains {{cost_gain}} literally" in text
    cost_json = json.dumps(
        json.loads(render_prompt(ctx, "{{cost_gain}}", "v1").text), indent=2, sort_keys=True, ensure_ascii=False
    )
    assert text.count(cost_json) == 1


def test_the_version_must_be_non_empty(ctx):
    with pytest.raises(PromptRenderError, match="template_version"):
        render_prompt(ctx, "{{run_id}}", " ")


def test_the_context_must_be_a_planning_context():
    with pytest.raises(TypeError, match="PlanningContext"):
        render_prompt(example_context_dict(), "{{run_id}}", "v1")


def test_check_template_returns_the_names_used():
    assert check_template("{{cost_gain}} {{ run_id }} {{run_id}}") == ("cost_gain", "run_id")


def test_feedback_is_appended_to_the_prompt_only(ctx):
    packed = render_prompt(ctx, "{{run_id}}", "v1")
    assert packed.prompt == packed.text == "run-003"
    retry = packed.with_feedback(("attempt 1: parse error: output is empty",))
    assert retry.text == "run-003"
    assert retry.prompt == (
        "run-003\n\n## Rejected attempts\n"
        "Your previous output was rejected for the reasons below. "
        "Fix them and output only one JSON object.\n"
        "- attempt 1: parse error: output is empty\n"
    )
    assert packed.feedback == ()
