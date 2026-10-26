"""vLLM 策略后端与起停上下文:serve 是注入的假替身(不连 SSH、不起 vLLM),
HTTP 对着本机 http.server 桩测,不 mock urllib。"""
from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from compute_fixtures import fake_endpoint

from compute.errors import ComputeError
from compute.service import ServiceHandle
from compute.tunnel import http_probe
from compute.vllm_service import BASE_MODEL_NAME, VllmServeConfig, build_serve_job
from factory.opponents import ModelRefError, OpponentApiError, chat_messages
from factory.vllm_policy import VllmPolicy, policy_context, vllm_opponent_factory
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario, TurnRecord

KEY = "vllm-key-must-not-leak"
ADAPTERS = (("trained", "/srv/work/exports/t1"), ("opp", "/srv/work/exports/o1"))


def _persona():
    return Persona(id="p", traits=["急躁"], motivation="想让对方道歉", speech_style="短句", values=["公平"])


def _scenario():
    return Scenario("s1", "合租屋里水电费分摊", [RoleSlot("a", "室友甲"), RoleSlot("b", "室友乙")],
                    "谁该多付", ["不人身攻击"], "training")


HISTORY = [TurnRecord(0, "b", "你为什么不付?"), TurnRecord(1, "a", "我付过了"), TurnRecord(2, "b", "证据呢")]


# ---------------- 本机 http.server 桩 ----------------

