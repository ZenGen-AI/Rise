"""data_spec 的严格解析:每个字段都有类型与范围用例。"""
import pytest

from controlplane.envelope import Envelope, OwnerRef
from mindtrainer.data_spec import (
    MAX_GROUP_SIZE, MAX_SCENARIO_COUNT, MAX_SEED, DataSpec, InvalidDataSpecError, StageSpec,
    data_spec_from_directive, parse_data_spec,
)


def _raw(**kw):
    base = {
        "scenario_count": 4, "seed": 0, "purpose": "training",
        "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}},
    }
    base.update(kw)
    return base


def _directive(payload):
    return Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )


def test_the_design_example_parses():
    spec = parse_data_spec(_raw())
    assert spec == DataSpec(4, 0, "training", {"sft": StageSpec(1), "grpo_opd": StageSpec(2)})
    assert spec.to_dict() == _raw()


def test_stages_come_out_in_canonical_order_whatever_the_input_order():
    spec = parse_data_spec(_raw(stages={"grpo_opd": {"group_size": 2}, "sft": {"group_size": 1}}))
    assert list(spec.stages) == ["sft", "grpo_opd"]


def test_a_single_stage_is_enough():
    assert list(parse_data_spec(_raw(stages={"sft": {"group_size": 1}})).stages) == ["sft"]


def test_boundary_values_are_accepted():
    spec = parse_data_spec(_raw(
        scenario_count=MAX_SCENARIO_COUNT, seed=MAX_SEED, purpose="deployment_simulation",
        stages={"sft": {"group_size": MAX_GROUP_SIZE}, "grpo_opd": {"group_size": MAX_GROUP_SIZE}},
    ))
    assert (spec.scenario_count, spec.seed, spec.purpose) == (
        MAX_SCENARIO_COUNT, MAX_SEED, "deployment_simulation")


@pytest.mark.parametrize("raw", [None, [], "spec", 3, True], ids=lambda v: type(v).__name__)
def test_a_data_spec_that_is_not_a_dict_is_rejected(raw):
    with pytest.raises(InvalidDataSpecError, match="data_spec must be a dict"):
        parse_data_spec(raw)


@pytest.mark.parametrize("key", ["scenario_count", "seed", "purpose", "stages"])
def test_a_missing_key_is_rejected(key):
    raw = _raw()
    del raw[key]
    with pytest.raises(InvalidDataSpecError, match=f"missing key.*{key}"):
        parse_data_spec(raw)


def test_an_unknown_key_is_rejected():
    with pytest.raises(InvalidDataSpecError, match="unknown key.*dimension"):
        parse_data_spec(_raw(dimension="tom"))


def test_a_non_string_key_does_not_break_the_error_message():
    raw = _raw()
    raw[1] = "x"
    with pytest.raises(InvalidDataSpecError, match="unknown key"):
        parse_data_spec(raw)


@pytest.mark.parametrize("field", ["scenario_count", "seed"])
@pytest.mark.parametrize("value", ["4", 4.0, None, True, [4], {"n": 4}],
                         ids=["str", "float", "none", "bool", "list", "dict"])
def test_integer_fields_reject_every_other_type(field, value):
    with pytest.raises(InvalidDataSpecError, match=f"data_spec.{field} must be an int"):
        parse_data_spec(_raw(**{field: value}))


