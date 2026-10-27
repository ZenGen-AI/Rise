"""经真实 loopback gRPC：调用方指定对手模型（model_ref），以及内部推理不跨角色泄漏。"""
import queue

import grpc
import pytest

from realtime_fixtures import plain_factory, plain_hook
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.codec import persona_to_proto, role_assignment_to_proto
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server


class _Stream:
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


class _TaggedOpponent:
    """回复里带上自己被指定的 model_ref，并记录它每次看到的历史。"""

    def __init__(self, ref, history_log):
        self._ref = ref
        self._log = history_log

    def respond(self, persona, scenario, history, speaker_slot):
        self._log.append([(t.speaker_slot, t.inner_thought) for t in history])
        return f"[{self._ref}] reply", "opp-secret"


def _factory(history_log):
    def factory(scenario, assignment, personas, client_slot):
        refs = {b.slot_id: b.model_ref for b in assignment.bindings}
        return {
            slot: _TaggedOpponent(refs[slot], history_log)
            for slot in assignment.slot_ids() - {client_slot}
        }
    return factory


def _create_msg(opponent_ref):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref=opponent_ref)],
        max_turns=2,
    )
    personas = {pid: persona_to_proto(Persona(pid, ["t"], "m", "s", ["v"])) for pid in ("pa", "pb")}
    return pb2.ClientMessage(create_session=pb2.CreateSession(
        scenario_id="s1", role_assignment=role_assignment_to_proto(assignment),
        personas=personas, purpose="training", client_driven_slot="a",
    ))


@pytest.fixture
def serve():
    servers = []

    def _serve():
        history_log, closed = [], []
        server = create_server(
            plain_factory(_factory(history_log)), port=0, turn_timeout_s=2.0,
            on_session_closed=plain_hook(closed.append),
        )
        server.start()
        servers.append(server)
        return f"127.0.0.1:{server.bound_port}", history_log, closed

    yield _serve
    for server in servers:
        server.stop(0)


def _play(address, opponent_ref):
    stream = _Stream(address)
    stream.send(_create_msg(opponent_ref))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(
        utterance="cand-say", inner_thought="cand-thought",
    )))
    turns, end = [], None
    while end is None:
        msg = stream.recv()
        kind = msg.WhichOneof("payload")
        if kind == "turn_update":
            turns.append(msg.turn_update.turn)
        elif kind == "end_session":
            end = msg.end_session
    stream.close()
    return turns, end


@pytest.mark.parametrize("ref", ["zengen-v1", "zengen-v2"])
def test_caller_chooses_which_frozen_model_the_opponent_is(serve, ref):
    address, _, closed = serve()
    turns, end = _play(address, ref)
    assert end.reason == "max_turns"
    opponent_turn = next(t for t in turns if t.speaker_slot == "b")
    assert opponent_turn.utterance == f"[{ref}] reply"
    # 落盘的 Episode 记着对手是哪个模型：事后查 reward hacking 要靠它
    assert [b.model_ref for b in closed[0].role_assignment.bindings] == [None, ref]


def test_opponent_inner_thought_is_not_streamed_to_the_client(serve):
    address, _, _ = serve()
    turns, _ = _play(address, "zengen-v1")
    own = next(t for t in turns if t.speaker_slot == "a")
    opp = next(t for t in turns if t.speaker_slot == "b")
    assert own.HasField("inner_thought") and own.inner_thought == "cand-thought"
    assert opp.HasField("inner_thought") is False


def test_opponent_backend_never_sees_the_clients_inner_thought(serve):
    address, history_log, _ = serve()
    _play(address, "zengen-v1")
    seen = [thought for snapshot in history_log for _, thought in snapshot]
    assert "cand-thought" not in seen


def test_persisted_episode_keeps_both_sides_inner_thoughts(serve):
    address, _, closed = serve()
    _play(address, "zengen-v1")
    assert [t.inner_thought for t in closed[0].turns] == ["cand-thought", "opp-secret"]
