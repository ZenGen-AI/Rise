"""FileTupleStore(记忆端口的落盘替身与部署管理面)与它复用的 _Ledger 的测试(场景二记忆设计 §4.1)。"""
import json
from pathlib import Path

import pytest

from actio.errors import ActioDataError
from actio.memory import FileTupleStore, TupleMemory, _Ledger, content_id
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


def _rows(root, persona="pa"):
    return {r["key"]: r for r in json.loads((root / f"{persona}.json").read_text(encoding="utf-8"))["events"]}


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
        led.begin(MemoryEvent(*args))
    assert led.events == {} and led.next_seq == 0


def test_ledger_begin_on_a_pending_row_with_a_different_text_conflicts():
    led = _Ledger()
    assert led.begin(MemoryEvent("k", "pb：一", "pb")) is None
    with pytest.raises(MemoryEventConflict):
        led.begin(MemoryEvent("k", "pb：二", "pb"))
    assert led.events["k"]["text"] == "pb：一" and led.next_seq == 1


def test_ledger_commit_refuses_an_abandoned_row_and_an_unknown_key():
    led = _Ledger()
    led.begin(MemoryEvent("k", "pb：一", "pb"))
    led.abandon("k")
    with pytest.raises(MemoryEventConflict):
        led.commit("k")
    assert led.entries == [] and led.events["k"]["state"] == "abandoned"
    with pytest.raises(MemoryFailureError) as info:
        led.commit("nope")
    assert info.value.transient is False and not isinstance(info.value, MemoryEventConflict)


def test_ledger_fail_only_applies_to_a_pending_row():
    led = _Ledger()
    led.begin(MemoryEvent("done", "pb：一", "pb"))
    led.commit("done")
    led.begin(MemoryEvent("gone", "pb：二", "pb"))
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


# ---------------- FileTupleStore ----------------

