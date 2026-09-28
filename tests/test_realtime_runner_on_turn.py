from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode


def _fixture():
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "role a"), RoleSlot("b", "role b")],
        conflict="c", norms=["n"], purpose="training",
    )
    personas = {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
                "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    backends = {"a": ScriptedBackend({"a": ["a1", "a2"]}),
                "b": ScriptedBackend({"b": ["b1", "b2"]})}
    return scenario, assignment, personas, backends


def test_backward_compatible_no_callback_unchanged():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1")
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_on_turn_called_once_per_turn_immediately():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     on_turn=lambda t: seen.append((t.turn_index, t.speaker_slot)))
    assert seen == [(0, "a"), (1, "b"), (2, "a"), (3, "b")]
    assert len(ep.turns) == 4


def test_on_turn_receives_same_turn_record_appended_to_episode():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     on_turn=lambda t: seen.append(t))
    assert seen == ep.turns


def test_on_turn_and_stop_predicate_compose():
    scenario, assignment, personas, backends = _fixture()
    seen = []

    def stop(turns):
        return len(turns) >= 2

    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     stop_predicate=stop,
                     on_turn=lambda t: seen.append(t.turn_index))
    assert seen == [0, 1]  # on_turn 在 stop_predicate 判定前对每轮都调用
    assert ep.terminated_reason == "goal_reached"
    assert len(ep.turns) == 2


def test_on_turn_none_is_noop():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1", on_turn=None)
    assert len(ep.turns) == 4
