"""StarlingDeploymentStore:部署管理面走记忆服务(40 篇"接服务后管理面的签名"表、00 篇 `ensure_world` 三步、90 篇 FC-49)。
契约用例(两个后端同义)在 `tests/test_simple_actio_deployment_store_contract.py`;这里测管理面自己的语义:世界名与标签、
创建标记、别的记忆根与非 rise 建的同名世界、按快照 id 的恢复与删除、重放世界、外部服务重启后的旧端口。

模块级共用一个真 starling-serve(数据目录与 HOME 在 tmp 下),每条用例一个世界前缀 `t<n>`,彼此看不见。
外部重启的用例另起一个本条独占的服务。抽取与嵌入走本机回环的假端点。"""
import pytest

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (DeploymentMemoryStore, MemoryBackend, MemoryEvent, MemoryFailureError,
                               PendingKey)
from starling_fakes import FakeOpenAI

pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
from starling_service import (delete_world, deployment_store, held_snapshots, open_session,  # noqa: E402
                              start_service, unique_prefix)

KEY = "r1/deploy-m-s-0#1@pa"                   # 部署 key 带轮次前缀、含 "/",只进事件 key
TEXT = "pb：我担心预算超支"
BAD_NAMES = ["", ".hidden", "a/b", "..", "p" * 129, "p1\n", None, 7]


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


@pytest.fixture
def fake():
    with FakeOpenAI(dim=256) as f:
        yield f


@pytest.fixture
def session(service):
    s = open_session(service)
    yield s
    s.close()


@pytest.fixture
def store_factory(session, fake, tmp_path):
    """`make(prefix=None, root_label=None, marker_dir=None, seed_digest=None)` 返回 `(store, marker_dir, admin)`;
    `admin(op, body)` 直接调会话的管理调用,不经管理面。给了 `seed_digest` 就不建空种子。"""
    def make(prefix=None, root_label=None, marker_dir=None, seed_digest=None):
        marker_dir = marker_dir or tmp_path / f"mem-{unique_prefix()}" / "deploy"
        store = deployment_store(session, fake, marker_dir, prefix=prefix, root_label=root_label,
                                 seed_digest=seed_digest)
        return store, marker_dir, session.admin

    return make


def _deploys(admin, prefix):
    return admin("list_worlds", {"labels": {"repo": prefix, "kind": "deploy"}})["worlds"]


def test_the_store_and_its_ports_satisfy_the_protocols(store_factory):
    store, _, _ = store_factory()
    port = store.open("s1", "pa")
    try:
        assert isinstance(store, DeploymentMemoryStore) and isinstance(port, MemoryBackend)
    finally:
        port.close()


def test_a_deploy_world_is_created_once_with_rise_labels_and_a_creation_marker(store_factory):
    store, marker_dir, admin = store_factory()
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    store.remember_event("s1", "pb", "r1/deploy-m-s-0#2@pb", "pa：先谈预算", interlocutor="pa")
    (item,) = _deploys(admin, store.prefix)
    assert item["name"] == f"{store.prefix}.deploy.s1" and item["labels"]["scenario_id"] == "s1"
    assert item["labels"]["root"] == store.root_label and item["seed"] is False and item["read_only"] is False
    assert (marker_dir / "s1.created").read_text(encoding="utf-8") == f"{store.prefix}.deploy.s1\n"


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_world_ids_and_holders_are_checked_before_any_request(store_factory, bad):
    store, marker_dir, admin = store_factory()
    for call in (lambda: store.open(bad, "pa"), lambda: store.open("s1", bad),
                 lambda: store.remember_event(bad, "pa", KEY, TEXT, interlocutor="pb"),
                 lambda: store.abandon_event("s1", bad, KEY), lambda: store.digest(bad),
                 lambda: store.snapshot(bad, note="n"), lambda: store.open_replay(bad, "pa")):
        with pytest.raises(SimpleActioDataError):
            call()
    assert _deploys(admin, store.prefix) == [] and not marker_dir.exists()


def test_a_same_named_world_from_another_root_is_refused(store_factory):
    prefix = unique_prefix()
    a, _, _ = store_factory(prefix=prefix)
    a.snapshot("s1", note="n")
    b, b_markers, _ = store_factory(prefix=prefix, root_label="0" * 16,      # 同前缀、别的记忆根
                                    seed_digest=a.empty_seed_digest)        # 跳过空种子,只看部署世界
    with pytest.raises(MemoryFailureError, match="belongs to another memory root") as info:
        b.snapshot("s1", note="n")
    assert info.value.transient is False and not (b_markers / "s1.created").exists()


