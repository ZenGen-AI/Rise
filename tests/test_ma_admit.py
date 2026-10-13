import pytest

from actio.memory import MemoryEntry
from actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT, failure_record_file, library_state_file, memory_file, proposal_file, run_dir, skill_file,
)
from controlplane.reconcile import Action
from mindagent.admit import handle_admit_proposal
from mindagent.library import load_library

PID = "proposal-agent-round-zengen-v3-1"
SKILL = {"id": "skill-candidate-emotion_high", "version": 1, "level": "micro", "priority": 100,
         "trigger": {"signal": "emotion_high"}, "policy": {"instructions": ["先回应情绪"]}, "description": "d"}
MEMORY = {"id": "mem-candidate", "version": 1, "subject": "pb", "content": "对方住在北京",
          "timestamp": "2026-10-01T00:00:00Z", "confidence": 0.8, "source_episode": "deploy-zengen-v3-s1-1"}


class Crash(Exception):
    pass


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    (s.repo_root / run_dir("run-003")).mkdir(parents=True)  # 提案所属的 run(产生绑定模型的那个)
    return s


def _put(store, pid=PID, status="approved", increments=None, run_id="run-003"):
    env = Envelope(
        id=pid, type="skill_memory_proposal", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1, links=[],
        payload={"round_id": "agent-round-zengen-v3", "run_id": run_id,
                 "increments": increments if increments is not None else [{"kind": "skill", "definition": SKILL}],
                 "verification": {"method": "replay_behavior"}},
    )
    store.write_product(proposal_file(pid), env, "test: proposal")


def _admit(store, pid=PID):
    handle_admit_proposal(store, Action(kind="admit_proposal", target_id=pid, detail={}))


def _crash_on(monkeypatch, store, path_fragment):
    real = store.write_product

    def flaky(rel, env, message):
        if path_fragment in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", flaky)
    return lambda: monkeypatch.setattr(store, "write_product", real)


def _state(store):
    return store.read_product(library_state_file()).payload


def test_an_approved_skill_proposal_is_admitted(store):
    _put(store)
    _admit(store)
    sid = "skill-agent-round-zengen-v3-1-1"
    env = store.read_product(skill_file(sid))
    assert (env.id, env.type, env.status, env.links) == (sid, "skill", "effective", [PID])
    assert Skill.from_dict(env.payload).id == sid
    assert env.payload["trigger"] == {"signal": "emotion_high"}
    assert _state(store) == {"skill_version": "1", "memory_version": "0", "admitted": [
        {"proposal_id": PID, "skill_version": "1", "memory_version": "0", "entries": [sid]}]}
    lib = store.read_product(library_state_file())
    assert (lib.id, lib.type, lib.status) == ("library-state", "library_state", "executed")
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert load_library(store).version == {"skill": "1", "memory": "0"}


def test_a_handwritten_memory_proposal_is_admitted_to_the_memory_library(store):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store)
    mid = "mem-agent-round-zengen-v3-1-1"
    env = store.read_product(memory_file(mid))
    assert (env.type, env.status) == ("memory", "effective")
    assert MemoryEntry.from_dict(env.payload).content == "对方住在北京"
    assert _state(store)["skill_version"] == "0" and _state(store)["memory_version"] == "1"


def test_a_mixed_proposal_bumps_both_versions_once_and_numbers_entries_by_position(store):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    _admit(store)
    assert store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert store.exists(memory_file("mem-agent-round-zengen-v3-1-2"))
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")


def test_a_second_proposal_increments_from_the_current_state(store):
    _put(store)
    _admit(store)
    _put(store, pid="proposal-agent-round-zengen-v4-1")
    _admit(store, "proposal-agent-round-zengen-v4-1")
    assert [a["proposal_id"] for a in _state(store)["admitted"]] == [PID, "proposal-agent-round-zengen-v4-1"]
    assert _state(store)["skill_version"] == "2"
    assert store.read_product(library_state_file()).version == 2


def test_readmitting_is_a_noop(store):
    _put(store)
    _admit(store)
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["pending_review", "rejected", "effective"])
def test_a_proposal_that_is_not_approved_is_left_alone(store, status):
    _put(store, status=status)
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head
    assert not store.exists(library_state_file())


@pytest.mark.parametrize("fragment", ["skill_library/", "library_state.yaml", "proposal.yaml"])
def test_a_crash_at_any_step_converges_on_rerun_without_double_counting(store, monkeypatch, fragment):
    _put(store)
    restore = _crash_on(monkeypatch, store, fragment)
    with pytest.raises(Crash):
        _admit(store)
    assert store.read_product(proposal_file(PID)).status == "approved"
    restore()
    _admit(store)
    _admit(store)
    assert _state(store)["skill_version"] == "1"
    assert len(_state(store)["admitted"]) == 1
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_library").glob("*.yaml"))) == 1


def test_a_crash_between_two_entries_converges(store, monkeypatch):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    restore = _crash_on(monkeypatch, store, "memory_library/")
    with pytest.raises(Crash):
        _admit(store)
    assert store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert not store.exists(library_state_file())
    restore()
    _admit(store)
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")


def test_a_non_decimal_library_version_fails_admission_without_crashing(store):
    store.write_product(library_state_file(), Envelope(
        id="library-state", type="library_state", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1,
        payload={"skill_version": "lib-3", "memory_version": "0", "admitted": []}), "human: library")
    _put(store)
    _admit(store)  # 不抛:守护进程不会每个 tick 都崩
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert "skill_version must be a decimal string" in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("increments,msg", [
    ([], "non-empty list"),
    ([{"kind": "recipe", "definition": {}}], "unknown kind"),
    ([{"kind": "skill"}], "malformed"),
    ([{"kind": "skill", "definition": dict(SKILL, level="huge")}], "level"),
])
def test_malformed_increments_write_one_failure_record_and_nothing_else(store, increments, msg):
    _put(store, increments=increments)
    _admit(store)
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    # 收录层面的确定性数据错误记 T2(设计 §11):处理器空操作、只记一次;T2/T3 只是留痕分类
    assert fr.payload["category"] == "T2" and fr.payload["point"] == "mindagent.admit"
    assert msg in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(library_state_file())
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("run_id", ["a/b", "..", "", None, "run-ghost"])
def test_a_bad_or_missing_run_id_writes_no_failure_record_and_no_run_dir(store, run_id):
    _put(store, increments=[], run_id=run_id)
    _admit(store)  # 不抛
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-003"]  # 没有凭空新建 run 目录
    assert not store.exists(failure_record_file("run-003", f"admit-{PID}"))
    assert store.read_product(proposal_file(PID)).status == "approved"  # 原因留在原地,等人处理
    assert not store.exists(library_state_file())
