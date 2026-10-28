import threading
import time
from types import SimpleNamespace

import grpc
import pytest

from actio.instance import instantiate
from actio.memory import FileTupleStore
from actio.memory_port import MemoryEventConflict, MemoryFailureError
from actio.skills import Skill
from actio.trace import validate_trace
from controlplane.gitstore import GitStore
from controlplane.paths import episode_file, episodes_dir
from mindagent import deployment
from mindagent.deployment import (
    PURPOSE, DeploymentFailedError, deployment_episode_id, rerun_episode_id, run_deployment_episodes,
)
from mindagent.errors import RestoreFailedError
from mindagent.memory_layout import MemoryLayout
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
BINDINGS = (OPP, AGENT)  # 对手先说,Actio 实例后说
ROUND = "agent-round-zengen-v1"
EID = "deploy-zengen-v1-s1-1"
KEY0 = f"{ROUND}/{EID}#0"          # 对手开场那句的事件 key:轮次前缀 + 部署 id + 轮次序号
V0 = {"skill": "0", "memory": "0"}


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


CALM = Skill("skill-calm", 1, "micro", {"signal": "emotion_high"}, ("先回应情绪",), priority=1)


def _make(skills=(), version=None, seen=None, backend=None):
    def make(episode_id, memory):
        if seen is not None:
            seen.append(episode_id)
        return instantiate(backend or Echo(), skills=list(skills), memory=memory, episode_id=episode_id,
                           interlocutors={"a": "pa", "b": "pb"},
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
    for pid, p in _personas().items():
        write_persona(s, p)
    return s


@pytest.fixture
def mem(tmp_path):
    failing: set[str] = set()      # 测试往里放 key,该事件的抽取就以 timeout 失败
    store = FileTupleStore(tmp_path / "mem" / "deploy",
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

    def on_start(eid, by_persona):
        digests[eid] = dict(by_persona)
        if started is not None:
            started.append((eid, dict(by_persona)))

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
    assert mem.store.pending_events("pa") == []
    with pytest.raises(MemoryEventConflict):                            # 成功后被放弃,是终态
        mem.store.remember_event("pa", KEY0, "pb：我换个说法", interlocutor="pb")
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()          # 删起点副本 = 提交


def test_a_second_round_with_the_same_deploy_id_does_not_hit_the_first_rounds_keys(store, mem):
    _run(store, _Factory(), mem)
    _run(store, _Factory({"opening": "今天好点了"}), mem, round_id="agent-round-zengen-v2", run_id="run-004")
    again = mem.store.remember_event("pa", KEY0, "pb：我很生气!!", interlocutor="pb")
    assert not again.failed and again.stored                           # 第一轮的行仍 committed、幂等
    second = mem.store.remember_event("pa", f"agent-round-zengen-v2/{EID}#0", "pb：今天好点了",
                                      interlocutor="pb")
    assert not second.failed and second.stored


def test_each_episode_gets_a_fresh_instance_whose_id_carries_the_round(store, mem):
    seen, started = [], []
    eps = _run(store, _Factory(), mem, make=_make(seen=seen), started=started, episodes_per_scenario=2)
    assert seen == [f"{ROUND}/{EID}", f"{ROUND}/deploy-zengen-v1-s1-2"]
    assert [e.id for e in eps] == [EID, "deploy-zengen-v1-s1-2"]
    for ep, (eid, by_persona) in zip(eps, started):
        first = next(t for t in ep.turns if t.speaker_slot == "a")
        assert eid == ep.id and first.activation_trace["memory"]["digest_before"] == by_persona["pa"]
    assert started[0][1]["pa"] != started[1][1]["pa"]                   # 第一条提交的记忆进了第二条的起点


def test_a_crash_mid_episode_is_rerun_from_the_restored_start(store, mem):
    digests = {}

    class Crash(Exception):
        pass

    class Crashing(_FakeClient):
        def submit_turn(self, *a, **kw):
            raise Crash()

    with pytest.raises(Crash):
        _run(store, lambda **kw: Crashing(), mem, digests=digests)
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()
    assert mem.store.digest("pa") != digests[EID]["pa"]                # 半截记忆已写进部署库
    f = _Factory({"opening": "我换个说法"})
    (ep,) = _run(store, f, mem, digests=digests)                       # 重入:先覆盖、核对摘要、再重跑
    assert len(f.clients) == 1 and ep.id == EID
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()
    assert not mem.store.remember_event("pa", KEY0, "pb：我换个说法", interlocutor="pb").failed


def test_an_episode_written_but_not_committed_is_rerun(store, mem, monkeypatch):
    class Crash(BaseException):
        """进程在落盘与删副本之间被杀(不是 OSError:管理面的 OSError 现在判 T3,见下一条)。"""

    digests = {}
    real = mem.store.pending_events
    monkeypatch.setattr(mem.store, "pending_events", lambda pid: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        _run(store, _Factory(), mem, digests=digests)                  # episode 已落盘、副本没删
    assert store.exists(episode_file(RUN, EID)) and mem.layout.snapshot(ROUND, EID, "pa").exists()
    monkeypatch.setattr(mem.store, "pending_events", real)
    f = _Factory()
    _run(store, f, mem, digests=digests)
    assert len(f.clients) == 1
    f2 = _Factory()
    _run(store, f2, mem, digests=digests)                              # 已提交的被复用
    assert f2.clients == []


@pytest.mark.parametrize("op", ["mkdir", "backup_to", "open", "pending_events", "abandon_event", "unlink"])
def test_a_permanent_memory_management_failure_is_a_round_failure_reporting_only_the_class(store, mem, monkeypatch, op):
    """终审 I-2:管理面与副本文件操作的 OSError 与非瞬态 MemoryFailureError 改抛 AgentRoundFailedError
    (失败点 mindagent.deployment.memory,只报类名),轮次处理器据此判 T3 并回滚;不原样逃出。"""
    from mindagent.errors import AgentRoundFailedError
    secret = "/secret/endpoint"
    boom = OSError(secret) if op in ("mkdir", "open", "unlink", "abandon_event") else MemoryFailureError(secret, transient=False)
    if op == "abandon_event":
        mem.failing.add(KEY0)                                          # 留一条本 episode 的 pending 行,才会调 abandon
        f = _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"})
    else:
        f = _Factory()
    if op == "mkdir":
        monkeypatch.setattr(deployment.Path, "mkdir", lambda self, *a, **kw: (_ for _ in ()).throw(boom))
    elif op == "unlink":
        real_unlink = deployment.Path.unlink
        monkeypatch.setattr(deployment.Path, "unlink",
                            lambda self, *a, **kw: (_ for _ in ()).throw(boom) if self.name.endswith("-pa.json")
                            else real_unlink(self, *a, **kw))
    else:
        monkeypatch.setattr(mem.store, op, lambda *a, **kw: (_ for _ in ()).throw(boom))
    with pytest.raises(AgentRoundFailedError) as exc:
        _run(store, f, mem)
    assert exc.value.point == "mindagent.deployment.memory"
    assert type(boom).__name__ in exc.value.reason and secret not in exc.value.reason


def test_a_transient_memory_management_failure_still_propagates_unchanged(store, mem, monkeypatch):
    boom = MemoryFailureError("timeout", transient=True)
    monkeypatch.setattr(mem.store, "backup_to", lambda *a: (_ for _ in ()).throw(boom))
    with pytest.raises(MemoryFailureError) as exc:
        _run(store, _Factory(), mem)
    assert exc.value is boom


def test_a_restore_whose_digest_disagrees_fails_and_keeps_library_and_copy(store, mem):
    digests = {}
    with pytest.raises(OSError):
        _run(store, lambda **kw: (_ for _ in ()).throw(OSError("x")), mem, digests=digests)
    before = mem.store.digest("pa")
    digests[EID] = {"pa": "not-the-recorded-digest"}
    with pytest.raises(RestoreFailedError, match=EID):
        _run(store, _Factory(), mem, digests=digests)
    assert mem.layout.snapshot(ROUND, EID, "pa").exists() and mem.store.digest("pa") == before


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


def test_exhausted_transient_retries_leave_the_library_at_the_start(store, mem):
    digests = {}
    with pytest.raises(DeploymentFailedError) as info:
        _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}), mem, digests=digests)
    assert info.value.transient
    assert mem.store.digest("pa") == digests[EID]["pa"]
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()              # 在途:留给重入或轮次回滚