class _Stub:
    """记下收到的请求;按 script 里下一项 (status, body[, headers[, delay_s]]) 回应。"""

    def __init__(self):
        self.requests = []
        self.script = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                stub.requests.append({
                    "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                })
                item = stub.script.pop(0) if stub.script else (500, b"empty script")
                status, body = item[0], item[1]
                if len(item) > 3:
                    time.sleep(item[3])
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # 客户端已超时离开

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self):
        return [json.loads(r["body"]) for r in self.requests]


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # 本机若配了代理,别让桩的请求绕出去
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    s = _Stub()
    yield s
    s.close()


def _ok(content="我不同意"):
    return 200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()


# ---------------- 假 serve ----------------

class FakeServe:
    """照 compute.service.serve 的签名:返回一个 @contextmanager 产生的上下文,产出指向桩的 ServiceHandle。
    进出都记进 trace;enter_raises 非空时进入就抛(模拟租约被占或服务起不来)。"""

    def __init__(self, base_url, *, enter_raises=None):
        self.base_url = base_url
        self.enter_raises = enter_raises
        self.trace = []
        self.calls = []

    def __call__(self, client, cfg, **kw):
        self.calls.append((client, cfg, kw))
        return self._context(cfg)

    @contextmanager
    def _context(self, cfg):
        self.trace.append("enter")
        if self.enter_raises is not None:
            raise self.enter_raises
        try:
            yield ServiceHandle("serve-job-a1", self.base_url, KEY,
                                (BASE_MODEL_NAME, *(name for name, _ in cfg.adapters)))
        finally:
            self.trace.append("exit")


def _client(alias="gpu-1"):
    return SimpleNamespace(alias=alias)


def _context(serve, *, adapters=ADAPTERS, trained_adapter="trained", client=None, **kw):
    return policy_context(
        client if client is not None else _client(), fake_endpoint(), adapters,
        serve_port=18081, trained_adapter=trained_adapter, serve=serve, **kw)


def _respond(backend, speaker="b"):
    return backend.respond(_persona(), _scenario(), HISTORY, speaker)


# ---------------- 训练策略 ----------------

def test_the_trained_policy_posts_the_trained_adapter_as_the_model(stub):
    stub.script.append(_ok("好吧"))
    serve = FakeServe(stub.url)
    with _context(serve) as policy:
        assert isinstance(policy, VllmPolicy)
        assert _respond(policy) == ("好吧", None)
    (request,) = stub.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {KEY}"      # 密钥来自 handle,不是别处
    body = stub.bodies()[0]
    assert body["model"] == "trained"
    assert body["messages"] == chat_messages(_persona(), _scenario(), HISTORY, "b")


def test_without_a_trained_adapter_the_policy_posts_the_base_model_name(stub):
    stub.script.append(_ok())
    with _context(FakeServe(stub.url), adapters=(), trained_adapter=None) as policy:
        _respond(policy)
    assert stub.bodies()[0]["model"] == "base"


def test_the_base_policy_can_run_next_to_an_opponent_adapter(stub):
    stub.script.append(_ok())
    with _context(FakeServe(stub.url), adapters=(("opp", "/srv/work/exports/o1"),), trained_adapter=None) as policy:
        _respond(policy)
    assert stub.bodies()[0]["model"] == "base"


def test_the_policy_speaks_in_chat_roles_with_its_own_earlier_turns_as_assistant(stub):
    stub.script.append(_ok())
    with _context(FakeServe(stub.url)) as policy:
        policy.respond(_persona(), _scenario(), HISTORY, "a")
    roles = [m["role"] for m in stub.bodies()[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_the_serve_config_is_built_from_the_endpoint_the_adapters_and_the_port():
    serve = FakeServe("http://127.0.0.1:1")
    with _context(serve):
        pass
    ((client, cfg, kw),) = serve.calls
    assert isinstance(cfg, VllmServeConfig)
    assert cfg.model_path == fake_endpoint().base_model_path
    assert cfg.adapters == ADAPTERS and cfg.remote_port == 18081
    assert kw["build_job"] is build_serve_job and kw["probe"] is http_probe


def test_the_default_tunnel_goes_through_the_clients_ssh_alias_and_the_serve_port(monkeypatch):
    opened = []

    class RecordingTunnel:
        def __init__(self, alias, *, remote_port):
            opened.append((alias, remote_port))

    monkeypatch.setattr("compute.tunnel.SshTunnel", RecordingTunnel)   # 不起真的 ssh(工厂在 compute.tunnel 里造隧道)
    serve = FakeServe("http://127.0.0.1:1")
    with _context(serve, client=_client("gpu-7")):
        pass
    (_, _, kw), = serve.calls
    assert opened == []                         # 工厂此刻还没被调用
    kw["tunnel_factory"]()
    assert opened == [("gpu-7", 18081)]


def test_an_injected_tunnel_factory_and_probe_are_passed_through():
    serve = FakeServe("http://127.0.0.1:1")
    tunnel, probe = (lambda: None), (lambda url, key: True)
    with _context(serve, tunnel_factory=tunnel, probe=probe, build_job=lambda cfg: cfg):
        pass
    (_, _, kw), = serve.calls
    assert kw["tunnel_factory"] is tunnel and kw["probe"] is probe


def test_a_client_without_an_ssh_alias_cannot_open_the_default_tunnel():
    serve = FakeServe("http://127.0.0.1:1")
    with pytest.raises(ComputeError, match="alias"):
        with _context(serve, client=SimpleNamespace()):
            pass
    assert serve.trace == []                   # 没进 serve:不占租约


@pytest.mark.parametrize("trained", ["nope", "base", ""])
def test_a_trained_adapter_that_is_not_among_the_adapters_is_refused_before_serving(trained):
    serve = FakeServe("http://127.0.0.1:1")
    with pytest.raises(ComputeError, match="trained_adapter"):
        with _context(serve, trained_adapter=trained):
            pass
    assert serve.trace == [] and serve.calls == []


def test_a_bad_adapter_list_is_a_compute_error_before_serving():
    serve = FakeServe("http://127.0.0.1:1")
    too_many = (("a", "/p"), ("b", "/q"), ("c", "/r"))
    with pytest.raises(ComputeError):
        with _context(serve, adapters=too_many, trained_adapter="a"):
            pass
    assert serve.trace == []


def test_the_service_is_stopped_when_the_body_raises():
    serve = FakeServe("http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="boom"):
        with _context(serve):
            raise RuntimeError("boom")
    assert serve.trace == ["enter", "exit"]


def test_the_service_is_stopped_when_the_body_finishes():
    serve = FakeServe("http://127.0.0.1:1")
    with _context(serve):
        assert serve.trace == ["enter"]
    assert serve.trace == ["enter", "exit"]


def test_an_error_on_entering_the_service_propagates_unchanged():
    from controlplane.deferred import DeferredError

    class Busy(DeferredError):
        pass

    serve = FakeServe("http://127.0.0.1:1", enter_raises=Busy("gpu lease is held"))
    with pytest.raises(Busy, match="gpu lease"):
        with _context(serve):
            pytest.fail("the body must not run when the service did not come up")
    assert serve.trace == ["enter"]


# ---------------- 失败处理:与外部 API 同一出口 ----------------

@pytest.mark.parametrize("script", [
    (401, json.dumps({"error": f"bad key {KEY}"}).encode()),
    (200, KEY.encode()),
    (200, b'{"choices": []}'),
    (500, b"x"),
])
def test_a_failed_request_is_an_api_error_that_never_carries_the_key(stub, script):
    stub.script.append(script)
    with _context(FakeServe(stub.url)) as policy:
        with pytest.raises(OpponentApiError) as info:
            _respond(policy)
    rendered = "".join(traceback.format_exception(info.value))
    assert KEY not in str(info.value) and KEY not in repr(info.value) and KEY not in rendered


def test_a_slow_service_times_out_as_an_api_error(stub):
    stub.script.append((*_ok(), {}, 1.0))
    with _context(FakeServe(stub.url), timeout_s=0.2) as policy:
        started = time.monotonic()
        with pytest.raises(OpponentApiError, match="timed out"):
            _respond(policy)
    assert time.monotonic() - started < 0.9


def test_a_policy_for_a_model_the_service_does_not_serve_is_refused():
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "trained"))
    with pytest.raises(ComputeError, match="other"):
        VllmPolicy(handle, "other", timeout_s=5.0)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
def test_a_policy_needs_a_finite_positive_timeout(timeout):
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "trained"))
    with pytest.raises(ValueError, match="timeout_s"):
        VllmPolicy(handle, "trained", timeout_s=timeout)


