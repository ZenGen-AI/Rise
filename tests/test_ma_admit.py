from types import SimpleNamespace

import pytest

from actio.memory import FileTupleStore
from actio.memory_port import MemoryEventConflict, MemoryFailureError
from actio.skills import Skill
from controlplane.deferred import DeferredError
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.lock import hold_file_lock
from controlplane.paths import (
    CONTROL_PLANE_ROOT, failure_record_file, library_state_file, proposal_file, run_dir, skill_file,
)
from controlplane.reconcile import Action
from mindagent.admit import MEMORY_DEFINITION_KEYS, make_admit_proposal_handler
from mindagent.library import load_library
from mindagent.memory_layout import MemoryLayout

PID = "proposal-agent-round-zengen-v3-1"
SKILL = {"id": "skill-candidate-emotion_high", "version": 1, "level": "micro", "priority": 100,
         "trigger": {"signal": "emotion_high"}, "policy": {"instructions": ["先回应情绪"]}, "description": "d"}
MEMORY = {"persona_id": "pa", "text": "对方住在北京", "interlocutor": "pb"}
KEY = f"{PID}#1"


class Crash(Exception):
    pass


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    (s.repo_root / run_dir("run-003")).mkdir(parents=True)  # 提案所属的 run(产生绑定模型的那个)
    return s


@pytest.fixture
def mem(tmp_path):
    failing: dict[str, str] = {}      # 事件 key → 这次抽取失败的 failure_category;不在其中的抽取成功
    extractions: list[str] = []       # 替身每真抽取一次(已提交的同文本事件不抽)就记下 key

    def extraction_failure(ev):
        extractions.append(ev.key)
        return failing.get(ev.key)

    root = tmp_path / "mem"
    return SimpleNamespace(
        store=FileTupleStore(root / "deploy", extraction_failure=extraction_failure),
        layout=MemoryLayout(root), failing=failing, extractions=extractions)


def _put(store, pid=PID, status="approved", increments=None, run_id="run-003"):
    env = Envelope(
        id=pid, type="skill_memory_proposal", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1, links=[],
        payload={"round_id": "agent-round-zengen-v3", "run_id": run_id,
                 "increments": increments if increments is not None else [{"kind": "skill", "definition": SKILL}],
                 "verification": {"method": "replay_behavior"}},
    )
    store.write_product(proposal_file(pid), env, "test: proposal")


def _admit(store, mem, pid=PID):
    make_admit_proposal_handler(memory_store=mem.store, memory_layout=mem.layout)(
        store, Action(kind="admit_proposal", target_id=pid, detail={}))


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


def test_an_approved_skill_proposal_is_admitted(store, mem):
    _put(store)
    _admit(store, mem)
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


def test_a_memory_increment_is_remembered_in_the_personas_deploy_library(store, mem):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    again = mem.store.remember_event("pa", KEY, "对方住在北京", interlocutor="pb")
    assert list(again.stored) == proposal.payload["admitted_statements"][KEY]   # 收据是事件行
    assert _state(store)["memory_version"] == "1" and _state(store)["admitted"][0]["entries"] == [KEY]
    assert store.list_products(f"{CONTROL_PLANE_ROOT}/memory_library") == []   # 不再写树里的记忆库


def test_a_mixed_proposal_bumps_both_versions_once_and_numbers_entries_by_position(store, mem):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    assert store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert _state(store)["admitted"][0]["entries"] == ["skill-agent-round-zengen-v3-1-1", f"{PID}#2"]
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")


def test_a_second_proposal_increments_from_the_current_state(store, mem):
    _put(store)
    _admit(store, mem)
    _put(store, pid="proposal-agent-round-zengen-v4-1")
    _admit(store, mem, "proposal-agent-round-zengen-v4-1")
    assert [a["proposal_id"] for a in _state(store)["admitted"]] == [PID, "proposal-agent-round-zengen-v4-1"]
    assert _state(store)["skill_version"] == "2"
    assert store.read_product(library_state_file()).version == 2


def test_readmitting_is_a_noop(store, mem):
    _put(store)
    _admit(store, mem)
    head = store._git("rev-parse", "HEAD")
    _admit(store, mem)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["pending_review", "rejected", "effective"])
