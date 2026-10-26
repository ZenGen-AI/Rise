"""对手位的三种驱动:驱动选择、model_ref 解析、外部 API 客户端。
HTTP 客户端对着本机 http.server 桩测,不 mock urllib。"""
import json
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import model_file
from factory.opponents import (
    ExternalApiBackend,
    ModelRefError,
    OpponentApiError,
    OpponentCredentialError,
    OpponentTimeoutError,
    chat_messages,
    driver_for,
    post_chat,
    resolve_model_ref,
)
from factory.sombench_runner import EXPORT_FORMAT
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.realtime.backend_timeout import RoleBackendTimeoutError

SECRET = "sk-opponent-must-not-leak"


def _persona():
    return Persona(id="p", traits=["急躁", "爱面子"], motivation="想让对方道歉",
                   speech_style="短句,带情绪", values=["公平"])


def _scenario():
    return Scenario("s1", "合租屋里水电费分摊", [RoleSlot("a", "室友甲"), RoleSlot("b", "室友乙")],
                    "谁该多付", ["不人身攻击"], "training")


# ---------------- driver_for ----------------

@pytest.mark.parametrize("binding, expected", [
    (RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"), "vllm"),
    (RoleBinding("b", "p", "external_api", model_ref="gpt-x"), "external_api"),
    (RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted"), "scripted"),
    (RoleBinding("b", "p", "external_api", model_ref="gpt-x", driver="scripted"), "scripted"),
    (RoleBinding("b", "p", "external_api", model_ref="zengen-v1", driver="vllm"), "vllm"),
])
def test_driver_defaults_follow_model_backend(binding, expected):
    assert driver_for(binding) == expected


def test_the_trained_slot_never_gets_an_opponent_driver():
    with pytest.raises(ValueError, match="self_play"):
        driver_for(RoleBinding("a", "p", "self_play"))


def test_an_unknown_explicit_driver_is_a_value_error():
    with pytest.raises(ValueError, match="magic"):
        driver_for(RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="magic"))


def test_an_unknown_model_backend_without_a_driver_is_a_value_error():
    with pytest.raises(ValueError, match="magic"):
        driver_for(RoleBinding("b", "p", "magic", model_ref="x"))


# ---------------- resolve_model_ref ----------------

ADAPTER = {"type": "roll_ssh", "format": EXPORT_FORMAT}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _seed(store, n, checkpoint_ref="absent"):
    payload = {"promoted_from": f"cand-run-{n}"}
    if checkpoint_ref != "absent":
        payload["checkpoint_ref"] = checkpoint_ref
    env = Envelope(id=f"zengen-v{n}", type="zengen_model", version=1, created_at="t",
                   created_by=OwnerRef(), status="effective", schema_version=1, payload=payload)
    store.write_product(model_file(n), env, f"seed zengen-v{n}")


def _ref(**kw):
    base = dict(uri="ssh://gpu-1/ckpt/zengen-v1", sha256="a" * 64, size_bytes=10, fetch_hint=dict(ADAPTER))
    base.update(kw)
    return BigRef(**base)


def test_resolve_returns_the_checkpoint_ref_of_an_adapter_export(store):
    _seed(store, 1, _ref().to_dict())
    assert resolve_model_ref(store, "zengen-v1") == _ref()


def test_resolve_rejects_a_missing_model(store):
    with pytest.raises(ModelRefError, match="zengen-v99"):
        resolve_model_ref(store, "zengen-v99")


@pytest.mark.parametrize("model_ref", ["gpt-x", "zengen-v", "zengen-vX", "zengen-v1.yaml", "../zengen-v1", "",
                                       "zengen-v1\n", "zengen-v01"])  # 末尾换行、前导零:正则会放行,但不是规范 id
def test_resolve_rejects_an_id_that_is_not_zengen_vn(store, model_ref):
    with pytest.raises(ModelRefError, match="zengen-vN"):
        resolve_model_ref(store, model_ref)


def test_resolve_rejects_a_model_without_checkpoint_ref(store):
    _seed(store, 1)
    with pytest.raises(ModelRefError, match="zengen-v1.*checkpoint_ref"):
        resolve_model_ref(store, "zengen-v1")


@pytest.mark.parametrize("raw", [
    "not a dict",
    {"uri": "ssh://gpu-1/x", "sha256": "xyz", "size_bytes": 1, "fetch_hint": dict(ADAPTER)},  # 坏哈希
    {"sha256": "a" * 64, "size_bytes": 1},                                                    # 缺 uri
])
def test_resolve_wraps_a_malformed_checkpoint_ref(store, raw):
    _seed(store, 1, raw)
    with pytest.raises(ModelRefError, match="zengen-v1.*checkpoint_ref"):
        resolve_model_ref(store, "zengen-v1")


@pytest.mark.parametrize("hint", [
    None,
    {},
    {"type": "http", "format": EXPORT_FORMAT},
    {"type": "roll_ssh", "format": "something_else"},
    {"type": "roll_ssh"},
    {"format": EXPORT_FORMAT},
])
def test_resolve_rejects_a_checkpoint_that_is_not_an_adapter_export(store, hint):
    _seed(store, 1, _ref(fetch_hint=hint).to_dict())
    with pytest.raises(ModelRefError, match="zengen-v1.*adapter export"):
        resolve_model_ref(store, "zengen-v1")


def test_resolve_wraps_an_unreadable_model_file(store):
    path = store.repo_root / model_file(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<<<<<<< HEAD\n: : :\n", encoding="utf-8")
    with pytest.raises(ModelRefError, match="zengen-v1"):
        resolve_model_ref(store, "zengen-v1")


# ---------------- chat_messages ----------------

def test_chat_messages_labels_the_speakers_own_turns_assistant_and_everyone_elses_user():
    """vLLM 的聊天模板把说话者自己的历史发言当 assistant 轮;别人的话是 user,带角色位前缀。"""
    history = [
        TurnRecord(0, "a", "你为什么不付?"),
        TurnRecord(1, "b", "我付过了", inner_thought="其实没付", activation_trace={"skill": ["x"]}),
        TurnRecord(2, "a", "证据呢"),
    ]
    messages = chat_messages(_persona(), _scenario(), history, "b")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    system = messages[0]["content"]
    for needle in ("b", "室友乙", "合租屋里水电费分摊", "谁该多付", "不人身攻击",
                   "急躁", "想让对方道歉", "短句,带情绪", "公平"):
        assert needle in system, needle
    assert messages[1]["content"] == "a:你为什么不付?"
    assert messages[2]["content"] == "我付过了"        # 自己的发言不带前缀,免得模型学着加
    assert messages[3]["content"] == "a:证据呢"


def test_chat_messages_gives_every_other_speaker_the_user_role_with_its_own_prefix():
    history = [TurnRecord(0, "a", "甲说"), TurnRecord(1, "c", "丙说"), TurnRecord(2, "b", "乙说")]
    messages = chat_messages(_persona(), _scenario(), history, "b")
    assert [(m["role"], m["content"]) for m in messages[1:]] == [
        ("user", "a:甲说"), ("user", "c:丙说"), ("assistant", "乙说")]


def test_chat_messages_opens_with_a_user_message_when_the_speaker_itself_spoke_first():
    """自己的发言是 assistant 轮;它前面没有 user 轮时补开场,与历史为空时补开场同理。"""
    history = [TurnRecord(0, "b", "我先说"), TurnRecord(1, "a", "好")]
    messages = chat_messages(_persona(), _scenario(), history, "b")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == "我先说" and messages[3]["content"] == "a:好"


def test_chat_messages_never_shows_the_other_sides_inner_thought_or_trace():
    history = [
        TurnRecord(0, "a", "我付过了", inner_thought="SECRET-THOUGHT", activation_trace={"k": "SECRET-TRACE"}),
        TurnRecord(1, "b", "是吗", inner_thought="MY-THOUGHT", activation_trace={"k": "MY-TRACE"}),
    ]
    text = json.dumps(chat_messages(_persona(), _scenario(), history, "b"), ensure_ascii=False)
    for secret in ("SECRET-THOUGHT", "SECRET-TRACE", "MY-THOUGHT", "MY-TRACE"):
        assert secret not in text


def test_chat_messages_gives_an_opening_user_message_when_the_opponent_speaks_first():
    messages = chat_messages(_persona(), _scenario(), [], "b")
    assert [m["role"] for m in messages] == ["system", "user"]


def test_chat_messages_skips_empty_scenario_and_persona_fields():
    scenario = Scenario("s1", "", [RoleSlot("b", "")], "", [], "training")
    persona = Persona("p", [], "", "", [])
    messages = chat_messages(persona, scenario, [], "b")
    assert "None" not in messages[0]["content"] and "背景" not in messages[0]["content"]


# ---------------- ExternalApiBackend:本机 http.server 桩 ----------------

class _Stub:
    """记下收到的请求;按 script 里下一项回应。项:(status, body_bytes[, headers[, delay_s]])。"""

    def __init__(self):
        self.requests = []
        self.script = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                stub.requests.append({
                    "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length), "method": "POST",
                })
                self._reply()

            def do_GET(self):  # noqa: N802
                stub.requests.append({"path": self.path, "headers": {}, "body": b"", "method": "GET"})
                self._reply()

            def _reply(self):
                item = stub.script.pop(0) if stub.script else (500, b"empty script")
                status, body = item[0], item[1]
                headers = item[2] if len(item) > 2 else {}
                if len(item) > 3:
                    time.sleep(item[3])
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
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
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # 本机环境若配了代理,别让桩的请求绕出去
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    s = _Stub()
    yield s
    s.close()


def _ok(content="我不同意"):
    return 200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()


def _backend(url, **kw):
    args = dict(api_key=SECRET, api_key_env="FORGE_OPP_KEY", timeout_s=5.0)
    args.update(kw)
    return ExternalApiBackend(url, "gpt-x", **args)


def _respond(backend):
    return backend.respond(_persona(), _scenario(), [TurnRecord(0, "a", "你好")], "b")


def test_a_missing_key_is_reported_by_variable_name_only_at_construction():
    with pytest.raises(OpponentCredentialError) as info:
        ExternalApiBackend("http://x", "m", api_key="", api_key_env="FORGE_OPP_KEY", timeout_s=1.0)
    assert "FORGE_OPP_KEY" in str(info.value) and "sk-" not in str(info.value)


@pytest.mark.parametrize("key", ["", "   ", "sk-abc\n", "sk-abc\r\nX-Evil: 1", "sk-密钥"])
def test_a_blank_or_header_unsafe_key_is_a_credential_error_that_never_echoes_it(key):
    # 带换行或非 ASCII 的值放进请求头会让 http.client 抛出含整个头值的 ValueError,所以在构造时就拦下
    with pytest.raises(OpponentCredentialError) as info:
        ExternalApiBackend("http://x", "m", api_key=key, api_key_env="FORGE_OPP_KEY", timeout_s=1.0)
    text = str(info.value)
    assert "FORGE_OPP_KEY" in text
    assert not key.strip() or key.strip() not in text


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "x.example.com", ""])
def test_only_http_and_https_base_urls_are_accepted(url):
    with pytest.raises(ValueError, match="base_url"):
        _backend(url)


