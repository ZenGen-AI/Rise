# tests/test_starling_fakes.py
"""离线假端点本身的钉子。

前半不依赖 Starling(CI 只装 dev extra 也要真跑):fake_vector,以及父子进程同步层
(开关、计数、fail_keys 的就地改与整体赋值、单例、子进程的启停与失败路径)。
后半是被 Starling 的真适配器调用时,行为与本步各用例的假设一致;没装 Starling 就在用例里跳过。
"""
import http.client
import json
import math
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

import starling_fakes
from starling_fakes import FakeOpenAI, fake_vector, shared_fake_openai

TESTS_DIR = str(Path(__file__).resolve().parent)
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _call(fake, path, body):
    """直接打假端点,返回 (状态码, 解析后的 JSON)。"""
    conn = http.client.HTTPConnection("127.0.0.1", urlparse(fake.base_url).port, timeout=10)
    try:
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def _belief_prompt(key="ep#0", speaker="pb", utterance="我担心预算超支"):
    head = starling_fakes._BELIEF_PROMPT_HEAD
    return f"{head}\n\nGiven a conversation, extract ALL Statements.\n\nConversation:\n[{key}] {speaker}：{utterance}"


def _chat(fake, content):
    return _call(fake, "/v1/chat/completions",
                 {"model": "m", "messages": [{"role": "user", "content": content}]})


def _answer(body):
    return json.loads(body["choices"][0]["message"]["content"])


# ---------------- 不依赖 Starling ----------------

def test_vectors_follow_token_overlap_and_change_with_the_salt():
    a = fake_vector("我担心预算超支", dim=256)
    assert math.isclose(sum(x * x for x in a), 1.0)
    assert _cos(a, fake_vector("预算超支怎么办", dim=256)) > 0
    assert _cos(a, fake_vector("天气", dim=256)) == 0.0
    assert fake_vector("天气", dim=256, salt="b") != fake_vector("天气", dim=256)


def test_the_salt_changes_the_vectors_the_server_returns():
    with FakeOpenAI(dim=64) as fake:
        status, body = _call(fake, "/v1/embeddings", {"model": "m", "input": ["预算超支", "天气"]})
        assert status == 200
        assert [d["index"] for d in body["data"]] == [0, 1]
        assert body["data"][0]["embedding"] == fake_vector("预算超支", dim=64)
        fake.salt = "b"
        _, salted = _call(fake, "/v1/embeddings", {"model": "m", "input": "预算超支"})
        assert salted["data"][0]["embedding"] == fake_vector("预算超支", dim=64, salt="b")
        assert salted["data"][0]["embedding"] != body["data"][0]["embedding"]


def test_fail_embeddings_answers_503_until_it_is_switched_off():
    with FakeOpenAI(dim=64) as fake:
        fake.fail_embeddings = True
        assert _call(fake, "/v1/embeddings", {"model": "m", "input": "x"})[0] == 503
        fake.fail_embeddings = False
        assert _call(fake, "/v1/embeddings", {"model": "m", "input": "x"})[0] == 200


def test_chat_delay_takes_effect_and_can_be_switched_off():
    with FakeOpenAI(dim=64) as fake:
        fake.chat_delay_s = 0.4
        start = time.monotonic()
        assert _chat(fake, "hello")[0] == 200
        assert time.monotonic() - start >= 0.4
        fake.chat_delay_s = 0.0
        start = time.monotonic()
        _chat(fake, "hello")
        assert time.monotonic() - start < 0.4


def test_belief_prompt_gets_one_belief_and_other_prompts_get_an_empty_list():
    with FakeOpenAI(dim=64) as fake:
        status, body = _chat(fake, _belief_prompt())
        assert status == 200
        [belief] = _answer(body)
        assert (belief["subject"], belief["object"]) == ("cog-pb", "我担心预算超支")
        assert _answer(_chat(fake, "You are a general-fact extractor for a Statement-based memory system.")[1]) == []
        assert _answer(_chat(fake, "You are an episodic-event extractor for a memory system.")[1]) == []


