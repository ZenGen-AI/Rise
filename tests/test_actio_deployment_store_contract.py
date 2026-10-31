"""两个部署记忆管理面(`FileTupleStore` 与 `StarlingDeploymentStore`)共用的契约(设计二 4.1、5.3 节)。

只钉两边最容易分叉的语义:没有库时的读与放弃、坏副本与开着端口时的还原、备份覆盖、未知 key 与重复放弃。
两边各自文件里的同义用例不删;这里防的是以后一边改了另一边没改。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from actio.memory import FileTupleStore
from actio.memory_port import MemoryEvent, MemoryFailureError
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
    from actio.memory_starling import StarlingDeploymentStore

    with FakeOpenAI(dim=256) as fake, offline_api_key():
        yield Case(StarlingDeploymentStore(root, endpoints=fake.endpoints()), root, fake.fail_keys.add)


def _assert_permanent(info):
    assert type(info.value) is MemoryFailureError and info.value.transient is False


def test_without_a_library_reads_give_the_empty_library_and_build_no_file(case, tmp_path):
    store = case.store
    assert store.pending_events("pa") == []
    empty = store.digest("pa")
    assert not case.root.exists()
    assert store.backup_to("pb", tmp_path / "snap" / "pb.db") == empty    # 真空库的摘要就是它


@pytest.mark.parametrize("body", [b"", b"not a library file\n"], ids=["zero-bytes", "wrong-header"])
def test_restore_refuses_a_bad_copy_and_leaves_the_library_alone(case, tmp_path, body):
    store = case.store
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    before = store.digest("pa")
    bad = tmp_path / "bad.db"
    bad.write_bytes(body)
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", bad)
    _assert_permanent(info)
    assert store.digest("pa") == before and bad.read_bytes() == body


def test_restore_refuses_while_a_port_is_open(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "pa.db"
    start = store.backup_to("pa", snap)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    changed = store.digest("pa")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", snap)
    _assert_permanent(info)
    assert store.digest("pa") == changed
    port.close()
    assert store.restore_from("pa", snap) == start == store.digest("pa")


def test_backup_overwrites_an_existing_copy(case, tmp_path):
    store = case.store
    snap = tmp_path / "snap" / "pa.db"
    first = store.backup_to("pa", snap)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    second = store.backup_to("pa", snap)
    assert second != first and second == store.digest("pa")
    store.restore_from("pa", snap)
    assert store.digest("pa") == second


def test_abandoning_an_unknown_key_is_a_permanent_failure(case):
    store = case.store
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", KEY)                              # 没有库
    _assert_permanent(info)
    assert not case.root.exists()
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", "never-written#0")                # 有库,没有这个 key
    _assert_permanent(info)


def test_abandoning_an_abandoned_event_again_is_a_no_op(case):
    store = case.store
    case.fail(KEY)
    out = store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and store.pending_events("pa") == [KEY]
    store.abandon_event("pa", KEY)
    after = store.digest("pa")
    store.abandon_event("pa", KEY)
    assert store.pending_events("pa") == [] and store.digest("pa") == after
