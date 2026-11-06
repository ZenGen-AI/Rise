"""StarlingMemory 的专属用例:失败映射(第 10 节的表)、渲染(4.4 节)、close 的期限。契约用例在共享契约里。"""
import re
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import starling_fakes
from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (MemoryBackend, MemoryClosedError, MemoryEvent, MemoryEventConflict,
                               MemoryFailureError, MemoryHit, RememberOutcome)
from starling_fakes import FakeOpenAI, offline_api_key

starling = pytest.importorskip("starling")
from simple_actio.memory_starling import EPOCH, StarlingMemory, translate_failure  # noqa: E402

EV = MemoryEvent("ep-1#0", "pb：我担心预算超支", "pb")


class FakeStarling:
    """只有 StarlingMemory 用到的门面方法;raise_on[name] 给定时该方法抛它。"""

    def __init__(self, **raise_on):
        self.raise_on = raise_on
        self.closed_with = []
        self.hits = []              # recall 返回它
        self.result = None          # remember_event 返回它
        self.ids = {}               # content_ids 的映射
        self.seq = 0
        self.remembered = []
        self.embed_nows = []

    def _maybe(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def recall(self, query, **kw):
        self._maybe("recall"); return list(self.hits)

    def remember_event(self, key, text, *, interlocutor, epoch):
        self._maybe("remember_event")
        self.remembered.append((key, text, interlocutor, epoch))
        return self.result

    def content_ids(self, statement_ids):
        self._maybe("content_ids"); return {s: self.ids[s] for s in statement_ids}

    def embed_pending(self, now):
        self._maybe("embed_pending")
        self.embed_nows.append(now)
        return starling.EmbedStats(embedded=self.raise_on.get("embedded", 0), failed=self.raise_on.get("failed", 0))

    def next_seq(self):
        return self.seq

    def digest(self):
        self._maybe("digest"); return "0" * 64

    def close(self, timeout=None):
        self.closed_with.append(timeout); self._maybe("close")


def _row(*, subject="cog-pb", predicate="responsible_for", obj="我担心预算超支",
         modality="believes", polarity="pos", confidence=0.7):
    return SimpleNamespace(subject_id=subject, predicate=predicate, object_value=obj,
                           modality=modality, polarity=polarity, confidence=confidence)


def _hit(content_id, score=0.5, **row):
    return {"content_id": content_id, "row": _row(**row), "score": score}


@pytest.mark.parametrize("exc, cls, transient", [
    (starling.RecallDegradedError("x"), MemoryFailureError, True),
    (starling.EventConflict("x"), MemoryEventConflict, False),
    (starling.MemoryClosed("x"), MemoryClosedError, False),
    (KeyError("ep-1#0"), MemoryFailureError, False),           # abandon_event 未知 key()
    (sqlite3.OperationalError("disk I/O error"), MemoryFailureError, False),
    (OSError(5, "EIO"), MemoryFailureError, False),
    (RuntimeError("boom"), MemoryFailureError, False),
    (TimeoutError(), MemoryFailureError, False),
])
def test_every_failure_has_a_fixed_transient_value_and_names_only_the_class(exc, cls, transient):
    out = translate_failure(exc)
    assert type(out) is cls and out.transient is transient
    assert type(exc).__name__ in str(out)
    assert "127.0.0.1" not in str(out) and "disk I/O" not in str(out)   # 不带异常原文


def test_a_memory_failure_error_passes_through_unchanged():
    original = MemoryFailureError("already translated", transient=True)
    assert translate_failure(original) is original


def test_the_translated_error_hides_the_original_text_and_its_cause():
    port = StarlingMemory(FakeStarling(remember_event=RuntimeError("POST http://127.0.0.1:9/v1 failed")))
    with pytest.raises(MemoryFailureError) as info:
        port.remember(EV)
    assert "127.0.0.1" not in str(info.value) and "RuntimeError" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True   # from None


def test_a_degraded_recall_is_a_transient_failure_not_an_empty_hit_list():
    port = StarlingMemory(FakeStarling(recall=starling.RecallDegradedError("query embedding failed")))
    with pytest.raises(MemoryFailureError) as info:
        port.recall("预算", limit=3)
    assert info.value.transient is True


def test_a_facade_that_is_closing_reads_as_a_closed_port():
    port = StarlingMemory(FakeStarling(recall=starling.MemoryClosed("Memory is closed")))
    with pytest.raises(MemoryClosedError):
        port.recall("预算", limit=3)


def test_embed_failures_raise_transient_even_though_starling_only_counts_them():
    port = StarlingMemory(FakeStarling(failed=2))
    with pytest.raises(MemoryFailureError) as info:
        port.embed_pending()
    assert info.value.transient is True and "2" in str(info.value)


def test_embed_pending_returns_the_embedded_count_at_the_event_clock():
    fake = FakeStarling(embedded=4)
    fake.seq = 3725                                       # 1 小时 2 分 5 秒
    assert StarlingMemory(fake).embed_pending() == 4
    assert fake.embed_nows == ["2026-01-01T01:02:05Z"] and EPOCH == "2026-01-01T00:00:00Z"


def test_recall_asks_starling_for_strict_volatile_semantic_hits():
    seen = {}

    class Spy(FakeStarling):
        def recall(self, query, **kw):
            seen.update(kw); return []

    assert StarlingMemory(Spy()).recall("预算", limit=3) == []
    assert seen == {"k": 3, "mode": "semantic", "strict": True, "include_volatile": True}


@pytest.mark.parametrize("limit", [-1, True, 1.5, "3", None])
def test_a_limit_that_is_not_a_non_negative_int_is_a_data_error(limit):
    with pytest.raises(SimpleActioDataError, match="recall.limit"):
        StarlingMemory(FakeStarling()).recall("预算", limit=limit)


def test_a_zero_limit_returns_nothing_without_asking_starling():
    port = StarlingMemory(FakeStarling(recall=AssertionError("must not be called")))
    assert port.recall("预算", limit=0) == []


def test_hits_with_a_non_positive_score_are_dropped():
    fake = FakeStarling()
    fake.hits = [_hit("a" * 64, 0.4), _hit("b" * 64, 0.0), _hit("c" * 64, -0.2), _hit("d" * 64, 0.1)]
    ids = [h.id for h in StarlingMemory(fake).recall("预算", limit=4)]
    assert ids == ["mem-" + "a" * 16, "mem-" + "d" * 16]


def test_hits_with_the_same_content_id_collapse_to_the_first_in_order():
    fake = FakeStarling()
    fake.hits = [_hit("a" * 64, 0.9), _hit("b" * 64, 0.8), _hit("a" * 64, 0.7, obj="另一行")]
    hits = StarlingMemory(fake).recall("预算", limit=3)
    assert [h.id for h in hits] == ["mem-" + "a" * 16, "mem-" + "b" * 16]
    assert hits[0].text.startswith("cog-pb responsible_for 我担心预算超支（")   # 留下的是先到的那行


def test_a_negative_statement_gets_the_negation_before_the_predicate():
    fake = FakeStarling()
    fake.hits = [_hit("a" * 64, polarity="neg", modality="desires", confidence=0.5)]
    [hit] = StarlingMemory(fake).recall("预算", limit=1)
    assert hit == MemoryHit("mem-" + "a" * 16, "cog-pb 不responsible_for 我担心预算超支（desires，置信度 0.50）")


def test_remember_stores_content_ids_and_passes_the_clock_epoch_explicitly():
    fake = FakeStarling()
    fake.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1", "u-2"])
    fake.ids = {"u-1": "ab" * 32, "u-2": "cd" * 32}
    out = StarlingMemory(fake).remember(EV)
    assert out == RememberOutcome(("mem-" + "ab" * 8, "mem-" + "cd" * 8), False, None)
    assert fake.remembered == [("ep-1#0", "pb：我担心预算超支", "pb", EPOCH)]   # 不加 [key] 前缀;epoch 显式传


@pytest.mark.parametrize("category, expected", [("transport_failure", "transport_failure"), ("", "unknown")])
def test_remember_reports_an_extraction_failure_as_a_counted_outcome(category, expected):
    fake = FakeStarling()
    fake.result = SimpleNamespace(extraction_failed=True, failure_category=category, statement_ids=[])
    assert StarlingMemory(fake).remember(EV) == RememberOutcome((), True, expected)


def test_remember_refuses_a_statement_the_library_does_not_know_instead_of_minting_a_fake_id():
    fake = FakeStarling()
    fake.result = SimpleNamespace(extraction_failed=False, failure_category="", statement_ids=["u-1", "u-2"])
    fake.ids = {"u-1": "ab" * 32, "u-2": "missing"}          # 门面对不认得的语句 id 给 "missing"
    with pytest.raises(MemoryFailureError) as info:
        StarlingMemory(fake).remember(EV)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert "mem-missing" not in str(info.value) and "not found" in str(info.value)


def test_the_port_satisfies_the_memory_backend_protocol():
    assert isinstance(StarlingMemory(FakeStarling()), MemoryBackend)


def test_close_waits_with_its_timeout_once_and_a_timeout_is_non_transient():
    fake = FakeStarling(close=TimeoutError())
    port = StarlingMemory(fake, close_timeout_s=0.5)
    with pytest.raises(MemoryFailureError) as info:
        port.close()
    assert info.value.transient is False and fake.closed_with == [0.5]


def test_a_successful_close_runs_on_close_once_and_closes_every_method():
    fake, closed = FakeStarling(), []
    port = StarlingMemory(fake, on_close=lambda: closed.append(1))
    port.close()
    port.close()                                            # 再关是空操作
    assert fake.closed_with == [60.0] and closed == [1]
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(EV), port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()


def test_a_failed_close_does_not_mark_the_port_closed_so_a_second_close_can_finish():
    class Flaky(FakeStarling):
        def close(self, timeout=None):
            self.closed_with.append(timeout)
            if len(self.closed_with) == 1:
                raise TimeoutError("1 call(s) still in flight")

    fake, closed = Flaky(), []
    port = StarlingMemory(fake, on_close=lambda: closed.append(1))
    with pytest.raises(MemoryFailureError):
        port.close()
    assert closed == [] and len(port.digest()) == 64          # 没关上:on_close 没跑,端口仍可用
    port.close()
    assert closed == [1] and len(fake.closed_with) == 2
    with pytest.raises(MemoryClosedError):
        port.digest()


def test_an_on_close_that_raises_is_translated_and_the_port_is_closed_anyway():
    fake, calls = FakeStarling(), []

    def broken():
        calls.append(1)
        raise RuntimeError("counter at http://127.0.0.1:9 broke")

    port = StarlingMemory(fake, on_close=broken)
    with pytest.raises(MemoryFailureError) as info:
        port.close()
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert "RuntimeError" in str(info.value) and "127.0.0.1" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True      # from None
    with pytest.raises(MemoryClosedError):                    # 门面已关:端口不能再用
        port.digest()
    port.close()                                              # 再关是空操作,不再调 on_close
    assert fake.closed_with == [60.0] and calls == [1]


def test_a_failure_while_opening_is_translated_and_leaks_no_path(tmp_path):
    (tmp_path / "plain-file").write_text("x", encoding="utf-8")
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        with pytest.raises(MemoryFailureError) as info:
            StarlingMemory.open(tmp_path / "plain-file" / "pa.db", holder="pa", endpoints=fake.endpoints())
    assert info.value.transient is False and "RuntimeError" in str(info.value)
    assert str(tmp_path) not in str(info.value) and "127.0.0.1" not in str(info.value)


# ---------------- 真门面经本机假端点 ----------------

def test_real_hits_are_rendered_with_content_ids(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        port = StarlingMemory.open(tmp_path / "pa.db", holder="pa", endpoints=fake.endpoints())
        out = port.remember(EV)
        assert out.failed is False and len(out.stored) == 1 and re.fullmatch(r"mem-[0-9a-f]{16}", out.stored[0])
        assert port.embed_pending() == 1
        hits = port.recall("预算超支怎么办", limit=3)
        assert [h.id for h in hits] == list(out.stored)
        # 落库后的行:modality 与 polarity 是小写,confidence 0.7;括号与逗号是全角(设计源 4.4 节)
        assert hits[0].text == "cog-pb responsible_for 我担心预算超支（believes，置信度 0.70）"
        assert port.recall("天气", limit=3) == []             # 零余弦不算命中
        port.close()


def test_a_failed_extraction_is_counted_not_raised(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.fail_keys.add(EV.key)
        port = StarlingMemory.open(tmp_path / "pa.db", holder="pa", endpoints=fake.endpoints())
        out = port.remember(EV)
        assert (out.stored, out.failed) == ((), True) and out.category
        port.close()


def test_a_failing_embedding_endpoint_surfaces_as_transient_failures(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        port = StarlingMemory.open(tmp_path / "pa.db", holder="pa", endpoints=fake.endpoints())
        assert port.remember(EV).failed is False
        fake.fail_embeddings = True
        with pytest.raises(MemoryFailureError) as embed_info:
            port.embed_pending()
        assert embed_info.value.transient is True and "1 statement" in str(embed_info.value)
        with pytest.raises(MemoryFailureError) as recall_info:    # 查询词嵌不出:降级,不是空结果
            port.recall("预算超支怎么办", limit=3)
        assert recall_info.value.transient is True
        assert "127.0.0.1" not in str(embed_info.value) + str(recall_info.value)
        port.close()


def test_a_deployment_store_write_is_recallable_on_the_next_port_without_a_round_in_between(tmp_path):
    """门② 经管理面写进部署库的语句当场就嵌入。真机 2026-11-01:之前只写不嵌,库里 statement_vectors 0 行,
    要等下一个部署轮次第一轮的 embed_pending 顺带补上,那一轮的召回看不到刚收录的记忆。"""
    from simple_actio.memory_starling import StarlingDeploymentStore

    with FakeOpenAI(dim=256) as fake, offline_api_key():
        store = StarlingDeploymentStore(tmp_path / "deploy", endpoints=fake.endpoints())
        out = store.remember_event("s1", "pa", EV.key, EV.text, interlocutor=EV.interlocutor)
        assert out.failed is False and len(out.stored) == 1
        port = store.open("s1", "pa")
        try:
            assert [h.id for h in port.recall("预算超支怎么办", limit=3)] == list(out.stored)
            assert port.embed_pending() == 0                  # 没有欠着的嵌入
        finally:
            port.close()


def test_a_deployment_store_write_whose_embedding_fails_is_a_transient_failure_and_retries_cleanly(tmp_path):
    """嵌入失败与端口同口径,抛 transient 的 MemoryFailureError。语句与事件已提交;门② 重入同 key 时
    Starling 返回同一批语句 id(不二次抽取),这一次嵌入补上。"""
    from simple_actio.memory_starling import StarlingDeploymentStore

    with FakeOpenAI(dim=256) as fake, offline_api_key():
        store = StarlingDeploymentStore(tmp_path / "deploy", endpoints=fake.endpoints())
        fake.fail_embeddings = True
        with pytest.raises(MemoryFailureError) as info:
            store.remember_event("s1", "pa", EV.key, EV.text, interlocutor=EV.interlocutor)
        assert info.value.transient is True and "127.0.0.1" not in str(info.value)
        chats = fake.chat_calls
        fake.fail_embeddings = False
        again = store.remember_event("s1", "pa", EV.key, EV.text, interlocutor=EV.interlocutor)
        assert again.failed is False and len(again.stored) == 1 and fake.chat_calls == chats
        port = store.open("s1", "pa")
        try:
            assert [h.id for h in port.recall("预算超支怎么办", limit=3)] == list(again.stored)
        finally:
            port.close()


def test_close_with_a_call_in_flight_times_out_and_the_port_stays_usable_for_a_second_close(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.chat_delay_s = 0.8       # 与 timeout_ms=2000 留 1.2 秒余量,负载高时抽取请求不会自己超时
        port = StarlingMemory.open(tmp_path / "pa.db", holder="pa", endpoints=fake.endpoints(),
                                   close_timeout_s=0.2)
        done = []
        t = threading.Thread(target=lambda: done.append(port.remember(EV))); t.start()
        fake.wait_for_chat_call(timeout_s=2)                 # 抽取请求已发出,remember_event 在途
        with pytest.raises(MemoryFailureError) as info:
            port.close()
        assert info.value.transient is False
        t.join()
        assert len(done) == 1 and done[0].failed is False    # 在途的写没有被关闭打断
        port.close()                                          # 在途调用返回后再关能关上
        with pytest.raises(MemoryClosedError):
            port.digest()


# ---------------- max_retries=0 真的是零次重试 ----------------

def _belief_prompt(key):
    return f"{starling_fakes._BELIEF_PROMPT_HEAD}\n\nConversation:\n[{key}] pb：我担心预算超支"


def test_endpoints_with_zero_retries_do_not_retry():
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.fail_keys.add(EV.key)
        fake.fail_embeddings = True
        endpoints = fake.endpoints()
        assert endpoints.max_retries == 0
        resp = endpoints.make_llm().extract(_belief_prompt(EV.key))
        assert resp.ok is False and fake.chat_calls == 1                 # 抽取:500 之后不再发
        with pytest.raises(RuntimeError):
            endpoints.make_embedder().embed("预算")
        assert fake.embedding_calls == 1                                  # 嵌入:503 之后不再发


def test_a_configured_retry_is_honoured_so_the_zero_case_is_not_a_default():
    """对照:max_retries=1 时同样的失败各多发一次。没有它,上一条在"配置被忽略"时也可能碰巧通过。"""
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        fake.fail_keys.add(EV.key)
        fake.fail_embeddings = True
        endpoints = fake.endpoints(max_retries=1)
        assert endpoints.make_llm().extract(_belief_prompt(EV.key)).ok is False
        assert fake.chat_calls == 2
        with pytest.raises(RuntimeError):
            endpoints.make_embedder().embed("预算")
        assert fake.embedding_calls == 2


# ---------------- 记忆运行时身份用到的三个函数(Task 17) ----------------

def test_starling_version_is_the_installed_dunder_version():
    from simple_actio.memory_starling import starling_version
    assert starling_version() == starling.__version__


def test_the_empty_digest_is_a_real_empty_library_and_is_computed_once(monkeypatch):
    from simple_actio.memory_starling import empty_digest
    empty_digest.cache_clear()
    opened = []
    original = starling.Memory.open
    monkeypatch.setattr(starling.Memory, "open", staticmethod(lambda *a, **kw: opened.append(a) or original(*a, **kw)))
    first = empty_digest()
    assert empty_digest() == first and len(opened) == 1               # 第二次不再开库
    monkeypatch.undo()
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        from simple_actio.memory_starling import StarlingDeploymentStore
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            port = StarlingDeploymentStore(pathlib.Path(tmp), endpoints=fake.endpoints()).open("s1", "pa")
            try:
                assert port.digest() == first                          # 空库的摘要就是新开一个库的摘要
            finally:
                port.close()


def test_a_failed_empty_digest_is_not_cached(monkeypatch):
    from simple_actio.memory_starling import empty_digest
    empty_digest.cache_clear()
    original = starling.Memory.open
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return original(*a, **kw)

    monkeypatch.setattr(starling.Memory, "open", staticmethod(flaky))
    with pytest.raises(MemoryFailureError):
        empty_digest()
    assert len(empty_digest()) == 64 and len(calls) == 2              # 第一次的失败没进缓存


def test_the_extraction_prompts_digest_is_the_sha256_of_the_default_extraction_config():
    import dataclasses
    import hashlib
    import json
    from simple_actio.memory_starling import extraction_prompts_digest
    expected = hashlib.sha256(json.dumps(dataclasses.asdict(starling.ExtractionConfig()), ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert extraction_prompts_digest() == expected and len(expected) == 64


def test_the_extraction_prompts_digest_follows_the_prompts(monkeypatch):
    """提示词或阈值变了,摘要跟着变:身份才能在 Starling 升级改了提示词时变,续训才判 T3。"""
    from simple_actio.memory_starling import extraction_prompts_digest
    before = extraction_prompts_digest()
    original = starling.ExtractionConfig()
    changed = type(original)(belief_prompt=original.belief_prompt + " (edited)")
    monkeypatch.setattr(starling, "ExtractionConfig", lambda: changed)
    assert extraction_prompts_digest() != before
    monkeypatch.setattr(starling, "ExtractionConfig", lambda: type(original)(weak_inference_floor=0.9))
    assert extraction_prompts_digest() != before


def test_no_connection_is_opened_with_a_custom_extraction(monkeypatch, tmp_path):
    """`extraction_prompts_digest` 摘的是默认配置,前提是本模块从不传 `extraction=`(见它的 docstring)。
    这条钉住前提:端口、管理面的每条连接都不带 extraction。哪天开始传了,这里红,提醒两处一起改。"""
    from simple_actio.memory_starling import StarlingDeploymentStore
    seen = []
    original = starling.Memory.open

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(starling.Memory, "open", staticmethod(spy))
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        store = StarlingDeploymentStore(tmp_path / "deploy", endpoints=fake.endpoints())
        store.remember_event("s1", "pa", "k#0@pa", "pb：我担心预算超支", interlocutor="pb")        # 带端点的短命连接
        port = store.open("s1", "pa")                                                        # 端口
        port.close()
        store.digest("s1")
        store.backup_to("s1", tmp_path / "snap.db")
        store.digest_of(tmp_path / "snap.db")
    assert len(seen) >= 5 and all(kw.get("extraction") is None for kw in seen)
