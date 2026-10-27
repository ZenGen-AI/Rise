import queue
import time

import grpc
import pytest

from realtime_fixtures import plain_factory
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
        server = create_server(plain_factory(factory), port=0, **kwargs)
        server.start()
        servers.append(server)
        return f"127.0.0.1:{server.bound_port}"

    yield _start
    for server in servers:
        server.stop(0)


def _expect_rejected(stream, reason, *needles):
    msg = stream.recv()
    assert msg.WhichOneof("payload") == "end_session", msg
    assert msg.end_session.reason == reason, msg.end_session
    for needle in needles:
        assert needle in msg.end_session.failure_detail, msg.end_session.failure_detail
    # 被拒绝的会话必须就此结束：没有 session_ready，也没有后续消息
    with pytest.raises(StopIteration):
        stream.recv()


def _expect_invalid_session(stream, *needles):
    """请求本身有问题（重试无益，T2）。"""
    _expect_rejected(stream, "invalid_session", *needles)


def _expect_server_fault(stream, *needles):
    """服务端依赖故障（可能瞬态，T1）：刻意与 invalid_session 区分开。"""
    _expect_rejected(stream, "role_backend_failure", *needles)


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


def test_backends_factory_exception_is_server_fault_not_invalid_session(start_server):
    # 工厂出错（模型加载失败、外部 API 不可达等）是服务端依赖故障，可能瞬态，
    # 不能冒充"客户端请求有问题"——否则调用方会误判为重试无益。
    def exploding_factory(scenario, assignment, personas, client_driven_slot):
        raise ValueError("factory-exploded")

    address = start_server(factory=exploding_factory)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_server_fault(stream, "factory-exploded")
    stream.close()


def test_backends_factory_missing_a_slot_is_rejected_before_any_driver_starts(start_server):
    # 曾经的行为：先发 session_ready，客户端已提交若干轮，之后 run_episode 在
    # backends[slot] 上 KeyError，被误报成会话中途的 role_backend_failure。
    def incomplete_factory(scenario, assignment, personas, client_driven_slot):
        return {}  # 漏返回对手 "b" 的后端

    address = start_server(factory=incomplete_factory)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_server_fault(stream, "['b']")  # 第一条消息就是拒绝，没有 session_ready
    stream.close()


def test_duplicate_create_session_winds_down_first_driver_with_invalid_session(
    start_server, monkeypatch,
):
    # 只断言 closed == [] 是空转的：拒绝后 Interact 立刻返回，不会再去 drain
    # 第一个 driver 的 outbox，on_session_closed 无论如何都不会被调用。所以这里
    # 直接拿到第一个 driver，检查它确实被主动收尾、且原因是 invalid_session。
    from simulator.realtime.session import SessionState
    import simulator.realtime.servicer as servicer_module

    drivers = []

    class RecordingDriver(servicer_module.SessionDriver):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            drivers.append(self)

    monkeypatch.setattr(servicer_module, "SessionDriver", RecordingDriver)
    closed = []
    # turn_timeout_s 取大：若第一个 driver 是靠自身超时才结束，就等不到 failed
    # 事件，测试会失败——以此证明它是被 request_abort 主动收尾的。
    address = start_server(
        on_session_closed=lambda _sid, ep: closed.append(ep.id), turn_timeout_s=30.0,
    )
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=100))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(_create_msg(max_turns=100))
    _expect_invalid_session(stream, "twice")
    stream.close()

    assert len(drivers) == 1
    failed = None
    deadline = time.monotonic() + 2.0
    while failed is None and time.monotonic() < deadline:
        event = drivers[0].poll_event(timeout_s=0.1)
        if event is not None and event.kind == "failed":
            failed = event
    assert failed is not None, "第一个 driver 没有被主动收尾"
    # 原因必须是重复创建触发的 invalid_session；Interact 的 finally 兜底随后会
    # 再调一次 request_abort("client_disconnected")，后者不得覆盖前者。
    assert failed.payload == "invalid_session"
    assert drivers[0].state == SessionState.FAILED
    assert closed == []


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


class _SlowChatServer:
    """本机 OpenAI 兼容桩:收到请求后先睡 delay_s 再回一句,用来逼外部 API 对手的单次调用超时。"""

    def __init__(self, delay_s, status=200, body=None):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        if body is None:
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "late"}}]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                time.sleep(delay_s)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass          # 客户端已超时离开

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def slow_chat(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")          # 本机若配了代理,别让桩的请求绕出去
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    made = []

    def _make(delay_s, status=200, body=None):
        server = _SlowChatServer(delay_s, status, body)
        made.append(server)
        return server

    yield _make
    for server in made:
        server.close()


def _external_opponent_session(start_server, base_url, *, opponent_timeout_s):
    """真实服务端 + 真实 ExternalApiBackend(对手位 b)。服务端每回合限时放宽,让对手自己的 HTTP 上限先到。"""
    from factory.opponents import ExternalApiBackend

    def factory(scenario, assignment, personas, client_driven_slot):
        return {"b": ExternalApiBackend(base_url, "ext-model", api_key="sk-test-not-a-secret",
                                        api_key_env="OPP_API_KEY", timeout_s=opponent_timeout_s)}

    address = start_server(factory=factory, backend_timeout_s=10.0)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=4))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    assert stream.recv().WhichOneof("payload") == "turn_update"          # 客户端自己那一轮
    end = stream.recv()
    stream.close()
    return end


