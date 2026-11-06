import pytest

from simple_actio.skills import Skill
from simple_actio.trace import build_trace
from mindagent.analysis import BehaviorFinding, analyze, finding_id, signal_turns
from mindagent.errors import MindAgentDataError
from simple_actio.signals import default_registry
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

LV = {"skill": "1", "memory": "0"}
GOAL = Skill("skill-goal", 1, "macro", {"turn_at_least": 0}, ("推进目标",))
CALM = Skill("skill-calm", 1, "micro", {"any": [{"signal": "emotion_high"}, {"turn_at_least": 99}]}, ("先回应情绪",))
NOT_CALM = Skill("skill-notcalm", 1, "micro", {"not": {"signal": "emotion_high"}}, ("x",))


def _trace(selected):
    return build_trace(signals=[], candidates=[selected] if selected else [], selected=selected,
                       memory_hits=[], experience=None, library_version=LV)


def _episode(eid, opp_lines, selected="skill-goal", trace=True):
    """对手 b 先说,agent a 后说。每句对手话后面跟一轮 agent。"""
    turns = []
    for line in opp_lines:
        turns.append(TurnRecord(len(turns), "b", line))
        turns.append(TurnRecord(len(turns), "a", "reply", None, _trace(selected) if trace else None))
    ra = RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
                               RoleBinding("a", "pa", "self_play")], len(turns))
    return Episode(eid, "s1", ra, turns, "max_turns")


ANGRY = "我真的很生气!!"


def test_empty_input_has_no_findings():
    assert analyze([], min_support=1, min_ratio=0.0) == []


def test_a_signal_handled_by_a_non_targeting_skill_is_a_finding():
    eps = [_episode(f"e{i}", [ANGRY, "好吧"]) for i in range(3)] + [_episode("e3", ["好吧", "嗯"])]
    (f,) = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL])
    assert f.signal == "emotion_high"
    assert (f.support, f.ratio, f.episode_refs) == (3, 0.75, ("e0", "e1", "e2"))
    assert f.id == finding_id("emotion_high", ["e2", "e0", "e1"])
    assert f.id.startswith("finding-emotion_high-") and len(f.id) == len("finding-emotion_high-") + 12
    assert f.to_dict()["episode_refs"] == ["e0", "e1", "e2"]


def test_a_targeting_skill_is_not_a_gap_and_negation_does_not_count_as_targeting():
    eps = [_episode(f"e{i}", [ANGRY], selected="skill-calm") for i in range(3)]
    assert analyze(eps, min_support=1, min_ratio=0.0, skills=[CALM]) == []
    eps = [_episode(f"e{i}", [ANGRY], selected="skill-notcalm") for i in range(3)]
    assert [f.signal for f in analyze(eps, min_support=1, min_ratio=0.0, skills=[NOT_CALM])] == ["emotion_high"]


def test_support_below_the_threshold_is_not_a_finding():
    eps = [_episode("e0", [ANGRY]), _episode("e1", [ANGRY])]
    assert analyze(eps, min_support=3, min_ratio=0.0, skills=[GOAL]) == []
    assert len(analyze(eps, min_support=2, min_ratio=0.0, skills=[GOAL])) == 1


def test_ratio_at_the_threshold_counts_and_just_below_does_not():
    eps = [_episode("e0", [ANGRY]), _episode("e1", [ANGRY]), _episode("e2", ["好"]), _episode("e3", ["嗯"])]
    assert len(analyze(eps, min_support=1, min_ratio=0.5, skills=[GOAL])) == 1
    assert analyze(eps, min_support=1, min_ratio=0.51, skills=[GOAL]) == []


def test_bare_model_episodes_without_traces_never_produce_findings():
    eps = [_episode(f"e{i}", [ANGRY], trace=False) for i in range(5)]
    assert analyze(eps, min_support=1, min_ratio=0.0) == []


def test_an_empty_library_selects_nothing_so_there_is_nothing_to_find():
    eps = [_episode(f"e{i}", [ANGRY], selected=None) for i in range(5)]
    assert analyze(eps, min_support=1, min_ratio=0.0, skills=[]) == []


def test_two_signals_each_get_their_own_finding_sorted_by_name():
    eps = [_episode(f"e{i}", ["我们谈谈预算", ANGRY, "我们谈谈预算"]) for i in range(3)]
    found = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL])
    assert [f.signal for f in found] == ["emotion_high", "no_new_information"]
    assert found[0].id != found[1].id


def test_an_episode_counts_once_even_if_the_signal_fires_on_many_turns():
    # 同一句重复三次也会触发 no_new_information,所以只看 emotion_high
    (f,) = analyze([_episode("e0", [ANGRY, ANGRY, ANGRY])], min_support=1, min_ratio=0.0, skills=[GOAL],
                   signals=("emotion_high",))
    assert f.support == 1 and f.ratio == 1.0


def test_episodes_without_agent_turns_are_not_in_the_denominator():
    only_opp = Episode("e9", "s1", RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="m"),
                                                          RoleBinding("a", "pa", "self_play")], 1),
                       [TurnRecord(0, "b", ANGRY)], "max_turns")
    (f,) = analyze([_episode("e0", [ANGRY]), only_opp], min_support=1, min_ratio=1.0, skills=[GOAL])
    assert f.ratio == 1.0


def test_signals_can_be_restricted_and_unknown_names_are_ignored():
    eps = [_episode(f"e{i}", ["我们谈谈预算", ANGRY, "我们谈谈预算"]) for i in range(3)]
    found = analyze(eps, min_support=1, min_ratio=0.0, skills=[GOAL], signals=("no_new_information", "ghost"))
    assert [f.signal for f in found] == ["no_new_information"]


def test_an_off_schema_trace_is_a_data_error():
    ep = _episode("e0", [ANGRY])
    ep.turns[1].activation_trace = {"selected": "skill-goal"}
    with pytest.raises(MindAgentDataError, match="episode e0 turn 1: trace"):
        analyze([ep], min_support=1, min_ratio=0.0, skills=[GOAL])


def test_a_trace_selecting_a_skill_outside_the_library_is_a_data_error():
    with pytest.raises(MindAgentDataError, match="not in the library"):
        analyze([_episode("e0", [ANGRY])], min_support=1, min_ratio=0.0, skills=[])


def test_an_episode_without_exactly_one_self_play_binding_is_a_data_error():
    bad = _episode("e0", [ANGRY])
    bad.role_assignment.bindings[1].model_backend = "frozen_reference"
    with pytest.raises(MindAgentDataError, match="self_play"):
        analyze([bad], min_support=1, min_ratio=0.0, skills=[GOAL])


@pytest.mark.parametrize("kw", [{"min_support": 0, "min_ratio": 0.5}, {"min_support": 1, "min_ratio": 1.2},
                                {"min_support": True, "min_ratio": 0.5}])
def test_thresholds_are_validated(kw):
    with pytest.raises(ValueError):
        analyze([], **kw)


def test_signal_turns_uses_the_agents_view_of_the_history():
    ep = _episode("e0", ["好", ANGRY])
    assert signal_turns(ep, "emotion_high", default_registry()) == [3]


def test_analyze_is_pure_and_deterministic():
    eps = [_episode(f"e{i}", [ANGRY]) for i in range(3)]
    before = [e.to_dict() for e in eps]
    a = analyze(eps, min_support=1, min_ratio=0.0, skills=[GOAL])
    b = analyze(list(reversed(eps)), min_support=1, min_ratio=0.0, skills=[GOAL])
    assert a == b and isinstance(a[0], BehaviorFinding)
    assert [e.to_dict() for e in eps] == before