def test_store_and_port_satisfy_their_protocols(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("pa")
    assert isinstance(store, DeploymentMemoryStore) and isinstance(port, MemoryBackend)
    assert port.digest() == store.digest("pa") == TupleMemory().digest()  # 没有文件就是空库
    port.close()


def test_the_same_events_give_the_same_hits_and_digest_as_tuple_memory(tmp_path):
    port, mem = FileTupleStore(tmp_path).open("pa"), TupleMemory()
    for ev in (EV, MemoryEvent("ep-1#2", "pb：人手也不够", "pb")):
        assert port.remember(ev) == mem.remember(ev)
    assert port.recall("人手不够预算", limit=3) == mem.recall("人手不够预算", limit=3)
    assert port.digest() == mem.digest()
    port.close()


def test_a_failed_extraction_leaves_a_pending_row_with_its_text_and_a_retry_commits_it(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path, extraction_failure=flaky)
    assert store.remember_event("pa", EV.key, EV.text, interlocutor="pb") == RememberOutcome((), True, "timeout")
    row = _rows(tmp_path)[EV.key]
    assert (row["state"], row["text"], row["interlocutor"], row["seq"], row["failure_category"]) == (
        "pending", EV.text, "pb", 0, "timeout")
    assert store.pending_events("pa") == [EV.key]
    port = store.open("pa")
    assert port.recall("预算", limit=3) == []
    flaky.failing.clear()
    out = port.remember(EV)  # 同 key 同文本:pending 行被推进,不撞 EventConflict
    assert out == RememberOutcome((content_id("pb", EV.text),), False, None)
    assert _rows(tmp_path)[EV.key]["state"] == "committed" and _rows(tmp_path)[EV.key]["seq"] == 0
    assert store.pending_events("pa") == []
    before = store.digest("pa")
    assert store.remember_event("pa", EV.key, EV.text, interlocutor="pb") == out  # 幂等
    assert store.digest("pa") == before
    port.close()


def test_seq_is_allocated_when_the_row_is_written(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#0"))
    store.remember_event("pa", "ep-1#0", "pb：一", interlocutor="pb")
    store.remember_event("pa", "ep-1#2", "pb：二", interlocutor="pb")
    assert {k: r["seq"] for k, r in _rows(tmp_path).items()} == {"ep-1#0": 0, "ep-1#2": 1}
    assert json.loads((tmp_path / "pa.json").read_text(encoding="utf-8"))["next_seq"] == 2


def test_conflicts_and_abandon(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=Flaky("ep-1#2"))
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", EV.key, "pb：换了一句", interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("pa", EV.key)                     # committed 行不许放弃
    store.remember_event("pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")
    store.abandon_event("pa", "ep-1#2")
    assert _rows(tmp_path)["ep-1#2"]["state"] == "abandoned"
    assert store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", "ep-1#2", "pb：人手也不够", interlocutor="pb")  # 放弃是终态
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", "never-seen#0")
    assert info.value.transient is False


def test_an_abandoned_row_counts_only_its_key_and_state_in_the_digest(tmp_path):
    a = FileTupleStore(tmp_path / "a", extraction_failure=lambda e: "timeout")
    b = FileTupleStore(tmp_path / "b", extraction_failure=lambda e: "schema_violation")
    a.remember_event("pa", "ep-1#0", "pb：一", interlocutor="pb")
    b.remember_event("pa", "ep-1#0", "pc：完全不同", interlocutor="pc")
    assert a.digest("pa") != b.digest("pa")
    a.abandon_event("pa", "ep-1#0")
    b.abandon_event("pa", "ep-1#0")
    assert a.digest("pa") == b.digest("pa")


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    start = store.digest("pa")
    assert store.backup_to("pa", tmp_path / "start.json") == start
    store.remember_event("pa", "ep-2#0", "pb：新的一句", interlocutor="pb")
    assert store.digest("pa") != start
    assert store.restore_from("pa", tmp_path / "start.json") == start
    assert store.digest("pa") == start


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.backup_to("pa", tmp_path / "empty.json")
    port = store.open("pa")
    port.remember(EV)
    before = (tmp_path / "lib" / "pa.json").read_bytes()
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", tmp_path / "empty.json")
    assert info.value.transient is False
    assert (tmp_path / "lib" / "pa.json").read_bytes() == before
    port.close()
    port.close()  # 重复关闭不会把计数减成负数
    assert store.restore_from("pa", tmp_path / "empty.json") == TupleMemory().digest()
    other = store.open("pb")  # 别的人格开着端口不挡 pa
    store.restore_from("pa", tmp_path / "empty.json")
    other.close()


def test_a_retry_after_restore_reuses_the_same_key_without_conflict(tmp_path):
    flaky = Flaky(EV.key)
    store = FileTupleStore(tmp_path / "lib", extraction_failure=flaky)
    start = store.backup_to("pa", tmp_path / "start.json")
    port = store.open("pa")
    assert port.remember(EV).failed
    port.close()
    assert store.restore_from("pa", tmp_path / "start.json") == start
    assert store.pending_events("pa") == []
    flaky.failing.clear()
    port = store.open("pa")
    assert not port.remember(EV).failed
    port.close()


def test_the_port_and_the_management_plane_see_the_same_library(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("pa")
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")  # 门② 直接写库
    assert [h.id for h in port.recall("预算", limit=3)] == [content_id("pb", EV.text)]
    assert port.digest() == store.digest("pa")
    port.close()


@pytest.mark.parametrize("bad", ["", "a/b", "../pa", ".hidden", "p a"])
def test_persona_ids_must_be_safe_file_names(tmp_path, bad):
    with pytest.raises(ActioDataError, match="persona_id"):
        FileTupleStore(tmp_path).open(bad)


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path):
    (tmp_path / "pa.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MemoryFailureError) as info:
        FileTupleStore(tmp_path).digest("pa")
    assert info.value.transient is False
    (tmp_path / "bad.json").write_text('{"entries": []}', encoding="utf-8")
    with pytest.raises(MemoryFailureError):
        FileTupleStore(tmp_path / "lib").restore_from("pa", tmp_path / "bad.json")


# ---------------- 补充:人格 id 的边界(carry 第 1 项)与其余管理面分支 ----------------

def test_persona_ids_are_checked_with_fullmatch_and_capped_at_128_characters(tmp_path):
    store = FileTupleStore(tmp_path)
    for bad in ("p1\n", "p" * 129):
        with pytest.raises(ActioDataError, match="persona_id"):
            store.open(bad)
        with pytest.raises(ActioDataError, match="persona_id"):
            store.digest(bad)
    store.open("p" * 128).close()  # 128 个字符恰好通过
    assert not any(tmp_path.glob("p1*"))  # 被拒的 id 没有碰磁盘


def test_every_management_call_validates_the_persona_id(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    calls = (
        lambda: store.remember_event("../x", "k", "pb：一", interlocutor="pb"),
        lambda: store.abandon_event("../x", "k"),
        lambda: store.pending_events("../x"),
        lambda: store.backup_to("../x", tmp_path / "b.json"),
        lambda: store.restore_from("../x", tmp_path / "b.json"),
    )
    for call in calls:
        with pytest.raises(ActioDataError, match="persona_id"):
            call()
    assert not (tmp_path / "b.json").exists() and not list((tmp_path / "lib").iterdir())


def test_a_closed_file_port_raises_closed_on_every_method(tmp_path):
    port = FileTupleStore(tmp_path).open("pa")
    port.close()
    for call in (lambda: port.recall("预算", limit=3), lambda: port.remember(EV), port.embed_pending, port.digest):
        with pytest.raises(MemoryClosedError):
            call()


def test_pending_events_come_back_in_seq_order(tmp_path):
    store = FileTupleStore(tmp_path, extraction_failure=lambda e: "timeout")
    for key in ("z", "a", "m"):
        store.remember_event("pa", key, f"pb：{key}", interlocutor="pb")
    assert store.pending_events("pa") == ["z", "a", "m"]


def test_a_failed_save_leaves_no_temp_file_and_the_old_library_intact(tmp_path, monkeypatch):
    store = FileTupleStore(tmp_path)
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    before = (tmp_path / "pa.json").read_bytes()

    def boom(self, target):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(MemoryFailureError) as info:
        store.remember_event("pa", "ep-9#0", "pb：新的", interlocutor="pb")
    assert info.value.transient is False and "disk gone" not in str(info.value)
    assert (tmp_path / "pa.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pa.json"]


def test_a_corrupt_error_message_reports_only_the_exception_class(tmp_path):
    (tmp_path / "pa.json").write_text('{"entries": [], "events": [], "next_seq": "x"}', encoding="utf-8")
    with pytest.raises(MemoryFailureError) as info:
        FileTupleStore(tmp_path).digest("pa")
    assert str(info.value) == "ActioDataError" and info.value.transient is False


def test_restore_of_a_missing_backup_is_a_non_transient_failure_and_leaves_the_library_alone(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    before = (tmp_path / "lib" / "pa.json").read_bytes()
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", tmp_path / "nope.json")
    assert info.value.transient is False
    assert (tmp_path / "lib" / "pa.json").read_bytes() == before


def test_backup_of_a_persona_with_no_file_writes_an_empty_ledger(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    assert store.backup_to("pa", tmp_path / "b" / "start.json") == TupleMemory().digest()
    assert json.loads((tmp_path / "b" / "start.json").read_text(encoding="utf-8")) == {
        "entries": [], "events": [], "next_seq": 0}


def test_a_new_store_on_the_same_root_reads_what_an_older_one_wrote(tmp_path):
    FileTupleStore(tmp_path).remember_event("pa", EV.key, EV.text, interlocutor="pb")
    again = FileTupleStore(tmp_path)
    assert [h.id for h in again.open("pa").recall("预算", limit=3)] == [content_id("pb", EV.text)]
    assert again.pending_events("pa") == []


def test_the_pending_row_is_on_disk_before_extraction_runs_and_a_committed_event_skips_extraction(tmp_path):
    seen = []

    def probe(event):
        seen.append(_rows(tmp_path)[event.key]["state"])  # 抽取发生时库里已经有这一行
        return None

    store = FileTupleStore(tmp_path, extraction_failure=probe)
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")
    assert seen == ["pending"] and _rows(tmp_path)[EV.key]["state"] == "committed"
    store.remember_event("pa", EV.key, EV.text, interlocutor="pb")  # 幂等:不再抽取
    assert seen == ["pending"]


def test_an_empty_field_is_rejected_before_anything_is_written(tmp_path):
    store = FileTupleStore(tmp_path)
    for key, text, who in (("", "pb：一", "pb"), ("k", "", "pb"), ("k", "pb：一", "")):
        with pytest.raises(ActioDataError):
            store.remember_event("pa", key, text, interlocutor=who)
    assert not (tmp_path / "pa.json").exists()


def test_two_ports_on_one_persona_both_have_to_close_before_a_restore(tmp_path):
    store = FileTupleStore(tmp_path / "lib")
    store.backup_to("pa", tmp_path / "start.json")
    first, second = store.open("pa"), store.open("pa")
    first.close()
    with pytest.raises(MemoryFailureError):
        store.restore_from("pa", tmp_path / "start.json")
    second.close()
    assert store.restore_from("pa", tmp_path / "start.json") == TupleMemory().digest()
