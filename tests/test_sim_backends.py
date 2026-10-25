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


def test_scripted_backend_keeps_the_last_line_when_loop_is_off():
    backend = ScriptedBackend({"b": ["一"]})
    assert backend.respond(_persona(), _scenario(), [], "b")[0] == "一"
    assert backend.respond(_persona(), _scenario(), [], "b")[0] == "(无更多发言)"


def test_scripted_backend_wraps_around_when_loop_is_on():
    backend = ScriptedBackend({"b": ["一", "二"]}, loop=True)
    said = [backend.respond(_persona(), _scenario(), [], "b")[0] for _ in range(5)]
    assert said == ["一", "二", "一", "二", "一"]


def test_scripted_backend_loop_with_an_empty_queue_still_closes():
    # 空队列即使 loop 也没有可循环的内容，不能越界
    backend = ScriptedBackend({"b": []}, loop=True)
    assert backend.respond(_persona(), _scenario(), [], "b")[0] == "(无更多发言)"


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
