"""StarlingDeploymentStore:部署管理面(设计二 4.1、5.3 节,设计三 6.2 节)。契约用例走端口,这里测管理面自己的语义:
世界 id 与主体校验、一个世界一个库、端口以 agent=<主体> 打开、端口计数与 wait_closed、短命连接、崩溃后重调、
备份与还原、摘要不改动被读的文件。按快照 id 的管理面是第 2、3 步的过渡实现,建在私有的 `_backup_to`、`_restore_from`
之上;这两个方法的崩溃点与坏副本用例直接调它们(第 4 步与它们一起删)。"""
import os
import shutil
import sqlite3
import threading

import pytest

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (DeploymentMemoryStore, MemoryBackend, MemoryEvent, MemoryEventConflict,
                               MemoryFailureError, PendingKey)
from starling_fakes import FakeOpenAI, offline_api_key

starling = pytest.importorskip("starling")
from simple_actio.memory_starling import EPOCH, StarlingDeploymentStore, copy_library  # noqa: E402

KEY = "r1/deploy-m-s-0#1"                      # 部署 key 带轮次前缀、含 "/",只进事件 key
KEY2 = "r1/deploy-m-s-1#1"
TEXT = "pb：我担心预算超支"


class _Crash(BaseException):
    """模拟进程在某一步死掉:不是 Exception,所以不会被管理面的失败翻译吞成 MemoryFailureError。"""


@pytest.fixture
def fake():
    with FakeOpenAI(dim=256) as f, offline_api_key():
        yield f


def _store(tmp_path, fake, **kw):
    return StarlingDeploymentStore(tmp_path / "deploy", held_root=tmp_path / "held", replay_root=tmp_path / "replay",
                                   endpoints=fake.endpoints(), **kw)


def _lib(tmp_path, world="s1"):
    return tmp_path / "deploy" / f"{world}.db"


def _names(directory):
    return sorted(p.name for p in directory.iterdir())


def _snapshot(directory):
    """目录里每个文件的名字与字节:证明一次调用没有动过它们。"""
    return {p.name: p.read_bytes() for p in directory.iterdir()}