def test_success_returns_the_reply_and_no_inner_thought(stub):
    stub.script.append(_ok("我不同意"))
    assert _respond(_backend(stub.url)) == ("我不同意", None)


def test_the_request_is_a_chat_completions_post_with_a_bearer_header(stub):
    stub.script.append(_ok())
    _respond(_backend(stub.url + "/"))   # 末尾斜杠不产生双斜杠
    (request,) = stub.requests
    assert request["method"] == "POST" and request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {SECRET}"
    assert request["headers"]["content-type"].startswith("application/json")
    body = json.loads(request["body"])
    assert body["model"] == "gpt-x"
    assert body["messages"] == chat_messages(_persona(), _scenario(), [TurnRecord(0, "a", "你好")], "b")


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_a_non_200_status_is_an_api_error_naming_the_status(stub, status):
    stub.script.append((status, json.dumps({"error": f"bad key {SECRET}"}).encode()))
    with pytest.raises(OpponentApiError, match=str(status)) as info:
        _respond(_backend(stub.url))
    assert SECRET not in str(info.value)


def test_a_2xx_that_is_not_200_is_an_api_error(stub):
    stub.script.append((204, b""))
    with pytest.raises(OpponentApiError, match="204"):
        _respond(_backend(stub.url))


def test_a_redirect_is_not_followed_so_the_key_cannot_travel_to_another_place(stub):
    stub.script.append((302, b"", {"Location": f"{stub.url}/elsewhere"}))
    stub.script.append(_ok())
    with pytest.raises(OpponentApiError, match="302") as info:
        _respond(_backend(stub.url))
    assert len(stub.requests) == 1 and SECRET not in str(info.value)


