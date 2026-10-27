import logging
import queue
import shutil
import threading
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.codec import persona_to_proto, role_assignment_to_proto
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.resources import SessionInfo, SessionResources
from simulator.realtime.server import create_server

PERSONAS = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _assignment(max_turns=2):
    return RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"),
                                 RoleBinding("b", "pb", "frozen_reference")], max_turns)


@pytest.fixture
def serve():
    servers, channels = [], []

    def _serve(factory, **kwargs):
        kwargs.setdefault("turn_timeout_s", 2.0)
        server = create_server(factory, port=0, **kwargs)
        server.start()
        servers.append(server)

        def connect():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        return server, connect

    yield _serve
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


def _open(client, *, episode_id=None, max_turns=2):
    return client.create_session(scenario_id="s1", role_assignment=_assignment(max_turns), personas=PERSONAS,
                                 purpose="training", client_driven_slot="a", episode_id=episode_id)


def _play(client):
    while not client.closed:
        client.submit_turn("嗯")
    return client.drain_until_end()


def _scripted(info):
    return SessionResources(backends={"b": ScriptedBackend({"b": ["一", "二"]})})


def _eventually(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_factory_gets_a_session_info_and_the_hook_gets_the_session_id(serve):
    infos, closed = [], []

    def factory(info):
        infos.append(info)
        return _scripted(info)

    _, connect = serve(factory, on_session_closed=lambda sid, ep: closed.append((sid, ep.id)))
    client = connect()
    session_id = _open(client, episode_id="run-1-grpo_opd-s1-r0")
    episode = _play(client)
    (info,) = infos
    assert isinstance(info, SessionInfo)
    assert (info.session_id, info.episode_id, info.client_slot) == (session_id, "run-1-grpo_opd-s1-r0", "a")
    assert info.scenario.id == "s1" and set(info.personas) == {"pa", "pb"}
    assert episode.id == "run-1-grpo_opd-s1-r0"
    assert closed == [(session_id, "run-1-grpo_opd-s1-r0")]


def test_without_an_episode_id_the_server_keeps_ep_session_id(serve):
    infos = []
    _, connect = serve(lambda info: infos.append(info) or _scripted(info))
    client = connect()
    session_id = _open(client)
    assert _play(client).id == f"ep-{session_id}"
    assert infos[0].episode_id == f"ep-{session_id}"


@pytest.mark.parametrize("bad", ["a/b", ".", "..", "x" * 129, "ep\n", "回合-1", "a b"])
def test_a_malformed_episode_id_is_invalid_session_and_the_factory_is_never_called(serve, bad):
    calls = []
    _, connect = serve(lambda info: calls.append(info) or _scripted(info))
    with pytest.raises(SessionRejectedError) as info:
        _open(connect(), episode_id=bad)
    assert info.value.reason == "invalid_session"
    assert calls == []


@pytest.mark.parametrize("good", ["x" * 128, "a.b", "deploy-zengen-v1-s1-1", "_-.", "..."])
def test_a_well_formed_episode_id_names_the_episode(serve, good):
    _, connect = serve(_scripted)
    client = connect()
    _open(client, episode_id=good)
    assert _play(client).id == good


# ---------------- Task 13:终态收尾 quiesce → join → close ----------------

_SECRET = "secret-token-9f3a"


class _Recording(SessionResources):
    """按调用顺序记下 quiesce/join/close。close 的事件记在真正关完之后:测试看到 "close"
    就能立刻断言副本目录已删,不会和 rmtree 抢。"""

    def __init__(self, events, copy_dir, backends):
        super().__init__(backends=backends, closers=(lambda: shutil.rmtree(copy_dir),))
        self.events = events

    def quiesce(self):
        self.events.append("quiesce")
        super().quiesce()

    def join(self, timeout_s):
        ok = super().join(timeout_s)
        self.events.append(("join", ok))
        return ok

    def close(self):
        super().close()
        self.events.append("close")


class _Failing(_Recording):
    """让指定的步骤在真正做完之后抛异常,异常文字里带一个不该出现在日志里的串。"""

    def __init__(self, events, copy_dir, backends, fail):
        super().__init__(events, copy_dir, backends)
        self.fail = fail

    def quiesce(self):
        super().quiesce()
        if "quiesce" in self.fail:
            raise RuntimeError(_SECRET)

    def join(self, timeout_s):
        ok = super().join(timeout_s)
        if "join" in self.fail:
            raise RuntimeError(_SECRET)
        return ok

    def close(self):
        super().close()
        if "close" in self.fail:
            raise RuntimeError(_SECRET)


def _recording_factory(tmp_path, events, backend_for=lambda: ScriptedBackend({"b": ["一", "二", "三"]}),
                       resources_cls=_Recording, **extra):
    def factory(info):
        copy_dir = tmp_path / info.session_id
        copy_dir.mkdir()
        return resources_cls(events, copy_dir, {"b": backend_for()}, **extra)
    return factory


def _create_msg(max_turns=4):
    return pb2.ClientMessage(create_session=pb2.CreateSession(
        scenario_id="s1",
        role_assignment=role_assignment_to_proto(_assignment(max_turns)),
        personas={pid: persona_to_proto(p) for pid, p in PERSONAS.items()},
        purpose="training",
        client_driven_slot="a",
    ))


class _RawStream:
    """一条裸 gRPC 双向流:精确控制发什么、何时取消,并能读到 RPC 的最终状态码。"""

    def __init__(self, server):
        self._channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        self._q = queue.Queue()
        self.call = pb2_grpc.EnvironmentServiceStub(self._channel).Interact(self._gen())

    def _gen(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item

    def send(self, msg):
        self._q.put(msg)

    def finish_sending(self):
        self._q.put(None)

    def close(self):
        self._q.put(None)
        self._channel.close()


@pytest.fixture
def raw_stream():
    streams = []

    def _open_raw(server):
        stream = _RawStream(server)
        streams.append(stream)
        return stream

    yield _open_raw
    for stream in streams:
        stream.close()


_WOUND_DOWN = ["quiesce", ("join", True), "close"]


@pytest.mark.parametrize("ending", ["max_turns", "turn_timeout", "client_abort"])
def test_every_terminal_state_quiesces_joins_and_closes_and_the_copy_is_removed(serve, tmp_path, ending):
    events = []
    _, connect = serve(_recording_factory(tmp_path, events), turn_timeout_s=0.3)
    client = connect()
    session_id = _open(client, max_turns=4)
    if ending == "max_turns":
        _play(client)
    elif ending == "turn_timeout":
        assert client.drain_until_end().terminated_reason == "timeout"
    else:
        client.submit_turn("嗯")
        client.abort()
    assert _eventually(lambda: "close" in events)
    assert events == _WOUND_DOWN
    assert not (tmp_path / session_id).exists()


def test_a_cancelled_stream_still_quiesces_joins_and_closes(serve, raw_stream, tmp_path):
    events = []
    server, _ = serve(_recording_factory(tmp_path, events))
    stream = raw_stream(server)
    stream.send(_create_msg())
    assert next(stream.call).WhichOneof("payload") == "session_ready"
    stream.call.cancel()                       # 客户端取消整条流,没有任何 EndSession
    stream.finish_sending()                    # 放走客户端侧的请求生成器线程
    assert _eventually(lambda: "close" in events)
    assert events == _WOUND_DOWN
    assert list(tmp_path.iterdir()) == []


def test_a_second_create_on_the_stream_ends_it_and_winds_the_first_session_down(serve, raw_stream, tmp_path):
    events = []
    server, _ = serve(_recording_factory(tmp_path, events))
    stream = raw_stream(server)
    stream.send(_create_msg())
    assert next(stream.call).WhichOneof("payload") == "session_ready"
    stream.send(_create_msg())
    tail = list(stream.call)
    assert tail[-1].end_session.reason == "invalid_session"
    assert _eventually(lambda: "close" in events)
    assert events == _WOUND_DOWN
    assert list(tmp_path.iterdir()) == []


def test_a_failure_after_the_factory_returned_still_closes_the_resources(serve, tmp_path):
    events = []

    def incomplete(info):
        copy_dir = tmp_path / info.session_id
        copy_dir.mkdir()
        return _Recording(events, copy_dir, {})          # 漏了 b 位

    _, connect = serve(incomplete)
    with pytest.raises(SessionRejectedError) as info:
        _open(connect())
    assert info.value.reason == "role_backend_failure"
    assert _eventually(lambda: "close" in events)
    assert events == _WOUND_DOWN
    assert list(tmp_path.iterdir()) == []


def _boom(*args, **kwargs):
    raise RuntimeError(_SECRET)


@pytest.mark.parametrize("failing", ["driver_construction", "driver_start"])
def test_a_driver_that_cannot_be_built_or_started_is_a_server_fault_and_the_resources_close(
        serve, tmp_path, monkeypatch, failing):
    if failing == "driver_construction":
        monkeypatch.setattr("simulator.realtime.servicer.SessionDriver", _boom)
    else:
        monkeypatch.setattr(SessionDriver, "start", _boom)
    events = []
    _, connect = serve(_recording_factory(tmp_path, events))
    with pytest.raises(SessionRejectedError) as info:
        _open(connect())
    assert info.value.reason == "role_backend_failure"
    assert "RuntimeError" in info.value.detail and _SECRET not in info.value.detail   # 只报类名
    assert _eventually(lambda: "close" in events)
    assert events == _WOUND_DOWN
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fail", [("quiesce",), ("close",), ("quiesce", "close")])
def test_a_quiesce_or_close_that_raises_is_logged_by_class_name_and_does_not_break_the_rpc(
        serve, raw_stream, tmp_path, caplog, fail):
    events = []
    server, _ = serve(_recording_factory(tmp_path, events, resources_cls=_Failing, fail=fail))
    stream = raw_stream(server)
    with caplog.at_level(logging.WARNING, logger="simulator.realtime.servicer"):
        stream.send(_create_msg())
        assert next(stream.call).WhichOneof("payload") == "session_ready"
        stream.send(pb2.ClientMessage(end_session=pb2.EndSession(reason="client_terminated")))
        tail = list(stream.call)          # finally 抛出的话,迭代到末尾会抛 grpc.RpcError
        assert tail[-1].end_session.reason == "client_terminated"
        assert stream.call.code() == grpc.StatusCode.OK
        assert _eventually(lambda: "close" in events)
        assert _eventually(lambda: any("RuntimeError" in r.getMessage() for r in caplog.records))
    assert events == _WOUND_DOWN                 # 一步抛了,后面的步骤照样做
    assert _SECRET not in caplog.text


def test_a_join_that_raises_counts_as_not_quiesced_so_it_lingers_instead_of_closing(serve, raw_stream, tmp_path, caplog):
    events = []
    server, _ = serve(_recording_factory(tmp_path, events, resources_cls=_Failing, fail=("join",)))
    stream = raw_stream(server)
    with caplog.at_level(logging.WARNING, logger="simulator.realtime.servicer"):
        stream.send(_create_msg())
        assert next(stream.call).WhichOneof("payload") == "session_ready"
        stream.send(pb2.ClientMessage(end_session=pb2.EndSession(reason="client_terminated")))
        list(stream.call)
        assert stream.call.code() == grpc.StatusCode.OK
        assert _eventually(lambda: server.servicer.lingering_count() == 1)
    assert events[:2] == ["quiesce", ("join", True)] and "close" not in events      # 不关
    assert len(list(tmp_path.iterdir())) == 1                                       # 不删
    assert _SECRET not in caplog.text


class _Stuck:
    def __init__(self):
        self.release = threading.Event()

    def respond(self, persona, scenario, history, speaker_slot):
        self.release.wait(10)
        return "late", None


def test_a_join_timeout_keeps_the_copy_and_lingers_until_a_retry_succeeds(serve, tmp_path, caplog):
    events, stuck = [], _Stuck()
    server, connect = serve(_recording_factory(tmp_path, events, backend_for=lambda: stuck),
                            backend_timeout_s=0.1, resource_join_timeout_s=0.2, linger_retry_s=0.05)
    client = connect()
    session_id = _open(client)
    with caplog.at_level(logging.WARNING, logger="simulator.realtime.servicer"):
        client.submit_turn("嗯")
        assert client.drain_until_end().terminated_reason == "role_backend_timeout"
        assert _eventually(lambda: server.servicer.lingering_count() == 1)
        assert "close" not in events and (tmp_path / session_id).exists()      # 不关、不删
        assert _eventually(lambda: any("still lingering" in r.getMessage() for r in caplog.records))
        assert any("did not quiesce" in r.getMessage() for r in caplog.records)
        stuck.release.set()
        assert _eventually(lambda: server.servicer.lingering_count() == 0)
    assert events[-1] == "close" and not (tmp_path / session_id).exists()
    assert events.count("close") == 1                                          # close 只驱动一次


def test_a_new_lingering_session_after_the_table_emptied_gets_a_fresh_reaper(serve, tmp_path):
    events, stucks = [], []

    def backend_for():
        stuck = _Stuck()
        stucks.append(stuck)
        return stuck

    server, connect = serve(_recording_factory(tmp_path, events, backend_for=backend_for),
                            backend_timeout_s=0.1, resource_join_timeout_s=0.2, linger_retry_s=0.05)
    for round_no in (1, 2):
        client = connect()
        _open(client)
        client.submit_turn("嗯")
        assert client.drain_until_end().terminated_reason == "role_backend_timeout"
        assert _eventually(lambda: server.servicer.lingering_count() == 1)
        stucks[-1].release.set()
        assert _eventually(lambda: server.servicer.lingering_count() == 0)
        assert _eventually(lambda: events.count("close") == round_no)
    assert list(tmp_path.iterdir()) == []


def test_a_reaper_that_cannot_start_does_not_break_the_rpc_and_the_resources_stay_lingering(
        serve, tmp_path, monkeypatch, caplog):
    real_start = threading.Thread.start

    def start(self):
        if self.name == "session-reaper":
            raise RuntimeError(_SECRET)
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    events, stuck = [], _Stuck()
    server, connect = serve(_recording_factory(tmp_path, events, backend_for=lambda: stuck),
                            backend_timeout_s=0.1, resource_join_timeout_s=0.2, linger_retry_s=0.05)
    client = connect()
    session_id = _open(client)
    with caplog.at_level(logging.WARNING, logger="simulator.realtime.servicer"):
        client.submit_turn("嗯")
        assert client.drain_until_end().terminated_reason == "role_backend_timeout"
        assert _eventually(lambda: server.servicer.lingering_count() == 1)
        assert _eventually(lambda: any("could not start" in r.getMessage() for r in caplog.records))
    stuck.release.set()
    assert "close" not in events and (tmp_path / session_id).exists()      # 没有回收线程,不关、不删
    assert _SECRET not in caplog.text
