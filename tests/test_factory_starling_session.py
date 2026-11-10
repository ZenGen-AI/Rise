"""服务会话(设计四 Task 10、12;90 篇运行时身份表、"服务会话 ServiceSession"一节、FC-42、FC-46、FC-47)。

会话的用例用假客户端与假传输,不起服务;最后两条用真 starling-serve(模块级夹具,数据目录与 HOME 在 tmp 下)。
"""
import hashlib
import json
import threading
from pathlib import Path

import pytest

errors = pytest.importorskip("starling_client.errors")
from factory.errors import FactoryConfigError
from factory.memory_config import MemoryConfig
from factory.starling_names import settings_digest
from factory.starling_service import ServiceRestartsExhausted
from factory.starling_session import (ADMIN_TIMEOUT_S, DRAIN_WINDOW_S, HANDLE_TIMEOUT_S, ServiceIdentity,
                                      ServiceSession, service_runtime)
from simple_actio.memory_port import MemoryFailureError
from simple_actio.trace import TRACE_SCHEMA_V2
from starling_service import start_service

RUNTIME_INFO = {"version": "0.0.1", "commit": "c" * 40, "protocol": 1, "extraction_config": {"b": 1, "a": [2]}}


@pytest.fixture
def memory_config():
    return MemoryConfig(root=Path("/m"), extraction_base_url="http://127.0.0.1:9/v1", extraction_model="x",
                        embedding_base_url="http://127.0.0.1:9/v1", embedding_model="e", embedding_dim=64)


def test_the_runtime_identity_keeps_five_top_level_keys_and_names_the_service(memory_config):
    rt = service_runtime(RUNTIME_INFO, {"x": 1}, memory_config)
    assert set(rt) == {"backend", "starling_version", "extraction", "embedding", "trace_schema"}
    assert rt["backend"] == "starling" and rt["trace_schema"] == TRACE_SCHEMA_V2
    assert rt["starling_version"] == f"0.0.1+git:{'c' * 40}+protocol:1"
    assert rt["extraction"]["world_settings"] and len(rt["extraction"]["prompts"]) == 64
    assert rt["extraction"]["world_settings"] == settings_digest({"x": 1})
    assert rt["extraction"] == {"base_url": "http://127.0.0.1:9/v1", "model": "x", "timeout_ms": 4000,
                                "max_retries": 0, "prompts": rt["extraction"]["prompts"],
                                "world_settings": settings_digest({"x": 1})}
    assert rt["embedding"] == {"base_url": "http://127.0.0.1:9/v1", "model": "e", "dim": 64,
                               "timeout_ms": 4000, "max_retries": 0}


def test_the_external_identity_takes_the_endpoint_leaves_rise_actually_wrote():
    """外部模式:身份里的两个端点是 rise 设置里写出的叶子(没有地址与密钥);换模型身份就变。"""
    from factory.starling_names import rise_settings
    cfg = MemoryConfig(root=Path("/m"), timeout_ms=None, max_retries=None, url="http://127.0.0.1:8787",
                       token_file=Path("/t"))
    endpoint = {"provider": "openai", "provider_name": "default", "model": "m1", "dim": 0, "max_tokens": 0,
                "timeout_ms": 120000, "thinking_type": ""}
    embed = {**endpoint, "provider_name": "embedder", "model": "e1", "dim": 1024, "timeout_ms": 0}
    info = {**RUNTIME_INFO, "default_settings": {"extraction": endpoint, "embedding": embed},
            "key_set": {"extraction": True, "embedding": True}}
    settings = rise_settings(cfg, info)
    rt = service_runtime(info, settings, cfg)
    assert rt["extraction"] == {**endpoint, "enable_thinking": False, "prompts": rt["extraction"]["prompts"],
                                "world_settings": settings_digest(settings)}
    assert rt["embedding"] == {**embed, "enable_thinking": False}
    assert "base_url" not in rt["extraction"] and "base_url" not in rt["embedding"]
    other = {**info, "default_settings": {"extraction": {**endpoint, "model": "m2"}, "embedding": embed}}
    assert service_runtime(other, rise_settings(cfg, other), cfg)["extraction"]["model"] == "m2" != rt["extraction"]["model"]


