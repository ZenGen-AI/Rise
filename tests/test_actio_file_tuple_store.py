"""FileTupleStore(记忆端口的落盘替身与部署管理面)与它复用的 _Ledger 的测试(场景二记忆设计 §4.1)。"""
import json

import pytest

from actio.errors import ActioDataError
from actio.memory import _Ledger, content_id
from actio.memory_port import MemoryEvent, MemoryEventConflict, MemoryFailureError

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