def test_a_rejection_for_unavailable_role_memory_is_transient(store, mem):
    f = _Factory({"create_error": SessionRejectedError("role_memory_unavailable", "")})
    (ep,) = _run(store, f, mem)
    assert len(f.clients) == 2 and ep.id == EID


def test_a_transient_failure_then_success_commits_once(store, mem, monkeypatch):
    writes = []
    real = deployment.write_episode

    def recording(store_, run_id, episode):
        writes.append((episode.turns[0].utterance, mem.layout.snapshot(ROUND, EID, "pa").exists()))
        return real(store_, run_id, episode)

    monkeypatch.setattr(deployment, "write_episode", recording)
    (ep,) = _run(store, _Factory({"end_reason": "timeout"}, {"opening": "我换个说法"}), mem)
    assert writes == [("我换个说法", True)]                             # 只落一次,用第二次的结果,副本此时还在
    assert ep.turns[0].utterance == "我换个说法"
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()          # 落盘之后才删副本


def test_a_failed_attempt_is_rolled_back_after_its_port_is_closed(store, mem, tmp_path):
    log = []

    class Recording(FileTupleStore):
        def open(self, persona_id):
            log.append(("open", persona_id))
            return super().open(persona_id)

        def restore_from(self, persona_id, path):
            log.append(("restore", persona_id, self._open.get(persona_id, 0)))
            return super().restore_from(persona_id, path)

    rec = SimpleNamespace(store=Recording(tmp_path / "rec" / "deploy"), layout=MemoryLayout(tmp_path / "rec"))
    _run(store, _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}, {}), rec,
         max_retries_per_episode=2)
    assert log == [("open", "pa"), ("restore", "pa", 0), ("open", "pa"), ("restore", "pa", 0), ("open", "pa")]


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

    def make(episode_id, memory):
        real = inner(episode_id, memory)
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
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()


