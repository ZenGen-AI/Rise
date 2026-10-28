import json
from types import SimpleNamespace

import grpc
import pytest

from actio.memory import FileTupleStore
from actio.skills import Skill
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.lock import hold_file_lock
from controlplane.paths import (
    CONTROL_PLANE_ROOT, run_dir, agent_round_file, episode_file, episodes_dir, failure_record_file, model_file, proposal_file, skill_file,
    scenario_file,
)
from controlplane.reconcile import Action
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME
from inference_bridge import BridgeError, ScriptedBridge
from mindagent.config import AgentConfig
from mindagent.memory_layout import MemoryLayout
from mindagent.rounds import make_start_round_handler, round_id_for, supersede_stale_rounds, task_id_for
from simulator.datamodel import Episode, Persona, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.realtime.client import SessionRejectedError
from simulator.store_adapter import read_persona, write_persona, write_scenario

AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
ANGRY = "我真的很生气!!"
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": ""}
CKPT = BigRef("file:///ckpt/v1", "c" * 64, 1)
ROUND = "agent-round-zengen-v1"


class Crash(Exception):
    pass


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", None)


class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "reset"


class _FakeClient:
    """对方先说(每句来自 lines),agent 后说;max_turns=4。"""

    def __init__(self, lines, *, create_error=None, end_reason="max_turns", crash_on_submit=None):
        self._lines = list(lines)
        self._create_error = create_error
        self._end_reason = end_reason
        self._crash_on_submit = crash_on_submit
        self._turns = []
        self.closed = False
        self.created = None

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot,
                       episode_id=None):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment, episode_id=episode_id)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", self._lines[0]))
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        if self._crash_on_submit is not None:
            raise self._crash_on_submit()
        self._turns.append(TurnRecord(len(self._turns), "a", utterance, inner_thought, activation_trace))
        if len(self._turns) < 4:
            self._turns.append(TurnRecord(len(self._turns), "b", self._lines[len(self._turns) // 2]))
        if len(self._turns) >= 4:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode("ep-sess", self.created["scenario_id"], self.created["role_assignment"],
                       self.turns, self._end_reason)

    def abort(self):
        self.closed = True


class _Factory:
    def __init__(self, lines=(ANGRY, "好吧"), plans=()):
        self.lines = lines
        self.plans = list(plans)
        self.count = 0

    def __call__(self, **kw):
        self.count += 1
        return _FakeClient(self.lines, **(self.plans.pop(0) if self.plans else {}))


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, links=list(links), payload=payload)


def _model(store, n, run_id, status="effective"):
    store.write_product(model_file(n), _env(f"zengen-v{n}", "zengen_model", status,
                        {"promoted_from": f"cand-{run_id}", "checkpoint_ref": CKPT.to_dict()},
                        [f"cand-{run_id}", run_id]), "test: model")


def _skill(store, d):
    store.write_product(skill_file(d["id"]), _env(d["id"], "skill", "effective", d), "test: skill")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(s, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"],
                                   "deployment_simulation"))
    for pid in ("pa", "pb"):
        write_persona(s, Persona(pid, ["t"], "m", "s", ["v"]))
    _model(s, 1, "run-001")
    (s.repo_root / run_dir("run-001")).mkdir(parents=True)  # 产生绑定模型的 run(晋级时必然存在)
    return s


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "mem"
    return SimpleNamespace(store=FileTupleStore(root / "deploy"), layout=MemoryLayout(root))


def _cfg(**kw):
    base = dict(scenario_ids=("s1", "s2", "s3"), bindings=(OPP, AGENT), max_turns=4,
                min_support=3, min_ratio=0.5)
    base.update(kw)
    return AgentConfig(**base)


def _loader(store, ids):
    return {pid: read_persona(store, pid) for pid in ids}


def _handler(factory=None, bridge=None, backends=None, mem=None, **cfg):
    seen = backends if backends is not None else []

    def backend_factory(model_id, ckpt):
        seen.append((model_id, ckpt))
        return Echo()

    return make_start_round_handler(
        config=_cfg(**cfg), client_factory=factory or _Factory(), backend_factory=backend_factory,
        personas_loader=_loader, bridge=bridge, memory_store=mem.store, memory_layout=mem.layout,
        memory_runtime=STAND_IN_MEMORY_RUNTIME, scratch_store_factory=FileTupleStore)


def _start(store, handler, model="zengen-v1"):
    handler(store, Action(kind="start_agent_round", target_id=model, detail={}))
    return store.read_product(agent_round_file(model))


def test_the_first_round_with_an_empty_library_is_silent_by_design(store, mem):
    backends = []
    env = _start(store, _handler(mem=mem, backends=backends))
    assert (env.id, env.type, env.status) == ("agent-round-zengen-v1", "agent_round", "executed")
    p = env.payload
    assert p["model_id"] == "zengen-v1" and p["run_id"] == "run-001" and p["model_version"] == 1
    assert p["checkpoint_ref"] == CKPT.to_dict()
    assert p["library_version"] == {"skill": "0", "memory": "0"}
    assert p["episode_ids"] == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s2-1", "deploy-zengen-v1-s3-1"]
    assert (p["findings"], p["proposals"], p["dropped"], p["failure"]) == ([], [], [], None)
    assert p["generator_version"] == "rule_template.v1" and "prompt_template_version" not in p
    assert p["uses_bridge"] is False
    assert p["scenario_ids"] == ["s1", "s2", "s3"] and p["persona_ids"] == ["pb", "pa"]
    assert p["analyzer_config"] == {"min_support": 3, "min_ratio": 0.5}
    assert env.links == ["zengen-v1", "run-001"]
    assert not store.exists(failure_record_file("run-001", task_id_for("zengen-v1")))
    assert list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir()) == []
    assert backends and all(b == ("zengen-v1", CKPT) for b in backends)
    for eid in p["episode_ids"]:
        assert store.exists(episode_file("run-001", eid))


