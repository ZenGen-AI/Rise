import json

import pytest

from controlplane.lock import FileLockBusyError, hold_file_lock
from mindagent.errors import MindAgentDataError
from mindagent.memory_layout import MemoryLayout, SnapshotRecord


def _rec(lay, rid, eid, world, snapshot_id="snap-0"):
    p = lay.snapshot(rid, eid)
    lay.write_record(p, SnapshotRecord(world, snapshot_id, "d" * 64))
    return p


def test_records_follow_the_world_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.world_lock("s1") == tmp_path / "deploy" / "s1.lock"
    assert lay.snapshot("r1", "deploy-m-s1-1") == tmp_path / "snapshots" / "r1" / "deploy-deploy-m-s1-1.rec.json"
    assert lay.verify_snapshot("r1", "s1") == tmp_path / "snapshots" / "r1" / "verify-s1.rec.json"


def test_a_record_round_trips_and_names_its_world(tmp_path):
    lay = MemoryLayout(tmp_path)
    p = _rec(lay, "r1", "deploy-m-s1-1", "s1", "snap-ab")
    assert lay.read_record(p) == SnapshotRecord("s1", "snap-ab", "d" * 64) and lay.world_of(p) == "s1"
    assert not list(p.parent.glob(".*.tmp"))


def test_a_record_is_readable_json_with_exactly_three_keys_and_rewriting_it_replaces_it(tmp_path):
    # 人删孤儿快照前先读记录(50 篇):sort_keys、不转义中文以外的内容,临时文件加改名不留残渣
    lay = MemoryLayout(tmp_path)
    p = _rec(lay, "r1", "deploy-m-s1-1", "s1", "snap-ab")
    assert json.loads(p.read_text(encoding="utf-8")) == {"digest": "d" * 64, "snapshot_id": "snap-ab", "world_id": "s1"}
    assert p.read_text(encoding="utf-8").index('"digest"') < p.read_text(encoding="utf-8").index('"world_id"')
    lay.write_record(p, SnapshotRecord("s2", "snap-cd", "e" * 64))
    assert lay.read_record(p) == SnapshotRecord("s2", "snap-cd", "e" * 64)
    assert sorted(x.name for x in p.parent.iterdir()) == ["deploy-deploy-m-s1-1.rec.json"]


@pytest.mark.parametrize("body", ["", "[]", '{"world_id": "../s1", "snapshot_id": "x", "digest": "d"}',
                                  '{"world_id": "s1", "snapshot_id": "x"}'])
def test_an_unreadable_record_is_a_data_error_naming_it_relative_to_the_root(tmp_path, body):
    lay = MemoryLayout(tmp_path)
    p = lay.snapshot("r1", "deploy-m-s1-1"); p.parent.mkdir(parents=True); p.write_text(body)
    with pytest.raises(MindAgentDataError, match=r"snapshots/r1/deploy-deploy-m-s1-1\.rec\.json") as info:
        lay.read_record(p)
    assert str(tmp_path) not in str(info.value)


@pytest.mark.parametrize("body", [
    b"\xff\xfe",                                                                       # 不是 UTF-8
    b'{"world_id": "s1", "snapshot_id": "x", "digest": "d", "note": "n"}',             # 多一个键
    b'{"world_id": "s1", "snapshot_id": "", "digest": "d"}',
    b'{"world_id": "s1", "snapshot_id": "a/b", "digest": "d"}',                        # 快照 id 不许含 "/"
    b'{"world_id": "s1", "snapshot_id": 7, "digest": "d"}',
    b'{"world_id": "s1", "snapshot_id": "x", "digest": ""}',
    b'{"world_id": "s1\\n", "snapshot_id": "x", "digest": "d"}',
    b'{"world_id": "a b", "snapshot_id": "x", "digest": "d"}',
])
def test_a_record_with_a_bad_field_is_a_data_error_that_does_not_echo_it(tmp_path, body):
    lay = MemoryLayout(tmp_path)
    p = lay.snapshot("r1", "m-s1-1"); p.parent.mkdir(parents=True); p.write_bytes(body)
    with pytest.raises(MindAgentDataError, match=r"snapshots/r1/deploy-m-s1-1\.rec\.json") as info:
        lay.world_of(p)
    assert "a b" not in str(info.value) and "a/b" not in str(info.value)             # 不回显记录内容