def test_an_exhausted_admission_budget_is_a_transient_failure_and_the_library_stays_at_the_start(store, mem):
    clock, digests = _FakeClock(), {}
    f = _Factory(*[{"create_error": _BUSY}] * 100)
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, f, mem, digests=digests, max_retries_per_episode=0, admission_wait_s=20.0,
             sleep=clock.sleep, clock=clock.clock)
    assert (exc.value.reason, exc.value.attempts, exc.value.transient) == ("role_backend_unavailable", 1, True)
    assert clock.sleeps[:5] == [0.5, 1.0, 2.0, 4.0, 8.0] and max(clock.sleeps) <= 8.0
    assert clock.now == 20.0                                                   # 用够预算,不超出
    assert mem.store.digest("pa") == digests[EID]["pa"]
    assert mem.layout.snapshot(ROUND, EID, "pa").exists()


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
    assert not mem.layout.snapshot(ROUND, EID, "pa").exists()               # 提交了


def test_a_same_id_rerun_after_a_client_abort_backs_off_and_succeeds_against_a_real_server(
        store, mem, same_id_server):
    """客户端侧先 abort 的瞬态:相邻消息等待超时(message_timeout_s)让客户端合成 timeout 并放掉流,旧会话的
    对手调用还挂着、服务端在 join 里;同一部署 id 的立即重跑被准入拒,退避到旧会话收尾后建成。"""
    env = same_id_server
    threading.Timer(1.5, env.hang.release.set).start()
    (ep,) = _run(store, env.make_factory(message_timeout_s=0.5), mem, admission_wait_s=10.0)
    assert ep.id == EID and ep.terminated_reason == "max_turns"
    assert "role_backend_unavailable" in env.rejected
