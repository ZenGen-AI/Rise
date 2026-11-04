"""FileTupleStore(记忆端口的落盘替身与部署管理面)与它复用的 _Ledger 的测试(场景二记忆设计 §4.1)。"""
import json
import threading
from pathlib import Path

import pytest

from actio.errors import ActioDataError
from actio.memory import FileTupleStore, MemoryEntry, TupleMemory, _Ledger, content_id
from actio.memory_port import (
    DeploymentMemoryStore, MemoryBackend, MemoryClosedError, MemoryEvent, MemoryEventConflict,
    MemoryFailureError, RememberOutcome,
)

EV = MemoryEvent("ep-1#0", "pb：我担心预算超支", "pb")


class Flaky:
    """按 key 注入抽取失败;failing 清空后就恢复。"""

    def __init__(self, *keys):
        self.failing = set(keys)

    def __call__(self, event):
        return "timeout" if event.key in self.failing else None


def _rows(root, world="s1"):
    return {r["key"]: r for r in json.loads((root / f"{world}.json").read_text(encoding="utf-8"))["events"]}


# ---------------- _Ledger:FileTupleStore 是它的第一个真实调用方 ----------------


def _ev_row(key, seq, state, ids=(), text="pb：一", interlocutor="pb"):
    return {"key": key, "text": text, "interlocutor": interlocutor, "seq": seq, "state": state,
            "statement_ids": list(ids), "failure_category": None}


def _doc(*rows):
    return {"entries": [], "events": list(rows), "next_seq": max((r["seq"] for r in rows), default=-1) + 1}


@pytest.mark.parametrize("args", [("", "pb：一", "pb"), ("k", "", "pb"), ("k", "pb：一", "")])
def test_ledger_begin_rejects_an_empty_key_text_or_interlocutor(args):
    led = _Ledger()
    with pytest.raises(ActioDataError):
        led.begin(MemoryEvent(*args), holder="pb")
    assert led.events == {} and led.next_seq == 0


def test_ledger_begin_on_a_pending_row_with_a_different_text_conflicts():
    led = _Ledger()
    assert led.begin(MemoryEvent("k", "pb：一", "pb"), holder="pb") is None
    with pytest.raises(MemoryEventConflict):
        led.begin(MemoryEvent("k", "pb：二", "pb"), holder="pb")
    assert led.events["k"]["text"] == "pb：一" and led.next_seq == 1


def test_ledger_commit_refuses_an_abandoned_row_and_an_unknown_key():
    led = _Ledger()
    led.begin(MemoryEvent("k", "pb：一", "pb"), holder="pb")
    led.abandon("k")
    with pytest.raises(MemoryEventConflict):
        led.commit("k")
    assert led.entries == [] and led.events["k"]["state"] == "abandoned"
    with pytest.raises(MemoryFailureError) as info:
        led.commit("nope")
    assert info.value.transient is False and not isinstance(info.value, MemoryEventConflict)


def test_ledger_fail_only_applies_to_a_pending_row():
    led = _Ledger()
    led.begin(MemoryEvent("done", "pb：一", "pb"), holder="pb")
    led.commit("done")
    led.begin(MemoryEvent("gone", "pb：二", "pb"), holder="pb")
    led.abandon("gone")
    for key in ("done", "gone", "nope"):
        with pytest.raises(MemoryFailureError) as info:
            led.fail(key, "timeout")
        assert info.value.transient is False
    assert led.events["done"]["failure_category"] is None and led.events["gone"]["failure_category"] is None


def test_ledger_from_json_rejects_a_duplicate_event_key():
    doc = _doc(_ev_row("k", 0, "pending"), _ev_row("k", 1, "pending"))
    with pytest.raises(ActioDataError, match="duplicate event key"):
        _Ledger.from_json(doc)


