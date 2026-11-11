"""StarlingMemory 的专属用例:包客户端 `RemoteMemory` 的端口(40 篇第 8 节码表、4.4 节渲染、close 的期限与
"当作已关")。契约用例在共享契约里;真服务上的用例在管理面与训练世界的测试里。"""
from types import SimpleNamespace

import pytest

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (MemoryBackend, MemoryClosedError, MemoryEvent, MemoryEventConflict,
                               MemoryFailureError, MemoryHit, RememberOutcome)

errors = pytest.importorskip("starling_client.errors")
from starling_client.types import EmbedStats  # noqa: E402
from simple_actio.memory_starling import EPOCH, StarlingMemory  # noqa: E402

EV = MemoryEvent("ep-1#0", "pb：我担心预算超支", "pb")


class FakeRemote:
    """只有 StarlingMemory 用到的 RemoteMemory 方法(签名照 S:clients/python/starling_client/remote_memory.py:73-112);
    raise_on[name] 给定时该方法抛它。"""

    def __init__(self, raise_on=None, **counts):
        self.raise_on = dict(raise_on or {})
        self.counts = counts
        self.world = "rise.deploy.s1"
        self.close_calls = []
        self.hits = []              # recall 返回它
        self.result = None          # remember_event 返回它
        self.ids = {}               # content_ids 的映射
        self.seq = 0
        self.remembered = []
        self.embed_nows = []
        self.recall_kwargs = None

    def _maybe(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def recall(self, query, **kw):
        self._maybe("recall"); self.recall_kwargs = kw; return list(self.hits)

    def remember_event(self, key, text, *, interlocutor, epoch):
        self._maybe("remember_event")
        self.remembered.append((key, text, interlocutor, epoch))
        return self.result

    def content_ids(self, statement_ids):
        self._maybe("content_ids"); return {s: self.ids[s] for s in statement_ids}

    def embed_pending(self, now):
        self._maybe("embed_pending")
        self.embed_nows.append(now)
        return EmbedStats(embedded=self.counts.get("embedded", 0), failed=self.counts.get("failed", 0))

    def next_seq(self):
        return self.seq

    def digest(self):
        self._maybe("digest"); return "0" * 64

    def close(self, timeout=None):
        self.close_calls.append(timeout); self._maybe("close")


class Session:
    """SessionView 的最小实现:当前代号与报过的丢失。"""

    def __init__(self, current=1):
        self.current, self.lost = current, []

    def owns_generation(self, gen):
        return gen == self.current

    def mark_lost(self, gen, *, writable_worlds):
        self.lost.append((gen, list(writable_worlds)))


def _port(remote=None, session=None, **kw):
    """(端口, release 调用记录)。"""
    released = []
    port = StarlingMemory(remote if remote is not None else FakeRemote(), generation=1,
                          session=session if session is not None else Session(),
                          release=kw.pop("release", lambda: released.append(1)), **kw)
    return port, released


def _row(*, subject="cog-pb", predicate="responsible_for", obj="我担心预算超支",
         modality="believes", polarity="pos", confidence=0.7):
    return SimpleNamespace(subject_id=subject, predicate=predicate, object_value=obj,
                           modality=modality, polarity=polarity, confidence=confidence)


def _hit(content_id, score=0.5, **row):
    return {"content_id": content_id, "row": _row(**row), "score": score}


@pytest.mark.parametrize("exc, cls, transient", [
    (errors.RecallDegradedError("recall_degraded", transient=True), MemoryFailureError, True),
    (errors.EventConflict("event_conflict"), MemoryEventConflict, False),
    (errors.EventNotFound("event_not_found"), MemoryFailureError, False),
    (errors.WorldError("world_busy", code="world_busy", transient=True), MemoryFailureError, True),
    (errors.ServiceTimeout("service request timed out"), MemoryFailureError, True),
    (RuntimeError("boom at http://127.0.0.1:9"), MemoryFailureError, False),
])
def test_every_failure_maps_through_the_client_table_and_hides_the_detail(exc, cls, transient):
    port, _ = _port(FakeRemote(raise_on={"digest": exc}))
    with pytest.raises(MemoryFailureError) as info:
        port.digest()
    assert type(info.value) is cls and info.value.transient is transient
    assert "127.0.0.1" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True     # from None


def test_a_read_only_port_refuses_writes_and_skips_embedding():
    remote = FakeRemote()
    port, _ = _port(remote, read_only=True)
    with pytest.raises(MemoryFailureError, match="world_read_only") as info:
        port.remember(EV)
    assert info.value.transient is False and remote.remembered == []
    assert port.embed_pending() == 0 and remote.embed_nows == []


def test_recall_asks_the_service_for_strict_volatile_semantic_hits():
    remote = FakeRemote()
    port, _ = _port(remote)
    assert port.recall("预算", limit=3) == []
    assert remote.recall_kwargs == {"k": 3, "mode": "semantic", "strict": True, "include_volatile": True}


def test_a_session_expiry_marks_the_generation_lost_and_is_transient():
    session, remote = Session(), FakeRemote(raise_on={"recall": errors.SessionExpired("session_expired")})
    port, _ = _port(remote, session)
    with pytest.raises(MemoryFailureError) as info:
        port.recall("q", limit=1)
    assert info.value.transient is True and session.lost == [(1, [remote.world])]


def test_a_service_that_went_away_marks_the_generation_lost_so_the_next_call_reconnects():
    session, remote = Session(), FakeRemote(raise_on={"digest": errors.ServiceUnavailable("service unreachable")})
    port, _ = _port(remote, session)
    with pytest.raises(MemoryFailureError) as info:
        port.digest()
    assert info.value.transient is True and session.lost == [(1, [remote.world])]


def test_handle_closed_on_a_port_rise_did_not_close_is_a_lost_session_not_a_closed_port():
    session, remote = Session(), FakeRemote(raise_on={"digest": errors.MemoryClosed("handle_closed")})
    port, _ = _port(remote, session, read_only=True)
    with pytest.raises(MemoryFailureError) as info:
        port.digest()
    assert type(info.value) is MemoryFailureError and info.value.transient is True
    assert session.lost == [(1, [])]                                    # 只读端口不记可写世界


def test_other_failures_do_not_report_the_session_lost():
    session = Session()
    port, _ = _port(FakeRemote(raise_on={"digest": errors.WorldError("world_busy", code="world_busy",
                                                                       transient=True)}), session)
    with pytest.raises(MemoryFailureError):
        port.digest()
    assert session.lost == []


@pytest.mark.parametrize("why", ["expired", "closed", "stale"])
def test_close_treats_an_expired_closed_or_stale_generation_port_as_closed(why):
    raise_on = {"expired": {"close": errors.SessionExpired("session_expired")},
                "closed": {"close": errors.MemoryClosed("handle_closed")}, "stale": {}}[why]
    remote = FakeRemote(raise_on=raise_on)
    port, released = _port(remote, Session(current=2 if why == "stale" else 1))
    port.close()
    assert released == [1] and (remote.close_calls == ([] if why == "stale" else [60.0]))
    with pytest.raises(MemoryClosedError):
        port.recall("q", limit=1)


def test_a_degraded_recall_is_a_transient_failure_not_an_empty_hit_list():
    port, _ = _port(FakeRemote(raise_on={"recall": errors.RecallDegradedError("recall_degraded", transient=True)}))
    with pytest.raises(MemoryFailureError) as info:
        port.recall("预算", limit=3)
    assert info.value.transient is True


def test_embed_failures_raise_transient_even_though_starling_only_counts_them():
    port, _ = _port(FakeRemote(failed=2))
    with pytest.raises(MemoryFailureError) as info:
        port.embed_pending()
    assert info.value.transient is True and "2" in str(info.value)


def test_embed_pending_returns_the_embedded_count_at_the_event_clock():
    remote = FakeRemote(embedded=4)
    remote.seq = 3725                                     # 1 小时 2 分 5 秒
    assert _port(remote)[0].embed_pending() == 4
    assert remote.embed_nows == ["2026-01-01T01:02:05Z"] and EPOCH == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("limit", [-1, True, 1.5, "3", None])
def test_a_limit_that_is_not_a_non_negative_int_is_a_data_error(limit):
    with pytest.raises(SimpleActioDataError, match="recall.limit"):
        _port()[0].recall("预算", limit=limit)


def test_a_zero_limit_returns_nothing_without_asking_the_service():
    port, _ = _port(FakeRemote(raise_on={"recall": AssertionError("must not be called")}))
    assert port.recall("预算", limit=0) == []


def test_hits_with_a_non_positive_score_are_dropped():
    remote = FakeRemote()
    remote.hits = [_hit("a" * 64, 0.4), _hit("b" * 64, 0.0), _hit("c" * 64, -0.2), _hit("d" * 64, 0.1)]
    ids = [h.id for h in _port(remote)[0].recall("预算", limit=4)]
    assert ids == ["mem-" + "a" * 16, "mem-" + "d" * 16]


def test_hits_with_the_same_content_id_collapse_to_the_first_in_order():
    remote = FakeRemote()
    remote.hits = [_hit("a" * 64, 0.9), _hit("b" * 64, 0.8), _hit("a" * 64, 0.7, obj="另一行")]
    hits = _port(remote)[0].recall("预算", limit=3)
    assert [h.id for h in hits] == ["mem-" + "a" * 16, "mem-" + "b" * 16]
    assert hits[0].text.startswith("cog-pb responsible_for 我担心预算超支（")   # 留下的是先到的那行


def test_a_negative_statement_gets_the_negation_before_the_predicate():
    remote = FakeRemote()
    remote.hits = [_hit("a" * 64, polarity="neg", modality="desires", confidence=0.5)]
    [hit] = _port(remote)[0].recall("预算", limit=1)
    assert hit == MemoryHit("mem-" + "a" * 16, "cog-pb 不responsible_for 我担心预算超支（desires，置信度 0.50）")


def test_remember_stores_content_ids_and_passes_the_clock_epoch_explicitly():
    remote = FakeRemote()
    remote.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1", "u-2"],
                                    outcome="accepted")
    remote.ids = {"u-1": "ab" * 32, "u-2": "cd" * 32}
    out = _port(remote)[0].remember(EV)
    assert out == RememberOutcome(("mem-" + "ab" * 8, "mem-" + "cd" * 8), False, None)
    assert remote.remembered == [("ep-1#0", "pb：我担心预算超支", "pb", EPOCH)]   # 不加 [key] 前缀;epoch 显式传


