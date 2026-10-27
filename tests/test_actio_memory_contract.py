"""记忆端口的共享契约(设计二 4.1 节、M17)。用例只经 MemoryBackend 的五个方法访问实现,
不碰任何实现的内部;计划 B 往 PORT_FACTORIES 加 "starling"(Starling 未装时跳过)。"""
import re
from pathlib import Path

import pytest
import yaml

from actio.memory import FileTupleStore, TupleMemory
from actio.memory_port import MemoryClosedError, MemoryEvent, MemoryEventConflict, MemoryHit

SAMPLE = yaml.safe_load(Path(__file__).with_name("memory_contract_sample.yaml").read_text(encoding="utf-8"))

PORT_FACTORIES = {
    "tuple": lambda fresh_dir: TupleMemory(),
    "file": lambda fresh_dir: FileTupleStore(fresh_dir).open("pa"),
}


@pytest.fixture(params=sorted(PORT_FACTORIES))
def make_port(request, tmp_path):
    made = []

    def make():
        port = PORT_FACTORIES[request.param](tmp_path / f"port-{len(made)}")
        made.append(port)
        return port

    yield make
    for port in made:
        port.close()  # close 可重复调用


def _event(d):
    return MemoryEvent(d["key"], d["text"], d["interlocutor"])


def test_the_sample_gives_the_same_hit_order_and_digest_curve(make_port):
    port = make_port()
    stored: dict[str, tuple[str, ...]] = {}
    assert port.recall(SAMPLE["empty_recall"], limit=3) == []
    prev = port.digest()
    for step in SAMPLE["steps"]:
        event = _event(step["remember"])
        if step.get("raises") == "MemoryEventConflict":
            with pytest.raises(MemoryEventConflict):
                port.remember(event)
        else:
            out = port.remember(event)
            assert (out.failed, out.category) == (False, None) and out.stored
            if step.get("idempotent"):
                assert out.stored == stored[event.key]  # 同 key 同文本:返回首次那批 id
            stored.setdefault(event.key, out.stored)
        assert port.embed_pending() >= 0
        now = port.digest()
        assert (now != prev) == (step["digest"] == "changed"), step
        prev = now
        for q in step["recall"]:
            hits = port.recall(q["query"], limit=q["limit"])
            assert [h.id for h in hits] == [stored[k][0] for k in q["hits"]], q


def test_empty_ports_share_a_fixed_digest_and_recall_nothing(make_port):
    a, b = make_port(), make_port()
    assert re.fullmatch(r"[0-9a-f]{64}", a.digest())
    assert a.digest() == b.digest()
    assert a.recall("预算", limit=3) == []
    assert a.embed_pending() == 0


def test_the_same_event_gets_the_same_content_ids_in_two_ports(make_port):
    a, b = make_port(), make_port()
    first = SAMPLE["steps"][0]["remember"]
    a.remember(_event(first))
    b.remember(_event(first))
    query = SAMPLE["steps"][0]["recall"][0]["query"]
    ha, hb = a.recall(query, limit=3), b.recall(query, limit=3)
    assert ha and [h.id for h in ha] == [h.id for h in hb]
    assert all(isinstance(h, MemoryHit) and re.fullmatch(r"mem-.+", h.id) for h in ha)
    assert len({h.id for h in ha}) == len(ha)


def test_recall_honours_the_limit(make_port):
    port = make_port()
    for step in SAMPLE["steps"][:3]:
        port.remember(_event(step["remember"]))
    query = SAMPLE["steps"][1]["recall"][0]["query"]
    three = port.recall(query, limit=3)
    assert port.recall(query, limit=0) == []
    assert port.recall(query, limit=1) == three[:1]


def test_a_closed_port_raises_closed_on_every_method_and_close_is_idempotent(make_port):
    port = make_port()
    port.close()
    port.close()
    first = _event(SAMPLE["steps"][0]["remember"])
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(first),
                 port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()