def test_the_empty_seed_of_another_root_is_refused(store_factory, session, fake):
    from factory import starling_names
    from simple_actio.memory_starling import ensure_empty_seed
    from starling_service import fake_memory_config

    a, marker_dir, _ = store_factory()
    settings = starling_names.rise_settings(fake_memory_config(fake, marker_dir.parent))
    with pytest.raises(MemoryFailureError, match=r"seed-empty belongs to another memory root"):
        ensure_empty_seed(session, names=starling_names, prefix=a.prefix, root_label="0" * 16, settings=settings)
    assert ensure_empty_seed(session, names=starling_names, prefix=a.prefix, root_label=a.root_label,
                             settings=settings) == (f"{a.prefix}.seed-empty", a.empty_seed_digest)


def test_a_same_named_world_rise_did_not_create_is_refused(store_factory):
    store, marker_dir, admin = store_factory()
    admin("create_world", {"name": f"{store.prefix}.deploy.s1", "labels": {"repo": store.prefix},
                           "settings": None, "seed": False})                 # 人在看板里建的,没有 kind 标签
    with pytest.raises(MemoryFailureError, match="was not created by rise") as info:
        store.open("s1", "pa")
    assert info.value.transient is False and not (marker_dir / "s1.created").exists()


def test_a_world_rise_created_whose_marker_was_lost_is_checked_and_marked_again(store_factory):
    prefix = unique_prefix()
    a, _, _ = store_factory(prefix=prefix)
    a.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    b, b_markers, _ = store_factory(prefix=prefix, root_label=a.root_label)    # 同一个记忆根,标记目录是空的
    assert b.digest("s1") == a.digest("s1")
    b.open("s1", "pa").close()
    assert (b_markers / "s1.created").is_file()


def test_a_deleted_deploy_world_is_not_rebuilt_silently(store_factory, service):
    store, marker_dir, admin = store_factory()
    store.snapshot("s1", note="n")
    admin("close_world", {"world": f"{store.prefix}.deploy.s1"})
    delete_world(service, f"{store.prefix}.deploy.s1")
    fresh, _, _ = store_factory(prefix=store.prefix, root_label=store.root_label, marker_dir=marker_dir)
    with pytest.raises(MemoryFailureError, match="was deleted") as info:
        fresh.open("s1", "pa")
    assert info.value.transient is False and "s1.created" in str(info.value)
    assert _deploys(admin, store.prefix) == []                             # 没有悄悄重建


def test_reading_an_unknown_world_builds_nothing_and_gives_the_empty_seed_digest(store_factory):
    store, marker_dir, admin = store_factory()
    assert store.digest("s9") == store.empty_seed_digest and store.pending_events("s9") == []
    with pytest.raises(MemoryFailureError) as info:
        store.abandon_event("s9", "pa", KEY)
    assert info.value.transient is False
    assert _deploys(admin, store.prefix) == [] and not (marker_dir / "s9.created").exists()


def test_a_fresh_deploy_world_has_the_empty_seed_digest(store_factory):
    store, _, _ = store_factory()
    assert store.snapshot("s1", note="n").digest == store.empty_seed_digest == store.digest("s1")


def test_a_snapshot_is_a_held_snapshot_on_the_service_with_its_note(store_factory, service):
    store, _, _ = store_factory()
    ref = store.snapshot("s1", note="rise episode e1")
    assert ref.snapshot_id in held_snapshots(service, f"{store.prefix}.deploy.s1")


def test_restore_is_refused_by_the_service_while_a_handle_is_open_and_leaves_the_world_alone(store_factory):
    store, _, _ = store_factory()
    ref = store.snapshot("s1", note="n")
    port = store.open("s1", "pa")
    port.remember(MemoryEvent(KEY, TEXT, "pb"))
    changed = store.digest("s1")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", ref.snapshot_id)
    assert str(info.value) == "starling world_held (WorldError)" and info.value.transient is False
    assert store.digest("s1") == changed
    port.close()
    assert store.restore("s1", ref.snapshot_id) == ref.digest == store.digest("s1")


def test_restoring_an_unknown_snapshot_is_permanent(store_factory):
    store, _, _ = store_factory()
    store.snapshot("s1", note="n")
    with pytest.raises(MemoryFailureError) as info:
        store.restore("s1", "20261110T000000Z-0000")
    assert str(info.value).startswith("starling snapshot_not_found (") and info.value.transient is False


def test_deleting_a_deleted_snapshot_is_a_success_against_the_real_service(store_factory):
    store, _, admin = store_factory()
    ref = store.snapshot("s1", note="n")
    store.delete_snapshot("s1", ref.snapshot_id)
    with pytest.raises(MemoryFailureError) as raw:                       # 服务本身回 snapshot_not_found
        admin("delete_snapshot", {"world": f"{store.prefix}.deploy.s1", "snapshot_id": ref.snapshot_id})
    assert "snapshot_not_found" in str(raw.value)
    store.delete_snapshot("s1", ref.snapshot_id)                         # 管理面当作成功,不抛