def test_a_missing_record_is_a_data_error(tmp_path):
    lay = MemoryLayout(tmp_path)
    with pytest.raises(MindAgentDataError, match="deploy-m-s1-1.rec.json"):
        lay.read_record(lay.snapshot("r1", "m-s1-1"))


@pytest.mark.parametrize("record", [SnapshotRecord("a b", "snap-0", "d"), SnapshotRecord("s1", "", "d"),
                                    SnapshotRecord("s1", "a/b", "d"), SnapshotRecord("s1", "snap-0", "")])
def test_a_record_the_reader_would_refuse_is_never_written(tmp_path, record):
    lay = MemoryLayout(tmp_path)
    with pytest.raises(ValueError):
        lay.write_record(lay.snapshot("r1", "m-s1-1"), record)
    assert not (tmp_path / "snapshots").exists()                       # 校验在落盘之前


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x", ".hidden", "a b", "s1\n", " s1", "s\x001", "x" * 129])
def test_scenario_ids_are_checked_before_they_reach_a_path(tmp_path, bad):
    lay = MemoryLayout(tmp_path)
    snap = lay.snapshot("r", "m-s-1")
    for call in (lambda: lay.world_lock(bad), lambda: lay.verify_snapshot("r", bad),
                 lambda: lay.hold_world(bad).__enter__(),
                 lambda: lay.write_record(snap, SnapshotRecord(bad, "snap-0", "d" * 64)),
                 lambda: lay.uncommitted_snapshots(bad)):
        with pytest.raises(ValueError):
            call()
    assert not (tmp_path / "deploy").exists() and not (tmp_path / "snapshots").exists()


@pytest.mark.parametrize("bad", ["agent-round-v1/deploy-m-s-1", "..", "."])
def test_a_round_prefixed_episode_id_never_becomes_a_file_name(tmp_path, bad):
    with pytest.raises(ValueError):
        MemoryLayout(tmp_path).snapshot("r", bad)


def test_uncommitted_snapshots_are_found_by_the_world_in_the_record_and_skip_verify_records(tmp_path):
    lay = MemoryLayout(tmp_path)
    a = _rec(lay, "r1", "deploy-m-s1-1", "s1")
    _rec(lay, "r2", "deploy-m-s2-1", "s2")
    lay.write_record(lay.verify_snapshot("r1", "s1"), SnapshotRecord("s1", "snap-v", "d" * 64))
    assert lay.uncommitted_snapshots("s1") == [a] and lay.round_snapshots("r1") == [a]
    assert [p.parent.name for p in lay.any_uncommitted_snapshots()] == ["r1", "r2"]


def test_uncommitted_snapshots_go_by_the_record_not_the_file_name(tmp_path):
    lay = MemoryLayout(tmp_path)
    liar = _rec(lay, "r3", "deploy-m-s1-9", "s2")                      # 名字像 s1、记录是 s2:按记录算
    assert lay.uncommitted_snapshots("s2") == [liar] and lay.uncommitted_snapshots("s1") == []
    assert lay.uncommitted_snapshots("s3") == []


def test_only_deploy_rec_json_files_are_in_flight_records(tmp_path):
    lay = MemoryLayout(tmp_path)
    rec = _rec(lay, "r1", "m-s1-1", "s1")
    d = rec.parent
    for name in ("deploy-m-s1-2.json", "deploy-m-s1-3.db", "deploy-m-s1-4.world", ".deploy-m-s1-5.rec.json.tmp"):
        (d / name).write_text("{}")                                    # 旧快照、旧旁注与写到一半的临时文件都不算
    (d / "verify").mkdir()
    (d / "verify" / "deploy-inner.rec.json").write_text("{}")          # 只看轮次目录直属的文件
    (tmp_path / "snapshots" / "deploy-stray.rec.json").write_text("{}")
    assert lay.round_snapshots("r1") == [rec] and lay.any_uncommitted_snapshots() == [rec]


