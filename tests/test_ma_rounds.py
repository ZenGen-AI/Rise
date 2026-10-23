import json

import grpc
import pytest

from actio.skills import Skill
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT, run_dir, agent_round_file, episode_file, episodes_dir, failure_record_file, model_file, proposal_file, skill_file,
)
from controlplane.reconcile import Action
from inference_bridge import BridgeError, ScriptedBridge
from mindagent.config import AgentConfig
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

    def __init__(self, lines, *, create_error=None, end_reason="max_turns"):
        self._lines = list(lines)
        self._create_error = create_error
        self._end_reason = end_reason
        self._turns = []
        self.closed = False
        self.created = None

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", self._lines[0]))
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
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

    def __call__(self):
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


def _cfg(**kw):
    base = dict(scenario_ids=("s1", "s2", "s3"), bindings=(OPP, AGENT), max_turns=4,
                min_support=3, min_ratio=0.5)
    base.update(kw)
    return AgentConfig(**base)


def _loader(store, ids):
    return {pid: read_persona(store, pid) for pid in ids}


def _handler(factory=None, bridge=None, backends=None, **cfg):
    seen = backends if backends is not None else []

    def backend_factory(model_id, ckpt):
        seen.append((model_id, ckpt))
        return Echo()

    return make_start_round_handler(config=_cfg(**cfg), client_factory=factory or _Factory(),
                                    backend_factory=backend_factory, personas_loader=_loader, bridge=bridge)


def _start(store, handler, model="zengen-v1"):
    handler(store, Action(kind="start_agent_round", target_id=model, detail={}))
    return store.read_product(agent_round_file(model))


def test_the_first_round_with_an_empty_library_is_silent_by_design(store):
    backends = []
    env = _start(store, _handler(backends=backends))
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


def test_a_library_that_ignores_emotion_produces_a_pending_review_proposal(store):
    _skill(store, GOAL)
    env = _start(store, _handler())
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


def test_dropped_drafts_are_recorded_in_the_round(store):
    strong = dict(GOAL, id="skill-top", priority=1000)
    _skill(store, strong)
    env = _start(store, _handler())
    assert env.status == "executed"
    assert env.payload["proposals"] == []
    (dropped,) = env.payload["dropped"]
    assert dropped["fired_on_motivating"] == 0.0
    assert dropped["reason"] == "fired_on_motivating 0.000 < 0.8"


def test_with_a_bridge_the_round_records_the_prompt_version_and_the_cost(store):
    _skill(store, GOAL)
    good = json.dumps({"instructions": ["先安抚"], "description": "d"})
    bridge = ScriptedBridge([good], store=store)
    env = _start(store, _handler(bridge=bridge))
    assert env.payload["generator_version"] == "bridge.v1"
    assert env.payload["prompt_template_version"] == "mindagent-skill-v1"
    assert env.payload["uses_bridge"] is True
    assert bridge.calls[0][1:] == ("run-001", "agent-zengen-v1-propose-1-attempt1")


def test_an_executed_round_is_a_noop(store):
    h = _handler()
    _start(store, h)
    head = store._git("rev-parse", "HEAD")
    factory = _Factory()
    _start(store, _handler(factory=factory))
    assert store._git("rev-parse", "HEAD") == head and factory.count == 0


def test_a_pending_round_resumes_without_regenerating_persisted_episodes(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_proposal(rel, env, message):
        if "skill_memory_proposals" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_proposal)
    with pytest.raises(Crash):
        _start(store, _handler())
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert len(store.list_products(episodes_dir("run-001"))) == 3
    monkeypatch.setattr(store, "write_product", real)
    factory = _Factory()
    env = _start(store, _handler(factory=factory))
    assert factory.count == 0
    assert env.status == "executed" and env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]


def test_a_crash_after_packaging_reruns_into_the_same_proposal_id(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product
    calls = {"n": 0}

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    env = _start(store, _handler())
    assert env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir())) == 1


def test_a_transient_failure_propagates_and_leaves_the_round_pending(store):
    factory = _Factory(plans=[{"create_error": _Unavailable()}] * 2)
    with pytest.raises(Exception) as exc:
        _start(store, _handler(factory=factory, max_retries_per_episode=1))
    assert type(exc.value).__name__ == "DeploymentFailedError" and exc.value.transient
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))
    env = _start(store, _handler())
    assert env.status == "executed"


def test_an_unexpected_exception_propagates_and_leaves_the_round_pending(store):
    def loader(store, ids):
        raise Crash("disk")

    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(),
                                 backend_factory=lambda m, c: Echo(), personas_loader=loader)
    with pytest.raises(Crash):
        _start(store, h)
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"


def test_an_exhausted_bridge_t1_fails_the_round_r18(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge([BridgeError("T1", "timeout", attempts=3)])))
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


def test_a_bridge_t2_fails_the_round(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge([BridgeError("T2", "no such tool")])))
    _assert_failed(store, "mindagent.proposals", "bridge T2: no such tool")


def test_unparseable_bridge_output_fails_the_round_after_bounded_retries(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge(["x", "y"]), max_parse_retries=1))
    fr = _assert_failed(store, "mindagent.proposals", "not a single JSON object")
    assert fr.payload["retries"] == 1


def test_a_rejected_session_fails_the_round(store):
    factory = _Factory(plans=[{"create_error": SessionRejectedError("invalid_session", "bad persona")}])
    _start(store, _handler(factory=factory))
    _assert_failed(store, "mindagent.deployment", "invalid_session")


