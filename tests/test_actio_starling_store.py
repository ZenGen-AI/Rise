"""StarlingDeploymentStore:部署管理面(设计二 4.1、5.3 节)。契约用例走端口,这里测管理面自己的语义:
人格 id 校验、短命连接、崩溃后重调、备份与还原、摘要不改动被读的文件。"""
import shutil
import sqlite3

import pytest

from actio.errors import ActioDataError
from actio.memory_port import (DeploymentMemoryStore, MemoryBackend, MemoryEvent, MemoryEventConflict,
                               MemoryFailureError)
from starling_fakes import FakeOpenAI, offline_api_key

starling = pytest.importorskip("starling")
from actio.memory_starling import EPOCH, StarlingDeploymentStore  # noqa: E402

KEY = "r1/deploy-m-s-0#1"                      # 部署 key 带轮次前缀、含 "/",只进事件 key
KEY2 = "r1/deploy-m-s-1#1"
TEXT = "pb：我担心预算超支"


@pytest.fixture
def fake():
    with FakeOpenAI(dim=256) as f, offline_api_key():
        yield f


def _store(tmp_path, fake, **kw):
    return StarlingDeploymentStore(tmp_path / "deploy", endpoints=fake.endpoints(), **kw)


def _lib(tmp_path, persona="pa"):
    return tmp_path / "deploy" / f"{persona}.db"


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
    port = store.open("pa")
    assert isinstance(port, MemoryBackend) and _lib(tmp_path).exists()
    assert port.digest() == store.digest("pa")
    port.close()


@pytest.mark.parametrize("bad", ["", "a/b", "../pa", ".hidden", "p a", "pa\n", None])
def test_persona_ids_are_checked_before_any_path_is_built(tmp_path, fake, bad):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    calls = (lambda: store.open(bad), lambda: store.digest(bad), lambda: store.pending_events(bad),
             lambda: store.remember_event(bad, KEY, TEXT, interlocutor="pb"),
             lambda: store.abandon_event(bad, KEY), lambda: store.backup_to(bad, snap),
             lambda: store.restore_from(bad, snap))
    for call in calls:
        with pytest.raises(ActioDataError, match="persona_id"):
            call()
    assert not (tmp_path / "deploy").exists() and not snap.exists()


def test_a_failed_admission_stays_pending_and_abandon_closes_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys.add(KEY)
    out = store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and out.stored == ()
    assert store.pending_events("pa") == [KEY]
    store.abandon_event("pa", KEY)
    assert store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):
        store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", "never-written#0")          # 未知 key:KeyError → 非瞬态
    assert info.value.transient is False


def test_pending_events_come_back_in_the_order_they_were_written(tmp_path, fake):
    store = _store(tmp_path, fake)
    fake.fail_keys = {KEY, KEY2}
    store.remember_event("pa", KEY2, TEXT, interlocutor="pb")
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert store.pending_events("pa") == [KEY2, KEY]