def test_an_unreadable_record_on_any_world_fails_loudly(tmp_path):
    lay = MemoryLayout(tmp_path)
    _rec(lay, "r1", "m-s1-1", "s1").write_text("../x")
    with pytest.raises(MindAgentDataError):
        lay.uncommitted_snapshots("s2")          # 宁可报错也不漏判在途快照
    assert len(lay.any_uncommitted_snapshots()) == 1                   # 门② 照样看得见它,照样延后


def test_lists_survive_missing_directories(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.any_uncommitted_snapshots() == [] and lay.round_snapshots("r1") == []
    assert lay.uncommitted_snapshots("s1") == []
    for backend in ("stand-in", "starling-inprocess", "starling"):
        assert lay.legacy_entries([], backend=backend) == []


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


@pytest.mark.parametrize("backend, legacy", [
    ("stand-in", ["deploy/pa.json", "deploy/s1.db", "deploy/s1.rec.json", "runs", "seeds",
                  "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
    ("starling-inprocess", ["deploy/pa.json", "deploy/s1.json", "deploy/s1.rec.json", "snapshots/r1/deploy-m-1.db",
                            "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
    ("starling", ["deploy/pa.json", "deploy/s1.db", "deploy/s1.json", "deploy/s1.rec.json", "held/s1/snap-1.db",
                  "replay/y.db", "runs", "seeds",
                  "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world"]),
])
def test_the_same_root_is_judged_by_its_backend(tmp_path, backend, legacy):
    for rel in ("deploy/s1.lock", "deploy/pa.json", "deploy/s1.json", "deploy/s1.db", "deploy/s1.rec.json",
                "snapshots/r1/deploy-m-1.db", "snapshots/r1/deploy-m-2.json", "snapshots/r1/deploy-m-3.world",
                "snapshots/r1/deploy-m-4.rec.json", "held/s1/snap-0.json", "replay/x.json",
                "held/s1/snap-1.db", "replay/y.db",                          # 第 2、3 步过渡留下的
                "seeds/k/s1.db", "runs/run-1/e.db", "service.log", ".owner",
                "deploy/s1.db-wal", "deploy/s1.db-shm", "deploy/s1.json.tmp"):   # 不带数据后缀的文件不报
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / rel).write_text("{}")
    assert MemoryLayout(tmp_path).legacy_entries(["s1"], backend=backend) == legacy


def test_a_creation_marker_is_legal_only_for_the_service_backend_and_a_known_scenario(tmp_path):
    (tmp_path / "deploy").mkdir()
    for name in ("s1.created", "s9.created", "s1.created.tmp"):
        (tmp_path / "deploy" / name).write_text("")
    lay = MemoryLayout(tmp_path)
    assert lay.legacy_entries(["s1"], backend="starling") == ["deploy/s9.created"]
    assert lay.legacy_entries(["s1"], backend="stand-in") == ["deploy/s1.created", "deploy/s9.created"]


def test_legacy_entries_ignore_verify_directories_subdirectories_of_deploy_and_service_files(tmp_path):
    lay = MemoryLayout(tmp_path)
    for rel in ("deploy/sub/pb.json", "snapshots/r1/verify/0/deploy-inner.json", "snapshots/r1/verify-s1.rec.json",
                "snapshots/stray.json", "service/x.db", "service.pid", "service.owner.lock", "service.token"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / rel).write_text("{}")
    for backend in ("stand-in", "starling-inprocess", "starling"):
        assert lay.legacy_entries(["s1"], backend=backend) == []
    assert lay.legacy_entries(iter(()), backend="stand-in") == []      # 可迭代对象即可,不要求是列表


def test_legacy_entries_report_seeds_and_runs_once_even_when_they_are_files(tmp_path):
    (tmp_path / "seeds").write_text("")
    (tmp_path / "runs").mkdir(); (tmp_path / "runs" / "a").mkdir(); (tmp_path / "runs" / "a" / "b.db").write_text("")
    lay = MemoryLayout(tmp_path)
    assert lay.legacy_entries([], backend="stand-in") == ["runs", "seeds"]
    assert lay.legacy_entries([], backend="starling-inprocess") == []


def test_an_unknown_backend_is_refused(tmp_path):
    with pytest.raises(ValueError, match="backend"):
        MemoryLayout(tmp_path).legacy_entries([], backend="sqlite")
