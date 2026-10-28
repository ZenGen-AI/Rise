import threading
import time

import grpc
import pytest

from realtime_fixtures import plain_factory
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


def test_create_session_sends_the_episode_id_and_names_the_local_episode_after_it():
    stub = _FakeStub([_ready("sess-fake")])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a", episode_id="run-1-grpo_opd-s1-r0",
    )
    assert next(stub.request_gen).create_session.episode_id == "run-1-grpo_opd-s1-r0"
    client.abort()
    assert client.drain_until_end().id == "run-1-grpo_opd-s1-r0"


def test_without_an_episode_id_the_request_leaves_it_empty_and_the_local_id_stays_ep_session():
    stub = _FakeStub([_ready("sess-fake")])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    assert next(stub.request_gen).create_session.episode_id == ""
    client.abort()
    assert client.drain_until_end().id == "ep-sess-fake"


def test_an_empty_episode_id_is_refused_before_anything_is_sent():
    # 空串在 proto3 里与"没给"分不出,服务端会悄悄改名为 ep-<session_id>,与本地 id 分叉
    stub = _FakeStub([_ready("sess-fake")])
    client = EnvironmentServiceClient(stub)
    with pytest.raises(ValueError, match="episode_id"):
        client.create_session(
            scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
            purpose="training", client_driven_slot="a", episode_id="",
        )
    assert not client.closed
    client.abort()


@pytest.fixture
def serve():
    resources = []

    def _serve(factory, **kwargs):
        server = create_server(plain_factory(factory), port=0, **kwargs)
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
    # detail 只报类名:工厂异常的原文可能带端点或密钥,部署路上还会写进 git 树里的失败记录。
    assert "ValueError" in exc.value.detail
    assert "factory-exploded" not in exc.value.detail
    assert client.closed


# ---- message_timeout_s:客户端相邻服务端消息的等待上限 ----


class _PacedReplies:
    """带 cancel() 的响应流,像真 gRPC 的流:cancel 让挂住的读取尽快结束。"""

    def __init__(self, replies, cancelled):
        self._replies = replies
        self._cancelled = cancelled

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._replies)

    def cancel(self):
        self._cancelled.set()


class _PacedStub:
    """按 (delay_s, msg) 回放;msg 为 _SILENT 时一直不回(最多 5 秒),模拟挂住的服务端。
    cancellable=True 时响应流带 cancel()(像真 gRPC),False 时是裸生成器(没有 cancel)。"""
    _SILENT = object()

    def __init__(self, script, *, cancellable=True):
        self._script = list(script)
        self._cancellable = cancellable
        self.request_gen = None
        self.cancelled = threading.Event()

    def Interact(self, request_gen):
        self.request_gen = request_gen

        def _replies():
            for delay_s, msg in self._script:
                if msg is _PacedStub._SILENT:
                    self.cancelled.wait(5)
                    return
                time.sleep(delay_s)
                yield msg

        replies = _replies()
        return _PacedReplies(replies, self.cancelled) if self._cancellable else replies


def _paced_client(script, timeout_s, **stub_kwargs):
    stub = _PacedStub(script, **stub_kwargs)
    client = EnvironmentServiceClient(stub, message_timeout_s=timeout_s)
    return client, stub


def _assert_receiver_exits(client):
    """接收线程必须能在有限时间内退出,否则客户端泄漏了后台线程。"""
    client._recv_thread.join(2.0)
    assert not client._recv_thread.is_alive(), "接收线程仍在运行:客户端泄漏了后台线程"


def test_a_silent_server_mid_session_ends_the_episode_as_timeout_and_releases_the_stream():
    client, stub = _paced_client([(0, _ready()), (0, _PacedStub._SILENT)], timeout_s=0.2)
    client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                          purpose="training", client_driven_slot="a")
    started = time.monotonic()
    assert client.submit_turn("hi") == []
    assert time.monotonic() - started < 2.0
    assert client.closed
    assert client.drain_until_end().terminated_reason == "timeout"
    _assert_request_stream_released(stub)
    assert stub.cancelled.is_set()  # 超时时取消了响应流
    _assert_receiver_exits(client)


