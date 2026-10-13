from google.protobuf.json_format import MessageToDict

from simulator.realtime.proto import realtime_env_pb2 as pb2


def test_submit_turn_without_a_trace_has_no_presence():
    back = pb2.SubmitTurn.FromString(pb2.SubmitTurn(utterance="hi").SerializeToString())
    assert back.utterance == "hi"
    assert back.HasField("activation_trace") is False


def test_submit_turn_carries_an_explicitly_empty_trace():
    msg = pb2.SubmitTurn(utterance="hi")
    msg.activation_trace.SetInParent()
    back = pb2.SubmitTurn.FromString(msg.SerializeToString())
    assert back.HasField("activation_trace") is True
    assert MessageToDict(back.activation_trace) == {}


def test_submit_turn_trace_round_trips_strings_lists_bools_and_nulls():
    trace = {"selected": "skill-a", "candidates": ["skill-a", "skill-b"], "conflict": True,
             "experience": None, "library_version": {"skill": "3", "memory": "1"}}
    msg = pb2.SubmitTurn(utterance="hi")
    msg.activation_trace.update(trace)
    back = pb2.SubmitTurn.FromString(msg.SerializeToString())
    assert MessageToDict(back.activation_trace) == trace


def test_struct_turns_integers_into_floats():
    # 本计划的"坑"被钉住:Struct 只有 double,整数 3 回来是 3.0。
    # 激活轨迹的模式因此不含任何数字字段(见计划"对外契约")。
    msg = pb2.SubmitTurn(utterance="hi")
    msg.activation_trace.update({"n": 3})
    back = MessageToDict(pb2.SubmitTurn.FromString(msg.SerializeToString()).activation_trace)
    assert back == {"n": 3.0}
    assert isinstance(back["n"], float)


def test_the_field_number_is_three():
    assert pb2.SubmitTurn.ACTIVATION_TRACE_FIELD_NUMBER == 3
