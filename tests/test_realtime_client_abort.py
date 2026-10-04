import threading
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server


def _assignment(max_turns=100):
    return RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=max_turns,
    )


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _factory(scenario, assignment, personas, client_slot):
    others = assignment.slot_ids() - {client_slot}
    return {s: ScriptedBackend({s: ["opp1", "opp2", "opp3", "opp4"]}) for s in others}


@pytest.fixture
def env():
    closed: list[str] = []
    server = create_server(
        _factory, port=0, turn_timeout_s=2.0,
        on_session_closed=lambda ep: closed.append(ep.id),
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    yield pb2_grpc.EnvironmentServiceStub(channel), closed
    channel.close()
    server.stop(0)


def _open(stub, max_turns=100):
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(max_turns),
        personas=_personas(), purpose="training", client_driven_slot="a",
    )
    return client


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------- turns ----------------

def test_turns_holds_own_echo_and_opponent_replies_in_order(env):
    stub, _ = env
    client = _open(stub)
    assert client.turns == []
    client.submit_turn("hello")
    assert [(t.speaker_slot, t.utterance) for t in client.turns] == [
        ("a", "hello"), ("b", "opp1"),
    ]
    client.abort()


def test_turns_is_a_deep_copy_so_a_policy_cannot_corrupt_the_clients_state(env):
    stub, _ = env
    client = _open(stub)
    client.submit_turn("hello")
    snapshot = client.turns
    snapshot[0].utterance = "TAMPERED"
    snapshot.clear()
    assert [t.utterance for t in client.turns] == ["hello", "opp1"]
    client.abort()


# ---------------- abort ----------------

def test_abort_closes_the_client_and_blocks_further_submits(env):
    stub, _ = env
    client = _open(stub)
    client.abort()
    assert client.closed
    with pytest.raises(RuntimeError):
        client.submit_turn("too late")


def test_abort_is_idempotent(env):
    stub, _ = env
    client = _open(stub)
    client.abort()
    client.abort()
    assert client.closed


def test_abort_after_a_normal_end_does_not_rewrite_the_reason(env):
    stub, _ = env
    client = _open(stub, max_turns=2)
    client.submit_turn("hi")  # a, b => 到达 max_turns,服务端发 EndSession
    assert client.closed
    client.abort()
    assert client.drain_until_end().terminated_reason == "max_turns"


def test_aborted_session_is_never_persisted_but_an_ended_one_is(env):
    stub, closed = env
    aborted = _open(stub)
    aborted.submit_turn("half-finished")
    aborted.abort()

    ended = _open(stub)
    ended.submit_turn("complete")
    ended_episode = ended.end_session()

    # 正向对照:落盘钩子确实会触发,所以下面"被放弃的那条没进来"不是空转
    assert _wait_until(lambda: ended_episode.id in closed)
    time.sleep(0.3)  # 给"错误地落盘"留出显形的时间
    assert closed == [ended_episode.id]


# ---------------- abort 释放请求流(不泄漏后台线程) ----------------

class _FakeStub:
    def __init__(self, script):
        self._script = list(script)
        self.request_gen = None

    def Interact(self, request_gen):
        self.request_gen = request_gen
        return iter(self._script)


def test_abort_releases_the_request_stream():
    ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id="sess-fake"))
    stub = _FakeStub([ready])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    client.abort()

    drained: list = []
    worker = threading.Thread(target=lambda: drained.extend(stub.request_gen), daemon=True)
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive(), "请求生成器仍阻塞:abort() 没有释放请求流"