def test_a_library_that_ignores_emotion_produces_a_pending_review_proposal(store, mem):
    _skill(store, GOAL)
    env = _start(store, _handler(mem=mem))
    p = env.payload
    assert env.status == "executed"
    (finding,) = p["findings"]
    assert finding["signal"] == "emotion_high" and finding["support"] == 3 and finding["ratio"] == 1.0
    assert p["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    prop = store.read_product(proposal_file("proposal-agent-round-zengen-v1-1"))
    assert prop.status == "pending_review"
    assert prop.payload["verification"]["fired_on_motivating"] == 1.0
    assert prop.payload["verification"]["fired_elsewhere"] == 0.0
    assert prop.payload["finding"]["episode_refs"] == p["episode_ids"]


def test_dropped_drafts_are_recorded_in_the_round(store, mem):
    strong = dict(GOAL, id="skill-top", priority=1000)
    _skill(store, strong)
    env = _start(store, _handler(mem=mem))
    assert env.status == "executed"
    assert env.payload["proposals"] == []
    (dropped,) = env.payload["dropped"]
    assert dropped["fired_on_motivating"] == 0.0
    assert dropped["reason"] == "fired_on_motivating 0.000 < 0.8"


def test_with_a_bridge_the_round_records_the_prompt_version_and_the_cost(store, mem):
    _skill(store, GOAL)
    good = json.dumps({"instructions": ["先安抚"], "description": "d"})
    bridge = ScriptedBridge([good], store=store)
    env = _start(store, _handler(mem=mem, bridge=bridge))
    assert env.payload["generator_version"] == "bridge.v1"
    assert env.payload["prompt_template_version"] == "mindagent-skill-v1"
    assert env.payload["uses_bridge"] is True
    assert bridge.calls[0][1:] == ("run-001", "agent-zengen-v1-propose-1-attempt1")


def test_an_executed_round_is_a_noop(store, mem):
    h = _handler(mem=mem)
    _start(store, h)
    head = store._git("rev-parse", "HEAD")
    factory = _Factory()
    _start(store, _handler(mem=mem, factory=factory))
    assert store._git("rev-parse", "HEAD") == head and factory.count == 0


def test_a_pending_round_resumes_without_regenerating_persisted_episodes(store, mem, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_proposal(rel, env, message):
        if "skill_memory_proposals" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_proposal)
    with pytest.raises(Crash):
        _start(store, _handler(mem=mem))
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert len(store.list_products(episodes_dir("run-001"))) == 3
    monkeypatch.setattr(store, "write_product", real)
    factory = _Factory()
    env = _start(store, _handler(mem=mem, factory=factory))
    assert factory.count == 0
    assert env.status == "executed" and env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]