@pytest.mark.parametrize("value", [0, -1, MAX_SCENARIO_COUNT + 1, 10**30])
def test_scenario_count_out_of_range_is_rejected(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.scenario_count must be in \[1, "):
        parse_data_spec(_raw(scenario_count=value))


@pytest.mark.parametrize("value", [-1, MAX_SEED + 1])
def test_seed_out_of_range_is_rejected(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.seed must be in \[0, "):
        parse_data_spec(_raw(seed=value))


@pytest.mark.parametrize("value", [1, None, ["training"], True], ids=["int", "none", "list", "bool"])
def test_purpose_must_be_a_string(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.purpose must be a str"):
        parse_data_spec(_raw(purpose=value))


@pytest.mark.parametrize("value", ["", "Training", "eval"])
def test_purpose_must_be_a_known_value(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.purpose must be one of"):
        parse_data_spec(_raw(purpose=value))


@pytest.mark.parametrize("value", [[], "sft", None, 1], ids=["list", "str", "none", "int"])
def test_stages_must_be_a_dict(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.stages must be a dict"):
        parse_data_spec(_raw(stages=value))


def test_stages_must_not_be_empty():
    with pytest.raises(InvalidDataSpecError, match="at least one stage"):
        parse_data_spec(_raw(stages={}))


def test_an_unknown_stage_is_rejected():
    with pytest.raises(InvalidDataSpecError, match="unknown stage.*'rl'"):
        parse_data_spec(_raw(stages={"sft": {"group_size": 1}, "rl": {"group_size": 2}}))


@pytest.mark.parametrize("value", [2, None, [2], "2"], ids=["int", "none", "list", "str"])
def test_a_stage_entry_must_be_a_dict(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.stages\['sft'\] must be a dict"):
        parse_data_spec(_raw(stages={"sft": value}))


def test_a_stage_entry_needs_exactly_group_size():
    with pytest.raises(InvalidDataSpecError, match="missing key.*group_size"):
        parse_data_spec(_raw(stages={"sft": {}}))
    with pytest.raises(InvalidDataSpecError, match="unknown key.*count"):
        parse_data_spec(_raw(stages={"sft": {"group_size": 1, "count": 3}}))


@pytest.mark.parametrize("value", ["1", 1.0, None, True, [1], {"n": 1}],
                         ids=["str", "float", "none", "bool", "list", "dict"])
def test_group_size_must_be_an_int(value):
    with pytest.raises(InvalidDataSpecError, match=r"group_size must be an int"):
        parse_data_spec(_raw(stages={"sft": {"group_size": value}}))


@pytest.mark.parametrize(
    "stage, value",
    [("sft", 0), ("sft", MAX_GROUP_SIZE + 1), ("grpo_opd", 1), ("grpo_opd", 0),
     ("grpo_opd", MAX_GROUP_SIZE + 1)],
)
def test_group_size_out_of_range_is_rejected(stage, value):
    with pytest.raises(InvalidDataSpecError, match=rf"data_spec.stages\['{stage}'\].group_size must be in"):
        parse_data_spec(_raw(stages={stage: {"group_size": value}}))


def test_a_directive_without_data_spec_has_none():
    assert data_spec_from_directive(_directive({"target_dimension": "tom"})) is None
    assert data_spec_from_directive(_directive({"data_spec": None})) is None


def test_a_directive_with_data_spec_is_parsed_strictly():
    assert data_spec_from_directive(_directive({"data_spec": _raw()})).scenario_count == 4
    with pytest.raises(InvalidDataSpecError):
        data_spec_from_directive(_directive({"data_spec": _raw(seed="0")}))


HUGE = int("f" * 4000, 16)  # 4817 位十进制,超过 int 转字符串的 4300 位上限;YAML 写 0xfff... 就能得到


@pytest.mark.parametrize(
    "raw, field",
    [
        (_raw(scenario_count=HUGE), "data_spec.scenario_count must be in"),
        (_raw(seed=HUGE), "data_spec.seed must be in"),
        (_raw(stages={"sft": {"group_size": HUGE}}), "group_size must be in"),
        ({**_raw(), HUGE: 1}, "unknown key"),
        (_raw(stages={"sft": {"group_size": 1}, HUGE: {"group_size": 2}}), "unknown stage"),
    ],
    ids=["scenario_count", "seed", "group_size", "top_level_key", "stage_key"],
)
def test_an_integer_too_long_to_print_is_still_an_invalid_data_spec_error(raw, field):
    with pytest.raises(InvalidDataSpecError, match=field) as info:
        parse_data_spec(raw)
    assert "<int>" in str(info.value)  # 值只写类型,不尝试转字符串


def test_parsing_never_raises_anything_but_invalid_data_spec_error():
    """守护循环的教训:错类型的输入若在这里漏成 TypeError/AttributeError,就会逃出派发器。"""
    weird = [
        _raw(stages={"sft": {"group_size": float("nan")}}),
        _raw(scenario_count=float("inf")),
        _raw(stages={("sft",): {"group_size": 1}}),
        _raw(stages={None: {"group_size": 1}}),
        {1: 2, "a": 3},
    ]
    for raw in weird:
        with pytest.raises(InvalidDataSpecError):
            parse_data_spec(raw)