def test_the_limit_is_per_gap_not_per_session():
    # 每条间隔 0.15 秒,低于上限 0.3;合计 0.45 秒高于上限,仍不超时
    end = pb2.ServerMessage(end_session=pb2.EndSession(reason="max_turns", episode_id="ep-sess-fake"))
    client, _ = _paced_client([(0, _ready()), (0.15, _turn(0, "a", "hi")), (0.15, _turn(1, "b", "yo")),
                               (0.15, end)], timeout_s=0.3)
    client.create_session(scenario_id="s1", role_assignment=_assignment(max_turns=2), personas=_personas(),
                          purpose="training", client_driven_slot="a")
    client.submit_turn("hi")
    assert client.drain_until_end().terminated_reason == "max_turns"
    _assert_receiver_exits(client)


def test_a_silent_server_before_session_ready_is_a_timeout_rejection():
    client, stub = _paced_client([(0, _PacedStub._SILENT)], timeout_s=0.2)
    with pytest.raises(SessionRejectedError) as info:
        client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                              purpose="training", client_driven_slot="a")
    assert info.value.reason == "timeout"
    assert "no server message" in info.value.detail
    assert client.closed
    _assert_request_stream_released(stub)
    _assert_receiver_exits(client)


def test_a_timeout_on_a_stream_without_cancel_still_ends_as_timeout():
    # 响应流不一定有 cancel()(裸迭代器):超时照样合成 timeout 收尾,只是不取消流
    client, stub = _paced_client([(0, _ready()), (0, _PacedStub._SILENT)], timeout_s=0.1,
                                 cancellable=False)
    client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                          purpose="training", client_driven_slot="a")
    try:
        assert client.submit_turn("hi") == []
        assert client.drain_until_end().terminated_reason == "timeout"
        _assert_request_stream_released(stub)
        assert not stub.cancelled.is_set()
    finally:
        stub.cancelled.set()  # 放走挂住的生成器,让接收线程退出
    _assert_receiver_exits(client)


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_message_timeout_must_be_a_finite_positive_number_and_opens_no_stream(bad):
    stub = _PacedStub([])
    with pytest.raises(ValueError):
        EnvironmentServiceClient(stub, message_timeout_s=bad)
    assert stub.request_gen is None  # 校验在开流之前,不留悬空的流


def test_without_a_message_timeout_there_is_no_receiver_thread():
    client = EnvironmentServiceClient(_FakeStub([_ready()]))
    assert client._message_timeout_s is None
    assert client._recv_thread is None


def test_the_message_timeout_is_kept_on_the_client():
    client = EnvironmentServiceClient(_FakeStub([]), message_timeout_s=90)
    assert client._message_timeout_s == 90
    _assert_receiver_exits(client)  # 空脚本:流立刻结束,接收线程随之退出


def test_with_a_message_timeout_a_normal_session_runs_to_its_end_and_the_receiver_exits():
    end = pb2.ServerMessage(end_session=pb2.EndSession(reason="goal_reached"))
    client, _ = _fake_client_with_timeout([_ready(), _turn(0, "a", "hi"), end])
    client.submit_turn("hi")
    assert client.closed
    assert client.drain_until_end().terminated_reason == "goal_reached"
    _assert_receiver_exits(client)


def _fake_client_with_timeout(script, message_timeout_s=5.0):
    stub = _FakeStub(script)
    client = EnvironmentServiceClient(stub, message_timeout_s=message_timeout_s)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    return client, stub


def test_with_a_message_timeout_a_transport_error_still_closes_and_reraises():
    client, stub = _fake_client_with_timeout([_ready(), _RpcBoom()])
    with pytest.raises(grpc.RpcError):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)
    _assert_receiver_exits(client)


def test_with_a_message_timeout_a_stream_ending_without_end_session_still_raises():
    client, stub = _fake_client_with_timeout([_ready()])
    with pytest.raises(RuntimeError, match="without sending EndSession"):
        client.submit_turn("x")
    assert client.closed
    _assert_request_stream_released(stub)
    _assert_receiver_exits(client)