def test_the_identity_changes_with_the_extraction_config_and_the_settings(memory_config):
    a = service_runtime(RUNTIME_INFO, {"x": 1}, memory_config)
    assert a != service_runtime({**RUNTIME_INFO, "extraction_config": {"b": 2}}, {"x": 1}, memory_config)
    assert a != service_runtime(RUNTIME_INFO, {"x": 2}, memory_config)
    assert a == service_runtime({**RUNTIME_INFO, "extraction_config": {"a": [2], "b": 1}}, {"x": 1}, memory_config)


def test_the_runtime_identity_is_a_fresh_dict_each_call(memory_config):
    a = service_runtime(RUNTIME_INFO, {"x": 1}, memory_config)
    a["extraction"]["model"] = "changed"
    assert service_runtime(RUNTIME_INFO, {"x": 1}, memory_config)["extraction"]["model"] == "x"


# ---------------- ServiceSession(Task 12) ----------------

def test_the_timeouts_match_the_design():
    assert (HANDLE_TIMEOUT_S, ADMIN_TIMEOUT_S, DRAIN_WINDOW_S) == (30.0, 300.0, 60.0)


class FakeClient:
    def __init__(self, info, *, open_error=None, gate=None):
        self._info, self.open_error, self.gate, self.closed = info, open_error, gate, False
        self.entered = threading.Event()
        self.opened = []

    def info(self):
        return self._info

    def open(self, world, *, holder, read_only, settings):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.open_error is not None:
            raise self.open_error
        self.opened.append((world, holder, read_only, settings))
        return object()

    def close(self):
        self.closed = True


class FakeTransport:
    """记下每次 POST 的路径与"请求体里有没有 session_id"。"""

    def __init__(self, timeout_s, reply=None, error=None):
        self.timeout_s, self.reply, self.error = timeout_s, reply or {}, error
        self.posts, self.closed = [], False

    def post(self, path, json=None, *, timeout_s=None):
        self.posts.append((path, "session_id" in (json or {})))
        if self.error is not None:
            raise self.error
        return self.reply

    def close(self):
        self.closed = True


def _capture(fn):
    try:
        fn()
    except Exception as exc:        # noqa: BLE001 - 测试要拿到异常本身
        return exc
    return None


