from simulator.datamodel import Persona, Scenario, RoleSlot, TurnRecord
from simulator.backends import ScriptedBackend, TemplateGenerationBackend


def _persona():
    return Persona(id="p", traits=[], motivation="", speech_style="", values=[])


def _scenario():
    return Scenario(
        id="s",
        background="bg",
        role_slots=[RoleSlot(slot_id="a", description="")],
        conflict="c",
        norms=[],
        purpose="training",
    )


def test_scripted_backend_returns_queued_utterances_in_order():
    be = ScriptedBackend(scripted={"a": ["第一句", "第二句"]})
    u1, thought1 = be.respond(_persona(), _scenario(), [], "a")
    u2, _ = be.respond(_persona(), _scenario(), [], "a")
    assert u1 == "第一句"
    assert u2 == "第二句"
    assert thought1 is None


def test_scripted_backend_exhausted_returns_closing():
    be = ScriptedBackend(scripted={"a": []})
    u, _ = be.respond(_persona(), _scenario(), [], "a")
    assert u == "(无更多发言)"


def test_scripted_backend_is_deterministic_per_slot():
    be = ScriptedBackend(scripted={"a": ["A1"], "b": ["B1"]})
    ua, _ = be.respond(_persona(), _scenario(), [], "a")
    ub, _ = be.respond(_persona(), _scenario(), [], "b")
    assert ua == "A1"
    assert ub == "B1"


def test_template_generation_backend_deterministic_by_seed():
    be = TemplateGenerationBackend()
    p1 = be.make_persona(seed=7)
    p2 = be.make_persona(seed=7)
    assert p1 == p2
    assert isinstance(p1, Persona)
    assert p1.source == "auto"


def test_template_scenario_respects_purpose():
    be = TemplateGenerationBackend()
    s = be.make_scenario(seed=3, purpose="deployment_simulation")
    assert s.purpose == "deployment_simulation"
    assert isinstance(s, Scenario)
    assert len(s.role_slots) >= 1
