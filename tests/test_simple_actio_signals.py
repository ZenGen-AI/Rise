import pytest

from simple_actio.signals import (
    DEFAULT_EMOTION_LEXICON, SignalRegistry, default_registry, first_turn,
    make_emotion_high, no_new_information,
)
from simple_actio.text import tokens
from simple_actio.triggers import (
    InvalidTriggerError, evaluate_trigger, referenced_signals, validate_trigger,
)
from simulator.datamodel import TurnRecord


def _h(*pairs):
    return [TurnRecord(i, slot, text) for i, (slot, text) in enumerate(pairs)]


# ---------------- 词面切分 ----------------

def test_tokens_split_ascii_words_and_cjk_bigrams():
    assert tokens("Hello, World!") == {"hello", "world"}
    assert tokens("我很生气") == {"我很", "很生", "生气"}
    assert tokens("好") == {"好"}
    assert tokens("") == frozenset()
    assert tokens("预算 budget 2026") == {"预算", "budget", "2026"}


# ---------------- 内置信号 ----------------

def test_emotion_high_looks_at_the_opponents_last_line_only():
    assert make_emotion_high()(_h(("b", "我真的很生气")), "a") is True
    assert make_emotion_high()(_h(("b", "我真的很生气"), ("b", "好吧")), "a") is False
    assert make_emotion_high()(_h(("a", "我真的很生气")), "a") is False  # 自己说的不算
    assert make_emotion_high()([], "a") is False


def test_emotion_high_counts_two_exclamation_marks_and_ignores_case():
    assert make_emotion_high()(_h(("b", "不行!!")), "a") is True
    assert make_emotion_high()(_h(("b", "不行！！")), "a") is True
    assert make_emotion_high()(_h(("b", "不行!")), "a") is False
    assert make_emotion_high()(_h(("b", "I am ANGRY")), "a") is True


def test_emotion_lexicon_is_replaceable_and_must_be_non_empty():
    custom = make_emotion_high(["吃醋"])
    assert custom(_h(("b", "你是不是吃醋了")), "a") is True
    assert custom(_h(("b", "我很生气")), "a") is False
    with pytest.raises(ValueError):
        make_emotion_high([])
    with pytest.raises(ValueError):
        make_emotion_high([""])
    assert "生气" in DEFAULT_EMOTION_LEXICON


def test_no_new_information_needs_two_opponent_lines_and_no_new_tokens():
    assert no_new_information(_h(("b", "预算不够")), "a") is False
    assert no_new_information(_h(("b", "预算不够"), ("a", "怎么办"), ("b", "预算不够")), "a") is True
    assert no_new_information(_h(("b", "预算不够"), ("b", "人手也不够")), "a") is False
    assert no_new_information(_h(("b", "预算不够"), ("b", "……")), "a") is True  # 空话也算


def test_first_turn_is_about_the_speaker_not_the_conversation():
    assert first_turn([], "a") is True
    assert first_turn(_h(("b", "你好")), "a") is True
    assert first_turn(_h(("a", "你好"), ("b", "嗯")), "a") is False


# ---------------- 注册表 ----------------

def test_default_registry_has_the_three_builtins_sorted():
    assert default_registry().names() == ["emotion_high", "first_turn", "no_new_information"]


def test_registries_are_independent():
    a, b = default_registry(), default_registry()
    a.register("always", lambda h, s: True)
    assert "always" in a and "always" not in b


def test_register_rejects_duplicates_bad_names_and_non_callables():
    reg = SignalRegistry()
    reg.register("x", lambda h, s: True)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("x", lambda h, s: True)
    with pytest.raises(ValueError):
        reg.register("Bad-Name", lambda h, s: True)
    with pytest.raises(ValueError):
        reg.register("trailing\n", lambda h, s: True)  # fullmatch:末尾换行也不行
    with pytest.raises(TypeError):
        reg.register("y", "not callable")
    with pytest.raises(KeyError, match="unknown signal"):
        reg.get("nope")


def test_fired_lists_true_signals_by_name():
    reg = default_registry()
    assert reg.fired(_h(("b", "我很生气")), "a") == ["emotion_high", "first_turn"]


# ---------------- 谓词语言 ----------------

@pytest.mark.parametrize("trigger, expected", [
    ({"signal": "emotion_high"}, True),
    ({"signal": "first_turn"}, True),
    ({"all": [{"signal": "emotion_high"}, {"signal": "first_turn"}]}, True),
    ({"all": [{"signal": "emotion_high"}, {"signal": "no_new_information"}]}, False),
    ({"any": [{"signal": "no_new_information"}, {"signal": "emotion_high"}]}, True),
    ({"not": {"signal": "emotion_high"}}, False),
    ({"turn_at_least": 1}, True),
    ({"turn_at_least": 2}, False),
    ({"turn_at_least": 0}, True),
])
def test_evaluate(trigger, expected):
    reg = default_registry()
    validate_trigger(trigger, reg)
    assert evaluate_trigger(trigger, _h(("b", "我很生气")), "a", reg) is expected


@pytest.mark.parametrize("bad", [
    {}, {"signal": "a", "all": []}, {"xor": []}, "signal", None,
    {"signal": ""}, {"signal": 3},
    {"all": []}, {"any": {"signal": "a"}}, {"all": [{"oops": 1}]},
    {"not": []}, {"turn_at_least": -1}, {"turn_at_least": 1.0}, {"turn_at_least": True},
])
def test_validate_rejects_malformed_triggers(bad):
    with pytest.raises(InvalidTriggerError):
        validate_trigger(bad)


def test_validate_with_a_registry_rejects_unknown_signals():
    validate_trigger({"signal": "custom"})  # 没给注册表:只查结构
    with pytest.raises(InvalidTriggerError, match="unknown signal 'custom'"):
        validate_trigger({"any": [{"signal": "custom"}]}, default_registry())


def test_the_error_names_where_the_problem_is():
    with pytest.raises(InvalidTriggerError, match=r"trigger\.all\[1\]\.not"):
        validate_trigger({"all": [{"signal": "a"}, {"not": {"bad": 1}}]})


def _nested_not(depth):
    node = {"signal": "first_turn"}
    for _ in range(depth - 1):
        node = {"not": node}
    return node


def test_nesting_up_to_the_depth_limit_is_accepted_and_beyond_it_is_rejected():
    from simple_actio.triggers import MAX_TRIGGER_DEPTH
    assert MAX_TRIGGER_DEPTH == 32
    validate_trigger(_nested_not(32), default_registry())
    with pytest.raises(InvalidTriggerError, match="nested deeper than 32 levels"):
        validate_trigger(_nested_not(33))
    with pytest.raises(InvalidTriggerError, match="nested deeper than 32 levels"):
        validate_trigger(_nested_not(5000))  # 远超解释器递归上限,仍是确定性的 InvalidTriggerError


def test_a_self_referencing_trigger_is_rejected_not_a_recursion_error():
    loop = {"any": []}
    loop["any"].append(loop)  # YAML 锚点 &a / *a 可以造出这种结构
    with pytest.raises(InvalidTriggerError, match="nested deeper"):
        validate_trigger(loop)


def test_referenced_signals_skips_negated_ones():
    trig = {"all": [{"signal": "emotion_high"}, {"not": {"signal": "first_turn"}},
                    {"not": {"not": {"signal": "no_new_information"}}}, {"turn_at_least": 2}]}
    assert referenced_signals(trig) == {"emotion_high", "no_new_information"}