def _sha256_of_canonical(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _session(clients, transports=None, **kw):
    """`clients` 里的项是 FakeClient,或调用时返回 FakeClient、抛异常的函数(connect 返回 FakeClient 或抛)。"""
    made = []

    def connect(url, *, token, timeout_s):
        assert timeout_s == HANDLE_TIMEOUT_S
        c = clients.pop(0)
        c = c() if callable(c) else c
        made.append(c)
        return c

    def transport(url, token, timeout_s):
        t = transports.pop(0) if transports else FakeTransport(timeout_s)
        t.timeout_s = timeout_s
        return t

    s = ServiceSession(url_of=lambda: "http://127.0.0.1:1", token_of=lambda: "t", pinned_commit="c" * 40,
                       connect=connect, transport=transport, **kw)
    return s, made


INFO = {"commit": "c" * 40, "protocol": 1, "extraction_config": {}, "version": "0.0.1"}


def _open(s, world="rise.deploy.s1", *, read_only=False, wrap=lambda r, rel, g: None):
    return s.open_port(world, "pa", read_only=read_only, settings={}, wrap=wrap)


def test_construction_records_the_first_generation_and_its_identity():
    s, made = _session([FakeClient(INFO)])
    assert s.generation == 1 and s.info == INFO and s.fatal is None and s.owns_generation(1)
    seen = []
    _open(s, wrap=lambda remote, release, gen: seen.append(gen) or "port")
    assert seen == [1] and made[0].opened == [("rise.deploy.s1", "pa", False, {})]


def test_an_old_generation_client_is_closed_only_after_its_last_port_closes():
    s, made = _session([FakeClient(INFO), FakeClient(INFO)])
    releases = []
    s.open_port("rise.deploy.s1", "pa", read_only=False, settings={},
                wrap=lambda remote, release, gen: releases.append((release, gen)))
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    s.open_port("rise.deploy.s2", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    assert s.generation == 2 and made[0].closed is False                 # 旧代号还有一个端口开着
    assert not s.owns_generation(1) and s.owns_generation(2)
    releases[0][0]()
    assert made[0].closed is True and made[1].closed is False
    releases[0][0]()                                                     # 再调一次是空操作
    assert made[1].closed is False


def test_an_old_generation_with_no_open_port_is_closed_at_the_switch():
    s, made = _session([FakeClient(INFO), FakeClient(INFO)])
    s.mark_lost(1, writable_worlds=[])
    _open(s)
    assert s.generation == 2 and made[0].closed is True


def test_a_call_racing_a_reconnect_gets_session_expired_not_a_closed_transport():
    gate = threading.Event()
    first = FakeClient(INFO, gate=gate, open_error=errors.SessionExpired("session_expired"))
    s, made = _session([first, FakeClient(INFO)])
    seen = []
    t = threading.Thread(target=lambda: seen.append(_capture(lambda: s.open_port(
        "rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None))))
    t.start()
    assert first.entered.wait(5)                                          # 线程 A 已在第 1 代的调用里
    s.mark_lost(1, writable_worlds=[])                                    # 另一条线程发现过期
    s.open_port("rise.deploy.s2", "pb", read_only=False, settings={}, wrap=lambda r, rel, g: None)   # 换成第 2 代
    assert first.closed is False                                          # A 还在调用里,不关
    gate.set(); t.join(5)
    (exc,) = seen
    assert isinstance(exc, MemoryFailureError) and exc.transient and "session_expired" in str(exc)
    assert first.closed is True                                           # 线程 A 的调用退出后才关
    assert s.generation == 2 and made[1].closed is False                  # 旧代号的过期报告不影响第 2 代


def test_session_expired_on_open_marks_the_current_generation_lost():
    expired = errors.SessionExpired("session_expired")
    s, made = _session([FakeClient(INFO, open_error=expired), FakeClient(INFO)])
    with pytest.raises(MemoryFailureError) as info:
        _open(s)
    assert info.value.transient is True
    _open(s)
    assert s.generation == 2 and made[0].closed is True


def test_world_held_is_transient_only_within_sixty_seconds_of_losing_a_writer_there():
    now = [100.0]
    held = errors.WorldError("world_held", code="world_held", transient=False)
    s, _ = _session([FakeClient(INFO), FakeClient(INFO, open_error=held)], clock=lambda: now[0])
    s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    with pytest.raises(MemoryFailureError) as inside:
        s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    now[0] += 61
    with pytest.raises(MemoryFailureError) as outside:
        s.open_port("rise.deploy.s1", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    with pytest.raises(MemoryFailureError) as other_world:
        now[0] -= 61
        s.open_port("rise.deploy.s9", "pa", read_only=False, settings={}, wrap=lambda r, rel, g: None)
    assert inside.value.transient is True and outside.value.transient is False and other_world.value.transient is False


def test_the_drain_window_also_covers_writers_the_session_saw_but_the_report_left_out():
    """丢失时这一代在哪些世界上有可写句柄,会话自己也记着;适配器只报它自己的世界时,别的世界照样进排空窗口。"""
    s, _ = _session([FakeClient(INFO), FakeClient(INFO)], clock=lambda: 0.0)
    _open(s, "rise.deploy.s1")
    _open(s, "rise.deploy.s2")
    _open(s, "rise.deploy.s3", read_only=True)
    releases = []
    _open(s, "rise.deploy.s4", wrap=lambda r, rel, g: releases.append(rel))
    releases[0]()                                                         # s4 的端口丢失前已关
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    assert s.in_drain_window("rise.deploy.s1") and s.in_drain_window("rise.deploy.s2")
    assert not s.in_drain_window("rise.deploy.s3") and not s.in_drain_window("rise.deploy.s4")


def test_the_drain_window_outlives_the_lost_generations_ports_and_client():
    """适配器收到 SessionExpired 就标丢失并把端口当作已关;旧代号的客户端随即关掉,排空窗口仍要算满 60 秒。"""
    now = [0.0]
    held = errors.WorldError("world_held", code="world_held", transient=False)
    s, made = _session([FakeClient(INFO), FakeClient(INFO, open_error=held)], clock=lambda: now[0])
    releases = []
    _open(s, "rise.deploy.s1", wrap=lambda r, rel, g: releases.append(rel))
    s.mark_lost(1, writable_worlds=["rise.deploy.s1"])
    releases[0]()                                                         # 端口当作已关
    with pytest.raises(MemoryFailureError) as inside:
        _open(s, "rise.deploy.s1")                                        # 换成第 2 代,第 1 代的客户端关掉
    assert made[0].closed is True and s.in_drain_window("rise.deploy.s1")
    assert inside.value.transient is True
    now[0] = 59.9
    assert s.in_drain_window("rise.deploy.s1")
    now[0] = 60.0
    assert not s.in_drain_window("rise.deploy.s1")


def test_an_admin_call_in_flight_keeps_the_old_transport_open_across_a_switch():
    gate, entered = threading.Event(), threading.Event()

    class SlowTransport(FakeTransport):
        def post(self, path, json=None, *, timeout_s=None):
            entered.set(); gate.wait(5)
            if self.closed:
                raise RuntimeError("transport closed under a call")
            return super().post(path, json, timeout_s=timeout_s)

    first, second = SlowTransport(0), FakeTransport(0)
    s, made = _session([FakeClient(INFO), FakeClient(INFO)], transports=[first, second])
    seen = []
    t = threading.Thread(target=lambda: seen.append(_capture(lambda: s.admin("restore", {"world": "w"}))))
    t.start()
    assert entered.wait(5)
    s.mark_lost(1, writable_worlds=[])
    _open(s)                                                              # 换成第 2 代
    assert first.closed is False and made[0].closed is False             # 管理调用还在第 1 代的传输上
    gate.set(); t.join(5)
    assert seen == [None] and first.closed is True and made[0].closed is True
    assert s._admin_transport is second


def test_a_failing_wrap_gives_the_count_back():
    s, made = _session([FakeClient(INFO), FakeClient(INFO)])

    def boom(remote, release, gen):
        raise ValueError("bad port")

    with pytest.raises(ValueError):
        _open(s, wrap=boom)
    s.mark_lost(1, writable_worlds=[])
    _open(s)
    assert made[0].closed is True                                         # 计数已归零,换会话时就关


def test_a_report_for_an_old_generation_is_a_no_op():
    s, _ = _session([FakeClient(INFO), FakeClient(INFO)], clock=lambda: 0.0)
    s.mark_lost(1, writable_worlds=[])
    _open(s, "rise.deploy.s2")                                            # 换成第 2 代
    s.mark_lost(1, writable_worlds=["rise.deploy.s2"])
    assert not s.in_drain_window("rise.deploy.s2") and s.generation == 2
    _open(s, "rise.deploy.s2")                                            # 第 2 代没被标丢失,不换会话
    assert s.generation == 2


def test_a_transient_failure_to_reconnect_leaves_the_generation_lost_and_retries_next_time():
    s, made = _session([FakeClient(INFO), lambda: (_ for _ in ()).throw(errors.ServiceUnavailable("down")),
                        FakeClient(INFO)])
    s.mark_lost(1, writable_worlds=[])
    with pytest.raises(MemoryFailureError) as info:
        _open(s)
    assert info.value.transient is True and s.fatal is None and s.generation == 1
    _open(s)
    assert s.generation == 2


@pytest.mark.parametrize("second, why", [
    (lambda: FakeClient({**INFO, "commit": "d" * 40}), "commit"),
    (lambda: FakeClient({**INFO, "protocol": 2}), "protocol"),
    (lambda: FakeClient({**INFO, "extraction_config": {"changed": True}}), "extraction_config"),
    (lambda: (_ for _ in ()).throw(errors.Unauthorized("unauthorized")), "unauthorized"),
    (lambda: (_ for _ in ()).throw(errors.ProtocolMismatch("protocol_mismatch")), "protocol_mismatch"),
])
def test_a_reconnect_to_a_different_service_turns_fatal_and_every_later_call_defers(second, why):
    s, made = _session([FakeClient(INFO), second])                       # connect 返回 FakeClient 或抛
    s.open_port("rise.deploy.s1", "pa", read_only=True, settings={}, wrap=lambda r, rel, g: None)
    s.mark_lost(1, writable_worlds=[])
    for _ in range(2):
        with pytest.raises(MemoryFailureError) as info:
            s.open_port("rise.deploy.s1", "pa", read_only=True, settings={}, wrap=lambda r, rel, g: None)
        assert info.value.transient is True and "stopping" in str(info.value)
    with pytest.raises(MemoryFailureError) as admin:
        s.admin("digest", {"world": "rise.deploy.s1"})
    assert admin.value.transient is True
    assert why in s.fatal and "t" * 3 not in s.fatal and "127.0.0.1" not in s.fatal
    if len(made) == 2:
        assert made[1].closed is True                                     # 认错的新客户端已关


def test_running_out_of_restarts_turns_fatal():
    def restart():
        raise ServiceRestartsExhausted("restarted 3 times; see service.log")

    s, made = _session([FakeClient(INFO)], restart=restart)
    s.mark_lost(1, writable_worlds=[])
    with pytest.raises(MemoryFailureError) as info:
        _open(s)
    assert info.value.transient is True and "service.log" in s.fatal and len(made) == 1


def test_a_restart_runs_before_the_new_connect():
    order = []
    s, _ = _session([FakeClient(INFO), lambda: order.append("connect") or FakeClient(INFO)],
                    restart=lambda: order.append("restart"))
    s.mark_lost(1, writable_worlds=[])
    _open(s)
    assert order == ["restart", "connect"]


def test_an_unreachable_service_marks_the_generation_lost_so_the_next_call_reconnects():
    """服务进程不在了(ServiceUnavailable):这一代标为丢失,下一次需要服务时(拉起模式下先 restart)换会话。"""
    down = FakeTransport(0, error=errors.ServiceUnavailable("service unreachable"))
    order = []
    s, made = _session([FakeClient(INFO), FakeClient(INFO)], transports=[down, FakeTransport(0)],
                       restart=lambda: order.append("restart"))
    with pytest.raises(MemoryFailureError) as info:
        s.admin("digest", {"world": "w"})
    assert info.value.transient is True and s.generation == 1
    s.admin("digest", {"world": "w"})
    assert s.generation == 2 and order == ["restart"]


def test_admin_calls_go_through_a_sessionless_transport_with_the_long_timeout():
    s, _ = _session([FakeClient(INFO)])
    s.admin("list_worlds", {"labels": None, "page": 1, "page_size": 50, "with_digest": False})
    t = s._admin_transport
    assert t.timeout_s == ADMIN_TIMEOUT_S and t.posts == [("/api/v1/client/admin/list_worlds", False)]
    assert s.identity == ServiceIdentity("c" * 40, 1, _sha256_of_canonical({}))


def test_admin_failures_are_translated_without_the_drain_window():
    held = errors.WorldError("world_held", code="world_held", transient=False)
    s, _ = _session([FakeClient(INFO)], transports=[FakeTransport(0, error=held)], clock=lambda: 0.0)
    _open(s)
    with pytest.raises(MemoryFailureError) as info:
        s.admin("restore", {"world": "rise.deploy.s1", "snapshot_id": "x"})
    assert info.value.transient is False and str(info.value) == "starling world_held (WorldError)"


def test_the_admin_transport_is_rebuilt_with_the_session_and_the_old_one_closed_after():
    first, second = FakeTransport(0), FakeTransport(0)
    s, made = _session([FakeClient(INFO), FakeClient(INFO)], transports=[first, second])
    s.admin("digest", {"world": "w"})
    s.mark_lost(1, writable_worlds=[])
    s.admin("digest", {"world": "w"})
    assert s._admin_transport is second and first.closed is True and made[0].closed is True
    assert first.posts == second.posts == [("/api/v1/client/admin/digest", False)]
    s.close()
    assert second.closed is True and made[1].closed is True


def test_close_closes_every_client_still_open_and_later_calls_fail():
    s, made = _session([FakeClient(INFO), FakeClient(INFO)])
    _open(s)                                                              # 第 1 代留一个端口
    s.mark_lost(1, writable_worlds=[])
    _open(s)
    s.close()
    assert made[0].closed is True and made[1].closed is True
    with pytest.raises(MemoryFailureError) as info:
        _open(s)
    assert info.value.transient is False


def test_a_service_on_another_commit_is_refused_at_construction_naming_both_commits():
    with pytest.raises(FactoryConfigError, match=f"{'d' * 40}.*{'c' * 40}|{'c' * 40}.*{'d' * 40}"):
        _session([FakeClient({**INFO, "commit": "d" * 40})])


@pytest.mark.parametrize("first, needle", [
    (lambda: (_ for _ in ()).throw(errors.Unauthorized("unauthorized")), "unauthorized"),
    (lambda: (_ for _ in ()).throw(errors.ProtocolMismatch("protocol_mismatch")), "protocol_mismatch"),
    (lambda: (_ for _ in ()).throw(errors.ServiceUnavailable("service unreachable")), "service_unavailable"),
    (lambda: FakeClient({**INFO, "protocol": 2}), "protocol"),
])
def test_a_bad_first_connection_is_a_config_error_naming_only_the_code(first, needle):
    with pytest.raises(FactoryConfigError) as info:
        _session([first])
    assert needle in str(info.value) and "127.0.0.1" not in str(info.value) and "ttt" not in str(info.value)


def test_the_private_transport_still_has_the_signature_the_admin_path_relies_on():
    import inspect
    from starling_client._transport import Transport
    assert str(inspect.signature(Transport.__init__)) == \
        "(self, url: 'str', token: 'str', timeout_s: 'float' = 30.0, *, _httpx_transport=None)"
    assert str(inspect.signature(Transport.post)) == \
        "(self, path: 'str', json: 'dict | None' = None, *, timeout_s: 'float | None' = None) -> 'dict'"


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


def test_admin_calls_still_work_after_the_session_expired_against_a_real_service(service):
    import starling_client
    s = ServiceSession(url_of=lambda: service.url, token_of=lambda: service.token,
                       pinned_commit=starling_client.COMMIT)
    try:
        s.admin("create_world", {"name": "rise.deploy.t1", "labels": {"repo": "rise"}, "settings": None, "seed": False})
        s.mark_lost(1, writable_worlds=[])
        assert s.admin("digest", {"world": "rise.deploy.t1"})["digest"]
        assert s.generation == 2 and s.fatal is None
    finally:
        s.close()
    assert service.token not in service.stderr()


def test_a_real_port_opened_through_the_session_counts_against_its_generation(service):
    import starling_client
    s = ServiceSession(url_of=lambda: service.url, token_of=lambda: service.token,
                       pinned_commit=starling_client.COMMIT)
    try:
        s.admin("create_world", {"name": "rise.deploy.t2", "labels": {"repo": "rise"}, "settings": None, "seed": False})
        ports = []
        for holder in ("pa", "pb"):                                       # 同一会话、同一世界的两个主体(训练位与对手位)
            s.open_port("rise.deploy.t2", holder, read_only=False, settings=None,
                        wrap=lambda remote, release, gen: ports.append((remote, release, gen)))
        assert [g for _, _, g in ports] == [1, 1]
        assert all(isinstance(r, starling_client.RemoteMemory) for r, _, _ in ports)
        with pytest.raises(MemoryFailureError) as missing:
            s.open_port("rise.deploy.nope", "pa", read_only=False, settings=None, wrap=lambda r, rel, g: None)
        assert str(missing.value).startswith("starling world_not_found (") and missing.value.transient is False
        for remote, release, _ in ports:
            remote.close(); release()
    finally:
        s.close()
