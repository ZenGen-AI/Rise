import pytest

from actio.errors import ActioDataError
from controlplane.lock import FileLockBusyError, hold_file_lock
from mindagent.memory_layout import MemoryLayout


def test_paths_follow_the_design_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.deploy_lock("pa") == tmp_path / "deploy" / "pa.lock"
    assert lay.snapshot("agent-round-zengen-v1", "deploy-zengen-v1-s1-1", "pa") == \
        tmp_path / "snapshots" / "agent-round-zengen-v1" / "deploy-zengen-v1-s1-1-pa.json"
    assert lay.verify_snapshot("agent-round-zengen-v1", "pa") == \
        tmp_path / "snapshots" / "agent-round-zengen-v1" / "verify-pa.json"
    assert MemoryLayout(tmp_path, suffix=".db").snapshot("r", "deploy-m-s-1", "pa").name == "deploy-m-s-1-pa.db"


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "../x", "x" * 129])
def test_persona_ids_are_checked_before_they_reach_a_path(tmp_path, bad):
    with pytest.raises(ActioDataError):
        MemoryLayout(tmp_path).deploy_lock(bad)


@pytest.mark.parametrize("bad", ["agent-round-v1/deploy-m-s-1", "..", "."])
def test_a_round_prefixed_episode_id_never_becomes_a_file_name(tmp_path, bad):
    with pytest.raises(ValueError):
        MemoryLayout(tmp_path).snapshot("r", bad, "pa")


def test_uncommitted_snapshots_ignore_verify_copies_and_other_personas(tmp_path):
    lay = MemoryLayout(tmp_path)
    for p in (lay.snapshot("r1", "deploy-m-s-1", "pa"), lay.snapshot("r2", "deploy-m-s-2", "pb"),
              lay.verify_snapshot("r1", "pa")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    assert lay.uncommitted_snapshots("pa") == [lay.snapshot("r1", "deploy-m-s-1", "pa")]
    assert lay.round_snapshots("r1") == [lay.snapshot("r1", "deploy-m-s-1", "pa")]


def test_the_persona_lock_is_exclusive_and_released(tmp_path):
    lay = MemoryLayout(tmp_path)
    with lay.persona_lock("pa"):
        with pytest.raises(FileLockBusyError, match="pa.lock"):
            with hold_file_lock(lay.deploy_lock("pa")):
                pass
        with lay.persona_lock("pb"):
            pass
    with lay.persona_lock("pa"):
        pass