@pytest.mark.parametrize("category, expected", [("transport_failure", "transport_failure"), ("", "unknown")])
def test_remember_reports_an_extraction_failure_as_a_counted_outcome(category, expected):
    remote = FakeRemote()
    remote.result = SimpleNamespace(extraction_failed=True, failure_category=category, statement_ids=[])
    assert _port(remote)[0].remember(EV) == RememberOutcome((), True, expected)


def test_remember_refuses_a_statement_the_world_does_not_know_instead_of_minting_a_fake_id():
    remote = FakeRemote()
    remote.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1", "u-2"],
                                    outcome="accepted")
    remote.ids = {"u-1": "ab" * 32, "u-2": "missing"}        # 服务对不认得的语句 id 给 "missing"
    with pytest.raises(MemoryFailureError) as info:
        _port(remote)[0].remember(EV)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert "mem-missing" not in str(info.value) and "not found" in str(info.value)


@pytest.mark.parametrize("outcome", ["no_store", "rejected", "source_missing", ""])
def test_a_write_whose_outcome_is_not_accepted_or_idempotent_is_a_permanent_failure(outcome):
    remote = FakeRemote()
    remote.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=[], outcome=outcome)
    with pytest.raises(MemoryFailureError, match=f"remember: outcome {outcome}$") as info:
        _port(remote)[0].remember(EV)
    assert info.value.transient is False