def test_a_crash_after_packaging_reruns_into_the_same_proposal_id(store, mem, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product
    calls = {"n": 0}

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler(mem=mem))
    monkeypatch.setattr(store, "write_product", real)
    env = _start(store, _handler(mem=mem))
    assert env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir())) == 1


def test_a_transient_failure_propagates_and_leaves_the_round_pending(store, mem):
    factory = _Factory(plans=[{"create_error": _Unavailable()}] * 2)
    with pytest.raises(Exception) as exc:
        _start(store, _handler(mem=mem, factory=factory, max_retries_per_episode=1))
    assert type(exc.value).__name__ == "DeploymentFailedError" and exc.value.transient
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))
    env = _start(store, _handler(mem=mem))
    assert env.status == "executed"


def test_an_unexpected_exception_propagates_and_leaves_the_round_pending(store, mem):
    def loader(store, ids):
        raise Crash("disk")

    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(),
                                 backend_factory=lambda m, c: Echo(), personas_loader=loader,
                                 memory_store=mem.store, memory_layout=mem.layout,
                                 memory_runtime=STAND_IN_MEMORY_RUNTIME, scratch_store_factory=FileTupleStore)
    with pytest.raises(Crash):
        _start(store, h)
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"


def test_an_exhausted_bridge_t1_fails_the_round_r18(store, mem):
    _skill(store, GOAL)
    _start(store, _handler(mem=mem, bridge=ScriptedBridge([BridgeError("T1", "timeout", attempts=3)])))
    fr = _assert_failed(store, "mindagent.proposals", "bridge T1: timeout")
    assert fr.payload["retries"] == 2  # 已重试次数 = attempts - 1


def _assert_failed(store, point, fragment):
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T3" and env.payload["failure"]["point"] == point
    assert fragment in env.payload["failure"]["reason"]
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T3", point)
    assert fragment in fr.payload["reason"]
    return fr


def test_a_bridge_t2_fails_the_round(store, mem):
    _skill(store, GOAL)
    _start(store, _handler(mem=mem, bridge=ScriptedBridge([BridgeError("T2", "no such tool")])))
    _assert_failed(store, "mindagent.proposals", "bridge T2: no such tool")


def test_unparseable_bridge_output_fails_the_round_after_bounded_retries(store, mem):
    _skill(store, GOAL)
    _start(store, _handler(mem=mem, bridge=ScriptedBridge(["x", "y"]), max_parse_retries=1))
    fr = _assert_failed(store, "mindagent.proposals", "not a single JSON object")
    assert fr.payload["retries"] == 1


def test_a_rejected_session_fails_the_round(store, mem):
    factory = _Factory(plans=[{"create_error": SessionRejectedError("invalid_session", "bad persona")}])
    _start(store, _handler(mem=mem, factory=factory))
    _assert_failed(store, "mindagent.deployment", "invalid_session")


def test_a_missing_scenario_fails_the_round_before_any_session(store, mem):
    factory = _Factory()
    _start(store, _handler(mem=mem, factory=factory, scenario_ids=("s1", "ghost")))
    _assert_failed(store, "mindagent.rounds.inputs", "scenario 'ghost'")
    assert factory.count == 0


def test_a_malformed_library_entry_fails_the_round(store, mem):
    _skill(store, dict(GOAL, level="huge"))
    _start(store, _handler(mem=mem))
    _assert_failed(store, "mindagent.rounds", "ActioDataError")


def test_a_failed_round_is_terminal(store, mem):
    _start(store, _handler(mem=mem, scenario_ids=("ghost",)))
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler(mem=mem))
    assert store._git("rev-parse", "HEAD") == head


def test_an_unknown_model_fails_the_round_without_a_failure_record(store, mem):
    _start(store, _handler(mem=mem), model="zengen-v9")
    env = store.read_product(agent_round_file("zengen-v9"))
    assert env.status == "failed" and "not found" in env.payload["failure"]["reason"]