@pytest.mark.parametrize("body", [
    b"not json at all",
    b"",
    b"\xff\xfe\x00 not utf-8",
    b"[]",
    b'"text"',
    b"{}",
    b'{"choices": []}',
    b'{"choices": [{}]}',
    b'{"choices": [{"message": {}}]}',
    b'{"choices": [{"message": {"content": null}}]}',
    b'{"choices": [{"message": {"content": 7}}]}',
    b'{"choices": [{"message": {"content": "   "}}]}',
    b'{"choices": "x"}',
])
def test_a_malformed_reply_is_an_api_error(stub, body):
    stub.script.append((200, body))
    with pytest.raises(OpponentApiError) as info:
        _respond(_backend(stub.url))
    assert SECRET not in str(info.value)


def test_a_slow_server_times_out_as_an_api_error(stub):
    stub.script.append((*_ok(), {}, 1.0))
    started = time.monotonic()
    with pytest.raises(OpponentApiError, match="timed out") as info:
        _respond(_backend(stub.url, timeout_s=0.2))
    assert time.monotonic() - started < 0.9 and SECRET not in str(info.value)


def test_a_slow_server_raises_the_timeout_error_that_is_both_an_api_error_and_a_role_backend_timeout(stub):
    """服务端驱动按 RoleBackendTimeoutError 归 role_backend_timeout(T1,整条重跑);
    策略上下文按 OpponentApiError 把训练位的超时译成延后。一个类要同时是两者。"""
    stub.script.append((*_ok(), {}, 1.0))
    with pytest.raises(OpponentTimeoutError, match="timed out") as info:
        _respond(_backend(stub.url, timeout_s=0.2))
    assert isinstance(info.value, OpponentApiError) and isinstance(info.value, RoleBackendTimeoutError)
    rendered = "".join(traceback.format_exception(info.value))
    assert SECRET not in str(info.value) and SECRET not in rendered
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_post_chat_timeout_scrubs_the_key_from_a_label_that_carries_it(stub):
    stub.script.append((*_ok(), {}, 1.0))
    with pytest.raises(OpponentTimeoutError) as info:
        post_chat(stub.url, "m1", SECRET, _msgs(), timeout_s=0.2, host_label=f"host-{SECRET}")
    assert SECRET not in str(info.value) and "***" in str(info.value)


