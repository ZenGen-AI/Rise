"""设计源 §13 的两项未核实:真端点的单次延迟、真嵌入下的召回质量。连真网络、要真密钥,不进 CI:
只有 RISE_MEMORY_LIVE=1 且 RISE_MEMORY_CONFIG 指向真配置时才跑。"""
import os
import statistics
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.memory_live,
    pytest.mark.skipif(os.environ.get("RISE_MEMORY_LIVE") != "1", reason="set RISE_MEMORY_LIVE=1 to run"),
]
pytest.importorskip("starling")

TURNS = [f"pb：第{i}轮,我担心预算超支,人手也不够" for i in range(10)]


@pytest.fixture
def live_port(tmp_path):
    from actio.memory_starling import StarlingEndpoints, StarlingMemory
    from factory.memory_config import check_api_key, read_memory_config
    cfg = read_memory_config(env=os.environ, repo_root=Path(__file__).resolve().parents[1])
    assert cfg is not None, "RISE_MEMORY_CONFIG must point at a real config"
    check_api_key(os.environ)
    port = StarlingMemory.open(tmp_path / "live.db", holder="pa", endpoints=StarlingEndpoints(
        cfg.extraction_base_url, cfg.extraction_model, cfg.embedding_base_url, cfg.embedding_model,
        cfg.embedding_dim, cfg.timeout_ms, cfg.max_retries))
    yield port, cfg.timeout_ms / 1000.0
    port.close()


def _timed(took, name, call):
    """只给 call 本身计时,返回它的返回值;断言放在计时之外。"""
    t0 = time.monotonic()
    result = call()
    took[name].append(time.monotonic() - t0)
    return result


def test_ten_turns_stay_within_the_per_call_budget(live_port, capsys):
    from actio.memory_port import MemoryEvent
    port, limit = live_port
    took = {"recall": [], "remember": [], "embed_pending": []}
    embedded = 0
    for i, text in enumerate(TURNS):
        hits = _timed(took, "recall", lambda: port.recall(text, limit=3))
        out = _timed(took, "remember", lambda: port.remember(MemoryEvent(f"live#{i}", text, "pb")))
        embedded += _timed(took, "embed_pending", port.embed_pending)
        assert not out.failed, f"turn {i}: remember failed, category={out.category}"
        if i >= 1:                                     # 第一轮之后已经 remember 并 embed_pending 过,库里有可召回的语句
            assert hits, f"turn {i}: recall returned nothing"
    with capsys.disabled():
        for name, xs in took.items():
            print(f"{name}: max {max(xs):.2f}s median {statistics.median(xs):.2f}s")
    assert embedded > 0, "no statement was embedded in ten turns"
    assert max(took["recall"]) <= limit and max(took["embed_pending"]) <= limit
    assert max(took["remember"]) <= 3 * limit          # 三段抽取,每段至多一个 timeout_ms(6.4 节)


def test_recall_brings_back_the_related_sentence(live_port):
    from actio.memory_port import MemoryEvent
    port, _ = live_port
    first = port.remember(MemoryEvent("q#0", "pb：我负责鉴权模块", "pb"))
    second = port.remember(MemoryEvent("q#1", "pb：明天下雨", "pb"))
    assert not first.failed, f"remember q#0 failed, category={first.category}"
    assert not second.failed, f"remember q#1 failed, category={second.category}"
    assert port.embed_pending() >= 1
    hits = port.recall("谁负责鉴权", limit=1)
    # 按 id 判,不按字面:命中渲染的是抽取出的三元组,抽取模型可能把"鉴权"写成 auth。
    assert hits and hits[0].id in first.stored, f"recalled {hits}, expected one of {first.stored}"