def test_a_model_with_an_unusable_checkpoint_ref_fails_the_round_instead_of_escaping(store, mem):
    """手改进树的坏引用(I8):轮次 failed、类别 T3、原因点名字段;InvalidBigRefError 不能从处理器
    漏出去(调用方只接 AgentRoundFailedError,漏出去会打崩守护进程)。"""
    env = store.read_product(model_file(1))
    env.payload["checkpoint_ref"] = {**CKPT.to_dict(), "sha256": "../../etc"}
    store.write_product(model_file(1), env, "human: spoil checkpoint_ref")
    _start(store, _handler(mem=mem))
    rnd = store.read_product(agent_round_file("zengen-v1"))
    assert rnd.status == "failed"
    assert rnd.payload["failure"]["category"] == "T3"
    assert "sha256" in rnd.payload["failure"]["reason"]


def test_a_corrupt_library_file_fails_the_round_instead_of_escaping(store, mem):
    """库文件读不出:轮次 failed,原因点名文件;处理器不抛,守护进程不退出。"""
    rel = skill_file("skill-broken")
    path = store.repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<<<<<<< HEAD\nid: skill-broken\n", encoding="utf-8")
    _start(store, _handler(mem=mem))
    rnd = store.read_product(agent_round_file("zengen-v1"))
    assert rnd.status == "failed"
    assert "skill-broken" in rnd.payload["failure"]["reason"]


def test_a_newer_model_supersedes_a_pending_round_and_an_old_action_is_refused(store, mem, monkeypatch):
    real = store.write_product

    def crash(rel, env, message):
        if "episodes/" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash)
    with pytest.raises(Crash):
        _start(store, _handler(mem=mem))
    monkeypatch.setattr(store, "write_product", real)
    _model(store, 2, "run-002")
    env2 = _start(store, _handler(mem=mem), model="zengen-v2")
    assert env2.status == "executed" and env2.payload["run_id"] == "run-002"
    old = store.read_product(agent_round_file("zengen-v1"))
    assert old.status == "failed"
    assert old.payload["failure"] == {"category": "superseded", "reason": "superseded by zengen-v2",
                                      "point": "mindagent.rounds.supersede"}
    assert env2.payload["episode_ids"][0] == "deploy-zengen-v2-s1-1"
    assert store.exists(episode_file("run-002", "deploy-zengen-v2-s1-1"))
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_action_for_an_old_model_is_superseded_not_run(store, mem):
    _model(store, 2, "run-002")
    factory = _Factory()
    _start(store, _handler(mem=mem, factory=factory))
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.payload["failure"]["category"] == "superseded"
    assert factory.count == 0
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_pending_proposal_from_an_earlier_run_is_reported_in_dropped(store, mem, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler(mem=mem))
    monkeypatch.setattr(store, "write_product", real)
    # 模拟"上一次运行多产出了一条":手工复制出 -2
    first = store.read_product(proposal_file("proposal-agent-round-zengen-v1-1"))
    first.id = "proposal-agent-round-zengen-v1-2"
    store.write_product(proposal_file(first.id), first, "test: leftover")
    env = _start(store, _handler(mem=mem))
    assert env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    assert env.payload["dropped"] == [{
        "proposal_id": "proposal-agent-round-zengen-v1-2",
        "reason": "stale: left by an earlier run of this round, not produced again; do not approve",
    }]
    assert store.read_product(proposal_file(first.id)).status == "pending_review"  # 不删不改


def test_supersede_only_touches_pending_rounds_of_older_models(store):
    for n, status in ((1, "pending"), (2, "executed"), (3, "pending")):
        store.write_product(agent_round_file(f"zengen-v{n}"), _env(round_id_for(f"zengen-v{n}"), "agent_round",
                            status, {"model_id": f"zengen-v{n}", "run_id": "", "failure": None}), "t")
    assert supersede_stale_rounds(store, 3) == ["agent-round-zengen-v1"]
    assert [store.read_product(agent_round_file(f"zengen-v{n}")).status for n in (1, 2, 3)] == [
        "failed", "executed", "pending"]


CONFLICTED = "<<<<<<< HEAD\nid: agent-round-zengen-v1\n=======\nid: other\n>>>>>>> branch\n"


