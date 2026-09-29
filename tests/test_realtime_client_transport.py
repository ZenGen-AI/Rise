import threading
import time

import grpc
import pytest

from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server


class _RpcBoom(grpc.RpcError):
    pass


class _FakeStub:
    """伪造 stub：Interact(gen) 记录客户端的请求生成器，并按脚本回放服务端消息。
    脚本项若是异常实例则抛出；脚本耗尽则迭代正常结束（对应服务端关流）。"""

    def __init__(self, script):
        self._script = list(script)
        self.request_gen = None

    def Interact(self, request_gen):
        self.request_gen = request_gen

        def _replies():
            for item in self._script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return _replies()


def _ready(session_id="sess-fake"):
    return pb2.ServerMessage(session_ready=pb2.SessionReady(session_id=session_id))


def _turn(index, slot, text):
    return pb2.ServerMessage(turn_update=pb2.TurnUpdate(turn=pb2.TurnRecordProto(
        turn_index=index, speaker_slot=slot, utterance=text,
    )))


def _assignment(max_turns=4):
    return RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _fake_client(script):
    stub = _FakeStub(script)
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    return client, stub


def _assert_request_stream_released(stub):
    """请求生成器必须能在有限时间内结束，否则 gRPC 的请求消费线程会永远阻塞在
    send_q.get()——即客户端泄漏了后台线程。"""
    drained = []
    worker = threading.Thread(target=lambda: drained.extend(stub.request_gen), daemon=True)
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive(), "请求生成器仍阻塞：客户端泄漏了请求流"


def test_transport_error_closes_client_and_releases_request_stream():
    client, stub = _fake_client([_ready(), _RpcBoom()])
    with pytest.raises(grpc.RpcError):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)


def test_stream_ending_without_end_session_raises_and_releases_request_stream():
    client, stub = _fake_client([_ready()])  # 脚本耗尽 = 服务端关流但没发 EndSession
    with pytest.raises(RuntimeError, match="without sending EndSession"):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)


def test_echo_of_wrong_speaker_raises_instead_of_silently_shifting_turns():
    # 客户端应收到自己（"a"）的回显，却收到了 "b" 的轮次：说明两端对轮转顺序的
    # 理解出现分歧。与其静默错位，不如立刻报错。
    client, stub = _fake_client([_ready(), _turn(0, "b", "wrong-speaker")])
    with pytest.raises(RuntimeError, match="expected echo"):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)


def test_unexpected_message_mid_session_raises_and_releases_request_stream():
    client, stub = _fake_client([_ready(), _ready()])
    with pytest.raises(RuntimeError, match="unexpected server message"):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)


@pytest.fixture
def serve():
    resources = []

    def _serve(factory, **kwargs):
        server = create_server(factory, port=0, **kwargs)
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        client = EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        resources.append((server, channel))
        return client

    yield _serve
    for server, channel in resources:
        channel.close()
        server.stop(0)


def _client_first_create(client):
    return client.create_session(
        scenario_id="s1", role_assignment=_assignment(max_turns=4), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )


def test_session_ending_while_client_is_idle_yields_empty_list_and_timeout_episode(serve):
    # 客户端迟迟不发言，服务端先因 turn 超时发出 EndSession。此后客户端再
    # submit_turn，在"等回显"阶段读到的是 EndSession 而不是回显，必须得到
    # 良构结果（空列表 + closed），而不是卡住或抛异常。
    from simulator.backends import ScriptedBackend

    def factory(scenario, assignment, personas, client_driven_slot):
        return {"b": ScriptedBackend({"b": ["opp1"]})}

    client = serve(factory, turn_timeout_s=0.1)
    _client_first_create(client)
    time.sleep(0.5)
    assert client.submit_turn("too-late") == []
    assert client.closed
    episode = client.drain_until_end()
    assert episode.terminated_reason == "timeout"
    assert episode.turns == []


def test_server_fault_at_create_raises_session_rejected_with_role_backend_failure(serve):
    def exploding_factory(scenario, assignment, personas, client_driven_slot):
        raise ValueError("factory-exploded")

    client = serve(exploding_factory, turn_timeout_s=2.0)
    with pytest.raises(SessionRejectedError) as exc:
        _client_first_create(client)
    assert exc.value.reason == "role_backend_failure"  # 可按 T1 重试，区别于 invalid_session
    assert "factory-exploded" in exc.value.detail
    assert client.closed