@pytest.mark.parametrize("rows, match", [
    ([_ev_row("k", 0, "committed", [])], "statement_ids"),                      # committed 行必须有语句 id
    ([_ev_row("k", 0, "pending", ["mem-x"])], "statement_ids"),                 # pending 行不许有
    ([_ev_row("k", 0, "abandoned", ["mem-x"])], "statement_ids"),               # abandoned 行不许有
    ([_ev_row("a", 0, "pending"), _ev_row("b", 0, "pending")], "duplicate event seq"),  # seq 互不相同
])
def test_ledger_from_json_checks_statement_ids_against_state_and_seq_uniqueness(rows, match):
    with pytest.raises(ActioDataError, match=match):
        _Ledger.from_json(_doc(*rows))


def test_ledger_from_json_still_accepts_a_consistent_ledger():
    led = _Ledger.from_json(_doc(_ev_row("a", 0, "committed", [content_id("pb", "pb：一")]),
                                 _ev_row("b", 1, "pending"), _ev_row("c", 2, "abandoned")))
    assert [led.events[k]["state"] for k in "abc"] == ["committed", "pending", "abandoned"] and led.next_seq == 3


def test_ledger_keeps_its_own_copy_of_the_events_it_is_given():
    src = {"k": _ev_row("k", 0, "committed", ["mem-a"])}
    led = _Ledger(events=src, next_seq=1)
    src["k"]["state"] = "pending"
    src["k"]["statement_ids"].append("mem-b")
    assert led.events["k"]["state"] == "committed" and led.events["k"]["statement_ids"] == ["mem-a"]


def test_ledger_to_json_hands_out_copies():
    led = _Ledger(events={"k": _ev_row("k", 0, "committed", ["mem-a"])}, next_seq=1)
    out = led.to_json()
    out["events"][0]["statement_ids"].append("mem-b")
    out["events"][0]["state"] = "abandoned"
    assert led.events["k"]["statement_ids"] == ["mem-a"] and led.events["k"]["state"] == "committed"


def test_ledger_from_json_does_not_share_rows_with_the_parsed_document():
    doc = json.loads(json.dumps(_doc(_ev_row("k", 0, "committed", ["mem-a"]))))
    led = _Ledger.from_json(doc)
    doc["events"][0]["statement_ids"].append("mem-b")
    assert led.events["k"]["statement_ids"] == ["mem-a"]


# ---------------- _Ledger:条目与事件行带主体 ----------------


