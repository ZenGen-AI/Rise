import time
from types import SimpleNamespace

import grpc
import pytest

from actio.instance import instantiate
from actio.skills import Skill
from actio.trace import validate_trace
from controlplane.gitstore import GitStore
from controlplane.paths import episode_file, episodes_dir
from mindagent.deployment import (
    PURPOSE, DeploymentFailedError, deployment_episode_id, run_deployment_episodes,
)
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


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


CALM = Skill("skill-calm", 1, "micro", {"signal": "emotion_high"}, ("先回应情绪",), priority=1)


def _instance(skills=(), version=None):
    return instantiate(Echo(), skills=list(skills), memories=[],
                       library_version=version or {"skill": 0, "memory": 0}, profile=None)


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
def live(store):
    channels, servers = [], []

    def start(lines=("我真的很生气!!", "好吧", "还有呢")):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {s: ScriptedBackend({s: list(lines)}) for s in assignment.slot_ids() - {client_slot}}

        server = create_server(factory, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
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


def _run(store, instance, client_factory, **kw):
    args = dict(run_id=RUN, scenario_ids=["s1"], personas=_personas(), bindings=BINDINGS,
                model_id=MODEL, max_turns=4)
    args.update(kw)
    return run_deployment_episodes(store, instance, client_factory, **args)


# ---------------- 真实 loopback 服务端 ----------------

def test_episodes_are_persisted_under_the_bound_run_with_deploy_ids(store, live):
    env = live()
    episodes = _run(store, _instance([CALM]), env.client_factory, scenario_ids=["s1", "s2"],
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


def test_agent_turns_carry_traces_and_opponent_turns_do_not(store, live):
    env = live()
    (episode,) = _run(store, _instance([CALM], {"skill": 2, "memory": 0}), env.client_factory)
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


def test_the_agent_sees_the_opponents_opening_turn(store, live):
    env = live()
    seen = []

    class Recorder(Echo):
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return super().respond_guided(persona, scenario, history, speaker_slot, guidance)

    inst = instantiate(Recorder(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    _run(store, inst, env.client_factory)
    assert seen[0] == [("b", "我真的很生气!!")]
    assert seen[1][-1] == ("b", "好吧")


def test_the_server_side_ep_copy_is_left_to_the_caller_and_the_deploy_copy_matches_it(store, live):
    env = live()
    (episode,) = _run(store, _instance([CALM]), env.client_factory)
    deadline = time.monotonic() + 3
    while not env.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    (server_side,) = env.closed
    assert server_side.id.startswith("ep-sess-")
    assert [t.to_dict() for t in server_side.turns] == [t.to_dict() for t in episode.turns]
    assert not store.exists(episode_file(RUN, server_side.id))


def test_reentry_reuses_persisted_episodes_and_runs_only_the_missing_ones(store, live):
    env = live()
    _run(store, _instance(), env.client_factory)
    created = []

    def counting():
        created.append(1)
        return env.client_factory()

    episodes = _run(store, _instance(), counting, episodes_per_scenario=2)
    assert [e.id for e in episodes] == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s1-2"]
    assert len(created) == 1


def test_a_persisted_episode_from_another_library_version_is_regenerated(store, live):
    env = live()
    _run(store, _instance(), env.client_factory)
    created = []

    def counting():
        created.append(1)
        return env.client_factory()

    (episode,) = _run(store, _instance([CALM], {"skill": 1, "memory": 0}), counting)
    assert len(created) == 1
    assert read_episode(store, RUN, episode.id).turns[1].activation_trace["library_version"]["skill"] == "1"


def test_a_stopped_server_is_transient_and_retried_then_reported(store, live):
    env = live()
    env.server.stop(0).wait()
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), env.client_factory, max_retries_per_episode=1)
    assert exc.value.reason == "transport_unavailable"
    assert exc.value.transient is True and exc.value.attempts == 2
    assert store.list_products(episodes_dir(RUN)) == []


def test_an_instance_exception_aborts_the_session_and_nothing_is_persisted(store, live):
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

    inst = instantiate(Exploding(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    with pytest.raises(Boom):
        _run(store, inst, env.client_factory)
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

    def __init__(self, *, create_error=None, end_reason="max_turns"):
        self._create_error = create_error
        self._end_reason = end_reason
        self._turns = []
        self.closed = False
        self.aborted = False
        self.created = None
        self.traces = []

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment,
                            purpose=purpose, slot=client_driven_slot)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", "我很生气!!"))
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

    def __call__(self):
        c = _FakeClient(**(self._plans.pop(0) if self._plans else {}))
        self.clients.append(c)
        return c


def test_create_session_gets_the_deployment_purpose_the_agent_slot_and_a_fresh_assignment(store):
    f = _Factory()
    _run(store, _instance(), f, episodes_per_scenario=2)
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
    ({"end_reason": "persistence_failed"}, "persistence_failed"),
    ({"create_error": _Unavailable()}, "transport_unavailable"),
])
def test_a_transient_failure_is_rerun_whole(store, plan, reason):
    f = _Factory(plan)
    (episode,) = _run(store, _instance(), f)
    assert episode.id == "deploy-zengen-v1-s1-1" and episode.terminated_reason == "max_turns"
    assert len(f.clients) == 2


def test_transient_retries_are_bounded(store):
    f = _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}, {"end_reason": "timeout"})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f, max_retries_per_episode=2)
    assert (exc.value.attempts, exc.value.transient, exc.value.reason) == (3, True, "timeout")
    assert [a["attempt"] for a in exc.value.failed_attempts] == [1, 2, 3]
    assert len(f.clients) == 3


@pytest.mark.parametrize("plan,reason", [
    ({"create_error": SessionRejectedError("invalid_session", "bad")}, "invalid_session"),
    ({"end_reason": "role_backend_failure"}, "role_backend_failure"),
    ({"end_reason": "role_backend_rejected"}, "role_backend_rejected"),
    ({"end_reason": "client_disconnected"}, "client_disconnected"),
])
def test_non_transient_failures_are_not_rerun(store, plan, reason):
    f = _Factory(plan)
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f)
    assert (exc.value.reason, exc.value.attempts, exc.value.transient) == (reason, 1, False)
    assert len(f.clients) == 1
    assert store.list_products(episodes_dir(RUN)) == []


def test_successful_episodes_before_a_failure_stay_in_the_tree(store):
    f = _Factory({}, {"create_error": SessionRejectedError("invalid_session", "x")})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f, episodes_per_scenario=2)
    assert exc.value.episode_id == "deploy-zengen-v1-s1-2"
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, "deploy-zengen-v1-s1-1")]


def test_an_instance_exception_aborts_the_fake_session_unwrapped(store):
    class Broken(Echo):
        def respond_guided(self, *a):
            raise RuntimeError("model bug")

    inst = instantiate(Broken(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    f = _Factory()
    with pytest.raises(RuntimeError, match="model bug") as exc:
        _run(store, inst, f)
    assert not isinstance(exc.value, DeploymentFailedError)
    assert f.clients[0].aborted


def test_episode_ids_are_deterministic():
    assert deployment_episode_id("zengen-v3", "scen-a", 2) == "deploy-zengen-v3-scen-a-2"
