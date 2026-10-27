"""MindAgent 端到端:真实 loopback gRPC 服务端 + 计划 6 的激活轨迹协议。

空库第一轮零提案 → 手工放一个不处理情绪的基线技能 → 第二轮发现 emotion_high 缺口并打包提案
→ 人工批准 → 收录 → 第三轮绑定新库版本,新技能在轨迹里被选中。"""
import grpc
import pytest

from actio.trace import validate_trace
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import agent_round_file, library_state_file, model_file, proposal_file, skill_file
from controlplane.reconcile import Action
from mindagent.admit import handle_admit_proposal
from mindagent.config import AgentConfig
from mindagent.library import load_library
from mindagent.rounds import make_start_round_handler
from realtime_fixtures import plain_factory
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_episode, read_persona, read_scenario, write_persona, write_scenario


class _DeadlineStub:
    """给每条 Interact 流加 gRPC deadline:服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED
    结束,而不是挂住整个测试套件(套件没有单测超时)。"""

    def __init__(self, stub, timeout_s=15.0):
        self._stub = stub
        self._timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self._timeout_s)


AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
OPP_LINES = ["我真的很生气!!", "好吧,你说说看", "那就这样吧"]
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": "baseline: ignores emotion"}


class Echo:
    """罐头带指导语后端:把指导语拼进话语,技能是否生效直接看得见。"""

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, links=list(links), payload=payload)


def _promote(store, n, run_id):
    ckpt = BigRef(f"file:///ckpt/v{n}", f"{n}" * 64, 1)
    store.write_product(model_file(n), _env(f"zengen-v{n}", "zengen_model", "effective",
                        {"promoted_from": f"cand-{run_id}", "checkpoint_ref": ckpt.to_dict()},
                        [f"cand-{run_id}", run_id]), f"test: promote zengen-v{n}")


@pytest.fixture
def world(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(store, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"],
                                       "deployment_simulation"))
    for pid in ("pa", "pb"):
        write_persona(store, Persona(pid, ["t"], "m", "s", ["v"]))

    def factory(scenario, assignment, personas, client_slot):
        return {s: ScriptedBackend({s: list(OPP_LINES)}) for s in assignment.slot_ids() - {client_slot}}

    server = create_server(plain_factory(factory), port=0, turn_timeout_s=2.0,
                           scenario_resolver=lambda sid: read_scenario(store, sid))
    server.start()
    channels = []

    def client_factory():
        channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        channels.append(channel)
        return EnvironmentServiceClient(_DeadlineStub(pb2_grpc.EnvironmentServiceStub(channel)))

    handler = make_start_round_handler(
        config=AgentConfig(scenario_ids=("s1", "s2", "s3"), bindings=(OPP, AGENT), max_turns=4,
                           min_support=3, min_ratio=0.5),
        client_factory=client_factory,
        backend_factory=lambda model_id, ckpt: Echo(),
        personas_loader=lambda s, ids: {pid: read_persona(s, pid) for pid in ids},
    )
    yield store, handler
    for c in channels:
        c.close()
    server.stop(0).wait()


def _round(store, handler, model):
    handler(store, Action(kind="start_agent_round", target_id=model, detail={}))
    return store.read_product(agent_round_file(model))


def _agent_traces(store, run_id, episode_ids):
    out = []
    for eid in episode_ids:
        ep = read_episode(store, run_id, eid)
        assert all(t.activation_trace is None for t in ep.turns if t.speaker_slot == "b")
        for t in ep.turns:
            if t.speaker_slot == "a":
                validate_trace(t.activation_trace)
                out.append((t.utterance, t.activation_trace))
    return out


def test_the_full_agent_rsi_loop_over_a_real_server(world):
    store, handler = world

    # 第一轮:空库,裸路径,零提案
    _promote(store, 1, "run-001")
    r1 = _round(store, handler, "zengen-v1")
    assert r1.status == "executed" and r1.payload["proposals"] == [] and r1.payload["findings"] == []
    traces = _agent_traces(store, "run-001", r1.payload["episode_ids"])
    assert len(traces) == 6 and all(t["selected"] is None for _, t in traces)
    assert traces[0][1]["signals"] == ["emotion_high", "first_turn"]

    # 手工放一个不处理情绪的基线技能(库版本 1)
    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")

    # 第二轮:发现缺口,提案待审
    _promote(store, 2, "run-002")
    r2 = _round(store, handler, "zengen-v2")
    assert r2.status == "executed"
    assert r2.payload["library_version"] == {"skill": "1", "memory": "0"}
    (finding,) = r2.payload["findings"]
    assert (finding["signal"], finding["support"], finding["ratio"]) == ("emotion_high", 3, 1.0)
    (pid,) = r2.payload["proposals"]
    assert pid == "proposal-agent-round-zengen-v2-1"
    proposal = store.read_product(proposal_file(pid))
    assert proposal.status == "pending_review"
    v = proposal.payload["verification"]
    assert (v["method"], v["fired_on_motivating"], v["fired_elsewhere"]) == ("replay_behavior", 1.0, 0.0)
    assert v["episodes"] == finding["episode_refs"] == r2.payload["episode_ids"]
    assert {u for u, t in _agent_traces(store, "run-002", r2.payload["episode_ids"])} == {"[skill:skill-goal] 推进目标"}

    # 门②:人工批准,收录
    proposal.status = "approved"
    store.write_product(proposal_file(pid), proposal, "human: approve")
    handle_admit_proposal(store, Action(kind="admit_proposal", target_id=pid, detail={}))
    assert store.read_product(proposal_file(pid)).status == "effective"
    lib = load_library(store)
    assert lib.version == {"skill": "2", "memory": "0"}
    assert [s.id for s in lib.skills] == ["skill-agent-round-zengen-v2-1-1", "skill-goal"]

    # 第三轮:绑定新库,新技能在动机轮次上被选中,其余轮次仍是基线技能
    _promote(store, 3, "run-003")
    r3 = _round(store, handler, "zengen-v3")
    assert r3.status == "executed" and r3.payload["library_version"] == {"skill": "2", "memory": "0"}
    assert r3.payload["findings"] == [] and r3.payload["proposals"] == []
    traces = _agent_traces(store, "run-003", r3.payload["episode_ids"])
    new_id = "skill-agent-round-zengen-v2-1-1"
    first_turns = traces[0::2]
    assert all(t["selected"] == new_id and t["library_version"] == {"skill": "2", "memory": "0"}
               for _, t in first_turns)
    assert all(t["candidates"] == [new_id, "skill-goal"] and t["conflict"] is not None for _, t in first_turns)
    assert first_turns[0][0] == f"[skill:{new_id}] 先回应对方的情绪,确认对方的感受,再谈具体问题"
    assert all(t["selected"] == "skill-goal" for _, t in traces[1::2])
