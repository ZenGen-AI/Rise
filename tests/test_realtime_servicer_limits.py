import queue
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.codec import persona_to_proto, role_assignment_to_proto
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server


class _Stream:
    """一条裸 gRPC 双向流，便于精确控制客户端发什么、何时发。"""

    def __init__(self, address):
        self._channel = grpc.insecure_channel(address)
        self._q = queue.Queue()
        stub = pb2_grpc.EnvironmentServiceStub(self._channel)
        self._resp = stub.Interact(self._gen())

    def _gen(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item

    def send(self, msg):
        self._q.put(msg)

    def recv(self):
        return next(self._resp)

    def close(self):
        self._q.put(None)
        self._channel.close()


def _create_msg(*, client_slot="a", max_turns=4, persona_ids=("pa", "pb"), scenario_id="s1"):
    assignment = RoleAssignment(
        scenario_id=scenario_id,
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    personas = {
        pid: persona_to_proto(Persona(pid, ["t"], "m", "s", ["v"])) for pid in persona_ids
    }
    return pb2.ClientMessage(create_session=pb2.CreateSession(
        scenario_id=scenario_id,
        role_assignment=role_assignment_to_proto(assignment),
        personas=personas,
        purpose="training",
        client_driven_slot=client_slot,
    ))


def _opp_factory(scenario, assignment, personas, client_driven_slot):
    other = assignment.slot_ids() - {client_driven_slot}
    return {slot: ScriptedBackend({slot: ["opp1", "opp2", "opp3", "opp4"]}) for slot in other}


@pytest.fixture
def start_server():
    servers = []

    def _start(factory=_opp_factory, **kwargs):
        kwargs.setdefault("turn_timeout_s", 2.0)
        server = create_server(factory, port=0, **kwargs)
        server.start()
        servers.append(server)
        return f"127.0.0.1:{server.bound_port}"

    yield _start
    for server in servers:
        server.stop(0)


def _expect_invalid_session(stream, *needles):
    msg = stream.recv()
    assert msg.WhichOneof("payload") == "end_session", msg
    assert msg.end_session.reason == "invalid_session"
    for needle in needles:
        assert needle in msg.end_session.failure_detail, msg.end_session.failure_detail
    # 被拒绝的会话必须就此结束：没有 session_ready，也没有后续消息
    with pytest.raises(StopIteration):
        stream.recv()


# ---------------- Important 3：CreateSession 校验 ----------------

def test_unbound_client_slot_rejected_without_running_autonomous_turns(start_server):
    address = start_server()
    stream = _Stream(address)
    stream.send(_create_msg(client_slot="ghost", max_turns=100))
    _expect_invalid_session(stream, "ghost")
    stream.close()


def test_missing_persona_rejected_as_invalid_session_not_backend_failure(start_server):
    address = start_server()
    stream = _Stream(address)
    stream.send(_create_msg(persona_ids=("pa",)))
    _expect_invalid_session(stream, "pb")
    stream.close()


def test_max_turns_over_cap_rejected(start_server):
    address = start_server(max_turns_cap=10)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=11))
    _expect_invalid_session(stream, "10")
    stream.close()


def test_backends_factory_exception_becomes_invalid_session(start_server):
    def exploding_factory(scenario, assignment, personas, client_driven_slot):
        raise ValueError("factory-exploded")

    address = start_server(factory=exploding_factory)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_invalid_session(stream, "factory-exploded")
    stream.close()


def test_duplicate_create_session_rejected_and_first_session_not_persisted(start_server):
    closed = []
    address = start_server(on_session_closed=lambda ep: closed.append(ep.id))
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=100))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(_create_msg(max_turns=100))
    _expect_invalid_session(stream, "twice")
    assert closed == []  # 被连带终止的第一个会话不得当作正常 episode 落盘
    stream.close()


def test_submit_turn_before_create_session_rejected(start_server):
    address = start_server()
    stream = _Stream(address)
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="too-early")))
    _expect_invalid_session(stream, "before create_session")
    stream.close()


def test_end_session_before_create_session_rejected(start_server):
    address = start_server()
    stream = _Stream(address)
    stream.send(pb2.ClientMessage(end_session=pb2.EndSession(reason="client_terminated")))
    _expect_invalid_session(stream, "before create_session")
    stream.close()


# ---------------- Important 8：资源上限 ----------------

def test_no_create_session_within_deadline_ends_stream(start_server):
    address = start_server(create_timeout_s=0.2)
    stream = _Stream(address)
    started = time.monotonic()
    _expect_invalid_session(stream, "create_session")
    assert time.monotonic() - started < 3.0
    stream.close()


def test_slow_server_backend_ends_session_with_role_backend_timeout(start_server):
    class SlowBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            time.sleep(1.0)
            return ("late", None)

    def slow_factory(scenario, assignment, personas, client_driven_slot):
        return {"b": SlowBackend()}

    address = start_server(factory=slow_factory, backend_timeout_s=0.1)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=4))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    assert stream.recv().WhichOneof("payload") == "turn_update"  # 客户端自己那一轮
    end = stream.recv()
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_timeout"  # T1，与 role_backend_failure 区分
    stream.close()


def test_concurrent_rpc_limit_rejects_extra_session(start_server):
    address = start_server(max_workers=1)
    first = _Stream(address)
    first.send(_create_msg(max_turns=100))
    assert first.recv().WhichOneof("payload") == "session_ready"
    second = _Stream(address)
    with pytest.raises(grpc.RpcError) as exc:
        second.recv()
    assert exc.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    second.close()
    first.close()


# ---------------- Important 7：scenario_resolver ----------------

class _RecordingBackend:
    def __init__(self, seen):
        self._seen = seen

    def respond(self, persona, scenario, history, speaker_slot):
        self._seen.append(scenario.background)
        return ("ok", None)


def _run_one_turn_and_drain(stream):
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    while True:
        msg = stream.recv()
        if msg.WhichOneof("payload") == "end_session":
            return msg


def test_scenario_resolver_supplies_full_scenario_to_backends(start_server):
    seen = []

    def resolver(scenario_id):
        return Scenario(
            id=scenario_id, background="REAL-BACKGROUND",
            role_slots=[RoleSlot("a", "x"), RoleSlot("b", "y")],
            conflict="c", norms=["n"], purpose="training",
        )

    def factory(scenario, assignment, personas, client_driven_slot):
        return {"b": _RecordingBackend(seen)}

    address = start_server(factory=factory, scenario_resolver=resolver)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=2))
    end = _run_one_turn_and_drain(stream)
    assert end.end_session.reason == "max_turns"
    assert seen == ["REAL-BACKGROUND"]
    stream.close()


def test_without_resolver_backends_still_get_empty_scenario(start_server):
    seen = []

    def factory(scenario, assignment, personas, client_driven_slot):
        return {"b": _RecordingBackend(seen)}

    address = start_server(factory=factory)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=2))
    _run_one_turn_and_drain(stream)
    assert seen == [""]  # 向后兼容：不提供 resolver 时行为不变
    stream.close()


def test_resolver_scenario_missing_required_slots_rejected(start_server):
    def resolver(scenario_id):
        return Scenario(
            id=scenario_id, background="bg", role_slots=[RoleSlot("a", "x")],
            conflict="", norms=[], purpose="training",
        )

    address = start_server(scenario_resolver=resolver)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_invalid_session(stream, "['b']")
    stream.close()


def test_resolver_exception_becomes_invalid_session(start_server):
    def resolver(scenario_id):
        raise KeyError("resolver-exploded")

    address = start_server(scenario_resolver=resolver)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_invalid_session(stream, "resolver-exploded")
    stream.close()