def test_fail_keys_work_for_in_place_changes_and_for_whole_assignment():
    with FakeOpenAI(dim=64) as fake:
        assert _chat(fake, _belief_prompt("ep#1"))[0] == 200
        fake.fail_keys.add("ep#1")                                  # 就地改
        assert _chat(fake, _belief_prompt("ep#1"))[0] == 500
        assert _chat(fake, _belief_prompt("ep#2"))[0] == 200
        fake.fail_keys = {"ep#2"}                                   # 整体赋值:换掉内容
        assert _chat(fake, _belief_prompt("ep#1"))[0] == 200
        assert _chat(fake, _belief_prompt("ep#2"))[0] == 500
        fake.fail_keys |= {"ep#3"}                                  # 原地运算符回写
        assert _chat(fake, _belief_prompt("ep#2"))[0] == 500
        assert _chat(fake, _belief_prompt("ep#3"))[0] == 500
        fake.fail_keys = set()                                      # 整体赋空:恢复
        assert _chat(fake, _belief_prompt("ep#2"))[0] == 200
        assert _chat(fake, _belief_prompt("ep#3"))[0] == 200


def _general_fact_prompt(key="ep#0", speaker="pb", utterance="我担心预算超支"):
    head = starling_fakes._GENERAL_FACT_PROMPT_HEAD
    return f"{head}\n\nPassage:\n[{key}] {speaker}：{utterance}"


def test_malformed_keys_break_only_the_general_fact_pass_of_that_key():
    with FakeOpenAI(dim=64) as fake:
        fake.malformed_keys.add("ep#1")
        status, body = _chat(fake, _general_fact_prompt("ep#1"))
        assert status == 200 and body["choices"][0]["message"]["content"] == "not json"
        assert _answer(_chat(fake, _general_fact_prompt("ep#2"))[1]) == []          # 别的 key 照旧
        [belief] = _answer(_chat(fake, _belief_prompt("ep#1"))[1])                   # 信念那一遍照旧
        assert belief["object"] == "我担心预算超支"
        fake.malformed_keys = set()                                                  # 整体赋空:恢复
        assert _answer(_chat(fake, _general_fact_prompt("ep#1"))[1]) == []


def test_assigning_fail_keys_keeps_the_same_synced_set_object():
    with FakeOpenAI(dim=64) as fake:
        before = fake.fail_keys
        fake.fail_keys = {"ep#1"}
        assert fake.fail_keys is before and fake.fail_keys == {"ep#1"}
        fake.fail_keys.discard("ep#1")
        assert _chat(fake, _belief_prompt("ep#1"))[0] == 200


def test_call_counts_are_per_request_and_survive_the_with_block():
    with FakeOpenAI(dim=64) as fake:
        assert (fake.chat_calls, fake.embedding_calls) == (0, 0)
        _chat(fake, "hello")
        _chat(fake, _belief_prompt())
        _call(fake, "/v1/embeddings", {"model": "m", "input": ["a", "b"]})   # 一次请求,两条输入
        fake.fail_embeddings = True
        _call(fake, "/v1/embeddings", {"model": "m", "input": "c"})           # 503 也计数
        assert (fake.chat_calls, fake.embedding_calls) == (2, 2)
    assert (fake.chat_calls, fake.embedding_calls) == (2, 2)


def test_two_instances_do_not_share_state():
    with FakeOpenAI(dim=64) as one, FakeOpenAI(dim=64) as two:
        assert one.base_url != two.base_url
        one.fail_keys.add("ep#0")
        _chat(one, "hello")
        assert (one.chat_calls, two.chat_calls) == (1, 0)
        assert _chat(two, _belief_prompt("ep#0"))[0] == 200


