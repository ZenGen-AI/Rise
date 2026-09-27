import pytest

from simulator.datamodel import (
    Persona,
    Scenario,
    RoleSlot,
    RoleBinding,
    RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode, RoleSlotMismatchError


def _scenario():
    return Scenario(
        id="scen-001",
        background="两位同事分歧",
        role_slots=[
            RoleSlot(slot_id="a", description="激进方"),
            RoleSlot(slot_id="b", description="保守方"),
        ],
        conflict="方案取舍",
        norms=["就事论事"],
        purpose="training",
    )


def _assignment(max_turns):
    return RoleAssignment(
        scenario_id="scen-001",
        bindings=[
            RoleBinding(slot_id="a", persona_id="pa", model_backend="self_play"),
            RoleBinding(slot_id="b", persona_id="pb", model_backend="frozen_reference"),
        ],
        max_turns=max_turns,
    )


def _personas():
    return {
        "pa": Persona(id="pa", traits=[], motivation="", speech_style="", values=[]),
        "pb": Persona(id="pb", traits=[], motivation="", speech_style="", values=[]),
    }


def test_run_episode_produces_turns_up_to_max():
    backends = {
        "a": ScriptedBackend(scripted={"a": ["A1", "A2"]}),
        "b": ScriptedBackend(scripted={"b": ["B1", "B2"]}),
    }
    ep = run_episode(_scenario(), _assignment(4), _personas(), backends, "ep-001")
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"
    assert [t.speaker_slot for t in ep.turns] == ["a", "b", "a", "b"]
    assert [t.utterance for t in ep.turns] == ["A1", "B1", "A2", "B2"]
    assert [t.turn_index for t in ep.turns] == [0, 1, 2, 3]


def test_run_episode_is_deterministic():
    def build():
        backends = {
            "a": ScriptedBackend(scripted={"a": ["A1", "A2"]}),
            "b": ScriptedBackend(scripted={"b": ["B1", "B2"]}),
        }
        return run_episode(_scenario(), _assignment(4), _personas(), backends, "ep-001")

    assert build().to_dict() == build().to_dict()


def test_run_episode_rejects_unknown_slot():
    bad = RoleAssignment(
        scenario_id="scen-001",
        bindings=[RoleBinding(slot_id="ghost", persona_id="pa", model_backend="self_play")],
        max_turns=2,
    )
    backends = {"ghost": ScriptedBackend(scripted={"ghost": ["x"]})}
    with pytest.raises(RoleSlotMismatchError):
        run_episode(_scenario(), bad, _personas(), backends, "ep-001")


def test_run_episode_rejects_missing_persona():
    backends = {
        "a": ScriptedBackend(scripted={"a": ["A1"]}),
        "b": ScriptedBackend(scripted={"b": ["B1"]}),
    }
    with pytest.raises(KeyError):
        run_episode(_scenario(), _assignment(2), {"pa": _personas()["pa"]}, backends, "ep-001")


def test_run_episode_records_activation_trace_none_for_bare_models():
    backends = {
        "a": ScriptedBackend(scripted={"a": ["A1"]}),
        "b": ScriptedBackend(scripted={"b": ["B1"]}),
    }
    ep = run_episode(_scenario(), _assignment(2), _personas(), backends, "ep-001")
    assert all(t.activation_trace is None for t in ep.turns)
