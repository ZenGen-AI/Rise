"""第 5 步的部署记忆语义在真 Starling 上再跑一遍(设计源第 11 节"真 Starling 或替身"的集成行)。

用例照 `tests/test_ma_deployment.py`、`tests/test_ma_admit.py` 里同名语义的替身用例写,辅助代码从那两个模块导入,
不复制。差别只有三处:库换成 `StarlingDeploymentStore`(配 `suffix=".db"` 的 `MemoryLayout`);抽取失败改用
假端点的 `fail_keys`(取代 `FileTupleStore(extraction_failure=)`);摘要比较用 `store.digest(world)`,不比库文件内容。
部署用例的世界是情境 s1(`WORLD`),智能体位主体是 pa。
假端点是本机回环子进程(`tests/starling_fakes.py`),不发任何外网请求。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

starling = pytest.importorskip("starling")
from actio.memory_port import MemoryEventConflict  # noqa: E402
from actio.memory_starling import StarlingDeploymentStore, copy_library  # noqa: E402
from controlplane.deferred import DeferredError  # noqa: E402
from controlplane.gitstore import GitStore  # noqa: E402
from controlplane.paths import failure_record_file, library_state_file, proposal_file, run_dir, skill_file  # noqa: E402
from mindagent.deployment import DeploymentFailedError  # noqa: E402
from mindagent.memory_layout import MemoryLayout  # noqa: E402
from starling_fakes import FakeOpenAI, offline_api_key  # noqa: E402
from test_ma_admit import KEY, PID, SKILL, _admit, _put  # noqa: E402
from test_ma_deployment import EID, KEY0, ROUND, WORLD, _Factory, _FakeClient, _run, _WorldFactory  # noqa: E402
from test_ma_deployment import store  # noqa: E402,F401  (部署用例的夹具:情境 s1、s2 与人格 pa、pb)

EID2 = "deploy-zengen-v1-s1-2"
# 假抽取器按「说话方：原话」切段(见 starling_fakes._SEGMENT);门② 的记忆增量正文是自由文本,
# 在这里补上说话方,否则假端点抽不出东西,fail_keys 也不会生效。
FIRST = {"scenario_id": "s1", "persona_id": "pa", "text": "pb：对方住在北京", "interlocutor": "pb"}
SECOND = {"scenario_id": "s1", "persona_id": "pa", "text": "pb：对方养了一只猫", "interlocutor": "pb"}
SKILL_AND_MEMORY = [{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": FIRST}]


@pytest.fixture
def mem(tmp_path):
    """与替身夹具同形:`_run`、`_admit` 只用 `.store` 与 `.layout`。`failing` 就是假端点的 `fail_keys`,
    就地 add/clear 立即同步给假端点子进程。"""
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        root = tmp_path / "mem"
        yield SimpleNamespace(
            store=StarlingDeploymentStore(root / "deploy", endpoints=fake.endpoints()),
            layout=MemoryLayout(root, suffix=".db"), failing=fake.fail_keys, fake=fake)


@pytest.fixture
def admit_store(tmp_path):
    """门② 用例的控制面仓库,照 tests/test_ma_admit.py 的 `store` 夹具(与部署的 `store` 内容不同,名字错开)。"""
    s = GitStore(str(tmp_path))
    s.init()
    (s.repo_root / run_dir("run-003")).mkdir(parents=True)  # 提案所属的 run
    return s


def _pending_in_copy(path: Path) -> list[str]:
    """库文件(或起点快照)里还有哪些 pending 行,不分主体(与 `pending_events` 同口径)。快照是提交点,
    字节不能变:拷出来再开。"""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "peek.db"
        copy_library(path, copy)
        memory = starling.Memory.open(str(copy), agent="peek")
        try:
            return [p.key for p in memory.pending_events()]
        finally:
            memory.close(timeout=10)


class _StartProbe:
    """`_run` 的 started 参数只调 append。每条 episode 开始时(起点快照已落)顺手看一眼部署世界与快照里的 pending 行。"""

    def __init__(self, mem):
        self._mem = mem
        self.seen = []

    def append(self, item):
        eid, digest = item
        self.seen.append({
            "eid": eid, "digest": digest,
            "library_pending": self._mem.store.pending_events(WORLD),
            "snapshot_pending": _pending_in_copy(self._mem.layout.snapshot(ROUND, eid)),
        })


# ---------------- 部署驱动器(替身用例:tests/test_ma_deployment.py) ----------------

def _record_restores(mem, monkeypatch):
    """记下每次 restore_from 之前世界的摘要,用来证明回滚前库里确实有这次尝试写的记忆。
    此时端口都已关,库静止,读摘要安全。不数调用次数。"""
    before_restore = []
    real = mem.store.restore_from

    def spy(world, path):
        before_restore.append(mem.store.digest(world))
        return real(world, path)

    monkeypatch.setattr(mem.store, "restore_from", spy)
    return before_restore


def test_a_retry_rolls_the_library_back_to_the_same_digest(store, mem, monkeypatch):
    """两次尝试都写了记忆后失败:每次失败之后 restore_from 起点副本,用尽重试时库停在起点;重入后的两次尝试
    仍从同一个起点开始。替身:test_exhausted_transient_retries_leave_the_library_at_the_start。"""
    digests = {}
    before_restore = _record_restores(mem, monkeypatch)
    with pytest.raises(DeploymentFailedError) as info:
        _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}), mem, digests=digests)
    assert info.value.transient and info.value.attempts == 2
    start = digests[EID]
    assert before_restore and all(d != start for d in before_restore)  # 回滚前库里确实有这次尝试写的记忆
    assert mem.store.digest(WORLD) == start                            # 用尽重试后库停在起点
    assert mem.layout.snapshot(ROUND, EID).exists()                    # 在途:留给重入或轮次回滚
    f = _Factory({"end_reason": "timeout"}, {})                        # 重入后第一次尝试再失败一次,第二次成功
    (ep,) = _run(store, f, mem, digests=digests)
    assert ep.id == EID and len(f.clients) == 2
    assert [c.traces[0]["memory"]["digest_before"] for c in f.clients] == [start, start]   # 两次尝试同一起点
    assert not mem.layout.snapshot(ROUND, EID).exists()                # 删起点快照 = 提交
    assert mem.store.digest(WORLD) != start                            # 提交了的记忆留在库里


def test_a_crash_mid_episode_is_rerun_from_the_restored_start(store, mem):
    """崩在 episode 中途:半截记忆已写进部署库、起点副本还在;重入先覆盖回起点,换了开场白后同一个 KEY0
    带新文本不冲突(崩溃遗留的是已提交的旧文本行)。替身:test_a_crash_mid_episode_is_rerun_from_the_restored_start。"""
    digests = {}

    class Crash(Exception):
        pass

    class Crashing(_FakeClient):
        def submit_turn(self, *a, **kw):
            raise Crash()

    with pytest.raises(Crash):
        _run(store, lambda **kw: Crashing(), mem, digests=digests)
    start = digests[EID]
    assert mem.layout.snapshot(ROUND, EID).exists()
    assert mem.store.digest(WORLD) != start                            # 半截记忆已写进部署世界
    f = _Factory({"opening": "我换个说法"})
    (ep,) = _run(store, f, mem, digests=digests)                       # 重入:先覆盖、核对摘要、再重跑
    assert len(f.clients) == 1 and ep.id == EID
    assert f.clients[0].traces[0]["memory"]["digest_before"] == start  # 重跑从起点开始
    assert not mem.layout.snapshot(ROUND, EID).exists()
    assert not mem.store.remember_event(WORLD, "pa", KEY0, "pb：我换个说法", interlocutor="pb").failed


def test_a_pending_row_from_a_failed_attempt_does_not_conflict_on_the_retry(store, mem):
    """第一次尝试 fail_keys 让 #0 抽取失败留 pending 行后整条失败;回滚后第二次尝试同一逻辑 id 换了开场白,
    不抛 MemoryEventConflict;成功后 pending_events 为空。替身:test_a_retry_restores_the_start_so_the_same_logical_id_never_conflicts。"""
    mem.failing.add(KEY0)                                              # 不清空,与替身用例一致
    f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    (ep,) = _run(store, f, mem)
    assert ep.id == EID and len(f.clients) == 2
    assert [c.created["episode_id"] for c in f.clients] == [EID, EID]  # 服务端拿不带轮次的 id
    # 不回滚的话 KEY0 的 pending 行带着旧文本,第二次尝试换了开场就会撞 MemoryEventConflict
    assert mem.store.pending_events(WORLD) == []
    with pytest.raises(MemoryEventConflict):                           # 成功后被放弃,是终态
        mem.store.remember_event(WORLD, "pa", KEY0, "pb：我换个说法", interlocutor="pb")
    assert not mem.layout.snapshot(ROUND, EID).exists()                # 删起点快照 = 提交


def test_the_second_round_reuses_the_episode_id_but_not_the_event_keys(store, mem):
    """两轮用同一 deploy-{model}-{scenario}-{k},事件 key 带轮次前缀,第二轮不撞第一轮已 committed 的 key。
    替身:test_a_second_round_with_the_same_deploy_id_does_not_hit_the_first_rounds_keys。"""
    _run(store, _Factory(), mem)
    _run(store, _Factory({"opening": "今天好点了"}), mem, round_id="agent-round-zengen-v2", run_id="run-004")
    calls = mem.fake.chat_calls
    again = mem.store.remember_event(WORLD, "pa", KEY0, "pb：我很生气!!", interlocutor="pb")
    assert not again.failed and again.stored                           # 第一轮的行仍 committed、幂等
    assert mem.fake.chat_calls == calls                                # 幂等重放不再调抽取端点
    second = mem.store.remember_event(WORLD, "pa", f"agent-round-zengen-v2/{EID}#0@pa", "pb：今天好点了",
                                      interlocutor="pb")
    assert not second.failed and second.stored
    assert mem.fake.chat_calls == calls                                # 第二轮的行也是 committed 的


def test_no_pending_rows_survive_an_episode_and_the_next_start_snapshot_is_clean(store, mem, monkeypatch, tmp_path):
    """episodes_per_scenario=2:两条 episode 各有一个事件抽取失败、留 pending 行,episode 提交时驱动器放弃它们。
    第二条开始时部署世界与它的起点快照里都没有 pending 行;全部结束后仍没有,两份起点快照都已删。
    替身没有同名用例。"""
    key2 = f"{ROUND}/{EID2}#2@pa"
    mem.failing.update({KEY0, key2})
    abandoned = []
    real = mem.store.abandon_event

    def spy(world, key):
        copy = tmp_path / f"before-abandon-{len(abandoned)}.db"
        copy_library(mem.layout.root / "deploy" / f"{world}.db", copy)   # 端口都已关,库静止
        abandoned.append((key, mem.store.pending_events(world), _pending_in_copy(copy)))
        real(world, key)

    monkeypatch.setattr(mem.store, "abandon_event", spy)
    probe = _StartProbe(mem)
    eps = _run(store, _Factory(), mem, started=probe, episodes_per_scenario=2)
    assert [e.id for e in eps] == [EID, EID2]
    assert abandoned == [(KEY0, [KEY0], [KEY0]), (key2, [key2], [key2])]   # 确有 pending 行,每条在提交时只放弃自己的;探针读得到
    first, second = probe.seen
    assert (first["eid"], second["eid"]) == (EID, EID2)
    assert first["library_pending"] == second["library_pending"] == []
    assert first["snapshot_pending"] == second["snapshot_pending"] == []
    assert first["digest"] != second["digest"]                         # 第一条提交的记忆进了第二条的起点
    assert mem.store.pending_events(WORLD) == []
    for eid in (EID, EID2):
        snap = mem.layout.snapshot(ROUND, eid)
        assert not snap.exists() and not snap.with_suffix(".world").exists()
    with pytest.raises(MemoryEventConflict):                           # 放弃是终态
        mem.store.remember_event(WORLD, "pa", KEY0, "pb：我很生气!!", interlocutor="pb")


def test_a_failed_attempt_rolls_back_both_slots_writes_in_one_starling_world(store, mem, monkeypatch):
    """两个主体(智能体位 pa 与对手位 pb)写进同一个 Starling 世界,尝试失败后整世界回到起点;成功的第二次尝试之后
    两个主体的记忆都在。替身:test_a_failed_attempt_rolls_back_both_slots_writes。"""
    digests = {}
    before_restore = _record_restores(mem, monkeypatch)
    f = _WorldFactory(mem, {"end_reason": "timeout"}, {})
    (ep,) = _run(store, f, mem, digests=digests, **f.hooks())
    assert ep.id == EID and len(f.clients) == 2
    assert len(before_restore) == 1 and before_restore[0] != digests[EID]   # 回滚前两个主体都写过
    assert [c.traces[0]["memory"]["digest_before"] for c in f.clients] == [digests[EID]] * 2
    assert mem.store.pending_events(WORLD) == [] and f.registry == {}
    heard = [("pa", f"{ROUND}/{EID}#0@pa", "pb", ep.turns[0].utterance),   # 智能体记对手开场
             ("pb", f"{ROUND}/{EID}#1@pb", "pa", ep.turns[1].utterance)]   # 对手记智能体首轮
    for holder, key, speaker, utterance in heard:
        again = mem.store.remember_event(WORLD, holder, key, f"{speaker}：{utterance}", interlocutor=speaker)
        assert not again.failed and again.stored                       # 已提交:同 key 同文本幂等


# ---------------- 门② 记忆增量(替身用例:tests/test_ma_admit.py) ----------------

def test_a_transient_admission_extraction_failure_defers_and_keeps_the_event_pending(admit_store, mem):
    """终审 I-1:假端点回 500,真 Starling 把它归 transport_failure(瞬态)。一条都没提交过也不放弃事件、不拒提案:
    写 T1、抛 DeferredError;抽取恢复后重入收录完。确定性类别那两条见下面 malformed_keys 的用例。
    替身:test_a_transient_extraction_failure_defers_without_abandoning_and_reentry_admits。"""
    mem.failing.add(f"{PID}#2")
    _put(admit_store, increments=SKILL_AND_MEMORY)
    with pytest.raises(DeferredError, match="transport_failure"):
        _admit(admit_store, mem)
    assert admit_store.read_product(proposal_file(PID)).status == "approved"
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T1", "mindagent.admit.memory")
    assert mem.store.pending_events("s1") == [f"{PID}#2"]              # 没被放弃
    assert not admit_store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))   # 记忆先写,技能未落
    mem.failing.clear()                                                # 抽取恢复后重入
    _admit(admit_store, mem)
    proposal = admit_store.read_product(proposal_file(PID))
    assert proposal.status == "effective" and set(proposal.payload["admitted_statements"]) == {f"{PID}#2"}
    assert admit_store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert mem.store.pending_events("s1") == []


def test_a_later_admission_extraction_failure_defers_and_converges(admit_store, mem):
    """第 1 条记忆增量 committed、第 2 条抽取以 transport_failure 失败(瞬态):第 2 条留 pending(不放弃)、提案留
    approved、写 T1、抛 DeferredError;抽取恢复后重入收录完。
    替身:test_a_later_extraction_failure_after_an_earlier_commit_defers_and_then_converges。"""
    mem.failing.add(f"{PID}#2")
    _put(admit_store, increments=[{"kind": "memory", "definition": FIRST}, {"kind": "memory", "definition": SECOND}])
    with pytest.raises(DeferredError, match="transport_failure"):
        _admit(admit_store, mem)
    assert admit_store.read_product(proposal_file(PID)).status == "approved"
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T1" and f"{PID}#2" in fr.payload["reason"]
    assert mem.store.pending_events("s1") == [f"{PID}#2"]              # 留 pending,不放弃
    assert not admit_store.exists(library_state_file())
    mem.failing.clear()                                                # 抽取恢复后重入
    _admit(admit_store, mem)
    proposal = admit_store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    assert set(proposal.payload["admitted_statements"]) == {KEY, f"{PID}#2"}
    assert mem.store.pending_events("s1") == []


def test_a_deterministic_admission_extraction_failure_rejects_and_abandons_the_event(admit_store, mem):
    """假端点的 malformed_keys:general fact 那一遍回不是 JSON 数组的内容,真 Starling 判抽取失败、类别为空,
    适配器记 unknown,门② 当确定性。唯一一条记忆增量:事件 abandoned、提案 rejected、失败记录 T2。
    替身:test_a_deterministic_extraction_failure_of_a_single_increment_rejects。"""
    mem.fake.malformed_keys.add(KEY)
    _put(admit_store, increments=[{"kind": "memory", "definition": FIRST}])
    _admit(admit_store, mem)                                           # 不抛
    proposal = admit_store.read_product(proposal_file(PID))
    assert proposal.status == "rejected" and "unknown" in proposal.payload["reject_reason"]
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T2", "mindagent.admit.memory")
    assert mem.store.pending_events("s1") == []                        # 不是 pending:已放弃
    mem.fake.malformed_keys.clear()
    with pytest.raises(MemoryEventConflict):                           # abandoned 是终态
        mem.store.remember_event("s1", "pa", KEY, FIRST["text"], interlocutor="pb")
    assert not admit_store.exists(library_state_file())


def test_a_deterministic_failure_after_a_commit_is_t3_and_reentry_does_not_extract_again(admit_store, mem):
    """部分收录:第 1 条 committed,第 2 条撞 malformed_keys(确定性)。写 T3、提案留 approved、不抛延后;
    下一个 tick 入口见到 T3 记录就返回,假端点的 chat_calls 不增加。
    替身:test_a_deterministic_failure_after_a_commit_is_t3_and_reentry_does_not_extract_again。"""
    second = f"{PID}#2"
    mem.fake.malformed_keys.add(second)
    _put(admit_store, increments=[{"kind": "memory", "definition": FIRST}, {"kind": "memory", "definition": SECOND}])
    _admit(admit_store, mem)                                           # 不抛 DeferredError
    assert admit_store.read_product(proposal_file(PID)).status == "approved"
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}-partial"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T3", "mindagent.admit.memory")
    assert fr.payload["reason"] == f"partial admission: {second} unknown after 1 committed"
    assert mem.store.pending_events("s1") == [second]                  # 不放弃,等人处理
    assert not admit_store.exists(library_state_file())
    calls, head = mem.fake.chat_calls, admit_store._git("rev-parse", "HEAD")
    _admit(admit_store, mem)                                           # 下一个 tick
    assert mem.fake.chat_calls == calls                                # 没有再抽取
    assert admit_store._git("rev-parse", "HEAD") == head               # 树也没动
    assert admit_store.read_product(proposal_file(PID)).status == "approved"