# ---------------- 对手工厂 ----------------

def _binding(model_ref):
    return RoleBinding("b", "pb", "frozen_reference", model_ref=model_ref)


def test_the_policy_exposes_the_service_handle_so_the_opponent_factory_can_share_the_service():
    serve = FakeServe("http://127.0.0.1:1")
    with _context(serve) as policy:
        assert policy.handle.model_names == ("base", "trained", "opp")
        assert policy.handle.api_key == KEY


def test_the_opponent_factory_posts_the_adapter_bound_to_the_model_ref(stub):
    stub.script.extend([_ok("甲"), _ok("乙"), _ok("丙")])
    with _context(FakeServe(stub.url)) as policy:
        make = vllm_opponent_factory(policy.handle, {"zengen-v1": "opp", "zengen-v2": "trained"}, timeout_s=5.0)
        assert _respond(make(_binding("zengen-v1"), _scenario())) == ("甲", None)
        assert _respond(make(_binding("zengen-v2"), _scenario())) == ("乙", None)
        assert _respond(policy) == ("丙", None)               # 训练策略与对手共用一个服务,各用各的适配器
    assert [b["model"] for b in stub.bodies()] == ["opp", "trained", "trained"]
    assert {r["headers"]["authorization"] for r in stub.requests} == {f"Bearer {KEY}"}


def test_the_opponent_backend_sees_the_conversation_from_its_own_side(stub):
    stub.script.append(_ok())
    with _context(FakeServe(stub.url)) as policy:
        make = vllm_opponent_factory(policy.handle, {"zengen-v1": "opp"}, timeout_s=5.0)
        make(_binding("zengen-v1"), _scenario()).respond(_persona(), _scenario(), HISTORY, "a")
    assert stub.bodies()[0]["messages"] == chat_messages(_persona(), _scenario(), HISTORY, "a")


def test_an_opponent_request_that_fails_never_carries_the_key(stub):
    stub.script.append((401, json.dumps({"error": KEY}).encode()))
    handle = ServiceHandle("j", stub.url, KEY, ("base", "opp"))
    backend = vllm_opponent_factory(handle, {"zengen-v1": "opp"}, timeout_s=5.0)(_binding("zengen-v1"), _scenario())
    with pytest.raises(OpponentApiError) as info:
        _respond(backend)
    rendered = "".join(traceback.format_exception(info.value))
    assert KEY not in str(info.value) and KEY not in rendered


def test_each_call_of_the_opponent_factory_gives_a_fresh_backend():
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "opp"))
    make = vllm_opponent_factory(handle, {"zengen-v1": "opp"}, timeout_s=5.0)
    assert make(_binding("zengen-v1"), _scenario()) is not make(_binding("zengen-v1"), _scenario())


@pytest.mark.parametrize("model_ref", ["zengen-v9", "", None, "base", ["zengen-v1"]])
def test_an_unknown_model_ref_is_a_model_ref_error(model_ref):
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "opp"))
    make = vllm_opponent_factory(handle, {"zengen-v1": "opp"}, timeout_s=5.0)
    with pytest.raises(ModelRefError, match="model_ref"):
        make(_binding(model_ref), _scenario())


def test_the_opponent_factory_does_not_follow_later_changes_to_the_callers_mapping():
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "opp"))
    mapping = {"zengen-v1": "opp"}
    make = vllm_opponent_factory(handle, mapping, timeout_s=5.0)
    mapping["zengen-v2"] = "opp"
    with pytest.raises(ModelRefError):
        make(_binding("zengen-v2"), _scenario())


def test_an_adapter_the_service_does_not_serve_is_refused_when_the_factory_is_built():
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "opp"))
    with pytest.raises(ComputeError, match="missing"):
        vllm_opponent_factory(handle, {"zengen-v1": "missing"}, timeout_s=5.0)


def test_the_base_model_is_not_an_opponent_adapter():
    handle = ServiceHandle("j", "http://127.0.0.1:1", KEY, ("base", "opp"))
    with pytest.raises(ComputeError, match="base"):
        vllm_opponent_factory(handle, {"zengen-v1": "base"}, timeout_s=5.0)
