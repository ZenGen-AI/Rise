import json
import threading
import time
from types import SimpleNamespace

import grpc
import pytest

from simple_actio.instance import instantiate
from simple_actio.memory import FileTupleStore
from simple_actio.memory_port import MemoryEvent, MemoryEventConflict, MemoryFailureError
from simple_actio.skills import Skill
from simple_actio.trace import validate_trace
from controlplane.deferred import DeferredError
from controlplane.gitstore import GitStore
from controlplane.paths import episode_file, episodes_dir
from mindagent import deployment
from mindagent.deployment import (
    PURPOSE, DeploymentFailedError, deployment_episode_id, rerun_episode_id, run_deployment_episodes,
)
from mindagent.errors import MindAgentDataError, RestoreFailedError
from mindagent.memory_layout import MemoryLayout, SnapshotRecord
from realtime_fixtures import plain_factory, plain_hook
from simulator.backends import ScriptedBackend
from simulator.datamodel import Episode, Persona, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_episode, read_scenario, write_persona, write_scenario


class _DeadlineStub:
    """给每条 Interact 流加 gRPC deadline:服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED
    结束,而不是挂住整个测试套件(套件没有单测超时)。"""

    def __init__(self, stub, timeout_s=15.0):
        self._stub = stub
        self._timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self._timeout_s)


RUN = "run-003"
MODEL = "zengen-v1"
AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
BINDINGS = (OPP, AGENT)  # 对手先说,SimpleActio 实例后说
ROUND = "agent-round-zengen-v1"
EID = "deploy-zengen-v1-s1-1"
KEY0 = f"{ROUND}/{EID}#0@pa"       # 智能体记下对手开场那句的事件 key:轮次前缀 + 部署 id + 轮次序号 + 写入方主体
WORLD = "s1"                        # 部署世界就是情境
V0 = {"skill": "0", "memory": "0"}


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


CALM = Skill("skill-calm", 1, "micro", {"signal": "emotion_high"}, ("先回应情绪",), priority=1)


def _make(skills=(), version=None, seen=None, backend=None):
    def make(episode_id, memory, holder):
        if seen is not None:
            seen.append(episode_id)
        return instantiate(backend or Echo(), skills=list(skills), memory=memory, episode_id=episode_id,
                           holder=holder, interlocutors={"a": "pa", "b": "pb", "c": "pc"},
                           library_version=version or V0, profile=None)
    return make


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2"):
        write_scenario(s, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], PURPOSE))
    for p in _personas().values():
        write_persona(s, p)
    return s


@pytest.fixture
def mem(tmp_path):
    failing: set[str] = set()      # 测试往里放 key,该事件的抽取就以 timeout 失败
    root = tmp_path / "mem"
    store = FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay",
                           extraction_failure=lambda ev: "timeout" if ev.key in failing else None)
    return SimpleNamespace(store=store, layout=MemoryLayout(tmp_path / "mem"), failing=failing)


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start(lines=("我真的很生气!!", "好吧", "还有呢")):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {s: ScriptedBackend({s: list(lines)}) for s in assignment.slot_ids() - {client_slot}}

        server = create_server(plain_factory(factory), port=0, turn_timeout_s=2.0,
                               on_session_closed=plain_hook(closed.append),
                               scenario_resolver=lambda sid: read_scenario(store, sid))
        server.start()
        servers.append(server)

        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(_DeadlineStub(pb2_grpc.EnvironmentServiceStub(channel)))

        return SimpleNamespace(server=server, closed=closed, client_factory=client_factory)

    yield start
    for c in channels:
        c.close()
    for s in servers:
        s.stop(0).wait()


def _run(store, client_factory, mem, *, make=None, digests=None, started=None, **kw):
    digests = {} if digests is None else digests

    def on_start(eid, digest):
        digests[eid] = digest
        if started is not None:
            started.append((eid, digest))

    args = dict(run_id=RUN, round_id=ROUND, scenario_ids=["s1"], personas=_personas(), bindings=BINDINGS,
                model_id=MODEL, max_turns=4, library_version=V0)
    args.update(kw)
    return run_deployment_episodes(
        store, make or _make(version=args["library_version"]), client_factory,
        memory_store=mem.store, memory_layout=mem.layout, episode_digests=digests,
        on_episode_start=on_start, **args)


# ---------------- 真实 loopback 服务端 ----------------

def test_episodes_are_persisted_under_the_bound_run_with_deploy_ids(store, live, mem):
    env = live()
    episodes = _run(store, env.client_factory, mem, make=_make([CALM]), scenario_ids=["s1", "s2"],
                    episodes_per_scenario=2)
    ids = [e.id for e in episodes]
    assert ids == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s1-2",
                   "deploy-zengen-v1-s2-1", "deploy-zengen-v1-s2-2"]
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, i) for i in sorted(ids)]
    for e in episodes:
        persisted = read_episode(store, RUN, e.id)
        assert persisted.to_dict() == e.to_dict()
        assert [t.speaker_slot for t in persisted.turns] == ["b", "a", "b", "a"]
        assert persisted.terminated_reason == "max_turns"


def test_agent_turns_carry_traces_and_opponent_turns_do_not(store, live, mem):
    env = live()
    (episode,) = _run(store, env.client_factory, mem, make=_make([CALM], {"skill": 2, "memory": 0}),
                      library_version={"skill": "2", "memory": "0"})
    persisted = read_episode(store, RUN, episode.id)
    agent = [t for t in persisted.turns if t.speaker_slot == "a"]
    opp = [t for t in persisted.turns if t.speaker_slot == "b"]
    assert all(t.activation_trace is None and t.inner_thought is None for t in opp)
    for t in agent:
        validate_trace(t.activation_trace)
        assert t.activation_trace["library_version"] == {"skill": "2", "memory": "0"}
    assert agent[0].activation_trace["selected"] == "skill-calm"  # 对方开场"我真的很生气!!"
    assert agent[0].utterance == "[skill:skill-calm] 先回应情绪"
    assert agent[1].activation_trace["selected"] is None          # "好吧"


def test_the_agent_sees_the_opponents_opening_turn(store, live, mem):
    env = live()
    seen = []

    class Recorder(Echo):
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return super().respond_guided(persona, scenario, history, speaker_slot, guidance)

    _run(store, env.client_factory, mem, make=_make(backend=Recorder()))
    assert seen[0] == [("b", "我真的很生气!!")]
    assert seen[1][-1] == ("b", "好吧")


def test_the_server_side_copy_carries_the_logical_id_and_matches_the_deploy_copy(store, live, mem):
    env = live()
    (episode,) = _run(store, env.client_factory, mem, make=_make([CALM]))
    deadline = time.monotonic() + 3
    while not env.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    (server_side,) = env.closed
    assert server_side.id == episode.id == EID                 # 服务端拿到不带轮次的逻辑 id,不再是 ep-<session>
    assert [t.to_dict() for t in server_side.turns] == [t.to_dict() for t in episode.turns]
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, EID)]   # 落盘只归驱动器


def test_reentry_reuses_persisted_episodes_and_runs_only_the_missing_ones(store, live, mem):
    env = live()
    digests = {}
    _run(store, env.client_factory, mem, digests=digests)
    created = []

    def counting(**kw):
        created.append(1)
        return env.client_factory(**kw)

    episodes = _run(store, counting, mem, digests=digests, episodes_per_scenario=2)
    assert [e.id for e in episodes] == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s1-2"]
    assert len(created) == 1