def test_open_replay_opens_the_forked_name_as_is_and_open_never_treats_a_name_as_a_replay(store_factory):
    store, marker_dir, admin = store_factory()
    store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    ref = store.snapshot("s1", note="n")
    name = store.fork_replay("s1", ref.snapshot_id, round_id="r1")
    assert name.startswith(f"{store.prefix}.verify.r1.s1.") and len(name.rsplit(".", 1)[1]) == 8
    (item,) = [w for w in admin("list_worlds", {"labels": {"repo": store.prefix, "kind": "verify"}})["worlds"]
               if w["name"] == name]
    assert item["reclaimable"] is True and item["retention_group"] == "r1"
    assert item["labels"] == {"repo": store.prefix, "root": store.root_label, "kind": "verify",
                              "round_id": "r1", "scenario_id": "s1"}
    port = store.open_replay(name, "pa")
    assert port.digest() == ref.digest
    port.close()
    store.open(name, "pa").close()                                       # 情境 id 恰好等于重放世界名:建的是部署世界
    deploys = _deploys(admin, store.prefix)
    assert f"{store.prefix}.deploy.{name}" in {w["name"] for w in deploys} and (marker_dir / f"{name}.created").is_file()
    store.release_replay(name)
    store.release_replay(name)                                           # 关两次不报错


def test_open_replay_does_not_build_a_world_that_was_never_forked(store_factory):
    store, _, admin = store_factory()
    with pytest.raises(MemoryFailureError) as info:
        store.open_replay(f"{store.prefix}.verify.r1.s1.00000000", "pa")
    assert str(info.value).startswith("starling world_not_found (") and info.value.transient is False
    assert admin("list_worlds", {"labels": {"repo": store.prefix}})["worlds"] == [
        w for w in admin("list_worlds", {"labels": {"repo": store.prefix}})["worlds"] if w["seed"]]


def test_a_failed_admission_stays_pending_under_its_holder_and_abandon_closes_it(store_factory, fake):
    store, _, _ = store_factory()
    fake.fail_keys.add(KEY)
    out = store.remember_event("s1", "pa", KEY, TEXT, interlocutor="pb")
    assert out.failed and out.category
    assert store.pending_events("s1") == [PendingKey(KEY, "pa")]
    with pytest.raises(MemoryFailureError):                              # 别的主体名下没有这个事件
        store.abandon_event("s1", "pb", KEY)
    store.abandon_event("s1", "pa", KEY)
    store.abandon_event("s1", "pa", KEY)                                 # 放弃已放弃的是空操作
    assert store.pending_events("s1") == []


def test_ports_close_into_the_count_wait_closed_reads(store_factory):
    store, _, _ = store_factory()
    a, b = store.open("s1", "pa"), store.open("s1", "pb")
    assert store.wait_closed("s1", 0.05) is False
    a.close()
    assert store.wait_closed("s1", 0.05) is False
    b.close()
    assert store.wait_closed("s1", 0.05) is True and store.wait_closed("s2", 0.0) is True


@pytest.mark.parametrize("bad", [-1, True, None, "1"])
def test_wait_closed_rejects_a_bad_timeout(store_factory, bad):
    store, _, _ = store_factory()
    with pytest.raises(SimpleActioDataError):
        store.wait_closed("s1", bad)


def test_constructing_the_store_touches_no_disk_and_opens_no_deploy_world(store_factory):
    store, marker_dir, admin = store_factory()
    assert not marker_dir.exists() and _deploys(admin, store.prefix) == []


# ---------------- 外部服务重启(Review Focus:外部服务重启后的 handle_closed) ----------------

@pytest.fixture
def restartable(tmp_path, fake):
    """本条用例独占的服务:`service.restart()` 在同一端口、同一 token、同一数据目录上重起(外部模式的看板重启)。"""
    proc = start_service(tmp_path / "own-svc")
    sessions = []

    def make():
        s = open_session(proc)
        sessions.append(s)
        return deployment_store(s, fake, tmp_path / "mem" / "deploy"), s, proc

    yield make
    for s in sessions:
        s.close()
    proc.kill()


def test_after_an_external_restart_an_old_port_reads_handle_closed_as_a_lost_session(restartable):
    store, session, service = restartable()
    port = store.open("s1", "pa")
    service.restart()
    with pytest.raises(MemoryFailureError) as info:
        port.recall("预算", limit=3)
    assert info.value.transient is True and session.generation == 1 and session.fatal is None
    again = store.open("s1", "pa")                                       # 下一次需要服务时换会话
    assert session.generation == 2
    again.close(); port.close()                                          # 旧代号的端口当作已关
    assert store.wait_closed("s1", 0.1) is True