def _corrupt_round(store, text=CONFLICTED, model="zengen-v1"):
    path = store.repo_root / agent_round_file(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("text", [
    CONFLICTED,                                              # 合并冲突标记:YAML 解析失败
    "id: agent-round-zengen-v1\ntype: agent_round\n",       # 缺必填键:EnvelopeValidationError
], ids=["merge_conflict", "envelope_invalid"])
def test_an_unreadable_round_product_is_rewritten_failed_once_without_raising(store, mem, text):
    _corrupt_round(store, text)
    factory = _Factory()
    _start(store, _handler(mem=mem, factory=factory))  # 不抛:守护进程不会崩溃循环
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.id == "agent-round-zengen-v1"
    assert env.payload["failure"] == {"category": "T3", "reason": "unreadable round product",
                                      "point": "mindagent.rounds.read"}
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["reason"]) == ("T3", "unreadable round product")
    assert factory.count == 0
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler(mem=mem, factory=factory))  # 再来一次 tick:终态,没有动作
    assert store._git("rev-parse", "HEAD") == head
    assert len([p for p in (store.repo_root / run_dir("run-001")).iterdir() if p.name.startswith("failure-")]) == 1


@pytest.mark.parametrize("text", [CONFLICTED, "id: agent-round-zengen-v1\ntype: agent_round\n"],
                         ids=["merge_conflict", "envelope_invalid"])
def test_supersede_skips_an_unreadable_round(store, mem, text):
    _corrupt_round(store, text)
    assert supersede_stale_rounds(store, 5) == []
    assert (store.repo_root / agent_round_file("zengen-v1")).read_text(encoding="utf-8") == text
    _model(store, 2, "run-002")
    (store.repo_root / run_dir("run-002")).mkdir(parents=True)
    env = _start(store, _handler(mem=mem), model="zengen-v2")  # 别的模型的轮次不受这份坏文件影响
    assert env.status == "executed"


def test_an_os_error_reading_the_round_still_propagates(store, mem, monkeypatch):
    _start(store, _handler(mem=mem, scenario_ids=("ghost",)))  # 先有一份轮次产物
    real = store.read_product

    def flaky(rel):
        if rel == agent_round_file("zengen-v1"):
            raise OSError("disk")
        return real(rel)

    monkeypatch.setattr(store, "read_product", flaky)
    with pytest.raises(OSError):
        _start(store, _handler(mem=mem))


@pytest.mark.parametrize("bad_run", ["a/b", "run-ghost"])
def test_a_bad_or_missing_run_id_fails_the_round_without_a_failure_record_or_run_dir(store, mem, bad_run):
    store.write_product(model_file(1), _env("zengen-v1", "zengen_model", "effective",
                        {"promoted_from": "cand", "checkpoint_ref": CKPT.to_dict()}, ["cand", bad_run]), "t")
    _start(store, _handler(mem=mem, scenario_ids=("ghost",)))  # 不抛
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and "scenario 'ghost'" in env.payload["failure"]["reason"]
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-001"]


# ---------------- 部署记忆:人格库锁、起点摘要、T3 回滚、验证快照(设计二第 9 节) ----------------

def test_the_round_records_the_memory_runtime_and_each_episodes_start_digest(store, mem):
    env = _start(store, _handler(mem=mem))
    assert env.payload["memory_runtime"] == STAND_IN_MEMORY_RUNTIME
    digests = env.payload["episode_digests"]
    assert sorted(digests) == sorted(env.payload["episode_ids"])
    assert all(set(d) == {"pa"} for d in digests.values())            # 只备份智能体位的人格
    assert mem.layout.round_snapshots(ROUND) == []                    # 全部已提交
    assert not mem.layout.verify_snapshot(ROUND, "pa").exists()       # 验证快照轮次结束即删
    assert not mem.layout.verify_scratch(ROUND).exists()


def test_the_round_payload_gets_a_copy_of_the_memory_runtime(store, mem):
    runtime = json.loads(json.dumps(STAND_IN_MEMORY_RUNTIME))
    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(), backend_factory=lambda m, c: Echo(),
                                 personas_loader=_loader, memory_store=mem.store, memory_layout=mem.layout,
                                 memory_runtime=runtime, scratch_store_factory=FileTupleStore)
    runtime["backend"] = "mutated-after-assembly"
    assert _start(store, h).payload["memory_runtime"]["backend"] == "stand-in"