def test_abandoning_a_committed_event_is_a_conflict(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("pa", KEY)


def test_remember_event_after_a_crash_before_phase_three_completes_without_extracting_again(tmp_path, fake):
    """Review Focus:第三段之前崩溃后重调。Starling 侧补齐不抽取(Task 6);这里钉住适配器原样转发,
    重调返回同一批内容 id、库里没有多出语句。"""
    store = _store(tmp_path, fake)
    first = store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert first.stored and not first.failed
    committed = store.digest("pa")
    with sqlite3.connect(_lib(tmp_path)) as c:                # 模拟"语句已落、③ 没写成"
        c.execute("UPDATE events SET state='pending', statement_ids_json='[]' WHERE key=?", (KEY,))
    assert store.pending_events("pa") == [KEY]
    calls = fake.chat_calls
    again = store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert again.stored == first.stored and not again.failed
    assert fake.chat_calls == calls                           # 没有再调抽取
    assert store.pending_events("pa") == []
    assert store.digest("pa") == committed                    # 没有重复语句


def test_reading_an_unknown_persona_builds_no_file_and_gives_the_empty_library_digest(tmp_path, fake):
    store = _store(tmp_path, fake)
    assert store.pending_events("pa") == []
    empty = store.digest("pa")
    assert len(empty) == 64 and not (tmp_path / "deploy").exists()      # 连根目录都不建
    port = store.open("pb")                                             # 真空库的摘要就是它
    assert port.digest() == empty
    port.close()


def test_abandoning_in_an_unknown_persona_is_an_unknown_key_and_builds_no_file(tmp_path, fake):
    store = _store(tmp_path, fake)
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("pa", KEY)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert not (tmp_path / "deploy").exists()


def test_management_calls_share_the_library_with_an_open_port(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    assert store.digest("pa") == port.digest()
    store.remember_event("pa", KEY2, "pb：下周评审", interlocutor="pb")        # 另一条连接写
    assert store.digest("pa") == port.digest()
    port.remember(MemoryEvent("r1/deploy-m-s-2#1", "pb：预算还剩一半", "pb"))   # 端口没被短命连接弄坏
    assert store.digest("pa") == port.digest() and store.pending_events("pa") == []
    port.close()


def test_digests_agree_with_the_port_once_statements_have_been_embedded(tmp_path, fake):
    """端口用真嵌入器写了 statement_vectors 行;管理面的短命连接不配嵌入器,读出来的摘要仍要一致。"""
    store = _store(tmp_path, fake)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    assert port.embed_pending() >= 1
    snap = tmp_path / "snap" / "pa.db"
    assert store.digest("pa") == port.digest() == store.digest_of(_lib(tmp_path))
    assert store.backup_to("pa", snap) == port.digest()
    port.close()


def test_digest_of_counts_the_wal_of_a_library_that_is_still_open(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))                # 不关:写入还在 -wal 里
    lib = _lib(tmp_path)
    wal = lib.with_name("pa.db-wal")
    assert wal.exists() and wal.stat().st_size > 0
    lone = tmp_path / "lone" / "pa.db"
    lone.parent.mkdir()
    shutil.copyfile(lib, lone)                                 # 只拷主文件:摘要漏了 WAL 里的写入
    assert store.digest_of(lone) != port.digest()
    assert store.digest_of(lib) == port.digest()
    port.close()


def test_digest_of_a_missing_library_is_a_non_transient_failure_and_builds_nothing(tmp_path, fake):
    store = _store(tmp_path, fake)
    with pytest.raises(MemoryFailureError, match="no such library") as info:
        store.digest_of(tmp_path / "nope" / "pa.db")
    assert info.value.transient is False and not (tmp_path / "nope").exists()


def test_digest_of_does_not_change_the_file_it_reads_or_leave_files_beside_it(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("pa")
    snap = tmp_path / "snap" / "pa.db"
    _raw_copy(_lib(tmp_path), snap)                            # 单文件、回滚日志模式:Memory.open 会改它的字节
    before = _snapshot(snap.parent)
    assert store.digest_of(snap) == expected
    assert _snapshot(snap.parent) == before                    # 字节没变,旁边没多出 -wal、-shm


def test_digest_of_reads_a_library_in_a_read_only_directory(tmp_path, fake):
    """种子库只读(5.3 节):原地打开会因为建不了 -wal、-shm 而失败。"""
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("pa")
    seed = tmp_path / "seed" / "pa.db"
    _raw_copy(_lib(tmp_path), seed)
    seed.chmod(0o444)
    seed.parent.chmod(0o555)
    try:
        assert store.digest_of(seed) == expected
    finally:
        seed.parent.chmod(0o755)


def test_the_digest_does_not_depend_on_the_agent_the_library_is_opened_with(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    seen = set()
    for agent in ("digest", "pa", "someone-else"):
        copy = tmp_path / agent
        copy.mkdir()
        shutil.copyfile(_lib(tmp_path), copy / "lib.db")
        memory = starling.Memory.open(str(copy / "lib.db"), agent=agent)
        seen.add(memory.digest())
        memory.close(timeout=5)
    assert seen == {store.digest("pa")}


def test_backup_and_restore_return_logical_digests_and_roll_back(tmp_path, fake):
    store = _store(tmp_path, fake)
    before = store.backup_to("pa", tmp_path / "snap" / "pa.db")     # 人格还没有库:备份空库
    assert before == store.digest("pa") == store.digest_of(tmp_path / "snap" / "pa.db")
    assert _lib(tmp_path).exists()                                  # 备份先建了空库
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    assert store.digest("pa") != before
    assert store.restore_from("pa", tmp_path / "snap" / "pa.db") == before == store.digest("pa")
    again = store.remember_event("pa", KEY, TEXT, interlocutor="pb")       # 还原后的库照常可写
    assert again.stored and store.digest("pa") != before


def test_backup_overwrites_an_existing_copy_and_makes_its_parent_directory(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "a" / "b" / "pa.db"
    first = store.backup_to("pa", snap)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    for suffix in ("-wal", "-shm"):                                 # 旧副本留下的:新文件不能认领它们
        snap.with_name(snap.name + suffix).write_bytes(b"stale")
    second = store.backup_to("pa", snap)                            # Starling 拒绝已存在的目标,这里先删
    assert second != first and second == store.digest("pa") == store.digest_of(snap)
    assert _names(snap.parent) == ["pa.db"]


def test_backup_taken_while_a_port_holds_unmerged_writes_carries_them(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    snap = tmp_path / "snap" / "pa.db"
    assert store.backup_to("pa", snap) == port.digest()
    port.close()


def test_restore_refuses_while_a_port_is_open_and_leaves_the_library_alone(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store.backup_to("pa", snap)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    d = store.digest("pa")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", snap)
    assert type(info.value) is MemoryFailureError and info.value.transient is False and store.digest("pa") == d
    port.close()
    assert store.restore_from("pa", snap) != d


def test_restore_is_refused_until_every_port_of_the_persona_is_closed(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store.backup_to("pb", snap)

    def refused():
        with pytest.raises(MemoryFailureError, match="open port"):
            store.restore_from("pa", snap)

    first, second = store.open("pa"), store.open("pa")
    first.close()
    first.close()                                  # 再关是空操作:计数不能被减两次
    refused()
    store.restore_from("pb", snap)                 # 别的人格不受影响
    second.close()
    third = store.open("pa")
    refused()
    third.close()
    store.restore_from("pa", snap)


def test_a_port_that_failed_to_open_does_not_count_as_open(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    store.backup_to("pb", snap)
    _lib(tmp_path).write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError):
        store.open("pa")
    assert store.restore_from("pa", snap) == store.digest("pa")     # 好备份把坏库换掉了


def test_restore_does_not_open_its_source_in_place(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    expected = store.digest("pa")
    snap = tmp_path / "snap" / "pa.db"
    _raw_copy(_lib(tmp_path), snap)
    before = _snapshot(snap.parent)
    assert store.restore_from("pb", snap) == expected == store.digest("pb")
    assert _snapshot(snap.parent) == before


def test_restore_takes_a_source_that_still_has_its_wal(tmp_path, fake):
    store = _store(tmp_path, fake)
    port = store.open("pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    live = port.digest()
    src = tmp_path / "copied"
    src.mkdir()
    shutil.copyfile(_lib(tmp_path), src / "pa.db")
    shutil.copyfile(_lib(tmp_path).with_name("pa.db-wal"), src / "pa.db-wal")
    port.close()
    assert store.restore_from("pb", src / "pa.db") == live == store.digest("pb")


def test_restore_swaps_in_one_file_and_clears_what_was_left_beside_the_library(tmp_path, fake):
    store = _store(tmp_path, fake)
    snap = tmp_path / "snap.db"
    before = store.backup_to("pa", snap)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    lib = _lib(tmp_path)
    for suffix in ("-wal", "-shm", ".restore-partial"):                # 上一次崩溃留下的
        lib.with_name(lib.name + suffix).write_bytes(b"stale")
    assert store.restore_from("pa", snap) == before
    assert _names(lib.parent) == ["pa.db"]
    assert store.digest("pa") == before


def test_a_corrupt_library_or_backup_is_a_non_transient_failure(tmp_path, fake):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    lib = _lib(tmp_path)
    kept = _snapshot(lib.parent)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError) as info:
        store.restore_from("pa", bad)
    assert type(info.value) is MemoryFailureError and info.value.transient is False
    assert _snapshot(lib.parent) == kept                            # 原库不被半截覆盖,旁边也没留文件
    with pytest.raises(MemoryFailureError):
        store.digest_of(bad)


def test_a_corrupt_backup_leaves_no_root_behind_for_a_persona_without_a_library(tmp_path, fake):
    store = _store(tmp_path, fake)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(MemoryFailureError):
        store.restore_from("pa", bad)
    with pytest.raises(MemoryFailureError) as info:                 # 备份文件不存在
        store.restore_from("pa", tmp_path / "missing.db")
    assert info.value.transient is False
    assert not (tmp_path / "deploy").exists()


def test_a_corrupt_library_is_a_non_transient_failure_on_every_management_call(tmp_path, fake):
    store = _store(tmp_path, fake)
    _lib(tmp_path).parent.mkdir()
    _lib(tmp_path).write_bytes(b"not sqlite")
    for call in (lambda: store.digest("pa"), lambda: store.pending_events("pa"),
                 lambda: store.remember_event("pa", KEY, TEXT, interlocutor="pb"),
                 lambda: store.abandon_event("pa", KEY), lambda: store.open("pa")):
        with pytest.raises(MemoryFailureError) as info:
            call()
        assert info.value.transient is False and str(tmp_path) not in str(info.value)


def test_a_root_that_cannot_be_made_is_a_non_transient_failure_without_the_path(tmp_path, fake):
    (tmp_path / "deploy").write_text("a plain file where the root should be", encoding="utf-8")
    store = _store(tmp_path, fake)
    for call in (lambda: store.open("pa"), lambda: store.remember_event("pa", KEY, TEXT, interlocutor="pb"),
                 lambda: store.backup_to("pa", tmp_path / "snap.db")):
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
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    port = store.open("pa")
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
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    store.pending_events("pa")
    store.digest("pa")
    snap = tmp_path / "snap.db"
    store.backup_to("pa", snap)
    store.digest_of(snap)
    store.restore_from("pa", snap)
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("pa", KEY)
    assert len(timeouts) >= 8 and set(timeouts) == {7.5}


def test_calls_that_never_extract_do_not_need_the_api_key(tmp_path, fake, monkeypatch):
    store = _store(tmp_path, fake)
    store.remember_event("pa", KEY, TEXT, interlocutor="pb")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    snap = tmp_path / "snap.db"
    d = store.backup_to("pa", snap)
    assert store.digest("pa") == store.digest_of(snap) == d and store.pending_events("pa") == []
    assert store.restore_from("pb", snap) == d
    with pytest.raises(MemoryEventConflict):
        store.abandon_event("pa", KEY)