def test_a_proposal_that_is_not_approved_is_left_alone(store, mem, status):
    _put(store, status=status)
    head = store._git("rev-parse", "HEAD")
    _admit(store, mem)
    assert store._git("rev-parse", "HEAD") == head
    assert not store.exists(library_state_file())


@pytest.mark.parametrize("fragment", ["skill_library/", "library_state.yaml", "proposal.yaml"])
def test_a_crash_at_any_step_converges_on_rerun_without_double_counting(store, mem, monkeypatch, fragment):
    _put(store)
    restore = _crash_on(monkeypatch, store, fragment)
    with pytest.raises(Crash):
        _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    restore()
    _admit(store, mem)
    _admit(store, mem)
    assert _state(store)["skill_version"] == "1"
    assert len(_state(store)["admitted"]) == 1
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_library").glob("*.yaml"))) == 1


def test_a_crash_between_two_entries_converges(store, mem, monkeypatch):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    restore = _crash_on(monkeypatch, store, "skill_library/")   # 记忆先于技能写:崩在技能条目上
    with pytest.raises(Crash):
        _admit(store, mem)
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert not store.exists(library_state_file())
    before = mem.store.digest("pa")
    restore()
    _admit(store, mem)
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")
    assert mem.store.digest("pa") == before                           # 记忆已在库里,重入幂等


def test_a_non_decimal_library_version_fails_admission_without_crashing(store, mem):
    store.write_product(library_state_file(), Envelope(
        id="library-state", type="library_state", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1,
        payload={"skill_version": "lib-3", "memory_version": "0", "admitted": []}), "human: library")
    _put(store)
    _admit(store, mem)  # 不抛:守护进程不会每个 tick 都崩
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert "skill_version must be a decimal string" in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    head = store._git("rev-parse", "HEAD")
    _admit(store, mem)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("increments,msg", [
    ([], "non-empty list"),
    ([{"kind": "recipe", "definition": {}}], "unknown kind"),
    ([{"kind": "skill"}], "malformed"),
    ([{"kind": "skill", "definition": dict(SKILL, level="huge")}], "level"),
])
def test_malformed_increments_write_one_failure_record_and_nothing_else(store, mem, increments, msg):
    _put(store, increments=increments)
    _admit(store, mem)
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    # 收录层面的确定性数据错误记 T2(设计 §11):处理器空操作、只记一次;T2/T3 只是留痕分类
    assert fr.payload["category"] == "T2" and fr.payload["point"] == "mindagent.admit"
    assert msg in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(library_state_file())
    head = store._git("rev-parse", "HEAD")
    _admit(store, mem)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("run_id", ["a/b", "..", "", None, "run-ghost"])
def test_a_bad_or_missing_run_id_writes_no_failure_record_and_no_run_dir(store, mem, run_id):
    _put(store, increments=[], run_id=run_id)
    _admit(store, mem)  # 不抛
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-003"]  # 没有凭空新建 run 目录
    assert not store.exists(failure_record_file("run-003", f"admit-{PID}"))
    assert store.read_product(proposal_file(PID)).status == "approved"  # 原因留在原地,等人处理
    assert not store.exists(library_state_file())


# ---------------- 记忆增量走部署库(设计二第 9 节"门② kind: memory"与"门② 失败") ----------------

def test_a_failed_extraction_abandons_the_event_and_rejects_the_proposal(store, mem):
    mem.failing[f"{PID}#2"] = "schema_failure"                        # 确定性类别,一条都没提交过
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "rejected" and "schema_failure" in proposal.payload["reject_reason"]
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T2"
    assert mem.store.pending_events("pa") == []
    mem.failing.clear()
    with pytest.raises(MemoryEventConflict):                          # abandoned 是终态,任何路径都写不进
        mem.store.remember_event("pa", f"{PID}#2", "对方住在北京", interlocutor="pb")
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))     # 记忆先写,技能未落
    assert not store.exists(library_state_file())


SECOND = {"persona_id": "pa", "text": "对方养了一只猫", "interlocutor": "pb"}
TWO_MEMORIES = [{"kind": "memory", "definition": MEMORY}, {"kind": "memory", "definition": SECOND}]


