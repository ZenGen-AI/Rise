from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode


def _fixture():
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "role a"), RoleSlot("b", "role b")],
        conflict="c", norms=["n"], purpose="belief",
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


def test_backward_compatible_no_predicate_runs_full_max_turns():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1")
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_stop_predicate_triggers_goal_reached_early():
    scenario, assignment, personas, backends = _fixture()
    def stop(turns): return len(turns) >= 2
    ep = run_episode(scenario, assignment, personas, backends, "e1", stop_predicate=stop)
    assert len(ep.turns) == 2
    assert ep.terminated_reason == "goal_reached"


def test_stop_predicate_never_true_falls_back_to_max_turns():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     stop_predicate=lambda turns: False)
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_stop_predicate_receives_turns_so_far():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    def stop(turns):
        seen.append(len(turns))
        return False
    run_episode(scenario, assignment, personas, backends, "e1", stop_predicate=stop)
    assert seen == [1, 2, 3, 4]
