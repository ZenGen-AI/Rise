from simulator.datamodel import Persona, RoleSlot, Scenario, RoleBinding, RoleAssignment, TurnRecord, Episode


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


def test_opponent_script_is_optional_and_survives_a_round_trip():
    plain = Scenario(id="s", background="", role_slots=[], conflict="", norms=[], purpose="training")
    assert plain.to_dict() == {  # 旧形状逐字节不变
        "id": "s", "background": "", "role_slots": [], "conflict": "", "norms": [],
        "purpose": "training", "source": "auto",
    }
    scripted = Scenario(id="s", background="", role_slots=[], conflict="", norms=[],
                        purpose="training", opponent_script={"b": ["一", "二"]})
    assert scripted.to_dict()["opponent_script"] == {"b": ["一", "二"]}
    assert Scenario.from_dict(scripted.to_dict()).opponent_script == {"b": ["一", "二"]}
    assert Scenario.from_dict(plain.to_dict()).opponent_script == {}


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


def test_turn_record_round_trip_with_none_trace():
    t = TurnRecord(turn_index=0, speaker_slot="persuader", utterance="我认为我们该冒险一试")
    d = t.to_dict()
    assert d["turn_index"] == 0
    assert d["speaker_slot"] == "persuader"
    assert d["inner_thought"] is None
    assert d["activation_trace"] is None  # 裸模型：无激活轨迹语义
    assert TurnRecord.from_dict(d) == t


def test_turn_record_preserves_inner_thought():
    t = TurnRecord(
        turn_index=1,
        speaker_slot="skeptic",
        utterance="风险太高了",
        inner_thought="对方在施压，我需要坚持",
    )
    assert TurnRecord.from_dict(t.to_dict()) == t


def test_episode_round_trip():
    ra = RoleAssignment(
        scenario_id="scen-001",
        bindings=[RoleBinding(slot_id="persuader", persona_id="pers-001", model_backend="self_play")],
        max_turns=2,
    )
    ep = Episode(
        id="ep-001",
        scenario_id="scen-001",
        role_assignment=ra,
        turns=[TurnRecord(turn_index=0, speaker_slot="persuader", utterance="开场")],
        terminated_reason="max_turns",
    )
    d = ep.to_dict()
    assert d["id"] == "ep-001"
    assert d["terminated_reason"] == "max_turns"
    assert len(d["turns"]) == 1
    assert Episode.from_dict(d) == ep

