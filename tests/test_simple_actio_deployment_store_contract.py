"""两个部署记忆管理面(`FileTupleStore` 与 `StarlingDeploymentStore`)共用的契约(设计二 4.1、5.3 节,设计三 6.2 节)。

只钉两边最容易分叉的语义:没有库时的读与放弃、坏副本与开着端口时的还原、备份覆盖、未知 key 与重复放弃,
以及按世界组织后的几条:同世界两个主体互不可见而摘要覆盖全部主体、还原只看本世界的端口、wait_closed。
两边各自文件里的同义用例不删;这里防的是以后一边改了另一边没改。管理面按世界 id 管库,
这里的 "s1" 是世界,"pa"、"pb" 是世界里的主体。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from simple_actio.memory import FileTupleStore
from simple_actio.memory_port import MemoryEvent, MemoryFailureError
from starling_fakes import FakeOpenAI, offline_api_key

KEY = "r1/deploy-m-s-0#1"
TEXT = "pb：我担心预算超支"


@dataclass
class Case:
    store: object
    root: Path
    fail: Callable[[str], None]          # 让这个 key 的抽取失败,事件留在 pending


@pytest.fixture(params=["file", "starling"])
def case(request, tmp_path):
    root = tmp_path / "deploy"
    if request.param == "file":
        failing: set[str] = set()
        store = FileTupleStore(root, extraction_failure=lambda e: "extraction_failed" if e.key in failing else None)
        yield Case(store, root, failing.add)
        return
    pytest.importorskip("starling")
    from simple_actio.memory_starling import StarlingDeploymentStore

    with FakeOpenAI(dim=256) as fake, offline_api_key():
        yield Case(StarlingDeploymentStore(root, endpoints=fake.endpoints()), root, fake.fail_keys.add)


def _assert_permanent(info):
    assert type(info.value) is MemoryFailureError and info.value.transient is False


def test_without_a_library_reads_give_the_empty_library_and_build_no_file(case, tmp_path):
    store = case.store
    assert store.pending_events("s1") == []
    empty = store.digest("s1")
    assert not case.root.exists()
    assert store.backup_to("s2", tmp_path / "snap" / "s2.db") == empty    # 真空库的摘要就是它


@pytest.mark.parametrize("body", [b"", b"not a library file\n"], ids=["zero-bytes", "wrong-header"])
def test_restore_refuses_a_bad_copy_and_leaves_the_library_alone(case, tmp_path, body):
    store = case.store
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    before = store.digest("s1")
    bad = tmp_path / "bad.db"
    bad.write_bytes(body)
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("s1", bad)
    _assert_permanent(info)
    assert store.digest("s1") == before and bad.read_bytes() == body


def test_restore_refuses_while_a_port_is_open(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "s1.db"
    start = store.backup_to("s1", snap)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    changed = store.digest("s1")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("s1", snap)
    _assert_permanent(info)
    assert store.digest("s1") == changed
    port.close()
    assert store.restore_from("s1", snap) == start == store.digest("s1")


def test_backup_overwrites_an_existing_copy(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "s1.db"
    first = store.backup_to("s1", snap)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    second = store.backup_to("s1", snap)
    assert second != first and second == store.digest("s1")
    store.restore_from("s1", snap)
    assert store.digest("s1") == second


def test_abandoning_an_unknown_key_is_a_permanent_failure(case):
    store = case.store
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", KEY)                              # 没有库
    _assert_permanent(info)
    assert not case.root.exists()
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "never-written#0")                # 有库,没有这个 key
    _assert_permanent(info)


def test_abandoning_an_abandoned_event_again_is_a_no_op(case):
    store = case.store
    case.fail(KEY)
    out = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and store.pending_events("s1") == [KEY]
    store.abandon_event("s1", KEY)
    after = store.digest("s1")
    store.abandon_event("s1", KEY)
    assert store.pending_events("s1") == [] and store.digest("s1") == after


# ---- 按世界组织:同一世界里的多个主体(设计三 6.2 节) ----

def test_two_holders_in_one_world_recall_only_their_own(case):
    store = case.store
    a, b = store.open("s1", "pa"), store.open("s1", "pb")
    a.remember(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"))
    b.remember(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"))
    a.embed_pending()                                  # 替身是空操作;Starling 一次嵌完整个世界库
    b.embed_pending()
    from_a, from_b = a.recall("预算", limit=5), b.recall("预算", limit=5)
    assert from_a and all("砍一半" in h.text for h in from_a)
    assert from_b and all("先谈" in h.text for h in from_b)
    a.close()
    b.close()


def test_the_world_digest_covers_every_holder(case):
    store = case.store
    before = store.digest("s1")
    store.remember_event("s1", "pb", "k#1@pb", "pa：你好", interlocutor="pa")
    assert store.digest("s1") != before


def test_restore_is_refused_while_any_holder_port_of_the_world_is_open(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "s1.db"
    store.backup_to("s1", snap)
    port = store.open("s1", "pb")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("s1", snap)
    _assert_permanent(info)
    port.close()
    store.restore_from("s1", snap)


def test_another_worlds_open_port_does_not_block_a_restore(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "s1.db"
    store.backup_to("s1", snap)
    other = store.open("s2", "pa")
    store.restore_from("s1", snap)
    other.close()


def test_wait_closed_returns_when_a_port_closes_on_another_thread(case):
    store = case.store
    port = store.open("s1", "pa")
    timer = threading.Timer(0.1, port.close)
    timer.start()
    assert store.wait_closed("s1", 5.0) is True
    timer.join()
    again = store.open("s1", "pa")
    assert store.wait_closed("s1", 0.05) is False
    again.close()
