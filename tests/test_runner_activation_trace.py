import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.runner import run_episode


def _scenario():
    return Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _assignment(max_turns=4):
    return RoleAssignment(
        "s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns,
    )


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


class TracingBackend:
    """返回三元组的后端:第三项是该轮的激活轨迹。记录自己看到的 history。"""

    def __init__(self, traces):
        self._traces = list(traces)
        self.seen = []

    def respond(self, persona, scenario, history, speaker_slot):
        self.seen.append(history)
        k = len(self.seen) - 1
        return f"{speaker_slot}{k}", f"think{k}", self._traces[k]


def test_a_triple_writes_its_third_item_into_the_turn():
    traced = TracingBackend([{"selected": "skill-1"}, {"selected": None}])
    ep = run_episode(
        _scenario(), _assignment(), _personas(),
        {"a": traced, "b": ScriptedBackend({"b": ["b0", "b1"]})}, "ep-1",
    )
    assert [t.activation_trace for t in ep.turns] == [{"selected": "skill-1"}, None, {"selected": None}, None]
    assert [t.utterance for t in ep.turns] == ["a0", "b0", "a1", "b1"]
    assert ep.turns[0].inner_thought == "think0"


def test_a_triple_with_none_means_no_trace_and_an_empty_dict_is_kept():
    traced = TracingBackend([None, {}])
    ep = run_episode(
        _scenario(), _assignment(), _personas(),
        {"a": traced, "b": ScriptedBackend({"b": ["b0", "b1"]})}, "ep-1",
    )
    assert ep.turns[0].activation_trace is None
    assert ep.turns[2].activation_trace == {}


def test_pairs_still_give_no_trace():
    ep = run_episode(
        _scenario(), _assignment(), _personas(),
        {"a": ScriptedBackend({"a": ["a0", "a1"]}), "b": ScriptedBackend({"b": ["b0", "b1"]})},
        "ep-1",
    )
    assert all(t.activation_trace is None for t in ep.turns)


def test_the_stored_trace_is_a_copy_of_what_the_backend_returned():
    trace = {"candidates": ["skill-1"]}
    traced = TracingBackend([trace, {"candidates": []}])
    ep = run_episode(
        _scenario(), _assignment(), _personas(),
        {"a": traced, "b": ScriptedBackend({"b": ["b0", "b1"]})}, "ep-1",
    )
    trace["candidates"].append("mutated")
    assert ep.turns[0].activation_trace == {"candidates": ["skill-1"]}


def test_a_backend_sees_its_own_trace_but_not_the_opponents():
    mine = TracingBackend([{"me": "0"}, {"me": "1"}])
    theirs = TracingBackend([{"them": "0"}, {"them": "1"}])
    run_episode(_scenario(), _assignment(), _personas(), {"a": mine, "b": theirs}, "ep-1")
    # a 的第二次调用:history = [a0(自己), b0(对手)]
    own, opp = mine.seen[1]
    assert own.activation_trace == {"me": "0"}
    assert opp.activation_trace is None and opp.inner_thought is None
    # b 的第二次调用:history = [a0(对手), b0(自己), a1(对手)]
    assert [t.activation_trace for t in theirs.seen[1]] == [None, {"them": "0"}, None]


@pytest.mark.parametrize("reply", [("only",), ("u", None, None, None), "u", None])
def test_a_reply_that_is_not_a_pair_or_triple_is_rejected(reply):
    class Bad:
        def respond(self, persona, scenario, history, speaker_slot):
            return reply

    with pytest.raises(TypeError, match="2- or 3-tuple"):
        run_episode(_scenario(), _assignment(), _personas(),
                    {"a": Bad(), "b": ScriptedBackend({"b": ["b0"]})}, "ep-1")


def test_a_trace_that_is_not_a_dict_is_rejected():
    class Bad:
        def respond(self, persona, scenario, history, speaker_slot):
            return "u", None, ["not", "a", "dict"]

    with pytest.raises(TypeError, match="activation_trace"):
        run_episode(_scenario(), _assignment(), _personas(),
                    {"a": Bad(), "b": ScriptedBackend({"b": ["b0"]})}, "ep-1")
