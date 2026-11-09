"""两个部署记忆管理面(`FileTupleStore` 与 `StarlingDeploymentStore`)共用的契约(设计二 4.1、5.3 节,设计三 6.2 节)。

只钉两边最容易分叉的语义:没有库时的读与放弃、未知快照与开着端口时的恢复、按 id 的快照互不覆盖、未知 key 与重复放弃,
按世界组织后的几条:同世界两个主体互不可见而摘要覆盖全部主体、恢复只看本世界的端口、wait_closed,
以及按快照 id 的管理面(40 篇 5.1 节、AC-34):删已删的快照是空操作,重放世界从快照分叉、只读、按完整世界名计端口,
名字形如重放世界的情境 id 经 `open` 仍是部署世界。
两边各自文件里的同义用例不删;这里防的是以后一边改了另一边没改。管理面按世界 id 管库,
这里的 "s1" 是世界,"pa"、"pb" 是世界里的主体。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from simple_actio.memory import FileTupleStore
from simple_actio.memory_port import MemoryEvent, MemoryFailureError, PendingKey, SnapshotRef
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
    roots = {"held_root": tmp_path / "held", "replay_root": tmp_path / "replay"}
    if request.param == "file":
        failing: set[str] = set()
        store = FileTupleStore(root, **roots,
                               extraction_failure=lambda e: "extraction_failed" if e.key in failing else None)
        yield Case(store, root, failing.add)
        return
    pytest.importorskip("starling")
    from simple_actio.memory_starling import StarlingDeploymentStore

    with FakeOpenAI(dim=256) as fake, offline_api_key():
        yield Case(StarlingDeploymentStore(root, **roots, endpoints=fake.endpoints()), root, fake.fail_keys.add)


def _assert_permanent(info):
    assert type(info.value) is MemoryFailureError and info.value.transient is False


def test_without_a_library_reads_give_the_empty_library_and_build_no_file(case):
    store = case.store
    assert store.pending_events("s1") == []
    empty = store.digest("s1")
    assert not case.root.exists()
    assert store.snapshot("s2", note="rise episode e0").digest == empty     # 真空世界的快照摘要就是它


def test_restore_of_an_unknown_snapshot_is_permanent_and_leaves_the_world_alone(case):
    store = case.store
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    before = store.digest("s1")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", "snap-0000000000000000")
    _assert_permanent(info)
    assert store.digest("s1") == before


def test_restore_refused_by_an_open_port_leaves_the_world_alone(case):
    store = case.store
    start = store.snapshot("s1", note="n")
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    changed = store.digest("s1")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", start.snapshot_id)
    _assert_permanent(info)
    assert store.digest("s1") == changed
    port.close()
    assert store.restore("s1", start.snapshot_id) == start.digest == store.digest("s1")


def test_two_snapshots_of_one_world_are_kept_apart(case):
    store = case.store
    first = store.snapshot("s1", note="n")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    second = store.snapshot("s1", note="n")
    assert second.snapshot_id != first.snapshot_id and second.digest != first.digest
    assert store.restore("s1", first.snapshot_id) == first.digest
    assert store.restore("s1", second.snapshot_id) == second.digest == store.digest("s1")


def test_a_snapshot_then_writes_then_restore_gives_back_the_snapshot_digest(case):
    store = case.store
    ref = store.snapshot("s1", note="rise episode e1")
    assert isinstance(ref, SnapshotRef) and ref.digest == store.digest("s1")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert store.digest("s1") != ref.digest
    assert store.restore("s1", ref.snapshot_id) == ref.digest == store.digest("s1")


def test_restore_is_refused_while_a_port_of_the_world_is_open(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    port = store.open("s1", "pb")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", ref.snapshot_id)
    _assert_permanent(info)
    port.close()
    store.restore("s1", ref.snapshot_id)


def test_a_deleted_snapshot_deletes_again_quietly_but_cannot_be_restored(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    store.delete_snapshot("s1", ref.snapshot_id)
    store.delete_snapshot("s1", ref.snapshot_id)            # 服务回 snapshot_not_found,rise 当作成功
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", ref.snapshot_id)
    _assert_permanent(info)


def test_a_replay_world_starts_at_the_snapshot_and_takes_many_read_only_ports(case):
    store = case.store
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="rise verify r1 s1")
    store.remember_event("s1", "pa", "later#0@pa", "pb：后来的", interlocutor="pb")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert name.split(".")[1:4] == ["verify", "r1", "s1"] and len(name.rsplit(".", 1)[1]) == 8
    base, cand = store.open_replay(name, "pa"), store.open_replay(name, "pa")
    assert base.digest() == cand.digest() == ref.digest
    assert store.wait_closed("s1", 0.05) is True            # 重放端口记在重放世界名下,不挡部署世界
    with pytest.raises(MemoryFailureError) as info:
        base.remember(MemoryEvent("x#0@pa", "pb：写不进去", "pb"))
    _assert_permanent(info)
    base.close(); cand.close()
    store.release_replay(name)


def test_a_scenario_named_like_a_replay_world_is_still_a_deploy_world(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    replay = store.open_replay(name, "pa")
    store.remember_event(name, "pa", KEY, TEXT, interlocutor="pb")   # 情境 id 恰好等于重放世界名:写的是部署世界
    assert store.digest(name) != ref.digest and replay.digest() == ref.digest
    port = store.open(name, "pa")
    assert store.wait_closed(name, 0.05) is False           # 部署端口记在 <前缀>.deploy.<name> 下
    port.close()
    assert store.wait_closed(name, 0.05) is True            # 重放端口还开着,记在另一个键下
    replay.close()
    store.release_replay(name)


def test_open_replay_does_not_build_a_world_that_was_never_forked(case):
    with pytest.raises(MemoryFailureError) as info:
        case.store.open_replay("rise.verify.r1.s1.00000000", "pa")
    _assert_permanent(info)


def test_abandoning_an_unknown_key_is_a_permanent_failure(case):
    store = case.store
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pa", KEY)                        # 没有库
    _assert_permanent(info)
    assert not case.root.exists()
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pa", "never-written#0")          # 有库,没有这个 key
    _assert_permanent(info)


def test_abandoning_an_abandoned_event_again_is_a_no_op(case):
    store = case.store
    case.fail(KEY)
    out = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and store.pending_events("s1") == [PendingKey(KEY, "pa")]
    store.abandon_event("s1", "pa", KEY)
    after = store.digest("s1")
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == [] and store.digest("s1") == after


def test_the_same_key_under_two_holders_is_two_events(case):
    store = case.store
    case.fail(KEY)                                          # 两行都留 pending,好列出来
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    store.remember_event("s1", "pb", KEY, "pa：另一句", interlocutor="pa")
    assert store.pending_events("s1") == [PendingKey(KEY, "pa"), PendingKey(KEY, "pb")]


def test_abandoning_under_the_wrong_holder_is_a_permanent_failure(case):
    store = case.store
    case.fail(KEY)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pb", KEY)
    _assert_permanent(info)
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]     # 没被碰
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == []


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


def test_another_worlds_open_port_does_not_block_a_restore(case):
    store = case.store
    ref = store.snapshot("s1", note="n")
    other = store.open("s2", "pa")
    store.restore("s1", ref.snapshot_id)
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