def test_a_stopped_server_is_transient_and_retried_then_reported(store, live, mem):
    env = live()
    env.server.stop(0).wait()
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, env.client_factory, mem, max_retries_per_episode=1)
    assert exc.value.reason == "transport_unavailable"
    assert exc.value.transient is True and exc.value.attempts == 2
    assert store.list_products(episodes_dir(RUN)) == []


def test_an_instance_exception_aborts_the_session_and_nothing_is_persisted(store, live, mem):
    env = live()

    class Boom(Exception):
        pass

    class Exploding(Echo):
        calls = 0

        def respond_guided(self, *a):
            Exploding.calls += 1
            if Exploding.calls == 2:
                raise Boom("model bug")
            return super().respond_guided(*a)

    with pytest.raises(Boom):
        _run(store, env.client_factory, mem, make=_make(backend=Exploding()))
    env.server.stop(2).wait()
    assert env.closed == []
    assert store.list_products(episodes_dir(RUN)) == []


# ---------------- 替身客户端 ----------------

class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "reset"


class _FakeClient:
    """客户端后说:create 时收到对方开场,每次提交后对方回一句,max_turns=4。"""

    def __init__(self, *, create_error=None, end_reason="max_turns", opening="我很生气!!"):
        self._create_error = create_error
        self._end_reason = end_reason
        self._opening = opening
        self._turns = []
        self.closed = False
        self.aborted = False
        self.created = None
        self.traces = []

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot,
                       episode_id=None):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment,
                            purpose=purpose, slot=client_driven_slot, episode_id=episode_id)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", self._opening))
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        self.traces.append(activation_trace)
        self._turns.append(TurnRecord(len(self._turns), "a", utterance, inner_thought, activation_trace))
        if len(self._turns) < 4:
            self._turns.append(TurnRecord(len(self._turns), "b", "嗯"))
        if len(self._turns) >= 4:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode("ep-sess", self.created["scenario_id"], self.created["role_assignment"],
                       self.turns, self._end_reason)

    def abort(self):
        self.aborted = True
        self.closed = True


class _Factory:
    def __init__(self, *plans):
        self._plans = list(plans)
        self.clients = []

    def __call__(self, **kw):
        c = _FakeClient(**(self._plans.pop(0) if self._plans else {}))
        self.clients.append(c)
        return c


def test_create_session_gets_the_deployment_purpose_the_agent_slot_and_a_fresh_assignment(store, mem):
    f = _Factory()
    _run(store, f, mem, episodes_per_scenario=2)
    first, second = f.clients[0].created, f.clients[1].created
    assert (first["purpose"], first["slot"]) == ("deployment_simulation", "a")
    assert [b.to_dict() for b in first["role_assignment"].bindings] == [OPP.to_dict(), AGENT.to_dict()]
    assert first["role_assignment"].max_turns == 4
    assert first["role_assignment"] is not second["role_assignment"]
    assert all(t is not None for t in f.clients[0].traces)


@pytest.mark.parametrize("plan,reason", [
    ({"end_reason": "timeout"}, "timeout"),
    ({"end_reason": "role_backend_timeout"}, "role_backend_timeout"),
    ({"end_reason": "role_backend_unavailable"}, "role_backend_unavailable"),
    ({"end_reason": "role_memory_unavailable"}, "role_memory_unavailable"),
    ({"end_reason": "persistence_failed"}, "persistence_failed"),
    ({"create_error": _Unavailable()}, "transport_unavailable"),
])
def test_a_transient_failure_is_rerun_whole(store, mem, plan, reason):
    f = _Factory(plan)
    (episode,) = _run(store, f, mem)
    assert episode.id == "deploy-zengen-v1-s1-1" and episode.terminated_reason == "max_turns"
    assert len(f.clients) == 2


def test_transient_retries_are_bounded(store, mem):
    f = _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}, {"end_reason": "timeout"})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, f, mem, max_retries_per_episode=2)
    assert (exc.value.attempts, exc.value.transient, exc.value.reason) == (3, True, "timeout")
    assert [a["attempt"] for a in exc.value.failed_attempts] == [1, 2, 3]
    assert len(f.clients) == 3


@pytest.mark.parametrize("plan,reason", [
    ({"create_error": SessionRejectedError("invalid_session", "bad")}, "invalid_session"),
    ({"end_reason": "role_backend_failure"}, "role_backend_failure"),
    ({"end_reason": "role_backend_rejected"}, "role_backend_rejected"),
    ({"end_reason": "role_memory_failure"}, "role_memory_failure"),
    ({"end_reason": "client_disconnected"}, "client_disconnected"),
])
def test_non_transient_failures_are_not_rerun(store, mem, plan, reason):
    f = _Factory(plan)
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, f, mem)
    assert (exc.value.reason, exc.value.attempts, exc.value.transient) == (reason, 1, False)
    assert len(f.clients) == 1
    assert store.list_products(episodes_dir(RUN)) == []


def test_successful_episodes_before_a_failure_stay_in_the_tree(store, mem):
    f = _Factory({}, {"create_error": SessionRejectedError("invalid_session", "x")})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, f, mem, episodes_per_scenario=2)
    assert exc.value.episode_id == "deploy-zengen-v1-s1-2"
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, "deploy-zengen-v1-s1-1")]


def test_an_instance_exception_aborts_the_fake_session_unwrapped(store, mem):
    class Broken(Echo):
        def respond_guided(self, *a):
            raise RuntimeError("model bug")

    f = _Factory()
    with pytest.raises(RuntimeError, match="model bug") as exc:
        _run(store, f, mem, make=_make(backend=Broken()))
    assert not isinstance(exc.value, DeploymentFailedError)
    assert f.clients[0].aborted


def test_an_abort_that_raises_does_not_mask_the_exception_in_flight(store, mem, caplog):
    class Crash(Exception):
        pass

    class AbortFails(_FakeClient):
        def submit_turn(self, *a, **kw):
            raise Crash()

        def abort(self):
            raise RuntimeError("/abs/path/secret")

    with caplog.at_level("WARNING", logger="mindagent.deployment"):
        with pytest.raises(Crash):
            _run(store, lambda **kw: AbortFails(), mem)
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_episode_ids_are_deterministic():
    assert deployment_episode_id("zengen-v3", "scen-a", 2) == "deploy-zengen-v3-scen-a-2"


# ---------------- 部署记忆:起点副本、提交点、尝试间回滚(设计二第 9 节) ----------------

def test_a_retry_restores_the_start_so_the_same_logical_id_never_conflicts(store, mem):
    # 设计源第 11 节:第一次尝试留 pending 后失败,第二次同一逻辑 id 成功,不抛 EventConflict,成功后没有 pending 行
    mem.failing.add(KEY0)
    f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    (ep,) = _run(store, f, mem)
    assert ep.id == EID and len(f.clients) == 2
    assert [c.created["episode_id"] for c in f.clients] == [EID, EID]    # 服务端拿不带轮次的 id
    # 不回滚的话 KEY0 的 pending 行带着旧文本,第二次尝试换了开场就会撞 MemoryEventConflict
    assert mem.store.pending_events(WORLD) == []
    with pytest.raises(MemoryEventConflict):                            # 成功后被放弃,是终态
        mem.store.remember_event(WORLD, "pa", KEY0, "pb：我换个说法", interlocutor="pb")
    assert not mem.layout.snapshot(ROUND, EID).exists()                # 删起点快照 = 提交