def test_a_round_that_cannot_lock_a_persona_library_is_deferred(store, mem):
    # 修补第 1 轮:锁被占不判 T3,抛 DeferredError 让守护进程退避;拿锁在写任何东西之前
    from controlplane.deferred import DeferredError
    with hold_file_lock(mem.layout.deploy_lock("pb")):                # 对手人格也要锁(第 9 节"每个绑定人格")
        with pytest.raises(DeferredError, match="pb.lock"):
            _start(store, _handler(mem=mem))
    assert not store.exists(agent_round_file("zengen-v1"))
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))
    assert _start(store, _handler(mem=mem)).status == "executed"     # 锁放了,下一 tick 照常跑


def test_a_t3_round_rolls_back_the_in_flight_episode_and_drops_its_copy(store, mem):
    factory = _Factory(plans=[{}, {"create_error": SessionRejectedError("invalid_session", "bad")}])
    env = _start(store, _handler(factory=factory, mem=mem))
    assert env.status == "failed" and env.payload["failure"]["category"] == "T3"
    second = "deploy-zengen-v1-s2-1"
    assert mem.store.digest("pa") == env.payload["episode_digests"][second]["pa"]
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_data_error_mid_episode_is_rolled_back_by_the_round(store, mem):
    # 实例的数据错误不经驱动器覆盖(原样上抛);轮次判 T3 时由处理器用起点副本覆盖、再删副本
    from actio.errors import ActioDataError

    class Spoiled(Echo):
        calls = 0

        def respond_guided(self, *a):
            Spoiled.calls += 1
            if Spoiled.calls == 4:                                     # s2 的第二次回复:开场已记进部署库
                raise ActioDataError("bad reply")
            return super().respond_guided(*a)

    backend = Spoiled()
    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(), backend_factory=lambda m, c: backend,
                                 personas_loader=_loader, memory_store=mem.store, memory_layout=mem.layout,
                                 memory_runtime=STAND_IN_MEMORY_RUNTIME, scratch_store_factory=FileTupleStore)
    env = _start(store, h)
    _assert_failed(store, "mindagent.rounds", "ActioDataError")
    assert mem.store.digest("pa") == env.payload["episode_digests"]["deploy-zengen-v1-s2-1"]["pa"]
    assert mem.store.pending_events("pa") == []
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_crash_mid_episode_resumes_from_the_restored_start(store, mem):
    class Crash(Exception):
        pass

    factory = _Factory(plans=[{}, {"crash_on_submit": Crash}])
    with pytest.raises(Crash):
        _start(store, _handler(factory=factory, mem=mem))
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert len(mem.layout.round_snapshots(ROUND)) == 1
    again = _Factory()
    env = _start(store, _handler(factory=again, mem=mem))
    assert env.status == "executed" and again.count == 2               # s2 重跑、s3 新跑,s1 复用
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_failed_restore_fails_the_round_and_keeps_library_and_copy(store, mem):
    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        _start(store, _handler(factory=_Factory(plans=[{"crash_on_submit": Crash}]), mem=mem))
    env = store.read_product(agent_round_file("zengen-v1"))
    env.payload["episode_digests"]["deploy-zengen-v1-s1-1"]["pa"] = "tampered"
    store.write_product(agent_round_file("zengen-v1"), env, "test: tamper")
    _start(store, _handler(mem=mem))
    _assert_failed(store, "mindagent.deployment.restore", "digest mismatch")
    assert len(mem.layout.round_snapshots(ROUND)) == 1                # 副本保留;库停在覆盖后的状态,不再回滚


def test_verification_replays_on_read_only_snapshot_copies(store, mem, monkeypatch):
    import mindagent.rounds as rounds
    calls = []
    real = rounds.instantiate
    monkeypatch.setattr(rounds, "instantiate", lambda *a, **kw: calls.append(kw) or real(*a, **kw))
    _skill(store, GOAL)
    env = _start(store, _handler(mem=mem))
    assert env.payload["proposals"]                                    # 有草案才会验证
    replays = [kw for kw in calls if kw.get("replay_read_only")]
    assert replays and len(replays) % 2 == 0                           # 每份草案基线与候选各一
    assert all(kw["memory"] is not None for kw in replays)
    assert mem.store.pending_events("pa") == []


