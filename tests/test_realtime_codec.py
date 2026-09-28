from simulator.datamodel import Persona, RoleBinding, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, persona_from_proto,
    role_assignment_to_proto, role_assignment_from_proto,
    turn_record_to_proto, turn_record_from_proto,
)


def _persona():
    return Persona(id="pa", traits=["t1", "t2"], motivation="m", speech_style="s",
                   values=["v1"], source="auto")


def test_persona_round_trip():
    p = _persona()
    assert persona_from_proto(persona_to_proto(p)) == p


def test_role_assignment_round_trip_preserves_int_max_turns():
    ra = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    proto = role_assignment_to_proto(ra)
    assert proto.max_turns == 4
    back = role_assignment_from_proto(proto)
    assert back == ra
    assert isinstance(back.max_turns, int)  # 强类型字段回归防护：不是 Struct 里的 float


def test_turn_record_round_trip_bare_model_no_activation_trace():
    t = TurnRecord(turn_index=2, speaker_slot="a", utterance="hi")
    proto = turn_record_to_proto(t)
    assert proto.HasField("activation_trace") is False
    back = turn_record_from_proto(proto)
    assert back == t
    assert isinstance(back.turn_index, int)


def test_turn_record_round_trip_with_inner_thought():
    t = TurnRecord(turn_index=0, speaker_slot="b", utterance="hey", inner_thought="thinking")
    proto = turn_record_to_proto(t)
    assert proto.HasField("inner_thought") is True
    back = turn_record_from_proto(proto)
    assert back == t


def test_turn_record_round_trip_with_activation_trace():
    t = TurnRecord(
        turn_index=1, speaker_slot="a", utterance="x",
        activation_trace={"triggered_skills": ["s1"], "selected_skill": "s1", "memory_hits": []},
    )
    proto = turn_record_to_proto(t)
    assert proto.HasField("activation_trace") is True
    back = turn_record_from_proto(proto)
    assert back == t


def test_turn_record_round_trip_with_empty_activation_trace_preserves_presence():
    t = TurnRecord(turn_index=0, speaker_slot="a", utterance="x", activation_trace={})
    proto = turn_record_to_proto(t)
    assert proto.HasField("activation_trace") is True  # 显式空字典 ≠ 未设置
    back = turn_record_from_proto(proto)
    assert back.activation_trace == {}
    assert back == t


def test_turn_record_activation_trace_survives_yaml_round_trip():
    import yaml
    t = TurnRecord(
        turn_index=1, speaker_slot="a", utterance="x",
        activation_trace={"triggered_skills": ["s1", "s2"], "selected_skill": "s1", "memory_hits": []},
    )
    back = turn_record_from_proto(turn_record_to_proto(t))
    # 必须是纯 Python 类型（list/dict/str），能被 yaml.safe_dump 序列化——
    # 这是 write_episode 落盘所依赖的（ControlPlane 产物用 yaml.safe_dump）。
    dumped = yaml.safe_dump(back.to_dict())
    assert "triggered_skills" in dumped