def test_a_second_round_with_the_same_deploy_id_does_not_hit_the_first_rounds_keys(store, mem):
    _run(store, _Factory(), mem)
    _run(store, _Factory({"opening": "今天好点了"}), mem, round_id="agent-round-zengen-v2", run_id="run-004")
    again = mem.store.remember_event(WORLD, "pa", KEY0, "pb：我很生气!!", interlocutor="pb")
    assert not again.failed and again.stored                           # 第一轮的行仍 committed、幂等
    second = mem.store.remember_event(WORLD, "pa", f"agent-round-zengen-v2/{EID}#0@pa", "pb：今天好点了",
                                      interlocutor="pb")
    assert not second.failed and second.stored


def test_each_episode_gets_a_fresh_instance_whose_id_carries_the_round(store, mem):
    seen, started = [], []
    eps = _run(store, _Factory(), mem, make=_make(seen=seen), started=started, episodes_per_scenario=2)
    assert seen == [f"{ROUND}/{EID}", f"{ROUND}/deploy-zengen-v1-s1-2"]
    assert [e.id for e in eps] == [EID, "deploy-zengen-v1-s1-2"]
    for ep, (eid, digest) in zip(eps, started):
        first = next(t for t in ep.turns if t.speaker_slot == "a")
        assert eid == ep.id and first.activation_trace["memory"]["digest_before"] == digest   # 两位:智能体首轮前没人写过
    assert started[0][1] != started[1][1]                               # 第一条提交的记忆进了第二条的起点


def test_a_crash_mid_episode_is_rerun_from_the_restored_start(store, mem):
    digests = {}

    class Crash(Exception):
        pass

    class Crashing(_FakeClient):
        def submit_turn(self, *a, **kw):
            raise Crash()

    with pytest.raises(Crash):
        _run(store, lambda **kw: Crashing(), mem, digests=digests)
    rec = mem.layout.snapshot(ROUND, EID)
    assert rec.exists() and mem.layout.world_of(rec) == WORLD           # 在途记录自带世界
    held = mem.layout.root / "held" / WORLD / f"{mem.layout.read_record(rec).snapshot_id}.json"
    assert held.exists()
    assert mem.store.digest(WORLD) != digests[EID]                     # 半截记忆已写进部署世界
    f = _Factory({"opening": "我换个说法"})
    (ep,) = _run(store, f, mem, digests=digests)                       # 重入:先恢复、核对摘要、再重跑
    assert len(f.clients) == 1 and ep.id == EID
    assert not rec.exists() and not held.exists()                      # 提交:记录与快照都删了
    assert not mem.store.remember_event(WORLD, "pa", KEY0, "pb：我换个说法", interlocutor="pb").failed


