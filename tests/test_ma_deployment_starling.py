"""第 5 步的部署记忆语义在真 Starling 上再跑一遍(设计源第 11 节"真 Starling 或替身"的集成行)。

用例照 `tests/test_ma_deployment.py`、`tests/test_ma_admit.py` 里同名语义的替身用例写,辅助代码从那两个模块导入,
不复制。差别只有三处:库换成 `StarlingDeploymentStore`(配 `suffix=".db"` 的 `MemoryLayout`);抽取失败改用
假端点的 `fail_keys`(取代 `FileTupleStore(extraction_failure=)`);摘要比较用 `store.digest(pid)`,不比库文件内容。
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
from test_ma_deployment import EID, KEY0, ROUND, _Factory, _FakeClient, _run  # noqa: E402
from test_ma_deployment import store  # noqa: E402,F401  (部署用例的夹具:情境 s1、s2 与人格 pa、pb)

EID2 = "deploy-zengen-v1-s1-2"
# 假抽取器按「说话方：原话」切段(见 starling_fakes._SEGMENT);门② 的记忆增量正文是自由文本,
# 在这里补上说话方,否则假端点抽不出东西,fail_keys 也不会生效。
FIRST = {"persona_id": "pa", "text": "pb：对方住在北京", "interlocutor": "pb"}
SECOND = {"persona_id": "pa", "text": "pb：对方养了一只猫", "interlocutor": "pb"}
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


def _spy_restores(mem, monkeypatch):
    """记下每次 restore_from 之前人格库的摘要和它返回的摘要。此时端口都已关,库静止,读摘要安全。"""
    calls = []
    real = mem.store.restore_from

    def spy(persona_id, path):
        before = mem.store.digest(persona_id)
        after = real(persona_id, path)
        calls.append((before, after))
        return after

    monkeypatch.setattr(mem.store, "restore_from", spy)
    return calls


def _pending_in_copy(path: Path) -> list[str]:
    """起点副本里还有哪些 pending 行。副本是提交点,字节不能变:拷出来再开。"""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "peek.db"
        copy_library(path, copy)
        memory = starling.Memory.open(str(copy), agent="peek")
        try:
            return [p.key for p in memory.pending_events()]
        finally:
            memory.close(timeout=10)


class _StartProbe:
    """`_run` 的 started 参数只调 append。每条 episode 开始时(起点副本已落)顺手看一眼部署库与副本里的 pending 行。"""

    def __init__(self, mem):
        self._mem = mem
        self.seen = []

    def append(self, item):
        eid, by_persona = item
        self.seen.append({
            "eid": eid, "digest": by_persona["pa"],
            "library_pending": self._mem.store.pending_events("pa"),
            "snapshot_pending": _pending_in_copy(self._mem.layout.snapshot(ROUND, eid, "pa")),
        })


# ---------------- 部署驱动器(替身用例:tests/test_ma_deployment.py) ----------------

@pytest.mark.parametrize("how", ["retries_exhausted", "crash"])
def test_a_retry_rolls_the_library_back_to_the_same_digest(store, mem, monkeypatch, how):
    """第一次尝试写了记忆、库摘要已变;失败后 restore_from 起点副本,重试或重入从与第一次相同的起点开始。
    替身:test_exhausted_transient_retries_leave_the_library_at_the_start、
    test_a_crash_mid_episode_is_rerun_from_the_restored_start。"""
    digests = {}
    restores = _spy_restores(mem, monkeypatch)
    if how == "retries_exhausted":
        with pytest.raises(DeploymentFailedError) as info:
            _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}), mem, digests=digests)
        assert info.value.transient and info.value.attempts == 2
        start = digests[EID]["pa"]
        assert len(restores) == 2                                      # 每次失败的尝试之后各回滚一次
        assert all(before != start and after == start for before, after in restores)    # 回滚前确实写过记忆
        assert mem.store.digest("pa") == start                         # 用尽重试后库停在起点
    else:
        class Crash(Exception):
            pass

        class Crashing(_FakeClient):
            def submit_turn(self, *a, **kw):
                raise Crash()

        with pytest.raises(Crash):
            _run(store, lambda **kw: Crashing(), mem, digests=digests)
        start = digests[EID]["pa"]
        assert restores == []                                          # 崩溃不走尝试间回滚
        assert mem.store.digest("pa") != start                         # 半截记忆已写进部署库
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()              # 在途:留给重入或轮次回滚
    n = len(restores)
    f = _Factory({"end_reason": "timeout"}, {})                        # 重入后第一次尝试再失败一次,第二次成功
    (ep,) = _run(store, f, mem, digests=digests)
    assert ep.id == EID and len(f.clients) == 2
    assert [after for _, after in restores[n:]] == [start, start]      # 重入先覆盖,失败的尝试后再回滚
    assert (restores[n][0] == start) == (how == "retries_exhausted")   # 用尽重试时库本来就在起点;崩溃时是半截记忆
    assert [c.traces[0]["memory"]["digest_before"] for c in f.clients] == [start, start]   # 两次尝试同一起点
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()          # 删起点副本 = 提交
    assert mem.store.digest("pa") != start                             # 提交了的记忆留在库里


def test_a_pending_row_from_a_failed_attempt_does_not_conflict_on_the_retry(store, mem):
    """第一次尝试 fail_keys 让 #0 抽取失败留 pending 行后整条失败;回滚后第二次尝试同一逻辑 id 换了开场白,
    不抛 MemoryEventConflict;成功后 pending_events 为空。替身:test_a_retry_restores_the_start_so_the_same_logical_id_never_conflicts。"""
    mem.failing.add(KEY0)                                              # 不清空,与替身用例一致
    f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    (ep,) = _run(store, f, mem)
    assert ep.id == EID and len(f.clients) == 2
    assert [c.created["episode_id"] for c in f.clients] == [EID, EID]  # 服务端拿不带轮次的 id
    # 不回滚的话 KEY0 的 pending 行带着旧文本,第二次尝试换了开场就会撞 MemoryEventConflict
    assert mem.store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):                           # 成功后被放弃,是终态
        mem.store.remember_event("pa", KEY0, "pb：我换个说法", interlocutor="pb")
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()          # 删起点副本 = 提交


def test_the_second_round_reuses_the_episode_id_but_not_the_event_keys(store, mem):
    """两轮用同一 deploy-{model}-{scenario}-{k},事件 key 带轮次前缀,第二轮不撞第一轮已 committed 的 key。
    替身:test_a_second_round_with_the_same_deploy_id_does_not_hit_the_first_rounds_keys。"""
    _run(store, _Factory(), mem)
    _run(store, _Factory({"opening": "今天好点了"}), mem, round_id="agent-round-zengen-v2", run_id="run-004")
    calls = mem.fake.chat_calls
    again = mem.store.remember_event("pa", KEY0, "pb：我很生气!!", interlocutor="pb")
    assert not again.failed and again.stored                           # 第一轮的行仍 committed、幂等
    assert mem.fake.chat_calls == calls                                # 幂等重放不再调抽取端点
    second = mem.store.remember_event("pa", f"agent-round-zengen-v2/{EID}#0", "pb：今天好点了",
                                      interlocutor="pb")
    assert not second.failed and second.stored
    assert mem.fake.chat_calls == calls                                # 第二轮的行也是 committed 的


def test_no_pending_rows_survive_an_episode_and_the_next_start_snapshot_is_clean(store, mem, monkeypatch):
    """episodes_per_scenario=2:两条 episode 各有一个事件抽取失败、留 pending 行,episode 提交时驱动器放弃它们。
    第二条开始时部署库与它的起点副本里都没有 pending 行;全部结束后仍没有,两份起点副本都已删。
    替身没有同名用例。"""
    key2 = f"{ROUND}/{EID2}#2"
    mem.failing.update({KEY0, key2})
    abandoned = []
    real = mem.store.abandon_event

    def spy(persona_id, key):
        abandoned.append((key, mem.store.pending_events(persona_id)))
        real(persona_id, key)

    monkeypatch.setattr(mem.store, "abandon_event", spy)
    probe = _StartProbe(mem)
    eps = _run(store, _Factory(), mem, started=probe, episodes_per_scenario=2)
    assert [e.id for e in eps] == [EID, EID2]
    assert abandoned == [(KEY0, [KEY0]), (key2, [key2])]               # 确有 pending 行,每条在提交时只放弃自己的
    first, second = probe.seen
    assert (first["eid"], second["eid"]) == (EID, EID2)
    assert first["library_pending"] == second["library_pending"] == []
    assert first["snapshot_pending"] == second["snapshot_pending"] == []
    assert first["digest"] != second["digest"]                         # 第一条提交的记忆进了第二条的起点
    assert mem.store.pending_events("pa") == []
    for eid in (EID, EID2):
        assert not mem.layout.snapshot(ROUND, eid, "pa").exists()
    with pytest.raises(MemoryEventConflict):                           # 放弃是终态
        mem.store.remember_event("pa", KEY0, "pb：我很生气!!", interlocutor="pb")


# ---------------- 门② 记忆增量(替身用例:tests/test_ma_admit.py) ----------------

def test_admission_extraction_failure_rejects_the_proposal_and_abandons_the_event(admit_store, mem):
    """替身:test_a_failed_extraction_abandons_the_event_and_rejects_the_proposal。"""
    mem.failing.add(f"{PID}#2")
    _put(admit_store, increments=SKILL_AND_MEMORY)
    _admit(admit_store, mem)
    proposal = admit_store.read_product(proposal_file(PID))
    assert proposal.status == "rejected"
    # 原因里是 Starling 给的 failure_category;替身给 "timeout",这里不钉具体字符串,只要求有一个
    category = proposal.payload["reject_reason"].removeprefix(f"extraction failed for {PID}#2: ")
    assert category and category != proposal.payload["reject_reason"]
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T2"
    assert mem.store.pending_events("pa") == []
    mem.failing.clear()
    with pytest.raises(MemoryEventConflict):                           # abandoned 是终态,任何路径都写不进
        mem.store.remember_event("pa", f"{PID}#2", FIRST["text"], interlocutor="pb")
    assert not admit_store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))   # 记忆先写,技能未落
    assert not admit_store.exists(library_state_file())


def test_a_later_admission_extraction_failure_defers_and_converges(admit_store, mem):
    """第 1 条记忆增量 committed、第 2 条抽取失败:第 2 条留 pending(不放弃)、提案留 approved、抛 DeferredError;
    抽取恢复后重入收录完。替身:test_a_later_extraction_failure_after_an_earlier_commit_defers_and_then_converges。"""
    mem.failing.add(f"{PID}#2")
    _put(admit_store, increments=[{"kind": "memory", "definition": FIRST}, {"kind": "memory", "definition": SECOND}])
    with pytest.raises(DeferredError, match="partial admission"):
        _admit(admit_store, mem)
    assert admit_store.read_product(proposal_file(PID)).status == "approved"
    fr = admit_store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert fr.payload["category"] == "T2" and "partial admission" in fr.payload["reason"]
    assert mem.store.pending_events("pa") == [f"{PID}#2"]              # 留 pending,不放弃
    assert not admit_store.exists(library_state_file())
    mem.failing.clear()                                                # 抽取恢复后重入
    _admit(admit_store, mem)
    proposal = admit_store.read_product(proposal_file(PID))
    assert proposal.status == "effective"
    assert set(proposal.payload["admitted_statements"]) == {KEY, f"{PID}#2"}
    assert mem.store.pending_events("pa") == []