def test_leaving_the_with_block_ends_the_server_process():
    with FakeOpenAI(dim=64) as fake:
        proc = fake._proc
        assert proc is not None and proc.poll() is None
    assert proc.poll() is not None
    with pytest.raises(AssertionError):
        fake.base_url
    fake.stop()                                                      # 幂等


def test_shared_fake_is_one_lazily_started_instance(monkeypatch):
    first = shared_fake_openai()
    assert shared_fake_openai() is first
    assert first._proc is not None and first._proc.poll() is None
    assert _chat(first, "hello")[0] == 200
    # 导入模块不起服务器:换一个干净的解释器验证
    probe = ("import sys; sys.path[:0] = [%r, %r]; import starling_fakes; "
             "assert starling_fakes._shared is None; print('lazy')") % (TESTS_DIR, SRC_DIR)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "lazy", out.stderr


def test_a_bug_inside_the_fake_server_shows_up_on_stderr(capsys):
    with FakeOpenAI(dim=64) as fake:
        conn = http.client.HTTPConnection("127.0.0.1", urlparse(fake.base_url).port, timeout=10)
        try:
            conn.request("POST", "/v1/embeddings", body=json.dumps({"input": None}),
                         headers={"Content-Type": "application/json"})
            with pytest.raises((http.client.HTTPException, OSError)):
                conn.getresponse().read()
        finally:
            conn.close()
    assert "TypeError" in capsys.readouterr().err


# ---------------- 子进程起不来时的失败路径 ----------------

@pytest.fixture
def spawned(monkeypatch):
    """记下 start() 起的子进程,用来确认失败路径都收掉了它。"""
    procs = []
    real = subprocess.Popen

    def recording_popen(*args, **kwargs):
        procs.append(real(*args, **kwargs))
        return procs[-1]

    monkeypatch.setattr(starling_fakes.subprocess, "Popen", recording_popen)
    return procs


def _fake_server_script(tmp_path, body):
    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return lambda dim: [sys.executable, str(script)]


def test_a_server_that_prints_no_port_fails_fast_with_its_stderr(tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(starling_fakes, "_server_argv", _fake_server_script(tmp_path, """
        import sys
        sys.stderr.write("boom from the child\\n")
        sys.exit(3)
    """))
    with pytest.raises(RuntimeError) as err:
        FakeOpenAI(dim=64).start()
    assert "boom from the child" in str(err.value) and "退出码 3" in str(err.value)
    assert spawned[0].poll() is not None


def test_a_server_that_hangs_before_the_port_line_times_out_and_is_reaped(tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(starling_fakes, "_START_TIMEOUT_S", 0.5)
    monkeypatch.setattr(starling_fakes, "_server_argv", _fake_server_script(tmp_path, """
        import time
        time.sleep(60)
    """))
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="没有输出"):
        FakeOpenAI(dim=64).start()
    assert time.monotonic() - start < 10
    assert spawned[0].poll() is not None


def test_a_malformed_port_line_is_reported_and_the_child_is_reaped(tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(starling_fakes, "_server_argv", _fake_server_script(tmp_path, """
        import sys, time
        print("PORT notanumber", flush=True)
        time.sleep(60)
    """))
    with pytest.raises(RuntimeError, match="notanumber"):
        FakeOpenAI(dim=64).start()
    assert spawned[0].poll() is not None


def test_a_failing_first_push_still_reaps_the_child(monkeypatch, spawned):
    def broken_push(self):
        raise OSError("push failed")

    monkeypatch.setattr(FakeOpenAI, "_push", broken_push)
    fake = FakeOpenAI(dim=64)
    with pytest.raises(OSError, match="push failed"):
        fake.start()
    assert spawned[0].poll() is not None
    assert fake._proc is None


# ---------------- 依赖 Starling:真适配器经假端点 ----------------

def test_starling_extracts_the_utterance_as_one_belief_through_the_fake(tmp_path, monkeypatch):
    starling = pytest.importorskip("starling")
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
    starling = pytest.importorskip("starling")
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
