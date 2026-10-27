from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PersonaProto(_message.Message):
    __slots__ = ("id", "traits", "motivation", "speech_style", "values", "source")
    ID_FIELD_NUMBER: _ClassVar[int]
    TRAITS_FIELD_NUMBER: _ClassVar[int]
    MOTIVATION_FIELD_NUMBER: _ClassVar[int]
    SPEECH_STYLE_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    traits: _containers.RepeatedScalarFieldContainer[str]
    motivation: str
    speech_style: str
    values: _containers.RepeatedScalarFieldContainer[str]
    source: str
    def __init__(self, id: _Optional[str] = ..., traits: _Optional[_Iterable[str]] = ..., motivation: _Optional[str] = ..., speech_style: _Optional[str] = ..., values: _Optional[_Iterable[str]] = ..., source: _Optional[str] = ...) -> None: ...

class RoleBindingProto(_message.Message):
    __slots__ = ("slot_id", "persona_id", "model_backend", "model_ref", "driver")
    SLOT_ID_FIELD_NUMBER: _ClassVar[int]
    PERSONA_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_BACKEND_FIELD_NUMBER: _ClassVar[int]
    MODEL_REF_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    slot_id: str
    persona_id: str
    model_backend: str
    model_ref: str
    driver: str
    def __init__(self, slot_id: _Optional[str] = ..., persona_id: _Optional[str] = ..., model_backend: _Optional[str] = ..., model_ref: _Optional[str] = ..., driver: _Optional[str] = ...) -> None: ...

class RoleAssignmentProto(_message.Message):
    __slots__ = ("scenario_id", "bindings", "max_turns")
    SCENARIO_ID_FIELD_NUMBER: _ClassVar[int]
    BINDINGS_FIELD_NUMBER: _ClassVar[int]
    MAX_TURNS_FIELD_NUMBER: _ClassVar[int]
    scenario_id: str
    bindings: _containers.RepeatedCompositeFieldContainer[RoleBindingProto]
    max_turns: int
    def __init__(self, scenario_id: _Optional[str] = ..., bindings: _Optional[_Iterable[_Union[RoleBindingProto, _Mapping]]] = ..., max_turns: _Optional[int] = ...) -> None: ...

class TurnRecordProto(_message.Message):
    __slots__ = ("turn_index", "speaker_slot", "utterance", "inner_thought", "activation_trace")
    TURN_INDEX_FIELD_NUMBER: _ClassVar[int]
    SPEAKER_SLOT_FIELD_NUMBER: _ClassVar[int]
    UTTERANCE_FIELD_NUMBER: _ClassVar[int]
    INNER_THOUGHT_FIELD_NUMBER: _ClassVar[int]
    ACTIVATION_TRACE_FIELD_NUMBER: _ClassVar[int]
    turn_index: int
    speaker_slot: str
    utterance: str
    inner_thought: str
    activation_trace: _struct_pb2.Struct
    def __init__(self, turn_index: _Optional[int] = ..., speaker_slot: _Optional[str] = ..., utterance: _Optional[str] = ..., inner_thought: _Optional[str] = ..., activation_trace: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class CreateSession(_message.Message):
    __slots__ = ("scenario_id", "role_assignment", "personas", "purpose", "client_driven_slot", "episode_id")
    class PersonasEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: PersonaProto
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[PersonaProto, _Mapping]] = ...) -> None: ...
    SCENARIO_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_ASSIGNMENT_FIELD_NUMBER: _ClassVar[int]
    PERSONAS_FIELD_NUMBER: _ClassVar[int]
    PURPOSE_FIELD_NUMBER: _ClassVar[int]
    CLIENT_DRIVEN_SLOT_FIELD_NUMBER: _ClassVar[int]
    EPISODE_ID_FIELD_NUMBER: _ClassVar[int]
    scenario_id: str
    role_assignment: RoleAssignmentProto
    personas: _containers.MessageMap[str, PersonaProto]
    purpose: str
    client_driven_slot: str
    episode_id: str
    def __init__(self, scenario_id: _Optional[str] = ..., role_assignment: _Optional[_Union[RoleAssignmentProto, _Mapping]] = ..., personas: _Optional[_Mapping[str, PersonaProto]] = ..., purpose: _Optional[str] = ..., client_driven_slot: _Optional[str] = ..., episode_id: _Optional[str] = ...) -> None: ...

class SessionReady(_message.Message):
    __slots__ = ("session_id",)
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    def __init__(self, session_id: _Optional[str] = ...) -> None: ...

class SubmitTurn(_message.Message):
    __slots__ = ("utterance", "inner_thought", "activation_trace")
    UTTERANCE_FIELD_NUMBER: _ClassVar[int]
    INNER_THOUGHT_FIELD_NUMBER: _ClassVar[int]
    ACTIVATION_TRACE_FIELD_NUMBER: _ClassVar[int]
    utterance: str
    inner_thought: str
    activation_trace: _struct_pb2.Struct
    def __init__(self, utterance: _Optional[str] = ..., inner_thought: _Optional[str] = ..., activation_trace: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class TurnUpdate(_message.Message):
    __slots__ = ("turn",)
    TURN_FIELD_NUMBER: _ClassVar[int]
    turn: TurnRecordProto
    def __init__(self, turn: _Optional[_Union[TurnRecordProto, _Mapping]] = ...) -> None: ...

class EndSession(_message.Message):
    __slots__ = ("reason", "episode_id", "failure_detail")
    REASON_FIELD_NUMBER: _ClassVar[int]
    EPISODE_ID_FIELD_NUMBER: _ClassVar[int]
    FAILURE_DETAIL_FIELD_NUMBER: _ClassVar[int]
    reason: str
    episode_id: str
    failure_detail: str
    def __init__(self, reason: _Optional[str] = ..., episode_id: _Optional[str] = ..., failure_detail: _Optional[str] = ...) -> None: ...

class ClientMessage(_message.Message):
    __slots__ = ("create_session", "submit_turn", "end_session")
    CREATE_SESSION_FIELD_NUMBER: _ClassVar[int]
    SUBMIT_TURN_FIELD_NUMBER: _ClassVar[int]
    END_SESSION_FIELD_NUMBER: _ClassVar[int]
    create_session: CreateSession
    submit_turn: SubmitTurn
    end_session: EndSession
    def __init__(self, create_session: _Optional[_Union[CreateSession, _Mapping]] = ..., submit_turn: _Optional[_Union[SubmitTurn, _Mapping]] = ..., end_session: _Optional[_Union[EndSession, _Mapping]] = ...) -> None: ...

class ServerMessage(_message.Message):
    __slots__ = ("session_ready", "turn_update", "end_session")
    SESSION_READY_FIELD_NUMBER: _ClassVar[int]
    TURN_UPDATE_FIELD_NUMBER: _ClassVar[int]
    END_SESSION_FIELD_NUMBER: _ClassVar[int]
    session_ready: SessionReady
    turn_update: TurnUpdate
    end_session: EndSession
    def __init__(self, session_ready: _Optional[_Union[SessionReady, _Mapping]] = ..., turn_update: _Optional[_Union[TurnUpdate, _Mapping]] = ..., end_session: _Optional[_Union[EndSession, _Mapping]] = ...) -> None: ...
