from simulator.datamodel import Persona, RoleSlot, Scenario, RoleBinding, RoleAssignment


def test_persona_round_trip():
    p = Persona(
        id="pers-001",
        traits=["谨慎", "共情"],
        motivation="维护团队和谐",
        speech_style="温和、迂回",
        values=["集体优先"],
        source="auto",
    )
    d = p.to_dict()
    assert d["id"] == "pers-001"
    assert d["traits"] == ["谨慎", "共情"]
    assert d["source"] == "auto"
    assert Persona.from_dict(d) == p


def test_persona_default_source_is_auto():
    p = Persona(id="p", traits=[], motivation="", speech_style="", values=[])
    assert p.source == "auto"


def test_role_slot_round_trip():
    r = RoleSlot(slot_id="persuader", description="试图说服对方改变立场的一方")
    d = r.to_dict()
    assert d == {"slot_id": "persuader", "description": "试图说服对方改变立场的一方"}
    assert RoleSlot.from_dict(d) == r


def test_scenario_round_trip_with_purpose():
    s = Scenario(
        id="scen-001",
        background="两位同事就项目方向产生分歧",
        role_slots=[
            RoleSlot(slot_id="persuader", description="主张激进方案"),
            RoleSlot(slot_id="skeptic", description="主张保守方案"),
        ],
        conflict="资源有限下的方案取舍",
        norms=["不得人身攻击", "尊重对方发言"],
        purpose="training",
        source="auto",
    )
    d = s.to_dict()
    assert d["purpose"] == "training"
    assert len(d["role_slots"]) == 2
    assert d["role_slots"][0]["slot_id"] == "persuader"
    assert Scenario.from_dict(d) == s


def test_scenario_default_source_is_auto():
    s = Scenario(id="s", background="", role_slots=[], conflict="", norms=[], purpose="training")
    assert s.source == "auto"


def test_role_binding_round_trip():
    b = RoleBinding(slot_id="persuader", persona_id="pers-001", model_backend="self_play")
    d = b.to_dict()
    assert d == {
        "slot_id": "persuader",
        "persona_id": "pers-001",
        "model_backend": "self_play",
    }
    assert RoleBinding.from_dict(d) == b


def test_role_assignment_round_trip_and_slot_ids():
    ra = RoleAssignment(
        scenario_id="scen-001",
        bindings=[
            RoleBinding(slot_id="persuader", persona_id="pers-001", model_backend="self_play"),
            RoleBinding(slot_id="skeptic", persona_id="pers-002", model_backend="frozen_reference"),
        ],
        max_turns=6,
    )
    d = ra.to_dict()
    assert d["scenario_id"] == "scen-001"
    assert d["max_turns"] == 6
    assert len(d["bindings"]) == 2
    assert RoleAssignment.from_dict(d) == ra
    assert ra.slot_ids() == {"persuader", "skeptic"}

