from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc


def test_turn_record_proto_round_trip():
    msg = pb2.TurnRecordProto(turn_index=1, speaker_slot="a", utterance="hi")
    data = msg.SerializeToString()
    back = pb2.TurnRecordProto.FromString(data)
    assert back.turn_index == 1
    assert isinstance(back.turn_index, int)
    assert back.speaker_slot == "a"
    assert back.HasField("inner_thought") is False
    assert back.HasField("activation_trace") is False


def test_client_message_oneof_selects_create_session():
    cs = pb2.CreateSession(scenario_id="s1", purpose="training", client_driven_slot="a")
    msg = pb2.ClientMessage(create_session=cs)
    assert msg.WhichOneof("payload") == "create_session"


def test_grpc_servicer_base_classes_present():
    assert hasattr(pb2_grpc, "EnvironmentServiceServicer")
    assert hasattr(pb2_grpc, "EnvironmentServiceStub")
    assert hasattr(pb2_grpc, "add_EnvironmentServiceServicer_to_server")
