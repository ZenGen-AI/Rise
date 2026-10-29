# tests/test_starling_fakes.py
"""离线假端点本身的钉子:它被 Starling 的真适配器调用时,行为与本步各用例的假设一致。"""
import math

import pytest

from starling_fakes import FakeOpenAI, fake_vector

starling = pytest.importorskip("starling")


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_vectors_follow_token_overlap_and_change_with_the_salt():
    a = fake_vector("我担心预算超支", dim=256)
    assert math.isclose(sum(x * x for x in a), 1.0)
    assert _cos(a, fake_vector("预算超支怎么办", dim=256)) > 0
    assert _cos(a, fake_vector("天气", dim=256)) == 0.0
    assert fake_vector("天气", dim=256, salt="b") != fake_vector("天气", dim=256)


def test_starling_extracts_the_utterance_as_one_belief_through_the_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    with FakeOpenAI(dim=256) as fake:
        llm = starling.make_openai_llm(model="fake-extract", base_url=fake.base_url, timeout_ms=2000)
        emb = starling.make_openai_embedder(model="fake-embed", base_url=fake.base_url, dim=256, timeout_ms=2000)
        mem = starling.Memory.open(str(tmp_path / "pa.db"), agent="pa", llm=llm, embedder=emb)
        res = mem.remember_event("ep#0", "pb：我担心预算超支", interlocutor="pb")
        assert not res.extraction_failed and len(res.statement_ids) == 1
        assert mem.embed_pending("2026-01-01T00:00:01Z").embedded == 1
        hits = mem.recall("预算超支怎么办", k=3, strict=True, include_volatile=True)
        assert [h["row"].object_value for h in hits] == ["我担心预算超支"]
        mem.close()


def test_a_failing_key_and_a_failing_embedder_surface_as_starling_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    with FakeOpenAI(dim=256) as fake:
        fake.fail_keys.add("ep#0")
        llm = starling.make_openai_llm(model="fake-extract", base_url=fake.base_url, timeout_ms=2000)
        emb = starling.make_openai_embedder(model="fake-embed", base_url=fake.base_url, dim=256, timeout_ms=2000)
        mem = starling.Memory.open(str(tmp_path / "pa.db"), agent="pa", llm=llm, embedder=emb)
        res = mem.remember_event("ep#0", "pb：我担心预算超支", interlocutor="pb")
        assert res.extraction_failed and res.failure_category
        fake.fail_embeddings = True
        with pytest.raises(starling.RecallDegradedError):
            mem.recall("预算", k=3, strict=True, include_volatile=True)
        mem.close()