def _raw_copy(src, dst):
    """直接用 Starling 做单文件副本,不经被测的 store(store.backup_to 末尾会调 digest_of,
    被测实现若原地打开就在这里先改了字节,后面的前后对比就测不到)。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    memory = starling.Memory.open(str(src), agent="raw")
    try:
        memory.backup_to(dst)
    finally:
        memory.close(timeout=10)


def test_the_store_and_its_ports_satisfy_the_protocols_and_live_under_deploy(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert isinstance(store, DeploymentMemoryStore)
    port = store.open("s1", "pa")
    assert isinstance(port, MemoryBackend) and _lib(tmp_path).exists()
    assert port.digest() == store.digest("s1")
    port.close()


BAD_NAMES = ["", "a/b", "../s1", ".hidden", "p a", "s1\n", None]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_world_ids_are_checked_before_any_path_is_built(tmp_path, fake, bad):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    calls = (lambda: store.open(bad, "pa"), lambda: store.digest(bad), lambda: store.pending_events(bad),
             lambda: store.remember_event(bad, "pa", KEY, TEXT, interlocutor="pb"),
             lambda: store.abandon_event(bad, "pa", KEY), lambda: store._backup_to(bad, snap),
             lambda: store._restore_from(bad, snap), lambda: store.wait_closed(bad, 0.01),
             lambda: store.snapshot(bad, note="n"), lambda: store.restore(bad, "snap-0"),
             lambda: store.delete_snapshot(bad, "snap-0"), lambda: store.fork_replay(bad, "snap-0", round_id="r1"))
    for call in calls:
        with pytest.raises(SimpleActioDataError, match="world_id"):
            call()
    assert sorted(p.name for p in tmp_path.iterdir()) == []         # 部署、held、replay 都没建,也没写副本


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_holders_are_checked_before_any_library_is_built(tmp_path, fake, bad):
    store = _store(tmp_path, fake)
    for call in (lambda: store.open("s1", bad),
                 lambda: store.open_replay("rise.verify.r1.s1.00000000", bad),
                 lambda: store.remember_event("s1", bad, KEY, TEXT, interlocutor="pb"),
                 lambda: store.abandon_event("s1", bad, KEY)):
        with pytest.raises(SimpleActioDataError, match="holder"):
            call()
    assert not (tmp_path / "deploy").exists()
    assert store.wait_closed("s1", 0) is True               # 被拒的 open 没有计数


def test_world_ids_and_holders_are_capped_at_128_characters(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.open("w" * 128, "h" * 128).close()                # 128 个字符恰好通过
    for call in (lambda: store.open("w" * 129, "pa"), lambda: store.open("s1", "h" * 129)):
        with pytest.raises(SimpleActioDataError):
            call()
    assert _names(tmp_path / "deploy") == ["w" * 128 + ".db"]


def test_one_world_is_one_library_and_every_holder_writes_into_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    a, b = store.open("s1", "pa"), store.open("s1", "pb")
    a.remember(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"))
    b.remember(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"))
    store.remember_event("s1", "pc", "e#2@pc", "pa：下周评审", interlocutor="pa")
    assert a.digest() == b.digest() == store.digest("s1")
    a.close()
    b.close()
    assert _names(tmp_path / "deploy") == ["s1.db"]         # 一个世界一个库文件,关端口后旁边没有日志
    assert store.pending_events("s1") == []                 # 三个主体的事件都在这一个库的事件表里


def test_ports_and_short_connections_are_opened_with_the_holder_as_agent(tmp_path, fake, monkeypatch):
    agents = []
    original = starling.Memory.open

    def spy(*args, **kwargs):
        agents.append(kwargs.get("agent"))
        return original(*args, **kwargs)

    monkeypatch.setattr(starling.Memory, "open", staticmethod(spy))
    store = _store(tmp_path, fake)
    store.open("s1", "pa").close()
    store.remember_event("s1", "pb", KEY, TEXT, interlocutor="pa")
    assert agents[:2] == ["pa", "pb"]


def test_the_world_digest_covers_every_holder(tmp_path, fake):
    store = _store(tmp_path, fake)
    empty = store.digest("s1")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    one = store.digest("s1")
    store.remember_event("s1", "pb", KEY2, "pa：我们先谈预算", interlocutor="pa")
    assert empty != one != store.digest("s1")


def test_a_failed_admission_stays_pending_and_abandon_closes_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys.add(KEY)
    out = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and out.stored == ()
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pa", "never-written#0")    # 未知 key:KeyError → 非瞬态
    assert info.value.transient is False


def test_abandon_acts_only_on_the_named_holders_event(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys.add(KEY)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pb", KEY)                  # cff319d:别的主体名下的 key 是 KeyError
    assert info.value.transient is False
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]
    store.abandon_event("s1", "pa", KEY)
    assert store.pending_events("s1") == []


def test_pending_events_come_back_in_the_order_they_were_written(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys = {KEY, KEY2}
    store.remember_event("s1", "pa", KEY2, TEXT, interlocutor="pb")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert store.pending_events("s1") == [PendingKey(KEY2, "pa"), PendingKey(KEY, "pa")]


def test_abandoning_a_committed_event_is_a_conflict(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("s1", "pa", KEY)


def test_remember_event_after_a_crash_before_phase_three_completes_without_extracting_again(tmp_path, fake):
    """Review Focus:第三段之前崩溃后重调。Starling 侧补齐不抽取(Task 6);这里钉住适配器原样转发,
    重调返回同一批内容 id、库里没有多出语句。"""
    store = _store(tmp_path, fake)
    first = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert first.stored and not first.failed
    committed = store.digest("s1")
    with sqlite3.connect(_lib(tmp_path)) as c:                # 模拟"语句已落、③ 没写成"
        c.execute("UPDATE events SET state='pending', statement_ids_json='[]' WHERE key=?", (KEY,))
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]
    calls = fake.chat_calls
    again = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert again.stored == first.stored and not again.failed
    assert fake.chat_calls == calls                           # 没有再调抽取
    assert store.pending_events("s1") == []
    assert store.digest("s1") == committed                    # 没有重复语句


def test_reading_an_unknown_world_builds_no_file_and_gives_the_empty_library_digest(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert store.pending_events("s1") == []
    empty = store.digest("s1")
    assert len(empty) == 64 and not (tmp_path / "deploy").exists()      # 连根目录都不建
    port = store.open("s2", "pa")                                       # 真空库的摘要就是它
    assert port.digest() == empty
    port.close()


def test_abandoning_in_an_unknown_world_is_an_unknown_key_and_builds_no_file(tmp_path, fake):
    store = _store(tmp_path, fake)
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s1", "pa", KEY)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert not (tmp_path / "deploy").exists()


def test_management_calls_share_the_library_with_an_open_port(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    assert store.digest("s1") == port.digest()
    store.remember_event("s1", "pa", KEY2, "pb：下周评审", interlocutor="pb")        # 另一条连接写
    assert store.digest("s1") == port.digest()
    port.remember(MemoryEvent("r1/deploy-m-s-2#1", "pb：预算还剩一半", "pb"))   # 端口没被短命连接弄坏
    assert store.digest("s1") == port.digest() and store.pending_events("s1") == []
    port.close()


def test_digests_agree_with_the_port_once_statements_have_been_embedded(tmp_path, fake):
    """端口用真嵌入器写了 statement_vectors 行;管理面的短命连接不配嵌入器,读出来的摘要仍要一致。"""
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    assert port.embed_pending() >= 1
    snap = tmp_path / "snap" / "s1.db"
    assert store.digest("s1") == port.digest() == store.digest_of(_lib(tmp_path))
    assert store._backup_to("s1", snap) == port.digest()
    port.close()


def test_digest_of_counts_the_wal_of_a_library_that_is_still_open(tmp_path, fake):
    """钉的是「端口开着但此刻没在写」:写入还在 -wal 里,所以要连 -wal 一起拷。
    不是说写的同时算也行:digest_of 把主文件与 -wal 分两次拷,在写的库可能拷出对不上的一对(见 digest_of 的 docstring)。"""
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))                # 不关:写入还在 -wal 里,但这次 remember 已返回
    lib = _lib(tmp_path)
    wal = lib.with_name("s1.db-wal")
    assert wal.exists() and wal.stat().st_size > 0
    lone = tmp_path / "lone" / "s1.db"
    lone.parent.mkdir()
    shutil.copyfile(lib, lone)                                 # 只拷主文件:摘要漏了 WAL 里的写入
    assert store.digest_of(lone) != port.digest()
    assert store.digest_of(lib) == port.digest()
    port.close()


def test_digest_of_a_missing_library_is_a_non_transient_failure_and_builds_nothing(tmp_path, fake):
    store = _store(tmp_path, fake)
    with pytest.raises(MemoryFailureError, match="no such library") as info:
        store.digest_of(tmp_path / "nope" / "s1.db")
    assert info.value.transient is False and not (tmp_path / "nope").exists()


def test_digest_of_does_not_change_the_file_it_reads_or_leave_files_beside_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("s1")
    snap = tmp_path / "snap" / "s1.db"
    _raw_copy(_lib(tmp_path), snap)                            # 单文件、回滚日志模式:Memory.open 会改它的字节
    before = _snapshot(snap.parent)
    assert store.digest_of(snap) == expected
    assert _snapshot(snap.parent) == before                    # 字节没变,旁边没多出 -wal、-shm


def test_digest_of_reads_a_library_in_a_read_only_directory(tmp_path, fake):
    """种子库只读(5.3 节):原地打开会因为建不了 -wal、-shm 而失败。"""
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("s1")
    seed = tmp_path / "seed" / "s1.db"
    _raw_copy(_lib(tmp_path), seed)
    seed.chmod(0o444)
    seed.parent.chmod(0o555)
    try:
        assert store.digest_of(seed) == expected
    finally:
        seed.parent.chmod(0o755)


def test_the_digest_does_not_depend_on_the_agent_the_library_is_opened_with(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    seen = set()
    for agent in ("digest", "pa", "someone-else"):
        copy = tmp_path / agent
        copy.mkdir()
        shutil.copyfile(_lib(tmp_path), copy / "lib.db")
        memory = starling.Memory.open(str(copy / "lib.db"), agent=agent)
        seen.add(memory.digest())
        memory.close(timeout=5)
    assert seen == {store.digest("s1")}


def test_the_transitional_layout_keeps_held_and_replay_files_out_of_deploy(tmp_path, fake):
    root = tmp_path / "mem"
    store = StarlingDeploymentStore(root / "deploy", held_root=root / "held", replay_root=root / "replay",
                                    endpoints=fake.endpoints())
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="rise episode e1")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert (root / "held" / "s1" / f"{ref.snapshot_id}.db").is_file()
    assert (root / "replay" / f"{name}.db").is_file()
    assert sorted(p.name for p in (root / "deploy").iterdir()) == ["s1.db"]
    store.release_replay(name)
    store.delete_snapshot("s1", ref.snapshot_id)
    assert not (root / "replay" / f"{name}.db").exists() and not (root / "held" / "s1" / f"{ref.snapshot_id}.db").exists()


def test_constructing_the_store_touches_no_disk(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert store.digest("s1") and store.pending_events("s1") == [] and store.wait_closed("s1", 0) is True
    with pytest.raises(MemoryFailureError):
        store.open_replay("rise.verify.r1.s1.00000000", "pa")
    store.delete_snapshot("s1", "snap-0000000000000000")             # 不在是空操作,也不建目录
    store.release_replay("rise.verify.r1.s1.00000000")
    assert list(tmp_path.iterdir()) == []


def test_a_replay_world_name_has_the_starling_shape_and_a_replay_port_writes_nothing(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="n")
    a, b = (store.fork_replay("s1", ref.snapshot_id, round_id="agent-round-m") for _ in range(2))
    assert a != b and a.startswith("rise.verify.agent-round-m.s1.") and len(a.rsplit(".", 1)[1]) == 8
    port = store.open_replay(a, "pa")
    assert port.recall("预算", limit=3) and port.embed_pending() == 0
    with pytest.raises(MemoryFailureError, match="world_read_only"):
        port.remember(MemoryEvent(KEY2, "pb：下周评审", "pb"))
    assert port.digest() == ref.digest
    port.close()
    store.release_replay(a)
    store.release_replay(a)                                          # 已删是空操作
    assert _names(tmp_path / "replay") == [f"{b}.db"]


def test_an_open_replay_port_does_not_block_restoring_its_deploy_world(tmp_path, fake):
    store = _store(tmp_path, fake)
    ref = store.snapshot("s1", note="n")
    replay = store.open_replay(store.fork_replay("s1", ref.snapshot_id, round_id="r1"), "pa")
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert store.wait_closed("s1", 0) is True
    assert store.restore("s1", ref.snapshot_id) == ref.digest == store.digest("s1")
    replay.close()


def test_a_held_snapshot_that_is_not_a_library_file_is_refused_by_restore_and_fork(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="n")
    before = store.digest("s1")
    (tmp_path / "held" / "s1" / f"{ref.snapshot_id}.db").write_bytes(b"")
    for call in (lambda: store.restore("s1", ref.snapshot_id),
                 lambda: store.fork_replay("s1", ref.snapshot_id, round_id="r1")):
        with pytest.raises(MemoryFailureError, match="not a library file") as info:
            call()
        assert info.value.transient is False
    assert store.digest("s1") == before and not (tmp_path / "replay").exists()


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path, fake):
    store = _store(tmp_path, fake)
    before = store._backup_to("s1", tmp_path / "snap" / "s1.db")     # 世界还没有库:备份空库
    assert before == store.digest("s1") == store.digest_of(tmp_path / "snap" / "s1.db")
    assert _lib(tmp_path).exists()                                  # 备份先建了空库
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert store.digest("s1") != before
    assert store._restore_from("s1", tmp_path / "snap" / "s1.db") == before == store.digest("s1")
    again = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")       # 还原后的库照常可写
    assert again.stored and store.digest("s1") != before


def test_backup_overwrites_an_existing_copy_and_makes_its_parent_directory(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "a" / "b" / "s1.db"
    first = store._backup_to("s1", snap)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    for suffix in ("-wal", "-shm"):                                 # 旧副本留下的:新文件不能认领它们
        snap.with_name(snap.name + suffix).write_bytes(b"stale")
    second = store._backup_to("s1", snap)                            # Starling 拒绝已存在的目标,这里先删
    assert second != first and second == store.digest("s1") == store.digest_of(snap)
    assert _names(snap.parent) == ["s1.db"]


def test_backup_taken_while_a_port_holds_unmerged_writes_carries_them(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    snap = tmp_path / "snap" / "s1.db"
    assert store._backup_to("s1", snap) == port.digest()
    port.close()


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s1", snap)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    d = store.digest("s1")
    with pytest.raises(MemoryFailureError) as info:
        store._restore_from("s1", snap)
    assert type(info.value) is MemoryFailureError and info.value.transient is False and store.digest("s1") == d
    port.close()
    assert store._restore_from("s1", snap) != d


def test_restore_is_refused_until_every_port_of_the_world_is_closed(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s2", snap)

    def refused():
        with pytest.raises(MemoryFailureError, match="open port"):
            store._restore_from("s1", snap)

    first, second = store.open("s1", "pa"), store.open("s1", "pa")
    first.close()
    first.close()                                  # 再关是空操作:计数不能被减两次
    refused()
    store._restore_from("s2", snap)                 # 别的世界不受影响
    second.close()
    third = store.open("s1", "pa")
    refused()
    third.close()
    store._restore_from("s1", snap)


def test_restore_needs_every_holder_port_of_the_world_closed_and_other_worlds_do_not_count(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s1", snap)
    first, second = store.open("s1", "pa"), store.open("s1", "pb")
    other = store.open("s2", "pa")
    first.close()
    with pytest.raises(MemoryFailureError, match="open port"):       # pb 的端口还开着
        store._restore_from("s1", snap)
    second.close()
    store._restore_from("s1", snap)                                   # s2 的端口开着,不挡 s1
    other.close()


def test_wait_closed_returns_when_a_port_closes_on_another_thread(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    timer = threading.Timer(0.1, port.close)
    timer.start()
    assert store.wait_closed("s1", 10.0) is True
    timer.join()
    again = store.open("s1", "pa")
    assert store.wait_closed("s1", 0.05) is False
    again.close()


def test_wait_closed_is_immediate_for_a_world_with_no_open_port_and_blind_to_other_worlds(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert store.wait_closed("s1", 0) is True and not (tmp_path / "deploy").exists()
    other = store.open("s2", "pa")
    assert store.wait_closed("s1", 0) is True and store.wait_closed("s2", 0) is False
    other.close()
    assert store.wait_closed("s2", 0) is True


def test_wait_closed_needs_every_holder_port_of_the_world_closed(tmp_path, fake):
    store = _store(tmp_path, fake)
    first, second = store.open("s1", "pa"), store.open("s1", "pb")
    first.close()
    assert store.wait_closed("s1", 0.05) is False
    second.close()
    assert store.wait_closed("s1", 0.05) is True


@pytest.mark.parametrize("bad", [-1, True, None, "1"])
def test_wait_closed_rejects_a_bad_timeout(tmp_path, fake, bad):
    with pytest.raises(SimpleActioDataError, match="timeout_s"):
        _store(tmp_path, fake).wait_closed("s1", bad)


def test_the_open_port_count_survives_closes_from_many_threads(tmp_path, fake):
    """两条线程同时关同一个端口时,Starling 门面的"已关"判断没有锁,on_close 会被调两次。多减的那次不能
    扣掉同世界别的端口:这里留一个见证端口不关,其余端口各从两条线程关一次,见证端口的计数必须还在。"""
    store = _store(tmp_path, fake)
    snap = tmp_path / "empty.db"
    store._backup_to("s1", snap)
    witness = store.open("s1", "pb")
    ports = [store.open("s1", "pa") for _ in range(8)]
    start = threading.Barrier(len(ports) * 2)

    def close(port):
        start.wait()                                  # 同一个端口的两次关闭尽量同时进去
        try:
            port.close()
        except MemoryFailureError:
            pass                                      # 后到的一次关闭可以报已关;计数只能减一次

    threads = [threading.Thread(target=close, args=(p,)) for p in ports for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.wait_closed("s1", 0.05) is False     # 见证端口还开着:计数没被多减
    with pytest.raises(MemoryFailureError, match="open port"):
        store._restore_from("s1", snap)
    witness.close()
    assert store.wait_closed("s1", 0) is True         # 全关完,计数回到 0
    store._restore_from("s1", snap)


def test_a_port_opened_while_the_restore_prepares_its_copy_stops_the_swap_and_leaves_the_library_alone(
        tmp_path, fake, monkeypatch):
    """restore_from 拷贝与生成 .restore-partial 时不持锁;换文件前在锁里再查一次端口数。准备期间有端口开了,
    就删掉 .restore-partial 后拒绝,库不被换掉。"""
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s1", snap)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    changed = store.digest("s1")
    opened = []
    real = copy_library

    def copy_then_a_port_opens(src, dst):
        real(src, dst)
        opened.append(store.open("s1", "pb"))

    monkeypatch.setattr("simple_actio.memory_starling.copy_library", copy_then_a_port_opens)
    with pytest.raises(MemoryFailureError, match="open port") as info:
        store._restore_from("s1", snap)
    monkeypatch.undo()
    assert info.value.transient is False
    assert not _lib(tmp_path).with_name("s1.db.restore-partial").exists()
    opened[0].close()
    assert store.digest("s1") == changed
    assert _beside_s1(tmp_path) == ["s1.db"]


def test_a_restore_that_starts_while_a_port_is_still_being_opened_is_refused(tmp_path, fake, monkeypatch):
    """open 先计数再开库:开库的这段时间里端口还没返回,并发的 restore_from 就已经看到这个世界有端口,
    不会把正在被打开的文件换掉。"""
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s1", snap)
    seen = []
    original = starling.Memory.open

    def spy(path, *args, **kwargs):
        if str(path) == str(_lib(tmp_path)) and not seen:
            seen.append("restore")
            try:
                store._restore_from("s1", snap)               # 端口还在打开途中
            except MemoryFailureError as exc:
                seen.append(str(exc))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(starling.Memory, "open", staticmethod(spy))
    store.open("s1", "pa").close()
    assert seen == ["restore", "restore: world has an open port"]


def test_a_port_that_failed_to_open_does_not_count_as_open(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store._backup_to("s2", snap)
    _lib(tmp_path).write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError):
        store.open("s1", "pa")
    assert store._restore_from("s1", snap) == store.digest("s1")     # 好备份把坏库换掉了


def test_restore_does_not_open_its_source_in_place(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("s1")
    snap = tmp_path / "snap" / "s1.db"
    _raw_copy(_lib(tmp_path), snap)
    before = _snapshot(snap.parent)
    assert store._restore_from("s2", snap) == expected == store.digest("s2")
    assert _snapshot(snap.parent) == before


def test_restore_takes_a_source_that_still_has_its_wal(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    live = port.digest()
    src = tmp_path / "copied"
    src.mkdir()
    shutil.copyfile(_lib(tmp_path), src / "s1.db")
    shutil.copyfile(_lib(tmp_path).with_name("s1.db-wal"), src / "s1.db-wal")
    port.close()
    assert store._restore_from("s2", src / "s1.db") == live == store.digest("s2")


def test_restore_swaps_in_one_file_and_clears_what_was_left_beside_the_library(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    before = store._backup_to("s1", snap)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    lib = _lib(tmp_path)
    for suffix in ("-wal", "-shm", ".restore-partial"):                # 上一次崩溃留下的
        lib.with_name(lib.name + suffix).write_bytes(b"stale")
    assert store._restore_from("s1", snap) == before
    assert _names(lib.parent) == ["s1.db"]
    assert store.digest("s1") == before


SNAPSHOT_OF = ["s1", "sz"]      # 快照取自本世界的空库,或取自另一个世界的空库


def _library_with_a_stale_wal(tmp_path, store, snapshot_of):
    """造出「旧库的 -wal、-shm 留在库旁」:先备份一个空库当快照;端口写入后不关,把 -wal、-shm 拷出来;
    关端口(WAL 合并、旁边的日志被删);再把拷出来的旧日志放回库旁。
    返回 (快照路径, 快照摘要, 旧库摘要)。

    快照取自哪里,旧日志重放进新库的样子不同:取自本世界自己的库时页布局对不上,开库报
    `malformed database schema`;取自别的库时页布局对得上,重放无声无息,摘要回到还原前的值。"""
    snap = tmp_path / "snap" / "s1.db"
    snap_digest = store._backup_to(snapshot_of, snap)
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    old_digest = port.digest()
    lib, kept = _lib(tmp_path), tmp_path / "kept"
    kept.mkdir()
    for suffix in ("-wal", "-shm"):
        shutil.copyfile(lib.with_name(lib.name + suffix), kept / suffix)
    port.close()
    assert not lib.with_name(lib.name + "-wal").exists()            # 关端口时 WAL 已合并并删掉
    for suffix in ("-wal", "-shm"):
        shutil.copyfile(kept / suffix, lib.with_name(lib.name + suffix))
    assert snap_digest != old_digest
    return snap, snap_digest, old_digest


def _beside_s1(tmp_path):
    """s1 的库文件与它旁边的 -wal、-shm、.restore-partial(别的世界的库不算)。"""
    return [n for n in _names(_lib(tmp_path).parent) if n.startswith("s1.db")]


def _crash_at_replace(monkeypatch, *, after: bool):
    """让 restore_from 里把 .restore-partial 换进库路径的那一次 os.replace 在 after 前或后「死掉」。
    返回一个列表:每次拦到时记下库旁当时还有哪些 -wal、-shm。"""
    real, seen = os.replace, []

    def fake(src, dst, *args, **kwargs):
        if not str(src).endswith(".restore-partial"):
            return real(src, dst, *args, **kwargs)
        seen.append([n for n in (f"{dst}-wal", f"{dst}-shm") if os.path.exists(n)])
        if after:
            real(src, dst, *args, **kwargs)
        raise _Crash()

    monkeypatch.setattr("simple_actio.memory_starling.os.replace", fake)
    return seen


@pytest.mark.parametrize("snapshot_of", SNAPSHOT_OF)
def test_a_crash_right_after_the_swap_leaves_no_old_wal_to_replay_into_the_restored_library(
        tmp_path, fake, monkeypatch, snapshot_of):
    """旧库的 -wal 留在新库旁,下次任何 Memory.open 都会把它重放进新库,回滚就静默失效
    (VACUUM INTO 产出的是回滚日志模式的单文件,header 里没有 WAL 的 salt,挡不住)。
    所以旁边的日志必须在 os.replace 之前删掉,换完之后崩溃,库旁也不剩它们。"""
    store = _store(tmp_path, fake)
    snap, snap_digest, old_digest = _library_with_a_stale_wal(tmp_path, store, snapshot_of)
    _crash_at_replace(monkeypatch, after=True)
    with pytest.raises(_Crash):
        store._restore_from("s1", snap)
    monkeypatch.undo()
    assert store.digest("s1") == snap_digest != old_digest         # 重开后是快照,不是还原前的内容
    assert store._restore_from("s1", snap) == snap_digest           # 重跑回滚照常成功
    assert _beside_s1(tmp_path) == ["s1.db"]


@pytest.mark.parametrize("snapshot_of", SNAPSHOT_OF)
def test_a_crash_after_the_old_wal_is_cleared_but_before_the_swap_leaves_the_old_library_whole(
        tmp_path, fake, monkeypatch, snapshot_of):
    store = _store(tmp_path, fake)
    snap, snap_digest, old_digest = _library_with_a_stale_wal(tmp_path, store, snapshot_of)
    seen = _crash_at_replace(monkeypatch, after=False)
    with pytest.raises(_Crash):
        store._restore_from("s1", snap)
    monkeypatch.undo()
    assert seen == [[]]                                            # 换之前库旁已经没有 -wal、-shm
    assert store.digest("s1") == old_digest                        # 旧库仍完整可开
    assert store._restore_from("s1", snap) == snap_digest == store.digest("s1")     # 重跑回滚成功
    assert _beside_s1(tmp_path) == ["s1.db"]


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    lib = _lib(tmp_path)
    kept = _snapshot(lib.parent)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError) as info:
        store._restore_from("s1", bad)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert _snapshot(lib.parent) == kept                            # 原库不被半截覆盖,旁边也没留文件
    with pytest.raises(MemoryFailureError):
        store.digest_of(bad)


# SQLite 把已存在的 0 字节文件当成合法的空数据库:磁盘满、写副本时被 SIGKILL、手工 touch 都会造出一个。
# 不先校验,restore_from 会把世界库清成空库,而 rounds 的回滚接着 unlink 副本,原记忆就找不回来。
_NOT_A_LIBRARY = [
    pytest.param(b"", id="zero-bytes"),
    pytest.param(b"x" * 100, id="wrong-header-100-bytes"),
    pytest.param(b"SQLite format 2\x00" + b"\x00" * 100, id="wrong-version-header"),
    pytest.param(b"SQLite format 3", id="header-cut-short"),       # 15 字节:少了结尾的 NUL
]


@pytest.mark.parametrize("content", _NOT_A_LIBRARY)
def test_a_source_that_is_not_a_library_file_is_refused_before_the_library_or_root_is_touched(
        tmp_path, fake, content):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    before = store.digest("s1")
    kept = _snapshot(_lib(tmp_path).parent)
    bad = tmp_path / "bad.db"
    bad.write_bytes(content)
    for method, call in (("restore", lambda: store._restore_from("s1", bad)),
                         ("digest_of", lambda: store.digest_of(bad))):
        with pytest.raises(MemoryFailureError, match=f"{method}: not a library file") as info:
            call()
        assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert store.digest("s1") == before != store.digest_of(_raw_empty(tmp_path))     # 世界库没被清空
    assert _snapshot(_lib(tmp_path).parent) == kept
    assert bad.read_bytes() == content                                # 源文件也没被动


@pytest.mark.parametrize("content", _NOT_A_LIBRARY)
def test_a_source_that_is_not_a_library_file_builds_no_root_for_a_world_without_a_library(
        tmp_path, fake, content):
    store = _store(tmp_path, fake)
    bad = tmp_path / "bad.db"
    bad.write_bytes(content)
    with pytest.raises(MemoryFailureError, match="restore: not a library file"):
        store._restore_from("s1", bad)
    assert not (tmp_path / "deploy").exists()


def _raw_empty(tmp_path):
    """一个真空库的副本,只用来拿空库摘要,证明「世界库没被清空」这句断言有分辨力。"""
    empty = tmp_path / "raw-empty" / "empty.db"
    empty.parent.mkdir(exist_ok=True)
    memory = starling.Memory.open(str(tmp_path / "raw-empty" / "src.db"), agent="raw")
    try:
        memory.backup_to(empty)
    finally:
        memory.close(timeout=10)
    return empty


def test_a_corrupt_backup_leaves_no_root_behind_for_a_world_without_a_library(tmp_path, fake):
    store = _store(tmp_path, fake)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError):
        store._restore_from("s1", bad)
    with pytest.raises(MemoryFailureError) as info:                 # 备份文件不存在
        store._restore_from("s1", tmp_path / "missing.db")
    assert info.value.transient is False
    assert not (tmp_path / "deploy").exists()


def test_a_corrupt_library_is_a_non_transient_failure_on_every_management_call(tmp_path, fake):
    store = _store(tmp_path, fake)
    _lib(tmp_path).parent.mkdir()
    _lib(tmp_path).write_bytes(b"not sqlite")
    for call in (lambda: store.digest("s1"), lambda: store.pending_events("s1"),
                 lambda: store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb"),
                 lambda: store.abandon_event("s1", "pa", KEY), lambda: store.open("s1", "pa")):
        with pytest.raises(MemoryFailureError) as info:
            call()
        assert info.value.transient is False and str(tmp_path) not in str(info.value)


def test_a_root_that_cannot_be_made_is_a_non_transient_failure_without_the_path(tmp_path, fake):
    (tmp_path / "deploy").write_text("a plain file where the root should be", encoding="utf-8")
    store = _store(tmp_path, fake)
    for call in (lambda: store.open("s1", "pa"), lambda: store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb"),
                 lambda: store._backup_to("s1", tmp_path / "snap.db")):
        with pytest.raises(MemoryFailureError) as info:
            call()
        assert type(info.value) is MemoryFailureError and info.value.transient is False
        assert str(tmp_path) not in str(info.value)


def test_the_clock_origin_is_passed_explicitly_by_the_port_and_by_the_store(tmp_path, fake, monkeypatch):
    seen = []
    original = starling.Memory.remember_event

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("epoch", "<default>"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(starling.Memory, "remember_event", spy)
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY2, "pb：下周评审", "pb"))
    port.close()
    assert seen == [EPOCH, EPOCH]


def test_every_short_lived_connection_is_closed_with_the_store_timeout(tmp_path, fake, monkeypatch):
    timeouts = []
    original = starling.Memory.close

    def spy(self, timeout=None):
        timeouts.append(timeout)
        return original(self, timeout=timeout)

    monkeypatch.setattr(starling.Memory, "close", spy)
    store = _store(tmp_path, fake, close_timeout_s=7.5)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    store.pending_events("s1")
    store.digest("s1")
    snap = tmp_path / "snap.db"
    store._backup_to("s1", snap)
    store.digest_of(snap)
    store._restore_from("s1", snap)
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("s1", "pa", KEY)
    assert len(timeouts) >= 8 and set(timeouts) == {7.5}


def test_calls_that_never_extract_do_not_need_the_api_key(tmp_path, fake, monkeypatch):
    store = _store(tmp_path, fake)
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    snap = tmp_path / "snap.db"
    d = store._backup_to("s1", snap)
    assert store.digest("s1") == store.digest_of(snap) == d and store.pending_events("s1") == []
    assert store._restore_from("s2", snap) == d
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("s1", "pa", KEY)