# ---------------- 修补第 1 轮:轮次写成终态之前先回滚 ----------------

def _kept_copy(store, mem):
    """造一个 pending 轮次,s1 在途时崩:起点副本留着,部署库里有半截记忆。返回 (起点摘要, 副本路径)。"""
    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        _start(store, _handler(factory=_Factory(plans=[{"crash_on_submit": Crash}]), mem=mem))
    (snap,) = mem.layout.round_snapshots(ROUND)
    start = store.read_product(agent_round_file("zengen-v1")).payload["episode_digests"]["deploy-zengen-v1-s1-1"]["pa"]
    mem.store.remember_event("pa", f"{ROUND}/deploy-zengen-v1-s1-1#0", "pb：我真的很生气!!", interlocutor="pb")
    assert mem.store.digest("pa") != start
    return start, snap


def test_a_superseded_round_with_a_kept_copy_is_rolled_back_first(store, mem):
    start, snap = _kept_copy(store, mem)
    _model(store, 2, "run-002")
    (store.repo_root / run_dir("run-002")).mkdir(parents=True)
    env2 = _start(store, _handler(mem=mem), model="zengen-v2")
    old = store.read_product(agent_round_file("zengen-v1"))
    assert old.status == "failed" and old.payload["failure"]["category"] == "superseded"
    assert not snap.exists()
    # v1 的半截记忆在 v2 开跑之前就被覆盖回起点:v2 第一条 episode 的起点摘要就是 v1 那条的起点
    assert env2.payload["episode_digests"]["deploy-zengen-v2-s1-1"]["pa"] == start


def test_a_superseded_round_whose_persona_library_is_locked_is_deferred(store, mem):
    from controlplane.deferred import DeferredError
    start, snap = _kept_copy(store, mem)
    _model(store, 2, "run-002")
    with hold_file_lock(mem.layout.deploy_lock("pa")):
        with pytest.raises(DeferredError, match="pa.lock"):
            _start(store, _handler(mem=mem), model="zengen-v2")
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert snap.exists() and mem.store.digest("pa") != start


def test_a_stale_action_for_a_round_with_a_kept_copy_rolls_it_back(store, mem):
    # 旧模型自己的动作:supersede 先回滚并置 failed;处理器内 version < latest 那一支见到已是终态就不再迁移
    start, snap = _kept_copy(store, mem)
    _model(store, 2, "run-002")
    _start(store, _handler(mem=mem))
    assert store.read_product(agent_round_file("zengen-v1")).payload["failure"]["category"] == "superseded"
    assert mem.store.digest("pa") == start and not snap.exists()


def test_a_round_whose_model_disappeared_rolls_back_its_kept_copy(store, mem):
    # _read_model 失败那一支
    start, snap = _kept_copy(store, mem)
    env = store.read_product(model_file(1))
    env.status = "deprecated"
    store.write_product(model_file(1), env, "human: deprecate")
    _start(store, _handler(mem=mem))
    assert store.read_product(agent_round_file("zengen-v1")).status == "failed"
    assert mem.store.digest("pa") == start and not snap.exists()


def test_a_round_that_fails_its_inputs_on_reentry_rolls_back_its_kept_copy(store, mem):
    # _check_inputs 的 T3 那一支
    start, snap = _kept_copy(store, mem)
    (store.repo_root / scenario_file("s3")).unlink()
    _start(store, _handler(mem=mem))
    _assert_failed(store, "mindagent.rounds.inputs", "scenario 's3'")
    assert mem.store.digest("pa") == start and not snap.exists()


def test_a_failed_rollback_still_fails_the_round_and_keeps_library_and_copy(store, mem, monkeypatch):
    start, snap = _kept_copy(store, mem)
    before = mem.store.digest("pa")

    def broken(pid, path):
        raise OSError("disk")

    monkeypatch.setattr(mem.store, "restore_from", broken)
    _model(store, 2, "run-002")
    _start(store, _handler(mem=mem))
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.payload["failure"]["point"] == "mindagent.deployment.restore"
    assert "OSError" in env.payload["failure"]["reason"] and "disk" not in env.payload["failure"]["reason"]
    assert snap.exists() and mem.store.digest("pa") == before