def test_an_episode_written_but_not_committed_is_rerun(store, mem, monkeypatch):
    class Crash(BaseException):
        """进程在落盘与删副本之间被杀(不是 OSError:管理面的 OSError 现在判 T3,见下一条)。"""

    digests = {}
    real = mem.store.pending_events
    monkeypatch.setattr(mem.store, "pending_events", lambda world: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        _run(store, _Factory(), mem, digests=digests)                  # episode 已落盘、快照没删
    assert store.exists(episode_file(RUN, EID)) and mem.layout.snapshot(ROUND, EID).exists()
    monkeypatch.setattr(mem.store, "pending_events", real)
    f = _Factory()
    _run(store, f, mem, digests=digests)
    assert len(f.clients) == 1
    f2 = _Factory()
    _run(store, f2, mem, digests=digests)                              # 已提交的被复用
    assert f2.clients == []


@pytest.mark.parametrize("op", ["mkdir", "write_record", "snapshot", "open", "pending_events", "abandon_event",
                                "unlink"])
def test_a_permanent_memory_management_failure_is_a_round_failure_reporting_only_the_class(store, mem, monkeypatch, op):
    """终审 I-2:管理面与快照文件操作的 OSError 与非瞬态 MemoryFailureError 改抛 AgentRoundFailedError
    (失败点 mindagent.deployment.memory,只报类名),轮次处理器据此判 T3 并回滚;不原样逃出。"""
    from mindagent.errors import AgentRoundFailedError
    secret = "/secret/endpoint"
    boom = (OSError(secret) if op in ("mkdir", "write_record", "open", "unlink", "abandon_event")
            else MemoryFailureError(secret, transient=False))
    if op == "abandon_event":
        mem.failing.add(KEY0)                                          # 留一条本 episode 的 pending 行,才会调 abandon
        f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    else:
        f = _Factory()
    if op == "mkdir":
        real_mkdir = deployment.Path.mkdir
        monkeypatch.setattr(deployment.Path, "mkdir",                    # 只拦记录目录:held 快照照常拍
                            lambda self, *a, **kw: (_ for _ in ()).throw(boom) if self.name == ROUND
                            else real_mkdir(self, *a, **kw))
    elif op == "unlink":
        real_unlink = deployment.Path.unlink
        monkeypatch.setattr(deployment.Path, "unlink",
                            lambda self, *a, **kw: (_ for _ in ()).throw(boom) if self.name == f"deploy-{EID}.rec.json"
                            else real_unlink(self, *a, **kw))
    elif op == "write_record":
        monkeypatch.setattr(MemoryLayout, "write_record", lambda *a, **kw: (_ for _ in ()).throw(boom))
    else:
        monkeypatch.setattr(mem.store, op, lambda *a, **kw: (_ for _ in ()).throw(boom))
    with pytest.raises(AgentRoundFailedError) as exc:
        _run(store, f, mem)
    assert exc.value.point == "mindagent.deployment.memory"
    assert type(boom).__name__ in exc.value.reason and secret not in exc.value.reason


def test_a_transient_memory_management_failure_still_propagates_unchanged(store, mem, monkeypatch):
    boom = MemoryFailureError("timeout", transient=True)
    monkeypatch.setattr(mem.store, "snapshot", lambda *a, **kw: (_ for _ in ()).throw(boom))
    with pytest.raises(MemoryFailureError) as exc:
        _run(store, _Factory(), mem)
    assert exc.value is boom


def test_a_restore_whose_digest_disagrees_fails_and_keeps_library_and_copy(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    before = mem.store.digest(WORLD)
    digests[EID] = "not-the-recorded-digest"
    with pytest.raises(RestoreFailedError, match=EID):
        _run(store, _Factory(), mem, digests=digests)
    assert mem.layout.snapshot(ROUND, EID).exists() and mem.store.digest(WORLD) == before


def test_a_memory_admission_alone_reuses_committed_episodes(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    f = _Factory()
    (ep,) = _run(store, f, mem, digests=digests, library_version={"skill": "0", "memory": "1"})
    assert f.clients == [] and ep.id == EID


def test_a_new_skill_version_reruns_under_a_suffixed_id(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    seen = []
    v1 = {"skill": "1", "memory": "0"}
    (ep,) = _run(store, _Factory(), mem, digests=digests, library_version=v1, make=_make(version=v1, seen=seen))
    assert ep.id == f"{EID}-s1" and seen == [f"{ROUND}/{EID}-s1"]
    assert store.exists(episode_file(RUN, EID))                        # 原 episode 留着
    (again,) = _run(store, _Factory(), mem, digests=digests, library_version=v1)
    assert again.id == f"{EID}-s1"                                     # 重入复用带后缀的那条


def _tamper_turn(store, index, mutate):
    """改坏已落盘的 EID 的第 index 轮(直接改树里的 YAML 载荷,再提交)。"""
    env = store.read_product(episode_file(RUN, EID))
    mutate(env.payload["turns"][index])
    store.write_product(episode_file(RUN, EID), env, "test: tamper")


@pytest.mark.parametrize("index,mutate,needle", [
    pytest.param(3, lambda t: t.update(activation_trace={"selected": "skill-calm"}), "turn 3: trace",
                 id="later-agent-turn-off-schema-trace"),
    pytest.param(3, lambda t: t.update(activation_trace=None), "turn 3: trace", id="later-agent-turn-without-trace"),
    pytest.param(2, lambda t: t.update(speaker_slot="zz"), "turn 2: speaker_slot", id="later-turn-unbound-speaker"),
    pytest.param(3, lambda t: t.update(utterance=7), "turn 3: utterance", id="later-turn-utterance-not-a-str"),
])
def test_a_malformed_later_turn_of_a_committed_episode_is_a_data_error_not_a_rerun(store, mem, index, mutate, needle):
    """复用前逐轮核对:首个智能体轮次(第 1 轮)完好、坏的是后面的轮次时,同样抛 MindAgentDataError(轮次判 T3),
    不当成"技能版本对不上"静默重跑一条已提交的 episode。"""
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    first = read_episode(store, RUN, EID).turns[1]
    validate_trace(first.activation_trace)                              # 首个智能体轮次本身是好的
    _tamper_turn(store, index, mutate)
    f = _Factory()
    with pytest.raises(MindAgentDataError, match=needle) as info:
        _run(store, f, mem, digests=digests)
    assert EID in str(info.value) and f.clients == []                   # 没有开新会话,也没有重跑


def test_a_well_formed_committed_episode_passes_the_all_turns_check_and_is_reused(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    f = _Factory()
    (ep,) = _run(store, f, mem, digests=digests)
    assert f.clients == [] and ep.id == EID and len(ep.turns) == 4


def test_exhausted_transient_retries_leave_the_library_at_the_start(store, mem):
    digests = {}
    with pytest.raises(DeploymentFailedError) as info:
        _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}), mem, digests=digests)
    assert info.value.transient
    assert mem.store.digest(WORLD) == digests[EID]
    assert mem.layout.snapshot(ROUND, EID).exists()                    # 在途:留给重入或轮次回滚


def test_a_rejection_for_unavailable_role_memory_is_transient(store, mem):
    f = _Factory({"create_error": SessionRejectedError("role_memory_unavailable", "")})
    (ep,) = _run(store, f, mem)
    assert len(f.clients) == 2 and ep.id == EID


def test_a_transient_failure_then_success_commits_once(store, mem, monkeypatch):
    writes = []
    real = deployment.write_episode

    def recording(store_, run_id, episode):
        writes.append((episode.turns[0].utterance, mem.layout.snapshot(ROUND, EID).exists()))
        return real(store_, run_id, episode)

    monkeypatch.setattr(deployment, "write_episode", recording)
    (ep,) = _run(store, _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"}), mem)
    assert writes == [("我换个说法", True)]                             # 只落一次,用第二次的结果,快照此时还在
    assert ep.turns[0].utterance == "我换个说法"
    assert not mem.layout.snapshot(ROUND, EID).exists()                # 落盘之后才删快照


def test_a_failed_attempt_is_rolled_back_after_its_port_is_closed(store, mem, tmp_path):
    log = []

    class Recording(FileTupleStore):
        def open(self, world_id, holder):
            log.append(("open", world_id, holder))
            return super().open(world_id, holder)

        def restore(self, world_id, snapshot_id):
            log.append(("restore", world_id, self._open.get(self._deploy_key(world_id), 0)))
            return super().restore(world_id, snapshot_id)

    root = tmp_path / "rec"
    rec = SimpleNamespace(store=Recording(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
                          layout=MemoryLayout(root))
    _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}, {}), rec,
         max_retries_per_episode=2)
    assert log == [("open", WORLD, "pa"), ("restore", WORLD, 0), ("open", WORLD, "pa"), ("restore", WORLD, 0),
                   ("open", WORLD, "pa")]


@pytest.mark.parametrize("transient,reason", [(True, "role_memory_unavailable"), (False, "role_memory_failure")])
def test_a_memory_failure_inside_respond_maps_to_a_role_memory_reason(store, mem, transient, reason):
    built = []
    inner = _make()

    class Failing:
        def __init__(self, real):
            self.real = real

        def respond(self, *a):
            raise MemoryFailureError("http://secret-endpoint/v1", transient=transient)

        def close(self):
            self.real.close()

    def make(episode_id, memory, holder):
        real = inner(episode_id, memory, holder)
        built.append(real)
        return Failing(real) if len(built) == 1 else real

    f = _Factory()
    if transient:
        (ep,) = _run(store, f, mem, make=make)
        assert ep.id == EID and len(f.clients) == 2
        return
    with pytest.raises(DeploymentFailedError) as info:
        _run(store, f, mem, make=make)
    assert (info.value.reason, info.value.detail, info.value.transient) == (reason, "MemoryFailureError", False)
    assert "secret" not in str(info.value)                              # 只报类名,不带异常原文
    assert len(f.clients) == 1 and f.clients[0].aborted


def test_rerun_ids_carry_the_skill_version():
    assert rerun_episode_id("deploy-zengen-v1-s1-1", "3") == "deploy-zengen-v1-s1-1-s3"


# ---------------- 准入被拒(role_backend_unavailable)时同一次尝试里退避重建(终审 I-1) ----------------

class _FakeClock:
    """注入给驱动器的 sleep/clock:sleep 只把假时钟往前拨,不真睡。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


_BUSY = SessionRejectedError("role_backend_unavailable", f"episode {EID!r} already has an active session")


def test_an_admission_refusal_backs_off_inside_the_attempt_and_does_not_count_as_a_retry(store, mem):
    clock = _FakeClock()
    f = _Factory({"create_error": _BUSY}, {"create_error": _BUSY})
    (ep,) = _run(store, f, mem, max_retries_per_episode=0, sleep=clock.sleep, clock=clock.clock)
    assert ep.id == EID and ep.terminated_reason == "max_turns"                # 一次计数的尝试就成了
    assert len(f.clients) == 3 and [c.created["episode_id"] for c in f.clients] == [EID] * 3
    assert clock.sleeps == [0.5, 1.0]                                          # 指数退避,没有真睡
    assert not mem.layout.snapshot(ROUND, EID).exists()


def test_an_exhausted_admission_budget_is_a_transient_failure_and_the_library_stays_at_the_start(store, mem):
    clock, digests = _FakeClock(), {}
    f = _Factory(*[{"create_error": _BUSY}] * 100)
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, f, mem, digests=digests, max_retries_per_episode=0, admission_wait_s=20.0,
             sleep=clock.sleep, clock=clock.clock)
    assert (exc.value.reason, exc.value.attempts, exc.value.transient) == ("role_backend_unavailable", 1, True)
    assert clock.sleeps[:5] == [0.5, 1.0, 2.0, 4.0, 8.0] and max(clock.sleeps) <= 8.0
    assert clock.now == 20.0                                                   # 用够预算,不超出
    assert mem.store.digest(WORLD) == digests[EID]
    assert mem.layout.snapshot(ROUND, EID).exists()


def test_other_create_rejections_are_not_backed_off(store, mem):
    clock = _FakeClock()
    f = _Factory({"create_error": SessionRejectedError("invalid_session", "bad")})
    with pytest.raises(DeploymentFailedError):
        _run(store, f, mem, sleep=clock.sleep, clock=clock.clock)
    assert clock.sleeps == [] and len(f.clients) == 1


def test_the_default_admission_budget_outlasts_the_servers_join_timeout():
    import inspect
    join = inspect.signature(create_server).parameters["resource_join_timeout_s"].default
    assert deployment.ADMISSION_WAIT_S >= join + 5
    assert inspect.signature(run_deployment_episodes).parameters["admission_wait_s"].default == deployment.ADMISSION_WAIT_S


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_an_admission_budget_that_is_not_a_finite_non_negative_number_is_refused(store, mem, bad):
    f = _Factory()
    with pytest.raises(ValueError, match="admission_wait_s"):
        _run(store, f, mem, admission_wait_s=bad)
    assert f.clients == []


class _HangOnce:
    """对手位后端:第一次 respond 挂到 release(比服务端 backend_timeout_s 久),之后照常回话。"""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def respond(self, persona, scenario, history, speaker_slot):
        self.calls += 1
        if self.calls == 1:
            self.release.wait(10)
        return ("我真的很生气!!" if self.calls % 2 else "好吧"), None


@pytest.fixture
def same_id_server(store):
    """真 loopback 服务端:对手位第一次调用挂住;backend_timeout_s 取 1 秒、resource_join_timeout_s 取 3 秒。
    make_factory(message_timeout_s=...) 造的客户端记下 create_session 被拒的原因。"""
    hang = _HangOnce()
    rejected: list[str] = []
    channels = []

    def factory(scenario, assignment, personas, client_slot):
        return {s: hang for s in assignment.slot_ids() - {client_slot}}

    server = create_server(plain_factory(factory), port=0, turn_timeout_s=5.0, backend_timeout_s=1.0,
                           resource_join_timeout_s=3.0, linger_retry_s=0.05,
                           scenario_resolver=lambda sid: read_scenario(store, sid))
    server.start()

    def make_factory(message_timeout_s=None):
        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            client = EnvironmentServiceClient(_DeadlineStub(pb2_grpc.EnvironmentServiceStub(channel)),
                                              message_timeout_s=message_timeout_s)
            real = client.create_session

            def create_session(**kw):
                try:
                    return real(**kw)
                except SessionRejectedError as exc:
                    rejected.append(exc.reason)
                    raise
            client.create_session = create_session
            return client
        return client_factory

    yield SimpleNamespace(hang=hang, make_factory=make_factory, rejected=rejected)
    hang.release.set()
    for channel in channels:
        channel.close()
    server.stop(0).wait()


def test_a_same_id_rerun_after_a_hung_opponent_backs_off_and_succeeds_against_a_real_server(
        store, mem, same_id_server):
    """设计 §11:同一逻辑 episode id 仍有活动会话时,第二个 CreateSession 以 role_backend_unavailable 被拒、
    客户端按条重跑成功。对手开场那次调用挂过 backend_timeout_s → role_backend_timeout;服务端在 join 里
    最多等 resource_join_timeout_s,其间同一部署 id 的重跑被准入拒,驱动器退避到旧会话收尾后建成。"""
    env = same_id_server
    threading.Timer(1.5, env.hang.release.set).start()      # 挂住的线程在 join 期内退出
    (ep,) = _run(store, env.make_factory(), mem, admission_wait_s=10.0)
    assert ep.id == EID and ep.terminated_reason == "max_turns"
    assert "role_backend_unavailable" in env.rejected
    assert not mem.layout.snapshot(ROUND, EID).exists()                     # 提交了


def test_a_same_id_rerun_after_a_client_abort_backs_off_and_succeeds_against_a_real_server(
        store, mem, same_id_server):
    """客户端侧先 abort 的瞬态:相邻消息等待超时(message_timeout_s)让客户端合成 timeout 并放掉流,旧会话的
    对手调用还挂着、服务端在 join 里;同一部署 id 的立即重跑被准入拒,退避到旧会话收尾后建成。"""
    env = same_id_server
    threading.Timer(1.5, env.hang.release.set).start()
    (ep,) = _run(store, env.make_factory(message_timeout_s=0.5), mem, admission_wait_s=10.0)
    assert ep.id == EID and ep.terminated_reason == "max_turns"
    assert "role_backend_unavailable" in env.rejected


# ---------------- 修复波次 A:close 失败(终审 M-1) ----------------

class _CloseFailingStore(FileTupleStore):
    """开出的端口 close 时先真关(计数照常归零,restore 不被挡),再抛给定的异常。"""

    def __init__(self, root, *, exc):
        super().__init__(root, held_root=root.parent / "held", replay_root=root.parent / "replay")
        self._exc = exc

    def open(self, world_id, holder):
        port = super().open(world_id, holder)
        real_close, exc = port.close, self._exc

        def close():
            real_close()
            raise exc

        port.close = close
        return port


@pytest.mark.parametrize("boom", [MemoryFailureError("checkpoint at /secret/db", transient=False),
                                  OSError("disk full at /secret/db")])
def test_a_close_failure_after_a_successful_run_is_a_round_failure_reporting_only_the_class(store, tmp_path, boom):
    from mindagent.errors import AgentRoundFailedError
    m = SimpleNamespace(store=_CloseFailingStore(tmp_path / "cf" / "deploy", exc=boom), layout=MemoryLayout(tmp_path / "cf"))
    with pytest.raises(AgentRoundFailedError) as info:
        _run(store, _Factory(), m)
    assert info.value.point == "mindagent.deployment.memory"
    # 实例 close 经 _guard:端口抛的 OSError 先改成只带类名的永久 MemoryFailureError,再由 _memory_op 判 T3
    assert info.value.reason == f"close instance for {EID}: MemoryFailureError" and "secret" not in info.value.reason
    assert m.layout.snapshot(ROUND, EID).exists()                      # 没走到提交点:快照留给轮次回滚


def test_a_transient_close_failure_propagates_unchanged_like_other_management_calls(store, tmp_path):
    # 与 _memory_op 同一语义(见 test_a_transient_memory_management_failure_still_propagates_unchanged)
    boom = MemoryFailureError("checkpoint", transient=True)
    m = SimpleNamespace(store=_CloseFailingStore(tmp_path / "cf" / "deploy", exc=boom), layout=MemoryLayout(tmp_path / "cf"))
    with pytest.raises(MemoryFailureError) as info:
        _run(store, _Factory(), m)
    assert info.value is boom


def test_a_close_failure_does_not_mask_the_original_exception(store, tmp_path, caplog):
    m = SimpleNamespace(store=_CloseFailingStore(tmp_path / "cf" / "deploy",
                                                 exc=MemoryFailureError("checkpoint", transient=False)),
                        layout=MemoryLayout(tmp_path / "cf"))

    class Broken(Echo):
        def respond_guided(self, *a):
            raise RuntimeError("model bug")

    with caplog.at_level("WARNING", logger="mindagent.deployment"):
        with pytest.raises(RuntimeError, match="model bug"):          # 原异常优先,close 失败只记 warning
            _run(store, _Factory(), m, make=_make(backend=Broken()))
    assert "MemoryFailureError" in caplog.text and "checkpoint" not in caplog.text


# ---------------- 部署世界(设计三 4.3–4.5 节):对手位进世界、未提交快照的守卫、等端口全关、新复用判据 ----------------

class _WorldClient:
    """替身客户端连同替身服务端的对手位(设计三 4.5 节):对手位按 register_episode 登记的 round_id,在情境世界里开
    (世界, 对手人格) 端口;每次开口前把自己上次开口之后别人说的话记进世界,key 与 SimpleActio 实例同一套
    (<round_id>/<部署 id>#<turn>@<主体>)。order 是发言顺序。会话结束(到 max_turns 或 abort)时对每个对手端口调
    release(port),缺省立即关;测试拿它推迟关(另一条线程)或干脆不关。"""

    def __init__(self, mem, registry, *, order=("b", "a"), max_turns=4, end_reason="max_turns", release=None):
        self._mem, self._registry = mem, registry
        self._order, self._max_turns, self._end_reason = order, max_turns, end_reason
        self._release = release or (lambda port: port.close())
        self._turns, self._ports, self._seen = [], {}, {}
        self.closed = False
        self.created = None
        self.traces = []

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot,
                       episode_id=None):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment, episode_id=episode_id)
        self._agent = client_driven_slot
        self._pid = {b.slot_id: b.persona_id for b in role_assignment.bindings}
        self._prefix = f"{self._registry[episode_id]}/{episode_id}"     # 服务端只认登记表里的 round_id
        for slot, pid in self._pid.items():
            if slot != client_driven_slot:
                self._ports[slot] = self._mem.store.open(scenario_id, pid)
                self._seen[slot] = -1
        self._advance()
        return "sess"

    def _advance(self):
        while len(self._turns) < self._max_turns:
            slot = self._order[len(self._turns) % len(self._order)]
            if slot == self._agent:
                return
            pid = self._pid[slot]
            for t in self._turns[self._seen[slot] + 1:]:
                if t.speaker_slot != slot:
                    speaker = self._pid[t.speaker_slot]
                    self._ports[slot].remember(MemoryEvent(
                        f"{self._prefix}#{t.turn_index}@{pid}", f"{speaker}：{t.utterance}", speaker))
            self._seen[slot] = len(self._turns) - 1
            self._turns.append(TurnRecord(len(self._turns), slot, f"{pid} 第 {len(self._turns)} 句"))
        self._end()

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        self.traces.append(activation_trace)
        self._turns.append(TurnRecord(len(self._turns), self._agent, utterance, inner_thought, activation_trace))
        self._advance()
        return []

    def _end(self):
        if not self.closed:
            self.closed = True
            for port in self._ports.values():
                self._release(port)

    def drain_until_end(self):
        return Episode("ep-sess", self.created["scenario_id"], self.created["role_assignment"],
                       self.turns, self._end_reason)

    def abort(self):
        self._end()


class _WorldFactory:
    """_WorldClient 的工厂;hooks() 是交给驱动器的 register_episode / unregister_episode,登记表就是 registry。"""

    def __init__(self, mem, *plans, **common):
        self.registry = {}
        self._mem, self._plans, self._common = mem, list(plans), common
        self.clients = []

    def __call__(self, **kw):
        plan = self._plans.pop(0) if self._plans else {}
        c = _WorldClient(self._mem, self.registry, **{**self._common, **plan})
        self.clients.append(c)
        return c

    def hooks(self):
        return dict(register_episode=self.registry.__setitem__, unregister_episode=self.registry.pop)


def _events(mem, world):
    """世界文件里的事件行,按 seq 排(替身的落盘格式)。"""
    path = mem.layout.root / "deploy" / f"{world}.json"
    return json.loads(path.read_text(encoding="utf-8"))["events"] if path.exists() else []


def test_both_slots_write_into_the_scenario_world_under_their_own_persona(store, mem):
    # 智能体的 key 以 @<智能体位人格> 结尾(Task 2 的 holder 由驱动器传入),对手位的以 @<对手人格> 结尾
    f = _WorldFactory(mem)
    (ep,) = _run(store, f, mem, **f.hooks())
    assert ep.id == EID
    rows = _events(mem, WORLD)
    assert [(r["key"], r["holder"], r["state"]) for r in rows] == [
        (f"{ROUND}/{EID}#0@pa", "pa", "committed"),
        (f"{ROUND}/{EID}#1@pb", "pb", "committed"),
        (f"{ROUND}/{EID}#2@pa", "pa", "committed"),
    ]
    assert f.registry == {}                                            # 每次尝试结束都撤销登记
    assert not (mem.layout.root / "deploy" / "pa.json").exists()       # 不再按人格建库


def test_a_failed_attempt_rolls_back_both_slots_writes(store, mem, monkeypatch):
    digests, before_restore = {}, []
    real = mem.store.restore

    def spy(world, snapshot_id):
        before_restore.append([r["key"] for r in _events(mem, world)])
        return real(world, snapshot_id)

    monkeypatch.setattr(mem.store, "restore", spy)
    f = _WorldFactory(mem, {"end_reason": "role_backend_failure"})
    with pytest.raises(DeploymentFailedError):
        _run(store, f, mem, digests=digests, **f.hooks())
    # 对手位经 register_episode 拿到的 round_id 写进了世界;失败后整个世界(两个主体)回到起点
    assert before_restore == [[f"{ROUND}/{EID}#0@pa", f"{ROUND}/{EID}#1@pb", f"{ROUND}/{EID}#2@pa"]]
    assert mem.store.digest(WORLD) == digests[EID]
    assert _events(mem, WORLD) == []
    assert f.registry == {}                                            # 失败的尝试也撤销登记


def _foreign_record(mem, round_id, eid, world):
    """别的轮次留下的在途记录(R1 回滚失败时留下的那种):记录与它指的 held 快照都在。"""
    ref = mem.store.snapshot(world, note=f"rise episode {eid}")
    p = mem.layout.snapshot(round_id, eid)
    mem.layout.write_record(p, SnapshotRecord(world, ref.snapshot_id, ref.digest))
    return p


def test_another_rounds_uncommitted_snapshot_defers_the_episode(store, mem):
    # 设计三 4.3 节:R1 回滚失败留下的记录在,R2 不往这个世界写;原因写出 R1 的轮次 id
    _foreign_record(mem, "agent-round-zengen-v1", "deploy-zengen-v1-s1-1", WORLD)
    f = _Factory()
    with pytest.raises(DeferredError, match="round agent-round-zengen-v1; restore or delete it by hand"):
        _run(store, f, mem, round_id="agent-round-zengen-v2")
    assert f.clients == []
    assert mem.layout.round_snapshots("agent-round-zengen-v2") == []   # 一点没写
    assert not (mem.layout.root / "deploy" / f"{WORLD}.json").exists()


def test_another_rounds_snapshot_of_a_different_world_does_not_defer(store, mem):
    _foreign_record(mem, "agent-round-zengen-v1", "deploy-zengen-v1-s2-1", "s2")
    (ep,) = _run(store, _Factory(), mem, round_id="agent-round-zengen-v2")
    assert ep.id == EID


def test_an_unreadable_record_anywhere_stops_the_round_as_a_data_error(store, mem):
    p = mem.layout.snapshot("agent-round-zengen-v0", "deploy-zengen-v0-s2-1")
    p.parent.mkdir(parents=True)
    p.write_text('{"world_id": "../s1", "snapshot_id": "snap-0", "digest": "d"}')
    f = _Factory()
    with pytest.raises(MindAgentDataError):
        _run(store, f, mem)
    assert f.clients == []


def test_an_episode_start_takes_a_held_snapshot_and_records_it_before_announcing_the_digest(store, mem, monkeypatch):
    order = []
    real_snap, real_write = mem.store.snapshot, MemoryLayout.write_record
    monkeypatch.setattr(mem.store, "snapshot",
                        lambda world, *, note: order.append(("snapshot", world, note)) or real_snap(world, note=note))
    monkeypatch.setattr(MemoryLayout, "write_record",
                        lambda self, path, rec: order.append(("record", path.name, rec.world_id)) or real_write(self, path, rec))
    _run(store, _Factory(), mem, started=SimpleNamespace(append=lambda item: order.append(("start", item[0]))))
    assert order[:3] == [("snapshot", WORLD, f"rise episode {EID}"),
                         ("record", f"deploy-{EID}.rec.json", WORLD), ("start", EID)]


def test_the_record_carries_the_snapshot_id_and_the_start_digest(store, mem, monkeypatch):
    seen = []
    real_write = MemoryLayout.write_record
    monkeypatch.setattr(MemoryLayout, "write_record",
                        lambda self, path, rec: seen.append(rec) or real_write(self, path, rec))
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    (rec,) = seen
    assert rec.world_id == WORLD and rec.digest == digests[EID] and rec.snapshot_id.startswith("snap-")


def test_a_commit_drops_the_record_before_the_snapshot_and_a_failed_delete_is_only_a_warning(store, mem, monkeypatch, caplog):
    order = []
    monkeypatch.setattr(mem.store, "delete_snapshot",
                        lambda world, sid: order.append(("delete", mem.layout.snapshot(ROUND, EID).exists()))
                        or (_ for _ in ()).throw(MemoryFailureError("boom http://secret", transient=False)))
    (ep,) = _run(store, _Factory(), mem)
    assert ep.id == EID and order == [("delete", False)]                # 记录先删
    assert "delete_snapshot failed: MemoryFailureError" in caplog.text and "secret" not in caplog.text


def test_a_commit_deletes_the_held_snapshot_it_recorded(store, mem):
    _run(store, _Factory(), mem)
    assert mem.layout.round_snapshots(ROUND) == []
    assert list((mem.layout.root / "held" / WORLD).iterdir()) == []      # 提交后 held 快照也删了


@pytest.mark.parametrize("transient, raised", [(True, DeferredError), (False, RestoreFailedError)])
def test_a_restore_failure_is_split_by_its_transient_flag_and_keeps_the_record(store, mem, monkeypatch, transient, raised):
    monkeypatch.setattr(mem.store, "restore",
                        lambda world, sid: (_ for _ in ()).throw(MemoryFailureError("world_busy", transient=transient)))
    f = _WorldFactory(mem, {"end_reason": "role_backend_failure"})
    with pytest.raises(raised) as info:
        _run(store, f, mem, **f.hooks())
    assert mem.layout.snapshot(ROUND, EID).exists()                     # 记录与快照都留着,重入先恢复
    # 原因只报类名,原异常也不挂在 __cause__ 上:记忆失败的原文来自服务或替身,不保证不含端点
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_a_restore_from_a_snapshot_that_is_gone_keeps_the_record_and_fails_the_restore(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    rec = mem.layout.snapshot(ROUND, EID)
    mem.store.delete_snapshot(WORLD, mem.layout.read_record(rec).snapshot_id)   # 人误删了在途快照
    with pytest.raises(RestoreFailedError, match=f"restore {EID} in world {WORLD}: MemoryFailureError") as info:
        _run(store, _Factory(), mem, digests=digests)
    assert info.value.point == "mindagent.deployment.restore" and rec.exists()
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_a_record_of_another_world_at_this_episodes_path_fails_the_restore_and_is_kept(store, mem):
    rec = _foreign_record(mem, ROUND, EID, "s2")                       # 本轮本条的路径,记录却属于 s2
    f = _Factory()
    with pytest.raises(RestoreFailedError, match="record belongs to world s2") as info:
        _run(store, f, mem)
    assert info.value.point == "mindagent.deployment.restore" and rec.exists() and f.clients == []


def test_an_os_error_from_restore_keeps_no_cause_with_the_path(store, mem, monkeypatch):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    monkeypatch.setattr(mem.store, "restore",
                        lambda world, sid: (_ for _ in ()).throw(PermissionError(13, "denied", "/abs/root/s1.json")))
    with pytest.raises(RestoreFailedError, match="PermissionError") as info:
        _run(store, _Factory(), mem, digests=digests)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert "/abs/root" not in str(info.value)


def test_an_unreadable_record_of_this_round_stops_the_round_as_a_data_error_and_is_kept(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    rec = mem.layout.snapshot(ROUND, EID)
    rec.write_text("not json")
    f = _Factory()
    with pytest.raises(MindAgentDataError):                             # 查别的轮次的在途记录时就读到它
        _run(store, f, mem, digests=digests)
    assert rec.exists() and f.clients == []


THREE = (RoleBinding("c", "pc", "frozen_reference", model_ref="zengen-v1"), OPP, AGENT)


def test_a_committed_episode_is_reused_when_a_second_opponent_spoke_before_the_agent(store, mem):
    # 设计三 4.5 节"首轮摘要的顺序":c 先开口、b 第二个说话时把 c 的话记进世界,智能体位第三个说话,
    # 首轮 digest_before 不再等于起点摘要;复用判据不比它
    write_scenario(store, Scenario("s3", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b"), RoleSlot("c", "c")],
                                   "c", ["n"], PURPOSE))
    personas = {**_personas(), "pc": Persona("pc", ["t"], "m", "s", ["v"])}
    eid = "deploy-zengen-v1-s3-1"
    digests = {}
    kw = dict(scenario_ids=["s3"], bindings=THREE, personas=personas, max_turns=6)
    f = _WorldFactory(mem, order=("c", "b", "a"), max_turns=6)
    (ep,) = _run(store, f, mem, digests=digests, **kw, **f.hooks())
    assert [t.speaker_slot for t in ep.turns] == ["c", "b", "a", "c", "b", "a"]
    first = next(t for t in ep.turns if t.speaker_slot == "a")
    assert first.activation_trace["memory"]["digest_before"] != digests[eid]
    keys = [r["key"] for r in _events(mem, "s3")]
    assert f"{ROUND}/{eid}#0@pb" in keys and f"{ROUND}/{eid}#0@pa" in keys   # 同一句被两个主体各记一份,key 不撞
    again = _WorldFactory(mem, order=("c", "b", "a"), max_turns=6)
    (reused,) = _run(store, again, mem, digests=digests, **kw, **again.hooks())
    assert again.clients == [] and reused.to_dict() == ep.to_dict()


def test_an_old_shape_start_digest_counts_as_not_recorded_and_the_episode_reruns(store, mem):
    digests = {}
    _run(store, _Factory(), mem, digests=digests)
    digests[EID] = {"pa": digests[EID]}                                # 本次改动之前的轮次产物:{persona_id: 摘要}
    f = _Factory()
    (ep,) = _run(store, f, mem, digests=digests)
    assert len(f.clients) == 1 and ep.id == EID
    assert isinstance(digests[EID], str)                               # 重跑时按新形状重记


def test_the_driver_waits_for_server_ports_before_restoring(store, mem):
    # 服务端在另一条线程上晚 0.3 秒才关对手端口:驱动器先等端口全关再恢复,恢复不被"世界还开着端口"拒绝
    def late(port):
        threading.Timer(0.3, port.close).start()

    f = _WorldFactory(mem, {"end_reason": "timeout"}, {}, release=late)
    (ep,) = _run(store, f, mem, **f.hooks())
    assert ep.id == EID and len(f.clients) == 2
    assert not mem.layout.snapshot(ROUND, EID).exists()
    assert mem.store.wait_closed(WORLD, 0)                             # 提交之前也等过


def test_a_server_port_that_never_closes_fails_the_restore_and_keeps_the_snapshot(store, mem):
    digests = {}
    f = _WorldFactory(mem, {"end_reason": "timeout"}, release=lambda port: None)
    with pytest.raises(RestoreFailedError, match="still open") as info:
        _run(store, f, mem, digests=digests, close_wait_s=0.2, **f.hooks())
    assert info.value.point == "mindagent.deployment.restore"
    snap = mem.layout.snapshot(ROUND, EID)
    assert snap.exists() and mem.layout.world_of(snap) == WORLD        # 库与快照都留给人
    assert mem.store.digest(WORLD) != digests[EID]
    assert len(f.clients) == 1


def test_a_reentry_restore_with_a_port_left_open_and_no_wait_fails_and_keeps_the_record(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    leftover = mem.store.open(WORLD, "pb")                             # 服务端的对手端口一直没关
    f = _Factory()
    with pytest.raises(RestoreFailedError, match="memory ports still open after 0s") as info:
        _run(store, f, mem, digests=digests, close_wait_s=0)
    assert info.value.point == "mindagent.deployment.restore"
    assert mem.layout.snapshot(ROUND, EID).exists() and f.clients == []
    leftover.close()


def test_a_server_port_that_never_closes_after_a_success_defers_the_commit(store, mem):
    f = _WorldFactory(mem, release=lambda port: None)
    with pytest.raises(DeferredError, match=f"world {WORLD}"):
        _run(store, f, mem, close_wait_s=0.2, **f.hooks())
    assert not store.exists(episode_file(RUN, EID))                    # 没提交:重入时"快照在就先恢复"
    assert mem.layout.snapshot(ROUND, EID).exists()


def test_a_port_still_open_before_an_attempt_defers_without_opening_a_session(store, mem):
    leftover = mem.store.open(WORLD, "pb")                             # 上一个会话的服务端还没收尾
    f = _Factory()
    with pytest.raises(DeferredError, match=f"world {WORLD} still has open memory ports .* before attempt 1"):
        _run(store, f, mem, close_wait_s=0.2)
    assert f.clients == [] and mem.layout.snapshot(ROUND, EID).exists()   # 快照已拍,重入时先恢复
    leftover.close()
    (ep,) = _run(store, _Factory(), mem)
    assert ep.id == EID and not mem.layout.snapshot(ROUND, EID).exists()


def test_pending_rows_of_deployment_opponents_are_abandoned_on_commit(store, mem):
    opp_key = f"{ROUND}/{EID}#1@pb"                                    # 对手位记智能体第 1 轮那句
    mem.failing.add(opp_key)
    f = _WorldFactory(mem)
    (ep,) = _run(store, f, mem, **f.hooks())
    assert ep.id == EID
    assert mem.store.pending_events(WORLD) == []
    with pytest.raises(MemoryEventConflict):                           # 放弃是终态
        mem.store.remember_event(WORLD, "pb", opp_key, f"pa：{ep.turns[1].utterance}", interlocutor="pa")


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_a_close_wait_that_is_not_a_finite_non_negative_number_is_refused(store, mem, bad):
    f = _Factory()
    with pytest.raises(ValueError, match="close_wait_s"):
        _run(store, f, mem, close_wait_s=bad)
    assert f.clients == []


def test_the_default_close_wait_outlasts_the_servers_join_timeout():
    import inspect
    join = inspect.signature(create_server).parameters["resource_join_timeout_s"].default
    assert deployment.SESSION_CLOSE_WAIT_S == 70.0 >= join + 10
    assert inspect.signature(run_deployment_episodes).parameters["close_wait_s"].default == 70.0


class _RecallingWorldClient(_WorldClient):
    """_WorldClient 加一条:开完对手端口、第一次开口之前,用对手人格的端口召回 query,结果记进 recalled。"""

    def __init__(self, *a, query="plain", **kw):
        super().__init__(*a, **kw)
        self._query = query
        self.recalled = None

    def create_session(self, **kw):
        # 与父类同序:端口先开好;召回发生在 _advance 写入任何东西之前,所以看到的只有以前的 episode 留下的
        scenario_id, role_assignment = kw["scenario_id"], kw["role_assignment"]
        pid = {b.slot_id: b.persona_id for b in role_assignment.bindings}["b"]
        probe = self._mem.store.open(scenario_id, pid)
        try:
            self.recalled = probe.recall(self._query, limit=5)
        finally:
            probe.close()
        return super().create_session(**kw)


def test_the_opponent_slot_recalls_what_it_remembered_in_an_earlier_episode_of_the_same_world(store, mem):
    """补设计三清单"部署对手位跨两条 episode 记得"的测试缺口:同一情境的两条 episode 共用一个部署世界,
    第一条里对手位记下的智能体发言("plain"),第二条开口前就能用对手人格的端口召回;第一条开口前什么都没有。
    智能体位不一样:召回只看本主体名下的条目,对手位记下的东西不进智能体的召回。"""
    registry, clients = {}, []

    def factory(**kw):
        c = _RecallingWorldClient(mem, registry)
        clients.append(c)
        return c

    eps = _run(store, factory, mem, episodes_per_scenario=2,
               register_episode=registry.__setitem__, unregister_episode=registry.pop)
    assert [e.id for e in eps] == [EID, "deploy-zengen-v1-s1-2"]
    first, second = clients
    assert first.recalled == []                                         # 第一条开口前世界里没有对手位的记忆
    assert [h.text for h in second.recalled] == ["pa: pa：plain"]       # 第二条:第一条里对手位记下的智能体发言
    agent_port = mem.store.open(WORLD, "pa")
    try:
        assert agent_port.recall("plain", limit=5) == []                # 对手位记下的是 pa 说的话,不在 pa 自己名下
    finally:
        agent_port.close()