def test_a_later_extraction_failure_after_an_earlier_commit_defers_and_then_converges(store, mem):
    # 终审 I-1 改写:第 1 条记忆增量 committed、第 2 条抽取以瞬态类别(上游网络)失败。不能转 rejected
    # (已提交的放弃不了),也不放弃第 2 条;写 T1、抛 DeferredError 退避,重入时同 key 再抽一次
    mem.failing[f"{PID}#2"] = "transport_failure"
    _put(store, increments=TWO_MEMORIES)
    with pytest.raises(DeferredError, match="transport_failure"):
        _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T1", "mindagent.admit.memory")
    assert f"{PID}#2" in fr.payload["reason"] and "transport_failure" in fr.payload["reason"]
    assert not store.exists(failure_record_file("run-003", f"admit-{PID}-partial"))
    assert mem.store.pending_events("pa") == [f"{PID}#2"]              # 留 pending,不放弃
    assert not store.exists(library_state_file())
    mem.failing.clear()                                                # 抽取恢复后重入
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    assert set(proposal.payload["admitted_statements"]) == {KEY, f"{PID}#2"}
    assert mem.store.pending_events("pa") == []


def test_a_store_error_leaves_the_proposal_approved_and_reentry_completes_the_receipt(store, mem, monkeypatch):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    real = mem.store.remember_event

    def boom(*a, **kw):
        real(*a, **kw)                                                # 语句已写,"③ 写收据"之后崩
        raise OSError("disk")

    monkeypatch.setattr(mem.store, "remember_event", boom)
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert store.exists(failure_record_file("run-003", f"admit-{PID}"))
    monkeypatch.setattr(mem.store, "remember_event", real)
    before = mem.store.digest("pa")
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert mem.store.digest("pa") == before                           # 重入是幂等的,不二次抽取


@pytest.mark.parametrize("transient,category", [(True, "T1"), (False, "T2")])
def test_a_memory_failure_error_is_recorded_by_class_name_only(store, mem, monkeypatch, transient, category):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])

    def boom(*a, **kw):
        raise MemoryFailureError("https://user:secret@extract.example/v1", transient=transient)

    monkeypatch.setattr(mem.store, "remember_event", boom)
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["point"]) == (category, "mindagent.admit.memory")
    assert fr.payload["reason"] == f"remember {KEY}: MemoryFailureError"


def test_an_in_flight_round_defers_admission_until_its_copy_is_gone(store, mem):
    # 门② 与在途轮次同一 tick:该人格有未提交起点副本时延后,库不变;副本删掉后下一 tick 收录成功
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    snap = mem.layout.snapshot("agent-round-zengen-v3", "deploy-zengen-v3-s1-1", "pa")
    snap.parent.mkdir(parents=True)
    mem.store.backup_to("pa", snap)
    before, head = mem.store.digest("pa"), store._git("rev-parse", "HEAD")
    with pytest.raises(DeferredError, match="uncommitted"):
        _admit(store, mem)
    assert mem.store.digest("pa") == before and store._git("rev-parse", "HEAD") == head
    snap.unlink()
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"


def test_a_locked_persona_library_defers_admission(store, mem):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    with hold_file_lock(mem.layout.deploy_lock("pa")):
        with pytest.raises(DeferredError, match="pa.lock"):
            _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"


def test_a_skill_only_proposal_takes_no_persona_lock(store, mem):
    _put(store)
    with hold_file_lock(mem.layout.deploy_lock("pa")):
        _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"


@pytest.mark.parametrize("definition", [
    {"persona_id": "pa", "text": "x"},                                             # 少键
    {"persona_id": "pa", "text": "x", "interlocutor": "pb", "subject": "pb"},      # 多键
    {"persona_id": "a/b", "text": "x", "interlocutor": "pb"},                      # 人格 id 不合法
    {"persona_id": "pa", "text": "", "interlocutor": "pb"},                        # 空文本
    {"persona_id": "pa", "text": "x", "interlocutor": 3},
])
def test_a_malformed_memory_definition_is_t2_and_touches_nothing(store, mem, definition):
    _put(store, increments=[{"kind": "memory", "definition": definition}])
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert store.read_product(failure_record_file("run-003", f"admit-{PID}")).payload["category"] == "T2"
    assert mem.store.digest("pa") == FileTupleStore(mem.layout.root / "empty").digest("pa")