# ---------------- 终审 I-2:记忆层的确定性错误判 T3,不从处理器逃出 ----------------

def _tick(store, handler):
    """守护进程的一个 tick:只接 DeferredError,别的异常照样逃出(就是 I-2 说的守护进程退出)。"""
    from controlplane.daemon import reconcile_once
    return reconcile_once(store, handlers={"start_agent_round": handler})


def test_a_corrupt_deployment_library_fails_the_round_t3_and_the_tick_does_not_raise(store, mem):
    path = mem.layout.root / "deploy" / "pa.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _tick(store, _handler(mem=mem))
    fr = _assert_failed(store, "mindagent.deployment.memory", "backup deploy-zengen-v1-s1-1 for pa: MemoryFailureError")
    assert "not json" not in fr.payload["reason"]
    _tick(store, _handler(mem=mem))                                      # 终态:下一 tick 不再触发
    assert store.read_product(agent_round_file("zengen-v1")).status == "failed"


def test_a_snapshot_directory_that_cannot_be_created_fails_the_round_t3(store, mem):
    (mem.layout.root / "snapshots").parent.mkdir(parents=True, exist_ok=True)
    (mem.layout.root / "snapshots").write_text("a file where the directory should be", encoding="utf-8")
    _tick(store, _handler(mem=mem))
    fr = _assert_failed(store, "mindagent.deployment.memory", "snapshot dir for deploy-zengen-v1-s1-1: NotADirectoryError")
    assert "should be" not in fr.payload["reason"] and str(mem.layout.root) not in fr.payload["reason"]


def test_a_failing_verify_snapshot_fails_the_round_t3(store, mem, monkeypatch):
    real = mem.store.backup_to

    def backup_to(pid, path):
        if path.name.startswith("verify-"):
            raise OSError("disk full at /secret/path")
        return real(pid, path)

    monkeypatch.setattr(mem.store, "backup_to", backup_to)
    _tick(store, _handler(mem=mem))
    fr = _assert_failed(store, "mindagent.rounds.verify", "OSError")
    assert "/secret/path" not in fr.payload["reason"]
    assert mem.layout.round_snapshots(ROUND) == []


def test_a_persona_id_the_lock_refuses_fails_the_round_t3_without_rollback(store, mem):
    # 装配期已拦(read_agent_settings);这里绕过装配直接造处理器,locks.hold 的 ActioDataError 不能逃出
    bad = RoleBinding("a", "张三", "self_play")
    _tick(store, _handler(mem=mem, bindings=(OPP, bad)))
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.payload["failure"]["category"] == "T3"
    assert env.payload["failure"]["point"] == "mindagent.rounds.persona"
    assert "ActioDataError" in env.payload["failure"]["reason"]
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T3", "mindagent.rounds.persona")


def test_a_verify_copy_that_cannot_be_restored_fails_the_round_t3(store, mem):
    # 验证重放的临时副本 restore_from 抛永久 MemoryFailureError:T3(失败点 mindagent.rounds.verify),只报类名
    from actio.memory_port import MemoryFailureError

    class BrokenScratch(FileTupleStore):
        def restore_from(self, persona_id, path):
            raise MemoryFailureError("JSONDecodeError at /secret", transient=False)

    _skill(store, GOAL)                                                  # 有草案才会验证
    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(), backend_factory=lambda m, c: Echo(),
                                 personas_loader=_loader, memory_store=mem.store, memory_layout=mem.layout,
                                 memory_runtime=STAND_IN_MEMORY_RUNTIME, scratch_store_factory=BrokenScratch)
    _tick(store, h)
    fr = _assert_failed(store, "mindagent.rounds.verify", "verify snapshot: MemoryFailureError")
    assert "/secret" not in fr.payload["reason"]
    assert not (mem.layout.root / "snapshots" / ROUND).exists()          # 验证收尾照常清掉


def test_an_executed_round_leaves_no_empty_snapshot_directory(store, mem):
    # 终审 M-3:验证收尾之后 snapshots/<round_id>/ 已空就删掉,不每轮留一个空目录
    _skill(store, GOAL)
    env = _start(store, _handler(mem=mem))
    assert env.status == "executed" and env.payload["proposals"]
    assert not (mem.layout.root / "snapshots" / ROUND).exists()