@pytest.mark.parametrize("outcome", ["accepted", "idempotent"])
def test_accepted_and_idempotent_writes_keep_their_statements(outcome):
    remote = FakeRemote()
    remote.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1"],
                                    outcome=outcome)
    remote.ids = {"u-1": "ab" * 32}
    assert _port(remote)[0].remember(EV).stored == ("mem-" + "ab" * 8,)


def test_a_failed_extraction_is_still_counted_whatever_the_outcome_says():
    remote = FakeRemote()        # cff319d 上抽取失败时 outcome 是 accepted、extraction_failed 为真(2026-11-10 实测)
    remote.result = SimpleNamespace(extraction_failed=True, failure_category="transport_failure", statement_ids=[],
                                    outcome="accepted")
    assert _port(remote)[0].remember(EV) == RememberOutcome((), True, "transport_failure")


def test_the_port_satisfies_the_memory_backend_protocol():
    assert isinstance(_port()[0], MemoryBackend)


def test_close_waits_with_its_timeout_once_and_a_timeout_is_non_transient():
    remote = FakeRemote(raise_on={"close": TimeoutError("1 call(s) still in flight")})
    port, released = _port(remote, close_timeout_s=0.5)
    with pytest.raises(MemoryFailureError) as info:
        port.close()
    assert info.value.transient is False and remote.close_calls == [0.5] and released == []