def test_an_empty_interlocutor_passes_validation_but_the_stand_in_refusal_is_t2_and_does_not_escape(store, mem):
    # 设计源:interlocutor 空串表示一般事实,校验放行;替身库要求它非空、在登记之前就以 ActioDataError 拒收。
    # 处理器把它当 T2 记下(只报类名),提案留在 approved,库不动,守护进程不崩
    _put(store, increments=[{"kind": "memory", "definition": dict(MEMORY, interlocutor="")}])
    before = mem.store.digest("pa")
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    reason = fr.payload["reason"]
    assert fr.payload["category"] == "T2" and reason.startswith(f"remember {KEY}: ActioDataError")
    # 终审 M-6:点明替身收不了一般事实,否则人看不出提案为什么一直停在 approved;不带任何正文
    assert "general fact" in reason and "interlocutor" in reason
    assert MEMORY["text"] not in reason
    assert mem.store.digest("pa") == before and mem.store.pending_events("pa") == []


def test_memory_definition_keys_are_the_three_required_keys():
    assert MEMORY_DEFINITION_KEYS == frozenset({"persona_id", "text", "interlocutor"})


# ---------------- 修补第 1 轮 ----------------

def test_a_skill_only_proposal_is_deferred_while_any_round_has_an_uncommitted_copy(store, mem):
    # 在途轮次重入时会重读库:只收技能也会改技能版本,让已提交的 episode 换新 id 重跑、记下新记忆,
    # 之后在途那条的起点副本覆盖回去就把它们抹掉。所以有任何未提交副本时门② 一律延后
    _put(store)
    snap = mem.layout.snapshot("agent-round-zengen-v3", "deploy-zengen-v3-s1-1", "pb")   # 别的人格的副本也算
    snap.parent.mkdir(parents=True)
    mem.store.backup_to("pb", snap)
    head = store._git("rev-parse", "HEAD")
    with pytest.raises(DeferredError, match="deploy-zengen-v3-s1-1-pb.json"):
        _admit(store, mem)
    assert store._git("rev-parse", "HEAD") == head and store.read_product(proposal_file(PID)).status == "approved"
    snap.unlink()
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"


def test_a_crash_after_abandoning_the_failed_event_converges_to_rejected(store, mem, monkeypatch):
    # 抽取失败:先 abandon_event、再写 rejected。崩在两者之间时,下一 tick 的 remember_event 撞上已放弃的 key
    mem.failing[KEY] = "schema_failure"
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    restore = _crash_on(monkeypatch, store, "proposal.yaml")
    with pytest.raises(Crash):
        _admit(store, mem)
    restore()
    assert store.read_product(proposal_file(PID)).status == "approved"
    mem.failing.clear()
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "rejected" and "abandoned" in proposal.payload["reject_reason"]
    assert not store.exists(library_state_file())


def test_a_conflict_on_a_committed_key_is_t2_and_leaves_the_proposal_approved(store, mem):
    # 同 key 已以别的文本 committed:abandon_event 抛,提案不动,只记 T2
    mem.store.remember_event("pa", KEY, "别的文本", interlocutor="pb")
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["reason"]) == ("T2", f"remember {KEY}: MemoryEventConflict")


# ---------------- 终审 I-1:门② 抽取失败按 failure_category 分两路 ----------------

PARTIAL = f"admit-{PID}-partial"     # 部分收录的确定性失败(T3)单独一份记录,见 admit.py 模块 docstring