def test_an_external_api_opponent_that_times_out_ends_the_session_as_role_backend_timeout(
        start_server, slow_chat):
    """对手位的外部 API 超时是 T1(瞬态),轨迹源据此整条重跑;不能被通用异常分支吞成 role_backend_failure。"""
    server = slow_chat(1.5)
    end = _external_opponent_session(start_server, server.base_url, opponent_timeout_s=0.2)
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_timeout", end.end_session
    assert "sk-test-not-a-secret" not in end.end_session.failure_detail


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_external_api_opponent_answering_429_or_5xx_ends_the_session_as_role_backend_unavailable(
        start_server, slow_chat, status):
    """429 与 5xx 是对端暂时不可用(T1):轨迹源据此按条重跑;不是 role_backend_failure(那个不重跑)。"""
    server = slow_chat(0.0, status=status)
    end = _external_opponent_session(start_server, server.base_url, opponent_timeout_s=5.0)
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_unavailable", end.end_session
    assert "sk-test-not-a-secret" not in end.end_session.failure_detail


def test_an_external_api_opponent_that_cannot_be_reached_ends_the_session_as_role_backend_unavailable(
        start_server):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    end = _external_opponent_session(start_server, f"http://127.0.0.1:{port}", opponent_timeout_s=5.0)
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_unavailable", end.end_session


def test_an_external_api_opponent_with_a_malformed_reply_still_ends_the_session_as_role_backend_failure(
        start_server, slow_chat):
    """回复不是约定的形状,分不出 T1 与 T2:仍是 role_backend_failure,轨迹源不按条重跑。"""
    server = slow_chat(0.0, body=b"not json at all")
    end = _external_opponent_session(start_server, server.base_url, opponent_timeout_s=5.0)
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_failure", end.end_session


def test_a_backend_that_raises_role_backend_unavailable_ends_the_session_as_role_backend_unavailable(start_server):
    from simulator.realtime.backend_timeout import RoleBackendUnavailableError

    class DownBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RoleBackendUnavailableError("HTTP 503 from the peer")

    address = start_server(factory=lambda *_: {"b": DownBackend()}, backend_timeout_s=10.0)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=4))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    assert stream.recv().WhichOneof("payload") == "turn_update"
    end = stream.recv()
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_unavailable", end.end_session
    stream.close()


def test_a_backend_that_raises_role_backend_rejected_ends_the_session_as_role_backend_rejected(start_server):
    """对端明确拒绝(确定性,T2):终止原因独立于 role_backend_failure,经 gRPC 到客户端原样。"""
    from simulator.realtime.backend_timeout import RoleBackendRejectedError

    class RejectingBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RoleBackendRejectedError("HTTP 404 from the peer")

    address = start_server(factory=lambda *_: {"b": RejectingBackend()}, backend_timeout_s=10.0)
    stream = _Stream(address)
    stream.send(_create_msg(max_turns=4))
    assert stream.recv().WhichOneof("payload") == "session_ready"
    stream.send(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    assert stream.recv().WhichOneof("payload") == "turn_update"
    end = stream.recv()
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_rejected", end.end_session
    stream.close()


def test_an_external_api_opponent_answering_404_ends_the_session_as_role_backend_rejected(
        start_server, slow_chat):
    """4xx(429 除外)是对端明确拒绝:真实 ExternalApiBackend 抛 OpponentRejectedError,经 TimeoutBackend 原样重抛,
    服务端据此报 role_backend_rejected。信息里没有密钥。"""
    server = slow_chat(0.0, status=404)
    end = _external_opponent_session(start_server, server.base_url, opponent_timeout_s=5.0)
    assert end.WhichOneof("payload") == "end_session"
    assert end.end_session.reason == "role_backend_rejected", end.end_session
    assert "sk-test-not-a-secret" not in end.end_session.failure_detail


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


def test_resolver_unknown_scenario_id_is_invalid_session(start_server):
    # 未知 id 是客户端输入错误（T2）：重试无益
    def resolver(scenario_id):
        raise KeyError(scenario_id)

    address = start_server(scenario_resolver=resolver)
    stream = _Stream(address)
    stream.send(_create_msg(scenario_id="no-such-scenario"))
    _expect_invalid_session(stream, "no-such-scenario")
    stream.close()


def test_resolver_infrastructure_error_is_server_fault_not_invalid_session(start_server):
    # 场景库不可达之类的 IO 故障是服务端依赖故障（可能瞬态，T1）
    def resolver(scenario_id):
        raise OSError("scenario-store-unreachable")

    address = start_server(scenario_resolver=resolver)
    stream = _Stream(address)
    stream.send(_create_msg())
    _expect_server_fault(stream, "scenario-store-unreachable")
    stream.close()
