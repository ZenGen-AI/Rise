import pytest

from controlplane.lock import FileLockBusyError, hold_file_lock
from mindagent.errors import MindAgentDataError
from mindagent.memory_layout import MemoryLayout


def _snap(lay, rid, eid, world=None):
    """造一个起点快照;world 给了就同时写 .world 旁注。"""
    p = lay.snapshot(rid, eid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    if world is not None:
        lay.write_world_tag(p, world)
    return p


def test_paths_follow_the_world_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.world_lock("s1") == tmp_path / "deploy" / "s1.lock"
    assert lay.snapshot("r1", "deploy-m-s1-1") == tmp_path / "snapshots" / "r1" / "deploy-deploy-m-s1-1.json"
    assert lay.verify_snapshot("r1", "s1") == tmp_path / "snapshots" / "r1" / "verify-s1.json"
    assert lay.verify_scratch("r1") == tmp_path / "snapshots" / "r1" / "verify"
    assert MemoryLayout(tmp_path, suffix=".db").snapshot("r", "m-s-1").name == "deploy-m-s-1.db"


def test_the_world_tag_sits_next_to_its_snapshot_with_the_world_suffix(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = lay.snapshot("r1", "deploy-m-s1-1")
    lay.write_world_tag(snap, "s1")           # 先写旁注、后备份:此刻快照还不存在,目录也要建出来
    assert (snap.parent / "deploy-deploy-m-s1-1.world").read_text() == "s1"
    assert lay.world_of(snap) == "s1"
    lay.write_world_tag(snap, "s2")           # 同名重写覆盖,不留临时文件
    assert lay.world_of(snap) == "s2"
    assert sorted(p.name for p in snap.parent.iterdir()) == ["deploy-deploy-m-s1-1.world"]
    db = MemoryLayout(tmp_path, suffix=".db").snapshot("r1", "m-s1-1")
    MemoryLayout(tmp_path, suffix=".db").write_world_tag(db, "s1")
    assert (db.parent / "deploy-m-s1-1.world").is_file()


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x", ".hidden", "a b", "s1\n", " s1", "s\x001", "x" * 129])
def test_scenario_ids_are_checked_before_they_reach_a_path(tmp_path, bad):
    lay = MemoryLayout(tmp_path)
    snap = lay.snapshot("r", "m-s-1")
    for call in (lambda: lay.world_lock(bad), lambda: lay.verify_snapshot("r", bad),
                 lambda: lay.hold_world(bad).__enter__(), lambda: lay.write_world_tag(snap, bad),
                 lambda: lay.uncommitted_snapshots(bad)):
        with pytest.raises(ValueError):
            call()
    assert not (tmp_path / "deploy").exists() and not (tmp_path / "snapshots").exists()


@pytest.mark.parametrize("bad", ["agent-round-v1/deploy-m-s-1", "..", "."])
def test_a_round_prefixed_episode_id_never_becomes_a_file_name(tmp_path, bad):
    with pytest.raises(ValueError):
        MemoryLayout(tmp_path).snapshot("r", bad)


def test_world_of_refuses_a_missing_or_corrupt_tag(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = _snap(lay, "r1", "m-s1-1")
    with pytest.raises(MindAgentDataError, match="deploy-m-s1-1.world"):
        lay.world_of(snap)                                       # 没有旁注
    tag = snap.parent / "deploy-m-s1-1.world"
    for bad in ("", "../x", "a/b", "..", "s1\n\n", " s1", "s1 ", "s\x001", "a b", "s1\r\n", ".hidden"):
        tag.write_bytes(bad.encode())                            # 坏旁注不能变成路径,也不能和世界 id 对不上
        with pytest.raises(MindAgentDataError, match="snapshots/r1/deploy-m-s1-1.world"):
            lay.world_of(snap)
    tag.write_bytes(b"\xff\xfe")
    with pytest.raises(MindAgentDataError, match="snapshots/r1/deploy-m-s1-1.world"):
        lay.world_of(snap)


def test_world_of_errors_name_the_tag_relative_to_the_memory_root(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = _snap(lay, "r1", "m-s1-1")
    (snap.parent / "deploy-m-s1-1.world").write_text("a b")
    with pytest.raises(MindAgentDataError) as exc:
        lay.world_of(snap)
    assert str(tmp_path) not in str(exc.value)


def test_a_hand_written_tag_with_one_trailing_newline_is_accepted(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = _snap(lay, "r1", "m-s1-1")
    (snap.parent / "deploy-m-s1-1.world").write_text("s1\n")     # `echo s1 > x.world` 恢复丢失的旁注
    assert lay.world_of(snap) == "s1"
    assert lay.uncommitted_snapshots("s1") == [snap]
    assert lay.legacy_entries(["s1"]) == []


def test_writing_a_tag_never_adds_a_newline_and_refuses_ids_the_store_would_refuse(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = lay.snapshot("r1", "m-s1-1")
    for bad in ("s1\n", "a b", ".hidden", ""):
        with pytest.raises(ValueError):
            lay.write_world_tag(snap, bad)
    with pytest.raises(ValueError):
        lay.world_lock("a b")
    assert not snap.parent.exists()                              # 校验在落盘之前
    lay.write_world_tag(snap, "s1")
    assert (snap.parent / "deploy-m-s1-1.world").read_bytes() == b"s1"


def test_uncommitted_snapshots_are_found_by_their_world_tag_not_their_name(tmp_path):
    lay = MemoryLayout(tmp_path)
    for rid, eid, sid in (("r1", "deploy-m-s1-1", "s1"), ("r2", "deploy-m-s2-1", "s2")):
        _snap(lay, rid, eid, sid)
    assert [p.parent.name for p in lay.uncommitted_snapshots("s1")] == ["r1"]
    assert [p.parent.name for p in lay.uncommitted_snapshots("s2")] == ["r2"]
    assert lay.uncommitted_snapshots("s3") == []
    # 名字像 s1、旁注是 s2:按旁注算
    liar = _snap(lay, "r3", "m-s1-9", "s2")
    assert liar in lay.uncommitted_snapshots("s2") and liar not in lay.uncommitted_snapshots("s1")


def test_uncommitted_snapshots_skip_untagged_orphan_tags_and_verify_copies(tmp_path):
    lay = MemoryLayout(tmp_path)
    tagged = _snap(lay, "r1", "m-s1-1", "s1")
    _snap(lay, "r1", "m-s1-2")                                   # 无旁注:旧布局遗留,归 legacy_entries
    lay.write_world_tag(lay.snapshot("r1", "m-s1-3"), "s1")      # 只有旁注、快照还没取:不算在途
    lay.verify_snapshot("r1", "s1").write_text("{}")
    assert lay.uncommitted_snapshots("s1") == [tagged]
    assert lay.round_snapshots("r1") == [tagged, lay.snapshot("r1", "m-s1-2")]   # 只列快照,不列 .world


def test_a_corrupt_tag_on_any_snapshot_fails_loudly(tmp_path):
    lay = MemoryLayout(tmp_path)
    snap = _snap(lay, "r1", "m-s1-1", "s1")
    (snap.parent / "deploy-m-s1-1.world").write_text("../x")
    with pytest.raises(MindAgentDataError):
        lay.uncommitted_snapshots("s2")          # 宁可报错也不漏判在途快照


def test_the_world_lock_is_exclusive_and_released(tmp_path):
    lay = MemoryLayout(tmp_path)
    with lay.hold_world("s1"):
        with pytest.raises(FileLockBusyError, match="s1.lock"):
            with hold_file_lock(lay.world_lock("s1")):
                pass
        with lay.hold_world("s2"):
            pass
    with lay.hold_world("s1"):
        pass


def test_any_uncommitted_snapshots_lists_every_worlds_deploy_copies_but_not_verify_or_tags(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.any_uncommitted_snapshots() == []                       # 没有 snapshots/ 目录
    s1 = _snap(lay, "r1", "m-s-1", "s1")
    s2 = _snap(lay, "r2", "m-s-2")                                     # 无旁注也算:门② 仍要延后
    lay.verify_snapshot("r1", "s1").write_text("{}")
    (lay.verify_scratch("r1") / "0").mkdir(parents=True)
    assert lay.any_uncommitted_snapshots() == [s1, s2]


def test_legacy_entries_name_old_persona_files_and_untagged_snapshots(tmp_path):
    lay = MemoryLayout(tmp_path)
    (tmp_path / "deploy").mkdir(); (tmp_path / "deploy" / "pa.json").write_text("{}")
    (tmp_path / "deploy" / "s1.json").write_text("{}")
    old = tmp_path / "snapshots" / "r1" / "deploy-m-s1-1-pa.json"; old.parent.mkdir(parents=True); old.write_text("{}")
    assert lay.legacy_entries(known_scenarios=["s1"]) == ["deploy/pa.json", "snapshots/r1/deploy-m-s1-1-pa.json"]


def test_legacy_entries_ignore_locks_tagged_snapshots_orphan_tags_and_verify_files(tmp_path):
    lay = MemoryLayout(tmp_path)
    deploy = tmp_path / "deploy"; deploy.mkdir()
    for name in ("s1.json", "s1.lock", "pa.lock", "notes.txt", "x.db"):
        (deploy / name).write_text("{}")
    (deploy / "sub").mkdir(); (deploy / "sub" / "pb.json").write_text("{}")     # 只看 deploy/ 直属文件
    _snap(lay, "r1", "m-s1-1", "s1")                                   # 有旁注:新布局
    lay.write_world_tag(lay.snapshot("r1", "m-s1-2"), "s1")            # 孤儿旁注:不是遗留
    lay.verify_snapshot("r1", "s1").write_text("{}")
    (lay.verify_scratch("r1") / "0").mkdir(parents=True)
    (lay.verify_scratch("r1") / "0" / "deploy-inner.json").write_text("{}")
    (tmp_path / "snapshots" / "r2").mkdir(); (tmp_path / "snapshots" / "stray.json").write_text("{}")
    assert lay.legacy_entries(["s1"]) == []
    assert lay.legacy_entries(iter(())) == ["deploy/s1.json"]          # 可迭代对象即可,不要求是列表


def test_legacy_entries_are_sorted_posix_paths_and_survive_missing_directories(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.legacy_entries([]) == []                                # deploy/、snapshots/ 都不存在
    for rid, eid in (("r2", "m-b-1"), ("r1", "m-a-1")):
        _snap(lay, rid, eid)
    assert lay.legacy_entries([]) == ["snapshots/r1/deploy-m-a-1.json", "snapshots/r2/deploy-m-b-1.json"]
    (tmp_path / "deploy").mkdir(); (tmp_path / "deploy" / "pa.json").write_text("{}")
    assert lay.legacy_entries([]) == ["deploy/pa.json", "snapshots/r1/deploy-m-a-1.json",
                                      "snapshots/r2/deploy-m-b-1.json"]


def test_legacy_entries_only_count_files_with_this_layouts_suffix(tmp_path):
    lay = MemoryLayout(tmp_path, suffix=".db")
    deploy = tmp_path / "deploy"; deploy.mkdir()
    (deploy / "pa.db").write_text(""); (deploy / "pa.json").write_text("{}"); (deploy / "s1.db").write_text("")
    d = tmp_path / "snapshots" / "r1"; d.mkdir(parents=True)
    (d / "deploy-m-1.db").write_text(""); (d / "deploy-m-2.json").write_text("{}")
    assert lay.legacy_entries(["s1"]) == ["deploy/pa.db", "snapshots/r1/deploy-m-1.db"]