def test_a_successful_close_releases_once_and_closes_every_method():
    remote = FakeRemote()
    port, released = _port(remote)
    port.close()
    port.close()                                            # 再关是空操作
    assert remote.close_calls == [60.0] and released == [1]
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(EV), port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()


def test_a_failed_close_does_not_mark_the_port_closed_so_a_second_close_can_finish():
    class Flaky(FakeRemote):
        def close(self, timeout=None):
            self.close_calls.append(timeout)
            if len(self.close_calls) == 1:
                raise TimeoutError("1 call(s) still in flight")

    remote = Flaky()
    port, released = _port(remote)
    with pytest.raises(MemoryFailureError):
        port.close()
    assert released == [] and len(port.digest()) == 64          # 没关上:没 release,端口仍可用
    port.close()
    assert released == [1] and len(remote.close_calls) == 2
    with pytest.raises(MemoryClosedError):
        port.digest()


def test_force_close_releases_the_port_even_when_the_handle_will_not_close():
    """最终收尾的强关:句柄上有在途调用没返回(close 超时)时,抛出失败,但端口当作已关、release 一定调,
    两侧的端口计数才归零;之后再关是空操作。"""
    remote = FakeRemote(raise_on={"close": TimeoutError("1 call(s) still in flight")})
    port, released = _port(remote, close_timeout_s=60.0)
    with pytest.raises(MemoryFailureError):
        port.force_close(0.25)
    assert remote.close_calls == [0.25] and released == [1]                  # 用强关的期限,不用端口自己的 60 秒
    with pytest.raises(MemoryClosedError):
        port.digest()
    port.force_close(0.25)
    port.close()
    assert released == [1] and remote.close_calls == [0.25]


def test_force_close_on_a_healthy_port_is_an_ordinary_close():
    remote = FakeRemote()
    port, released = _port(remote)
    port.force_close(2.0)
    assert remote.close_calls == [2.0] and released == [1]


def test_handle_closed_after_rise_started_closing_is_a_closed_port():
    """rise 关端口时的 handle_closed 是自己关的:之后的调用照常是 MemoryClosedError,不报会话丢失。"""
    session = Session()
    port, released = _port(FakeRemote(), session)
    port.close()
    with pytest.raises(MemoryClosedError):
        port.digest()
    assert session.lost == [] and released == [1]


def test_a_release_that_raises_is_translated_and_the_port_is_closed_anyway():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("counter at http://127.0.0.1:9 broke")

    remote = FakeRemote()
    port, _ = _port(remote, release=broken)
    with pytest.raises(MemoryFailureError) as info:
        port.close()
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert "RuntimeError" in str(info.value) and "127.0.0.1" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True      # from None
    with pytest.raises(MemoryClosedError):                    # 句柄已关:端口不能再用
        port.digest()
    port.close()                                              # 再关是空操作,不再 release
    assert remote.close_calls == [60.0] and calls == [1]


def test_a_failed_translation_keeps_the_client_code_for_the_caller():
    """管理面按 `code` 认出 world_exists 之类的码,不解析消息文字。"""
    from simple_actio.memory_starling import translate_client_failure
    out = translate_client_failure(errors.WorldError("world_exists", code="world_exists"),
                                   port_closed_by_rise=False, in_drain_window=False)
    assert out.code == "world_exists" and str(out) == "starling world_exists (WorldError)"
    assert translate_client_failure(RuntimeError("x"), port_closed_by_rise=False, in_drain_window=False).code is None
