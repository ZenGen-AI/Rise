from __future__ import annotations

from google.protobuf.json_format import MessageToDict

from simulator.datamodel import Persona, RoleBinding, RoleAssignment, TurnRecord
from simulator.realtime.proto import realtime_env_pb2 as pb2


def persona_to_proto(p: Persona) -> pb2.PersonaProto:
    return pb2.PersonaProto(
        id=p.id, traits=list(p.traits), motivation=p.motivation,
        speech_style=p.speech_style, values=list(p.values), source=p.source,
    )


def persona_from_proto(proto: pb2.PersonaProto) -> Persona:
    return Persona(
        id=proto.id, traits=list(proto.traits), motivation=proto.motivation,
        speech_style=proto.speech_style, values=list(proto.values), source=proto.source,
    )


def role_binding_to_proto(b: RoleBinding) -> pb2.RoleBindingProto:
    return pb2.RoleBindingProto(
        slot_id=b.slot_id, persona_id=b.persona_id, model_backend=b.model_backend,
    )


def role_binding_from_proto(proto: pb2.RoleBindingProto) -> RoleBinding:
    return RoleBinding(
        slot_id=proto.slot_id, persona_id=proto.persona_id, model_backend=proto.model_backend,
    )


def role_assignment_to_proto(ra: RoleAssignment) -> pb2.RoleAssignmentProto:
    return pb2.RoleAssignmentProto(
        scenario_id=ra.scenario_id,
        bindings=[role_binding_to_proto(b) for b in ra.bindings],
        max_turns=ra.max_turns,
    )


def role_assignment_from_proto(proto: pb2.RoleAssignmentProto) -> RoleAssignment:
    return RoleAssignment(
        scenario_id=proto.scenario_id,
        bindings=[role_binding_from_proto(b) for b in proto.bindings],
        max_turns=proto.max_turns,
    )


def turn_record_to_proto(t: TurnRecord) -> pb2.TurnRecordProto:
    proto = pb2.TurnRecordProto(
        turn_index=t.turn_index, speaker_slot=t.speaker_slot, utterance=t.utterance,
    )
    if t.inner_thought is not None:
        proto.inner_thought = t.inner_thought
    if t.activation_trace is not None:
        # SetInParent() 显式置位 presence——仅 update({}) 对空字典不会设置
        # presence，会导致解码时把"显式空轨迹"误判为"未设置"（§2.6.3 要求
        # 区分这两种语义）。
        proto.activation_trace.SetInParent()
        proto.activation_trace.update(t.activation_trace)
    return proto


def turn_record_from_proto(proto: pb2.TurnRecordProto) -> TurnRecord:
    return TurnRecord(
        turn_index=proto.turn_index,
        speaker_slot=proto.speaker_slot,
        utterance=proto.utterance,
        inner_thought=proto.inner_thought if proto.HasField("inner_thought") else None,
        activation_trace=(
            # MessageToDict（非 dict()）：把嵌套的 ListValue/Struct 包装对象
            # 转成纯 list/dict——dict() 留下的包装对象无法被 yaml.safe_dump
            # 序列化，会在 write_episode 落盘时炸掉。Struct 的动态 key 不是
            # 具名 message 字段，MessageToDict 不会对它们做 camelCase 转换，
            # 原样保留。
            MessageToDict(proto.activation_trace) if proto.HasField("activation_trace") else None
        ),
    )