@pytest.mark.parametrize("script", [(500, b"x"), (429, b"slow down"), (200, b"not json")])
def test_a_non_200_or_a_malformed_reply_is_not_a_timeout(stub, script):
    """非 200 不归超时:它走 role_backend_failure,轨迹源不按条重跑(分不出 T1 与 T2)。"""
    stub.script.append(script)
    with pytest.raises(OpponentApiError) as info:
        _respond(_backend(stub.url))
    assert not isinstance(info.value, (OpponentTimeoutError, RoleBackendTimeoutError))


def test_a_refused_connection_is_an_api_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(OpponentApiError) as info:
        _respond(_backend(f"http://127.0.0.1:{port}"))
    assert SECRET not in str(info.value)


def test_the_key_appears_in_no_error_message_or_traceback(stub):
    cases = [(401, f"{SECRET}".encode()), (200, SECRET.encode()), (200, b'{"choices": []}')]
    for status, body in cases:
        stub.script.append((status, body))
        with pytest.raises(OpponentApiError) as info:
            _respond(_backend(stub.url))
        rendered = "".join(traceback.format_exception(info.value))
        assert SECRET not in str(info.value) and SECRET not in repr(info.value) and SECRET not in rendered


# ---------------- post_chat:ExternalApiBackend 与 vLLM 后端共用的一次 POST ----------------

def _msgs():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_post_chat_returns_the_reply_text_and_sends_model_messages_and_bearer(stub):
    stub.script.append(_ok("好的"))
    assert post_chat(stub.url + "/", "m1", SECRET, _msgs(), timeout_s=5.0, host_label="the stub") == "好的"
    (request,) = stub.requests
    assert request["method"] == "POST" and request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {SECRET}"
    assert json.loads(request["body"]) == {"model": "m1", "messages": _msgs()}


@pytest.mark.parametrize("script", [
    (500, b"x"), (302, b"", {"Location": "http://127.0.0.1:1/"}), (200, b"not json"), (200, b"{}"),
])
def test_post_chat_failures_are_api_errors_naming_the_label_and_never_the_key(stub, script):
    stub.script.append(script)
    with pytest.raises(OpponentApiError, match="the stub") as info:
        post_chat(stub.url, "m1", SECRET, _msgs(), timeout_s=5.0, host_label="the stub")
    rendered = "".join(traceback.format_exception(info.value))
    assert SECRET not in str(info.value) and SECRET not in rendered


def test_post_chat_scrubs_the_key_even_if_it_shows_up_in_a_failure_text(stub):
    """第二道防线:label 里若混进密钥(调用方的错),抛出前也被抹掉。"""
    stub.script.append((500, b"x"))
    with pytest.raises(OpponentApiError) as info:
        post_chat(stub.url, "m1", SECRET, _msgs(), timeout_s=5.0, host_label=f"host-{SECRET}")
    assert SECRET not in str(info.value) and "***" in str(info.value)