def test_ledger_recall_filters_by_holder_and_none_means_no_filter():
    led = _Ledger()
    led.begin(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"), holder="pa")
    led.commit("e#1@pa")
    led.begin(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"), holder="pb")
    led.commit("e#0@pb")
    assert [h.text for h in led.recall("预算", limit=5, holder="pa")] == ["pb: pb：预算砍一半"]
    assert [h.text for h in led.recall("预算", limit=5, holder="pb")] == ["pa: pa：我们先谈预算"]
    assert len(led.recall("预算", limit=5)) == 2                       # holder=None:不过滤
    assert led.recall("预算", limit=5, holder="pc") == []
    assert led.recall("预算", limit=5, holder="") == []                # "" 是一个具体主体,不是"不过滤"


def test_ledger_entries_given_at_construction_belong_to_the_empty_holder():
    entry = MemoryEntry("mem-1", 1, "pb", "预算不够", "2026-01-01T00:00:00Z", 1.0)
    led = _Ledger([entry])
    assert [h.text for h in led.recall("预算", limit=3, holder="")] == ["pb: 预算不够"]
    assert led.recall("预算", limit=3, holder="pa") == []


def test_the_digest_tells_apart_the_same_content_under_two_holders():
    a, b = _Ledger(), _Ledger()
    for led, who in ((a, "pa"), (b, "pb")):
        led.begin(MemoryEvent("k", "pc：预算不够", "pc"), holder=who)
        led.commit("k")
    assert [e.to_dict() for e in a.entries] == [e.to_dict() for e in b.entries]
    assert a.digest() != b.digest()


def test_ledger_to_json_carries_the_holders_and_round_trips():
    led = _Ledger()
    led.begin(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"), holder="pa")
    led.commit("e#1@pa")
    out = led.to_json()
    assert out["events"][0]["holder"] == "pa" and list(out["holders"].values()) == ["pa"]
    clone = _Ledger.from_json(json.loads(json.dumps(out)))
    assert clone.digest() == led.digest()
    assert clone.recall("预算", limit=3, holder="pa") == led.recall("预算", limit=3, holder="pa")
    assert clone.recall("预算", limit=3, holder="pb") == []


def test_a_ledger_written_without_holders_loads_with_every_entry_under_the_empty_holder():
    entry = MemoryEntry("mem-1", 1, "pb", "预算不够", "2026-01-01T00:00:00Z", 1.0).to_dict()
    old = {"entries": [entry], "events": [_ev_row("k", 0, "pending")], "next_seq": 1}   # 没有 holders,行里没有 holder
    led = _Ledger.from_json(old)
    assert led.events["k"]["holder"] == ""
    assert [h.text for h in led.recall("预算", limit=3, holder="")] == ["pb: 预算不够"]


@pytest.mark.parametrize("holders", [[], {"mem-nope": "pa"}, {"mem-1": 3}, {"mem-1": None}])
def test_ledger_from_json_rejects_a_bad_holders_table(holders):
    entry = MemoryEntry("mem-1", 1, "pb", "预算不够", "2026-01-01T00:00:00Z", 1.0).to_dict()
    with pytest.raises(ActioDataError):
        _Ledger.from_json({"entries": [entry], "events": [], "holders": holders, "next_seq": 0})


def test_ledger_begin_rejects_a_non_string_holder():
    led = _Ledger()
    with pytest.raises(ActioDataError):
        led.begin(MemoryEvent("k", "pb：一", "pb"), holder=None)
    assert led.events == {} and led.next_seq == 0


# ---------------- TupleMemory.world:同一账本上换一个主体的端口 ----------------


def test_tuple_memory_world_views_share_one_ledger_but_each_has_its_own_holder_and_closed_state():
    root = TupleMemory()
    pa, pb = root.world("pa"), root.world("pb")
    pa.remember(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"))
    pb.remember(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"))
    assert [h.text for h in pa.recall("预算", limit=5)] == ["pb: pb：预算砍一半"]
    assert [h.text for h in pb.recall("预算", limit=5)] == ["pa: pa：我们先谈预算"]
    assert pa.digest() == pb.digest() == root.digest()                  # 摘要是整个世界的
    pa.close()
    with pytest.raises(MemoryClosedError):
        pa.recall("预算", limit=5)
    assert pb.recall("预算", limit=5) and root.recall("预算", limit=5) == []   # 别的视图不受影响;根端口是主体 ""
    assert root.world("pa").recall("预算", limit=5)                     # 关了的视图不影响世界本身


def test_a_tuple_memory_without_a_holder_still_recalls_the_entries_it_was_built_with():
    entry = MemoryEntry("mem-1", 1, "pb", "预算不够", "2026-01-01T00:00:00Z", 1.0)
    port = TupleMemory((entry,))
    assert [h.text for h in port.recall("预算", limit=3)] == ["pb: 预算不够"]
    assert port.world("pa").recall("预算", limit=3) == []


# ---------------- FileTupleStore:一个世界一份账本,多个主体 ----------------


def test_two_holders_in_one_world_recall_only_their_own(tmp_path):
    store = FileTupleStore(tmp_path)
    a, b = store.open("s1", "pa"), store.open("s1", "pb")
    a.remember(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"))
    b.remember(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"))
    assert [h.text for h in a.recall("预算", limit=5)] and all("砍一半" in h.text for h in a.recall("预算", limit=5))
    assert all("先谈" in h.text for h in b.recall("预算", limit=5))
    assert [h.text for h in b.recall("预算", limit=5)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s1.json"]    # 一个世界一个文件


def test_the_world_digest_covers_every_holder(tmp_path):
    store = FileTupleStore(tmp_path)
    before = store.digest("s1")
    store.remember_event("s1", "pb", "k#1@pb", "pa：你好", interlocutor="pa")
    assert store.digest("s1") != before


def test_restore_is_refused_while_any_holder_port_of_the_world_is_open(tmp_path):
    store = FileTupleStore(tmp_path)
    snap = tmp_path / "snap.json"
    store.backup_to("s1", snap)
    port = store.open("s1", "pb")
    with pytest.raises(MemoryFailureError) as exc:
        store.restore_from("s1", snap)
    assert exc.value.transient is False
    port.close()
    store.restore_from("s1", snap)


def test_another_worlds_open_port_does_not_block_a_restore(tmp_path):
    store = FileTupleStore(tmp_path)
    snap = tmp_path / "snap.json"
    store.backup_to("s1", snap)
    other = store.open("s2", "pa")
    store.restore_from("s1", snap)
    other.close()


def test_wait_closed_returns_when_a_port_closes_on_another_thread(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("s1", "pa")
    threading.Timer(0.1, port.close).start()
    assert store.wait_closed("s1", 2.0) is True
    store.open("s1", "pa")
    assert store.wait_closed("s1", 0.05) is False


def test_wait_closed_is_immediate_for_a_world_with_no_open_port_and_blind_to_other_worlds(tmp_path):
    store = FileTupleStore(tmp_path)
    assert store.wait_closed("s1", 0) is True
    other = store.open("s2", "pa")
    assert store.wait_closed("s1", 0) is True and store.wait_closed("s2", 0) is False
    other.close()
    assert store.wait_closed("s2", 0) is True


def test_wait_closed_needs_every_holder_port_of_the_world_closed(tmp_path):
    store = FileTupleStore(tmp_path)
    first, second = store.open("s1", "pa"), store.open("s1", "pb")
    first.close()
    assert store.wait_closed("s1", 0.05) is False
    second.close()
    assert store.wait_closed("s1", 0.05) is True


@pytest.mark.parametrize("bad", [-1, True, None, "1"])
def test_wait_closed_rejects_a_bad_timeout(tmp_path, bad):
    with pytest.raises(ActioDataError, match="timeout_s"):
        FileTupleStore(tmp_path).wait_closed("s1", bad)


def test_the_open_port_count_survives_closes_from_many_threads(tmp_path):
    store = FileTupleStore(tmp_path)
    snap = tmp_path / "empty.json"
    store.backup_to("s1", snap)
    ports = [store.open("s1", "pa") for _ in range(64)]
    threads = [threading.Thread(target=p.close) for p in ports for _ in range(2)]  # 每个端口从两条线程各关一次
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.wait_closed("s1", 0) is True     # 多条线程关完以后,计数回到 0
    store.restore_from("s1", snap)


def test_concurrent_writes_from_two_holders_of_one_world_are_all_kept(tmp_path):
    # 一个世界的所有主体共用一个文件,端口会在两条线程上用(驱动器线程写智能体位,服务端线程写对手位):
    # 没有锁时,两条线程的"读整份、改、写整份"互相覆盖,丢行,或撞同一个 .tmp 文件
    store = FileTupleStore(tmp_path)
    ports = {who: store.open("s1", who) for who in ("pa", "pb")}
    start, errors = threading.Barrier(len(ports)), []

    def work(who):
        try:
            start.wait()  # 两条线程同时开写,没有错开的起跑
            for i in range(50):
                ports[who].remember(MemoryEvent(f"t#{i}@{who}", f"pc：第{i}句", "pc"))
        except BaseException as exc:  # 线程里的异常不会让测试失败,收起来在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(who,)) for who in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    doc = json.loads((tmp_path / "s1.json").read_text(encoding="utf-8"))
    keys = {f"t#{i}@{who}" for i in range(50) for who in ports}
    committed = {r["key"] for r in doc["events"] if r["state"] == "committed"}
    assert committed | set(store.pending_events("s1")) == keys and len(doc["events"]) == 100
    assert sorted(r["seq"] for r in doc["events"]) == list(range(100)) and doc["next_seq"] == 100
    assert sorted(doc["holders"].values()) == ["pa"] * 50 + ["pb"] * 50
    for port in ports.values():
        port.close()


def test_world_ids_and_holders_are_checked_before_any_path_is_built(tmp_path):
    store = FileTupleStore(tmp_path)
    for bad in ("", "../x", "a/b", ".hidden", "p1\n"):
        with pytest.raises(ActioDataError):
            store.open(bad, "pa")
        with pytest.raises(ActioDataError):
            store.open("s1", bad)


# ---------------- FileTupleStore ----------------

def test_store_and_port_satisfy_their_protocols(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("s1", "pa")
    assert isinstance(store, DeploymentMemoryStore) and isinstance(port, MemoryBackend)
    assert port.digest() == store.digest("s1") == TupleMemory().digest()  # 没有文件就是空库
    port.close()


def test_the_same_events_give_the_same_hits_and_digest_as_tuple_memory(tmp_path):
    port, mem = FileTupleStore(tmp_path).open("s1", "pa"), TupleMemory().world("pa")
    for ev in (EV, MemoryEvent("ep-1#2", "pb：人手也不够", "pb")):
        assert port.remember(ev) == mem.remember(ev)
    assert port.recall("人手不够预算", limit=3) == mem.recall("人手不够预算", limit=3)
    assert port.digest() == mem.digest()
    port.close()


def test_a_failed_extraction_leaves_a_pending_row_with_its_text_and_a_retry_commits_it(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path, extraction_failure=flaky)
    assert store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb") == RememberOutcome((), True, "timeout")
    row = _rows(tmp_path)[EV.key]
    assert (row["state"], row["text"], row["interlocutor"], row["seq"], row["failure_category"]) == (
        "pending", EV.text, "pb", 0, "timeout")
    assert store.pending_events("s1") == [EV.key]
    port = store.open("s1", "pa")
    assert port.recall("预算", limit=3) == []
    flaky.failing.clear()
    out = port.remember(EV)  # 同 key 同文本:pending 行被推进,不撞 EventConflict
    assert out == RememberOutcome((content_id("pb", EV.text),), False, None)
    assert _rows(tmp_path)[EV.key]["state"] == "committed" and _rows(tmp_path)[EV.key]["seq"] == 0
    assert store.pending_events("s1") == []
    before = store.digest("s1")
    assert store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb") == out  # 幂等
    assert store.digest("s1") == before
    port.close()


def test_seq_is_allocated_when_the_row_is_written(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#0"))
    store.remember_event("s1", "pa", "ep-1#0", "pb：一", interlocutor="pb")
    store.remember_event("s1", "pa", "ep-1#2", "pb：二", interlocutor="pb")
    assert {k: r["seq"] for k, r in _rows(tmp_path).items()} == {"ep-1#0": 0, "ep-1#2": 1}
    assert json.loads((tmp_path / "s1.json").read_text(encoding="utf-8"))["next_seq"] == 2


def test_conflicts_and_abandon(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#2"))
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.remember_event("s1", "pa", EV.key, "pb：换了一句", interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("s1", EV.key)                     # committed 行不许放弃
    store.remember_event("s1", "pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")
    store.abandon_event("s1", "ep-1#2")
    assert _rows(tmp_path)["ep-1#2"]["state"] == "abandoned"
    assert store.pending_events("s1") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("s1", "pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")  # 放弃是终态
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "never-seen#0")
    assert info.value.transient is False


def test_an_abandoned_row_counts_only_its_key_and_state_in_the_digest(tmp_path):
    a = FileTupleStore(tmp_path / "a", extraction_failure=lambda e: "timeout")
    b = FileTupleStore(tmp_path / "b", extraction_failure=lambda e: "schema_violation")
    a.remember_event("s1", "pa", "ep-1#0", "pb：一", interlocutor="pb")
    b.remember_event("s1", "pa", "ep-1#0", "pc：完全不同", interlocutor="pc")
    assert a.digest("s1") != b.digest("s1")
    a.abandon_event("s1", "ep-1#0")
    b.abandon_event("s1", "ep-1#0")
    assert a.digest("s1") == b.digest("s1")


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    start = store.digest("s1")
    assert store.backup_to("s1", tmp_path / "start.json") == start
    store.remember_event("s1", "pa", "ep-2#0", "pb：新的一句", interlocutor="pb")
    assert store.digest("s1") != start
    assert store.restore_from("s1", tmp_path / "start.json") == start
    assert store.digest("s1") == start


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.backup_to("s1", tmp_path / "empty.json")
    port = store.open("s1", "pa")
    port.remember(EV)
    before = (tmp_path / "lib" / "s1.json").read_bytes()
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("s1", tmp_path / "empty.json")
    assert info.value.transient is False
    assert (tmp_path / "lib" / "s1.json").read_bytes() == before
    port.close()
    port.close()  # 重复关闭不会把计数减成负数
    assert store.restore_from("s1", tmp_path / "empty.json") == TupleMemory().digest()


def test_a_retry_after_restore_reuses_the_same_key_without_conflict(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path / "lib", extraction_failure=flaky)
    start = store.backup_to("s1", tmp_path / "start.json")
    port = store.open("s1", "pa")
    assert port.remember(EV).failed
    port.close()
    assert store.restore_from("s1", tmp_path / "start.json") == start
    assert store.pending_events("s1") == []
    flaky.failing.clear()
    port = store.open("s1", "pa")
    assert not port.remember(EV).failed
    port.close()


def test_the_port_and_the_management_plane_see_the_same_library(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("s1", "pa")
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")  # 门② 直接写库
    assert [h.id for h in port.recall("预算", limit=3)] == [content_id("pb", EV.text)]
    assert port.digest() == store.digest("s1")
    port.close()


@pytest.mark.parametrize("bad", ["", "a/b", "../pa", ".hidden", "p a"])
def test_world_ids_and_holders_must_be_safe_names(tmp_path, bad):
    with pytest.raises(ActioDataError, match="world_id"):
        FileTupleStore(tmp_path).open(bad, "pa")
    with pytest.raises(ActioDataError, match="holder"):
        FileTupleStore(tmp_path).open("s1", bad)


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path):
    (tmp_path / "s1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MemoryFailureError) as info:
        FileTupleStore(tmp_path).digest("s1")
    assert info.value.transient is False
    (tmp_path / "bad.json").write_text('{"entries": []}', encoding="utf-8")
    with pytest.raises(MemoryFailureError):
        FileTupleStore(tmp_path / "lib").restore_from("s1", tmp_path / "bad.json")


# ---------------- 补充:世界 id 与主体的边界(carry 第 1 项)与其余管理面分支 ----------------

def test_world_ids_and_holders_are_checked_with_fullmatch_and_capped_at_128_characters(tmp_path):
    store = FileTupleStore(tmp_path)
    for bad in ("p1\n", "p" * 129):
        with pytest.raises(ActioDataError, match="world_id"):
            store.open(bad, "pa")
        with pytest.raises(ActioDataError, match="world_id"):
            store.digest(bad)
        with pytest.raises(ActioDataError, match="holder"):
            store.open("s1", bad)
    store.open("p" * 128, "p" * 128).close()  # 128 个字符恰好通过
    assert not any(tmp_path.glob("p1*"))  # 被拒的 id 没有碰磁盘


def test_every_management_call_validates_the_world_id_and_the_holder(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    calls = (
        lambda: store.remember_event("../x", "pa", "k", "pb：一", interlocutor="pb"),
        lambda: store.abandon_event("../x", "k"),
        lambda: store.pending_events("../x"),
        lambda: store.backup_to("../x", tmp_path / "b.json"),
        lambda: store.restore_from("../x", tmp_path / "b.json"),
        lambda: store.wait_closed("../x", 0.01),
    )
    for call in calls:
        with pytest.raises(ActioDataError, match="world_id"):
            call()
    with pytest.raises(ActioDataError, match="holder"):
        store.remember_event("s1", "../x", "k", "pb：一", interlocutor="pb")
    assert not (tmp_path / "b.json").exists() and not (tmp_path / "lib").exists()   # 构造也不建目录(终审 M-1)


def test_a_closed_file_port_raises_closed_on_every_method(tmp_path):
    port = FileTupleStore(tmp_path).open("s1", "pa")
    port.close()
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(EV), port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()


def test_pending_events_come_back_in_seq_order(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=lambda e: "timeout")
    for key in ("z", "a", "m"):
        store.remember_event("s1", "pa", key, f"pb：{key}", interlocutor="pb")
    assert store.pending_events("s1") == ["z", "a", "m"]


def test_a_failed_save_leaves_no_temp_file_and_the_old_library_intact(tmp_path, monkeypatch):
    store = FileTupleStore(tmp_path)
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    before = (tmp_path / "s1.json").read_bytes()

    def boom(self, target):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(MemoryFailureError) as info:
        store.remember_event("s1", "pa", "ep-9#0", "pb：新的", interlocutor="pb")
    assert info.value.transient is False and "disk gone" not in str(info.value)
    assert (tmp_path / "s1.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s1.json"]


def test_a_corrupt_error_message_reports_only_the_exception_class(tmp_path):
    (tmp_path / "s1.json").write_text('{"entries": [], "events": [], "next_seq": "x"}', encoding="utf-8")
    with pytest.raises(MemoryFailureError) as info:
        FileTupleStore(tmp_path).digest("s1")
    assert str(info.value) == "ActioDataError" and info.value.transient is False


def test_restore_of_a_missing_backup_is_a_non_transient_failure_and_leaves_the_library_alone(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    before = (tmp_path / "lib" / "s1.json").read_bytes()
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("s1", tmp_path / "nope.json")
    assert info.value.transient is False
    assert (tmp_path / "lib" / "s1.json").read_bytes() == before


def test_backup_of_a_world_with_no_file_writes_an_empty_ledger(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    assert store.backup_to("s1", tmp_path / "b" / "start.json") == TupleMemory().digest()
    assert json.loads((tmp_path / "b" / "start.json").read_text(encoding="utf-8")) == {
        "entries": [], "events": [], "holders": {}, "next_seq": 0}


def test_a_new_store_on_the_same_root_reads_what_an_older_one_wrote(tmp_path):
    FileTupleStore(tmp_path).remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    again = FileTupleStore(tmp_path)
    assert [h.id for h in again.open("s1", "pa").recall("预算", limit=3)] == [content_id("pb", EV.text)]
    assert again.pending_events("s1") == []


def test_the_pending_row_is_on_disk_before_extraction_runs_and_a_committed_event_skips_extraction(tmp_path):
    seen = []

    def probe(event):
        seen.append(_rows(tmp_path)[event.key]["state"])  # 抽取发生时库里已经有这一行
        return None

    store = FileTupleStore(tmp_path, extraction_failure=probe)
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")
    assert seen == ["pending"] and _rows(tmp_path)[EV.key]["state"] == "committed"
    store.remember_event("s1", "pa", EV.key, EV.text, interlocutor="pb")  # 幂等:不再抽取
    assert seen == ["pending"]


def test_an_empty_field_is_rejected_before_anything_is_written(tmp_path):
    store = FileTupleStore(tmp_path)
    for key, text, who in (("", "pb：一", "pb"), ("k", "", "pb"), ("k", "pb：一", "")):
        with pytest.raises(ActioDataError):
            store.remember_event("s1", "pa", key, text, interlocutor=who)
    assert not (tmp_path / "s1.json").exists()


def test_two_ports_on_one_world_both_have_to_close_before_a_restore(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.backup_to("s1", tmp_path / "start.json")
    first, second = store.open("s1", "pa"), store.open("s1", "pb")
    first.close()
    with pytest.raises(MemoryFailureError):
        store.restore_from("s1", tmp_path / "start.json")
    second.close()
    assert store.restore_from("s1", tmp_path / "start.json") == TupleMemory().digest()


def test_constructing_a_store_touches_no_disk_and_the_first_write_creates_the_root(tmp_path):
    # 终审 M-1:装配在预检之前就构造替身;预检失败(退出码 2)时不能在 .git 里留下空目录
    root = tmp_path / "git" / "rismforge-memory" / "deploy"
    store = FileTupleStore(root)
    assert not (tmp_path / "git").exists()
    assert store.pending_events("s1") == [] and store.digest("s1") == FileTupleStore(tmp_path / "x").digest("s1")
    assert not (tmp_path / "git").exists()                                # 读空库也不建
    store.remember_event("s1", "pa", "k#0", "pb：一", interlocutor="pb")
    assert (root / "s1.json").is_file()