def test_a_missing_scenario_fails_the_round_before_any_session(store):
    factory = _Factory()
    _start(store, _handler(factory=factory, scenario_ids=("s1", "ghost")))
    _assert_failed(store, "mindagent.rounds.inputs", "scenario 'ghost'")
    assert factory.count == 0


def test_a_malformed_library_entry_fails_the_round(store):
    _skill(store, dict(GOAL, level="huge"))
    _start(store, _handler())
    _assert_failed(store, "mindagent.rounds", "ActioDataError")


def test_a_failed_round_is_terminal(store):
    _start(store, _handler(scenario_ids=("ghost",)))
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler())
    assert store._git("rev-parse", "HEAD") == head


def test_an_unknown_model_fails_the_round_without_a_failure_record(store):
    _start(store, _handler(), model="zengen-v9")
    env = store.read_product(agent_round_file("zengen-v9"))
    assert env.status == "failed" and "not found" in env.payload["failure"]["reason"]


def test_a_model_with_an_unusable_checkpoint_ref_fails_the_round_instead_of_escaping(store):
    """手改进树的坏引用(I8):轮次 failed、类别 T3、原因点名字段;InvalidBigRefError 不能从处理器
    漏出去(调用方只接 AgentRoundFailedError,漏出去会打崩守护进程)。"""
    env = store.read_product(model_file(1))
    env.payload["checkpoint_ref"] = {**CKPT.to_dict(), "sha256": "../../etc"}
    store.write_product(model_file(1), env, "human: spoil checkpoint_ref")
    _start(store, _handler())
    rnd = store.read_product(agent_round_file("zengen-v1"))
    assert rnd.status == "failed"
    assert rnd.payload["failure"]["category"] == "T3"
    assert "sha256" in rnd.payload["failure"]["reason"]


def test_a_newer_model_supersedes_a_pending_round_and_an_old_action_is_refused(store, monkeypatch):
    real = store.write_product

    def crash(rel, env, message):
        if "episodes/" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    _model(store, 2, "run-002")
    env2 = _start(store, _handler(), model="zengen-v2")
    assert env2.status == "executed" and env2.payload["run_id"] == "run-002"
    old = store.read_product(agent_round_file("zengen-v1"))
    assert old.status == "failed"
    assert old.payload["failure"] == {"category": "superseded", "reason": "superseded by zengen-v2",
                                      "point": "mindagent.rounds.supersede"}
    assert env2.payload["episode_ids"][0] == "deploy-zengen-v2-s1-1"
    assert store.exists(episode_file("run-002", "deploy-zengen-v2-s1-1"))
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_action_for_an_old_model_is_superseded_not_run(store):
    _model(store, 2, "run-002")
    factory = _Factory()
    _start(store, _handler(factory=factory))
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.payload["failure"]["category"] == "superseded"
    assert factory.count == 0
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_pending_proposal_from_an_earlier_run_is_reported_in_dropped(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    # 模拟"上一次运行多产出了一条":手工复制出 -2
    first = store.read_product(proposal_file("proposal-agent-round-zengen-v1-1"))
    first.id = "proposal-agent-round-zengen-v1-2"
    store.write_product(proposal_file(first.id), first, "test: leftover")
    env = _start(store, _handler())
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
def test_an_unreadable_round_product_is_rewritten_failed_once_without_raising(store, text):
    _corrupt_round(store, text)
    factory = _Factory()
    _start(store, _handler(factory=factory))  # 不抛:守护进程不会崩溃循环
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.id == "agent-round-zengen-v1"
    assert env.payload["failure"] == {"category": "T3", "reason": "unreadable round product",
                                      "point": "mindagent.rounds.read"}
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["reason"]) == ("T3", "unreadable round product")
    assert factory.count == 0
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler(factory=factory))  # 再来一次 tick:终态,没有动作
    assert store._git("rev-parse", "HEAD") == head
    assert len([p for p in (store.repo_root / run_dir("run-001")).iterdir() if p.name.startswith("failure-")]) == 1


@pytest.mark.parametrize("text", [CONFLICTED, "id: agent-round-zengen-v1\ntype: agent_round\n"],
                         ids=["merge_conflict", "envelope_invalid"])
def test_supersede_skips_an_unreadable_round(store, text):
    _corrupt_round(store, text)
    assert supersede_stale_rounds(store, 5) == []
    assert (store.repo_root / agent_round_file("zengen-v1")).read_text(encoding="utf-8") == text
    _model(store, 2, "run-002")
    (store.repo_root / run_dir("run-002")).mkdir(parents=True)
    env = _start(store, _handler(), model="zengen-v2")  # 别的模型的轮次不受这份坏文件影响
    assert env.status == "executed"


def test_an_os_error_reading_the_round_still_propagates(store, monkeypatch):
    _start(store, _handler(scenario_ids=("ghost",)))  # 先有一份轮次产物
    real = store.read_product

    def flaky(rel):
        if rel == agent_round_file("zengen-v1"):
            raise OSError("disk")
        return real(rel)

    monkeypatch.setattr(store, "read_product", flaky)
    with pytest.raises(OSError):
        _start(store, _handler())


@pytest.mark.parametrize("bad_run", ["a/b", "run-ghost"])
def test_a_bad_or_missing_run_id_fails_the_round_without_a_failure_record_or_run_dir(store, bad_run):
    store.write_product(model_file(1), _env("zengen-v1", "zengen_model", "effective",
                        {"promoted_from": "cand", "checkpoint_ref": CKPT.to_dict()}, ["cand", bad_run]), "t")
    _start(store, _handler(scenario_ids=("ghost",)))  # 不抛
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and "scenario 'ghost'" in env.payload["failure"]["reason"]
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-001"]