@pytest.mark.parametrize("category", ["timeout", "transport_failure"])
def test_a_transient_extraction_failure_defers_without_abandoning_and_reentry_admits(store, mem, category):
    # 上游网络的抖动:不放弃事件、不拒提案,写 T1、抛 DeferredError 退避;重入时同 key 再抽一次
    mem.failing[KEY] = category
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    with pytest.raises(DeferredError, match=category):
        _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert mem.store.pending_events("pa") == [KEY]                    # 没被放弃
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T1", "mindagent.admit.memory")
    assert KEY in fr.payload["reason"] and category in fr.payload["reason"]
    assert MEMORY["text"] not in fr.payload["reason"]
    del mem.failing[KEY]                                               # 抽取恢复
    _admit(store, mem)
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "effective" and set(proposal.payload["admitted_statements"]) == {KEY}
    assert mem.extractions == [KEY, KEY]                               # 重入确实再抽了一次
    assert mem.store.pending_events("pa") == []


@pytest.mark.parametrize("category", ["schema_failure", "semantic_rejection", "a_category_nobody_knows"])
def test_a_deterministic_extraction_failure_of_a_single_increment_rejects(store, mem, category):
    mem.failing[KEY] = category
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)                                                 # 不抛
    proposal = store.read_product(proposal_file(PID))
    assert proposal.status == "rejected" and category in proposal.payload["reject_reason"]
    assert store.read_product(failure_record_file("run-003", f"admit-{PID}")).payload["category"] == "T2"
    assert mem.store.pending_events("pa") == []
    del mem.failing[KEY]
    with pytest.raises(MemoryEventConflict):                          # 事件已 abandoned
        mem.store.remember_event("pa", KEY, MEMORY["text"], interlocutor="pb")


def test_a_deterministic_failure_after_a_commit_is_t3_and_reentry_does_not_extract_again(store, mem):
    # 部分收录:第 1 条已 committed、第 2 条被确定性拒收。写 T3、提案留 approved、不抛延后(否则连续延后
    # 达上限会让守护进程退出);调和循环每 tick 都会再派一次,处理器见到 T3 记录就直接返回,不再花抽取的钱
    mem.failing[f"{PID}#2"] = "semantic_rejection"
    _put(store, increments=TWO_MEMORIES)
    _admit(store, mem)                                                 # 不抛 DeferredError
    assert store.read_product(proposal_file(PID)).status == "approved"
    fr = store.read_product(failure_record_file("run-003", PARTIAL))
    assert (fr.payload["category"], fr.payload["point"]) == ("T3", "mindagent.admit.memory")
    assert fr.payload["reason"] == f"partial admission: {PID}#2 semantic_rejection after 1 committed"
    assert mem.store.pending_events("pa") == [f"{PID}#2"]              # 不放弃,等人处理
    assert not store.exists(library_state_file())
    extractions, head = len(mem.extractions), store._git("rev-parse", "HEAD")
    _admit(store, mem)                                                 # 下一个 tick
    assert len(mem.extractions) == extractions                         # 没有再抽取
    assert store._git("rev-parse", "HEAD") == head                     # 树也没动
    assert store.read_product(proposal_file(PID)).status == "approved"


def test_a_partial_t3_is_recorded_even_after_an_earlier_transient_t1(store, mem):
    # fail_once 只写一次:先 T1(瞬态延后)、再确定性部分失败时,T3 写进单独的那份记录,不被 T1 挡住
    mem.failing[f"{PID}#2"] = "timeout"
    _put(store, increments=TWO_MEMORIES)
    with pytest.raises(DeferredError):
        _admit(store, mem)
    mem.failing[f"{PID}#2"] = "schema_failure"
    _admit(store, mem)
    assert store.read_product(failure_record_file("run-003", f"admit-{PID}")).payload["category"] == "T1"
    assert store.read_product(failure_record_file("run-003", PARTIAL)).payload["category"] == "T3"
    extractions = len(mem.extractions)
    _admit(store, mem)
    assert len(mem.extractions) == extractions


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_an_earlier_t1_or_t2_record_does_not_block_reentry(store, mem, monkeypatch, category):
    # 只有 T3 的部分收录记录挡重入:T1 是瞬态要重试,T2 是"写记忆抛异常、留 approved 重入补收据"
    from controlplane.ledger import record_failure
    record_failure(store, "run-003", task_id=f"admit-{PID}", category=category, point="mindagent.admit.memory",
                   retries=0, reason="earlier")
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store, mem)
    assert store.read_product(proposal_file(PID)).status == "effective"
