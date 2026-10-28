import threading
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from memory_fixtures import unresolved_memory
from mindtrainer.training_config import (
    InvalidTrainingConfigError, RewardConfig, TrainingConfig,
)
from mindtrainer.trajectory_source import (
    PER_TURN_DEADLINE_S, InteractTrajectorySource, RolloutFailedError, logical_episode_id,
)
from realtime_fixtures import plain_factory, plain_hook
from simulator.backends import ScriptedBackend
from simulator.dataset import episode_group_id
from simulator.datamodel import (
    Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord,
)
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.runner import run_episode
from simulator.store_adapter import read_scenario, write_persona, write_scenario


def _persona(pid):
    return Persona(pid, ["t"], "m", "s", ["v"])


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _trained():
    return RoleBinding("a", "pa", "self_play")


def _opponent():
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _cfg(**kw):
    base = dict(
        mode="interact", trained_slot="a",
        base_checkpoint_ref=BigRef("file:///ckpt/a", "a" * 64, 1),
        teacher_model_ref=BigRef("file:///ckpt/b", "b" * 64, 1),
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=0, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        scenario_ids=["s1", "s2"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=4, memory=unresolved_memory(),
    )
    base.update(kw)
    return TrainingConfig(**base)


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _shape(episode):
    return [(t.speaker_slot, t.utterance) for t in episode.turns], episode.terminated_reason


def _fixed(policy):
    return lambda episode_id: nullcontext(policy)


class _Rollouts:
    """policy_for_rollout:记下每次进出与 id,进入时交出 policy。"""
    def __init__(self, policy, events):
        self.policy, self.events = policy, events

    @contextmanager
    def __call__(self, episode_id):
        self.events.append(("enter", episode_id))
        try:
            yield self.policy
        finally:
            self.events.append(("exit", episode_id))


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(s, _scenario(sid))
    for pid in ("pa", "pb"):
        write_persona(s, _persona(pid))
    return s


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start(opponent_lines=("b1", "b2", "b3", "b4"), turn_timeout_s=2.0):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {
                s: ScriptedBackend({s: list(opponent_lines)})
                for s in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            plain_factory(factory), port=0, turn_timeout_s=turn_timeout_s, on_session_closed=plain_hook(closed.append),
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory(*, deadline_s=None):
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))

        return SimpleNamespace(server=server, closed=closed, client_factory=client_factory)

    yield start
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


# ====================== 对真实 loopback 服务端 ======================

def test_rollouts_match_what_run_episode_produces_for_the_same_scripts(store, live):
    env = live()
    policy = ScriptedBackend({"a": ["a1", "a2", "a3", "a4"]})
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, group_size=2)
    batch = InteractTrajectorySource(env.client_factory, _fixed(policy), store, cfg, run_id="run-1").collect()

    personas = {pid: _persona(pid) for pid in ("pa", "pb")}
    expected = []
    for k, lines in enumerate((["a1", "a2"], ["a3", "a4"])):
        assignment = RoleAssignment("s1", [_trained(), _opponent()], 4)
        backends = {
            "a": ScriptedBackend({"a": lines}),
            "b": ScriptedBackend({"b": ["b1", "b2"]}),
        }
        expected.append(run_episode(read_scenario(store, "s1"), assignment, personas, backends, f"direct-{k}"))
    assert [_shape(e) for e in batch.episodes] == [_shape(e) for e in expected]
    assert all(e.terminated_reason == "max_turns" for e in batch.episodes)


def test_each_scenario_forms_one_group_of_group_size(store, live):
    env = live()
    policy = ScriptedBackend({"a": [f"a{i}" for i in range(20)]})
    batch = InteractTrajectorySource(
        env.client_factory, _fixed(policy), store, _cfg(group_size=3), run_id="run-1").collect()
    assert [e.scenario_id for e in batch.episodes] == ["s1"] * 3 + ["s2"] * 3
    gids = [episode_group_id(e) for e in batch.episodes]
    assert len(set(gids[:3])) == 1 and len(set(gids[3:])) == 1 and gids[0] != gids[3]
    assert len({e.id for e in batch.episodes}) == 6
    assert batch.metadata["episode_ids"] == [e.id for e in batch.episodes]
    assert batch.metadata["failed_attempts"] == []


def test_the_server_persists_exactly_the_episodes_returned(store, live):
    env = live()
    policy = ScriptedBackend({"a": [f"a{i}" for i in range(20)]})
    batch = InteractTrajectorySource(env.client_factory, _fixed(policy), store, _cfg(), run_id="run-1").collect()
    assert _wait_until(lambda: len(env.closed) == 4)
    assert {e.id for e in env.closed} == {e.id for e in batch.episodes}


def test_the_server_persists_each_rollout_under_its_logical_id(store, live):
    env = live()
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, group_size=2)
    batch = InteractTrajectorySource(
        env.client_factory, _fixed(ScriptedBackend({"a": ["a1", "a2", "a3", "a4"]})), store, cfg,
        run_id="run-1").collect()
    assert batch.metadata["episode_ids"] == ["run-1-grpo0-s1-r0", "run-1-grpo0-s1-r1"]
    assert _wait_until(lambda: len(env.closed) == 2)
    assert sorted(e.id for e in env.closed) == batch.metadata["episode_ids"]


def test_trained_slot_speaking_second_sees_the_opponents_opening_in_its_history(store, live):
    env = live()
    calls, seen = [], []

    class Recorder:
        def respond(self, persona, scenario, history, speaker_slot):
            calls.append((persona.id, scenario.id, speaker_slot))
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return f"a{len(seen)}", None

    cfg = _cfg(bindings=[_opponent(), _trained()], scenario_ids=["s1"], groups_per_step=1)
    batch = InteractTrajectorySource(env.client_factory, _fixed(Recorder()), store, cfg, run_id="run-1").collect()
    assert calls[0] == ("pa", "s1", "a")
    assert seen[0] == [("b", "b1")]
    assert seen[1] == [("b", "b1"), ("a", "a1"), ("b", "b2")]
    assert [t.speaker_slot for t in batch.episodes[0].turns] == ["b", "a", "b", "a"]


def test_inner_thoughts_reach_the_server_side_episode_only_for_the_trained_slot(store, live):
    env = live()

    class Thinker:
        n = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.n += 1
            return f"a{self.n}", f"thought-{self.n}"

    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1)
    InteractTrajectorySource(env.client_factory, _fixed(Thinker()), store, cfg, run_id="run-1").collect()
    assert _wait_until(lambda: len(env.closed) == 2)
    for episode in env.closed:
        trained = [t for t in episode.turns if t.speaker_slot == "a"]
        opponent = [t for t in episode.turns if t.speaker_slot == "b"]
        assert trained and all((t.inner_thought or "").startswith("thought-") for t in trained)
        assert all(t.inner_thought is None for t in opponent)


class _Boom(Exception):
    pass


def test_a_policy_exception_aborts_the_session_and_no_half_episode_is_persisted(store, live):
    env = live()

    class Exploding:
        calls = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.calls += 1
            if self.calls == 2:  # 已经提交过一轮,会话进行到一半
                raise _Boom("model bug")
            return "a1", None

    clients = []

    def tracked(**kw):
        client = env.client_factory(**kw)
        clients.append(client)
        return client

    with pytest.raises(_Boom, match="model bug"):
        InteractTrajectorySource(tracked, _fixed(Exploding()), store, _cfg(), run_id="run-1").collect()
    env.server.stop(2).wait()  # 等在途会话收尾,再断言"没有落盘"
    assert env.closed == []
    assert len(clients) == 1 and clients[0].closed


def test_a_stopped_server_is_a_transient_transport_failure_retried_then_reported(store, live):
    env = live()
    env.server.stop(0).wait()
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, max_retries_per_episode=1)
    source = InteractTrajectorySource(
        env.client_factory, _fixed(ScriptedBackend({"a": ["x"] * 4})), store, cfg, run_id="run-1"
    )
    with pytest.raises(RolloutFailedError) as exc:
        source.collect()
    assert exc.value.reason == "transport_unavailable"
    assert exc.value.attempts == 2
    assert [a["attempt"] for a in exc.value.failed_attempts] == [1, 2]


# ====================== 对替身客户端(不走网络) ======================

class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "connection reset"


class _Exhausted(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.RESOURCE_EXHAUSTED

    def details(self):
        return "too many sessions"


class _FakeClient:
    """客户端先说,每次提交后对方回一句;凑够 max_turns 轮后以 end_reason 结束。"""

    def __init__(self, ep_id, *, create_error=None, submit_error=None,
                 end_reason="max_turns", max_turns=4):
        self._ep_id = ep_id
        self._create_error = create_error
        self._submit_error = submit_error
        self._end_reason = end_reason
        self._max_turns = max_turns
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
        self.created = dict(
            scenario_id=scenario_id, role_assignment=role_assignment, personas=personas,
            purpose=purpose, slot=client_driven_slot, episode_id=episode_id,
        )
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        self.traces.append(activation_trace)
        if self._submit_error is not None:
            self.closed = True
            raise self._submit_error
        self._turns.append(TurnRecord(len(self._turns), self.created["slot"], utterance, inner_thought))
        self._turns.append(TurnRecord(len(self._turns), "b", "opp"))
        if len(self._turns) >= self._max_turns:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode(
            self._ep_id, self.created["scenario_id"], self.created["role_assignment"],
            self.turns, self._end_reason,
        )

    def abort(self):
        self.aborted = True
        self.closed = True


class _FakeFactory:
    def __init__(self, *plans):
        self._plans = list(plans)
        self.clients = []
        self.deadlines = []

    def __call__(self, *, deadline_s=None):
        self.deadlines.append(deadline_s)
        plan = self._plans.pop(0) if self._plans else {}
        client = _FakeClient(f"ep-fake-{len(self.clients)}", **plan)
        self.clients.append(client)
        return client


def _source(store, factory, policy=None, **cfg_kw):
    policy = policy or ScriptedBackend({"a": [f"a{i}" for i in range(50)]})
    start_step = cfg_kw.pop("start_step", 0)
    return InteractTrajectorySource(
        factory, _fixed(policy), store, _cfg(**cfg_kw), run_id="run-1", start_step=start_step)


_ONE_GROUP = dict(scenario_ids=["s1"], groups_per_step=1)


def test_a_goal_reached_episode_is_accepted_like_max_turns(store):
    factory = _FakeFactory({"end_reason": "goal_reached"})
    batch = _source(store, factory, **_ONE_GROUP).collect()
    assert len(batch.episodes) == 2
    assert batch.episodes[0].terminated_reason == "goal_reached"
    assert batch.metadata["failed_attempts"] == []
    assert len(factory.clients) == 2


@pytest.mark.parametrize(
    "plan,reason",
    [
        ({"end_reason": "timeout"}, "timeout"),
        ({"end_reason": "role_backend_timeout"}, "role_backend_timeout"),
        ({"end_reason": "role_backend_unavailable"}, "role_backend_unavailable"),
        ({"end_reason": "role_memory_unavailable"}, "role_memory_unavailable"),
        ({"end_reason": "persistence_failed"}, "persistence_failed"),
        ({"create_error": _Unavailable()}, "transport_unavailable"),
        ({"submit_error": _Unavailable()}, "transport_unavailable"),
    ],
    ids=["timeout", "role_backend_timeout", "role_backend_unavailable", "role_memory_unavailable",
         "persistence_failed", "unavailable_on_create", "unavailable_on_submit"],
)
def test_a_transient_failure_is_retried_and_recorded_and_the_half_episode_is_not_kept(store, plan, reason):
    factory = _FakeFactory(plan)
    batch = _source(store, factory, **_ONE_GROUP).collect()
    assert len(batch.episodes) == 2
    assert all(e.terminated_reason == "max_turns" for e in batch.episodes)
    assert "ep-fake-0" not in batch.metadata["episode_ids"]
    assert batch.metadata["failed_attempts"] == [
        {"scenario_id": "s1", "rollout": 0, "attempt": 1, "reason": reason,
         "detail": batch.metadata["failed_attempts"][0]["detail"]}
    ]
    assert len(factory.clients) == 3  # 失败一次 + 两条成功


def test_retries_are_bounded_and_the_error_carries_every_attempt(store):
    factory = _FakeFactory({"end_reason": "timeout"}, {"end_reason": "timeout"})
    source = _source(store, factory, max_retries_per_episode=1, **_ONE_GROUP)
    with pytest.raises(RolloutFailedError) as exc:
        source.collect()
    err = exc.value
    assert (err.scenario_id, err.rollout, err.reason, err.attempts) == ("s1", 0, "timeout", 2)
    assert [a["attempt"] for a in err.failed_attempts] == [1, 2]
    assert "rollout 0" in str(err) and "'s1'" in str(err)
    assert len(factory.clients) == 2


def test_zero_retries_means_a_single_attempt(store):
    factory = _FakeFactory({"end_reason": "timeout"})
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, max_retries_per_episode=0, **_ONE_GROUP).collect()
    assert exc.value.attempts == 1 and len(factory.clients) == 1


@pytest.mark.parametrize(
    "plan,reason",
    [
        ({"create_error": SessionRejectedError("invalid_session", "bad persona")}, "invalid_session"),
        ({"create_error": SessionRejectedError("role_backend_failure", "factory exploded")}, "role_backend_failure"),
        ({"end_reason": "role_backend_failure"}, "role_backend_failure"),
        ({"end_reason": "role_backend_rejected"}, "role_backend_rejected"),
        ({"end_reason": "role_memory_failure"}, "role_memory_failure"),
        ({"end_reason": "client_disconnected"}, "client_disconnected"),
        ({"create_error": _Exhausted()}, "transport_resource_exhausted"),
        ({"submit_error": RuntimeError("server closed the stream without sending EndSession")}, "client_protocol_error"),
    ],
    ids=["invalid_session", "factory_fault", "backend_failure", "backend_rejected", "memory_failure",
         "disconnected", "exhausted", "protocol_error"],
)
def test_non_transient_failures_are_not_retried(store, plan, reason):
    factory = _FakeFactory(plan)
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, **_ONE_GROUP).collect()
    assert exc.value.reason == reason
    assert exc.value.attempts == 1
    assert len(factory.clients) == 1
    assert len(exc.value.failed_attempts) == 1


def test_a_failure_carries_the_earlier_transient_failures_of_the_same_collect(store):
    factory = _FakeFactory(
        {"end_reason": "timeout"},                                      # rollout 0,第 1 次
        {},                                                             # rollout 0,第 2 次成功
        {"create_error": SessionRejectedError("invalid_session", "x")}, # rollout 1,不可重试
    )
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, **_ONE_GROUP).collect()
    assert [(a["rollout"], a["attempt"], a["reason"]) for a in exc.value.failed_attempts] == [
        (0, 1, "timeout"), (1, 1, "invalid_session"),
    ]


def test_a_policy_exception_is_never_retried_wrapped_or_swallowed_even_if_it_is_a_runtimeerror(store):
    class Broken:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("model bug")

    factory = _FakeFactory()
    with pytest.raises(RuntimeError, match="model bug") as exc:
        _source(store, factory, policy=Broken(), **_ONE_GROUP).collect()
    assert not isinstance(exc.value, RolloutFailedError)
    assert len(factory.clients) == 1 and factory.clients[0].aborted


def test_scenarios_rotate_with_the_step_and_start_step_resumes_the_rotation(store):
    rotating = dict(scenario_ids=["s1", "s2", "s3"], groups_per_step=2)
    factory = _FakeFactory()
    source = _source(store, factory, **rotating)
    source.collect()
    source.collect()
    used = [c.created["scenario_id"] for c in factory.clients]
    assert used == ["s1", "s1", "s2", "s2", "s3", "s3", "s1", "s1"]

    resumed = _FakeFactory()
    _source(store, resumed, start_step=1, **rotating).collect()
    assert [c.created["scenario_id"] for c in resumed.clients] == ["s3", "s3", "s1", "s1"]


def test_a_failed_collect_does_not_advance_the_rotation(store):
    factory = _FakeFactory({"create_error": SessionRejectedError("invalid_session", "x")})
    source = _source(store, factory)
    with pytest.raises(RolloutFailedError):
        source.collect()
    source.collect()
    assert [c.created["scenario_id"] for c in factory.clients[1:]] == ["s1", "s1", "s2", "s2"]


def test_create_session_receives_the_configured_assignment_personas_and_purpose(store):
    factory = _FakeFactory()
    _source(store, factory, **_ONE_GROUP).collect()
    first, second = factory.clients[0].created, factory.clients[1].created
    assert first["purpose"] == "training" and first["slot"] == "a"
    assert set(first["personas"]) == {"pa", "pb"}
    assignment = first["role_assignment"]
    assert assignment.max_turns == 4
    assert [b.to_dict() for b in assignment.bindings] == [_trained().to_dict(), _opponent().to_dict()]
    # 内容相同(同组),但每次是独立对象
    assert assignment.to_dict() == second["role_assignment"].to_dict()
    assert assignment is not second["role_assignment"]


def test_constructor_rejects_the_wrong_mode_an_invalid_config_and_a_negative_start(store):
    batch_cfg = dict(
        mode="batch", sft_steps=0, grpo_steps=2, stage_manifest_refs={"grpo_opd": "m"},
        scenario_ids=[], bindings=[], max_turns_per_episode=0, memory=None,  # batch 配置不带 memory 段
    )
    with pytest.raises(ValueError, match="mode='interact'"):
        _source(store, _FakeFactory(), **batch_cfg)
    with pytest.raises(InvalidTrainingConfigError, match="trained_slot 'ghost'"):
        _source(store, _FakeFactory(), trained_slot="ghost")
    with pytest.raises(ValueError, match="start_step must be >= 0"):
        _source(store, _FakeFactory(), start_step=-1)


# ====================== 逻辑 episode id、按 rollout 进出上下文、轨迹上行 ======================

def test_the_logical_episode_id_is_run_stage_scenario_and_rollout():
    assert logical_episode_id("run-7", 3, "s1", 1) == "run-7-grpo3-s1-r1"


def test_each_rollout_gets_its_own_context_and_its_logical_id_reaches_create_session(store):
    events = []
    factory = _FakeFactory({"end_reason": "timeout"})
    source = InteractTrajectorySource(
        factory, _Rollouts(ScriptedBackend({"a": ["x"] * 50}), events), store, _cfg(**_ONE_GROUP),
        run_id="run-1", start_step=2)
    source.collect()
    ids = ["run-1-grpo2-s1-r0", "run-1-grpo2-s1-r0", "run-1-grpo2-s1-r1"]   # 第一条重试一次,id 不变
    assert [c.created["episode_id"] for c in factory.clients] == ids
    assert events == [e for i in ids for e in (("enter", i), ("exit", i))]
    assert factory.deadlines == [_cfg().max_turns_per_episode * PER_TURN_DEADLINE_S] * 3


def test_a_three_tuple_reply_sends_its_trace_and_a_two_tuple_sends_none(store):
    class Traced:
        def __init__(self):
            self.n = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.n += 1
            return ("a", None, {"schema": "actio.trace.v2", "n": str(self.n)}) if self.n % 2 else ("b", None)

    factory = _FakeFactory()
    InteractTrajectorySource(factory, _fixed(Traced()), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    traces = factory.clients[0].traces
    assert traces[0] == {"schema": "actio.trace.v2", "n": "1"} and traces[1] is None


def test_a_reply_of_the_wrong_length_is_a_policy_bug_and_is_not_retried(store):
    class Wrong:
        def respond(self, persona, scenario, history, speaker_slot):
            return ("a", None, {}, "extra")

    factory = _FakeFactory()
    with pytest.raises(TypeError, match="2- or 3-tuple"):
        InteractTrajectorySource(factory, _fixed(Wrong()), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(factory.clients) == 1 and factory.clients[0].aborted


@pytest.mark.parametrize("reason", ["timeout", "role_memory_unavailable"])
def test_a_create_rejection_with_a_transient_reason_is_retried(store, reason):
    factory = _FakeFactory({"create_error": SessionRejectedError(reason, "busy")})
    batch = _source(store, factory, **_ONE_GROUP).collect()
    assert len(batch.episodes) == 2
    assert [a["reason"] for a in batch.metadata["failed_attempts"]] == [reason]


# ====================== 准入被拒(role_backend_unavailable)时同一次尝试里退避重建 ======================

class _FakeClock:
    """注入给轨迹源的 sleep/clock:sleep 只把假时钟往前拨,不真睡。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


_BUSY = SessionRejectedError("role_backend_unavailable", "episode 'x' already has an active session")


def _backoff_source(store, factory, clock, **kw):
    cfg_kw = dict(_ONE_GROUP, group_size=2)
    cfg_kw.update(kw.pop("cfg", {}))
    return InteractTrajectorySource(
        factory, _fixed(ScriptedBackend({"a": [f"a{i}" for i in range(50)]})), store, _cfg(**cfg_kw),
        run_id="run-1", sleep=clock.sleep, clock=clock.clock, **kw)


def test_an_admission_refusal_backs_off_inside_the_attempt_and_does_not_count_as_a_retry(store):
    clock = _FakeClock()
    factory = _FakeFactory({"create_error": _BUSY}, {"create_error": _BUSY})
    batch = _backoff_source(store, factory, clock, cfg={"max_retries_per_episode": 0}).collect()
    assert len(batch.episodes) == 2 and batch.metadata["failed_attempts"] == []   # 一次计数的尝试就成了
    assert len(factory.clients) == 4                                             # 两次被拒 + 两条成功
    assert [c.created["episode_id"] for c in factory.clients[:3]] == ["run-1-grpo0-s1-r0"] * 3
    assert all(c.closed for c in factory.clients[:2])
    assert clock.sleeps == [0.5, 1.0]                                            # 指数退避,没有真睡


def test_the_admission_backoff_is_capped_and_its_budget_falls_through_to_a_transient_failure(store):
    clock = _FakeClock()
    factory = _FakeFactory(*[{"create_error": _BUSY}] * 100)
    source = _backoff_source(store, factory, clock, cfg={"max_retries_per_episode": 0}, admission_wait_s=20.0)
    with pytest.raises(RolloutFailedError) as exc:
        source.collect()
    assert (exc.value.reason, exc.value.attempts) == ("role_backend_unavailable", 1)
    assert exc.value.failed_attempts[0]["detail"] == _BUSY.detail
    assert max(clock.sleeps) <= 8.0 and clock.sleeps[:5] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert clock.now == 20.0                                                     # 用够预算,不超出


def test_the_default_admission_budget_outlasts_the_servers_join_timeout():
    import inspect
    from simulator.realtime.server import create_server as _cs
    join = inspect.signature(_cs).parameters["resource_join_timeout_s"].default
    default = inspect.signature(InteractTrajectorySource).parameters["admission_wait_s"].default
    assert default >= join + 5


def test_other_create_rejections_are_not_backed_off(store):
    clock = _FakeClock()
    factory = _FakeFactory({"create_error": SessionRejectedError("invalid_session", "x")})
    with pytest.raises(RolloutFailedError):
        _backoff_source(store, factory, clock).collect()
    assert clock.sleeps == [] and len(factory.clients) == 1


def test_an_admission_budget_that_is_not_a_finite_non_negative_number_is_refused(store):
    for bad in (-1.0, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="admission_wait_s"):
            InteractTrajectorySource(_FakeFactory(), _fixed(None), store, _cfg(**_ONE_GROUP), run_id="run-1",
                                     admission_wait_s=bad)


class _HangOnce:
    """对手位后端:第一次 respond 挂到 release(比服务端 backend_timeout_s 久),之后照常回话。"""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def respond(self, persona, scenario, history, speaker_slot):
        self.calls += 1
        if self.calls == 1:
            self.release.wait(10)
        return f"b{self.calls}", None


@pytest.fixture
def same_id_server(store):
    """真 loopback 服务端:对手位第一次调用挂住,backend_timeout_s 取 1 秒、resource_join_timeout_s 取 3 秒;返回 (hang, client_factory, 被拒原因)。
    client_factory(message_timeout_s=...) 记下每个客户端 create_session 被拒的原因。"""
    hang = _HangOnce()
    rejected: list[str] = []
    channels = []

    def factory(scenario, assignment, personas, client_slot):
        return {s: hang for s in assignment.slot_ids() - {client_slot}}

    server = create_server(
        plain_factory(factory), port=0, turn_timeout_s=5.0, backend_timeout_s=1.0,
        resource_join_timeout_s=3.0, linger_retry_s=0.05,
        scenario_resolver=lambda sid: read_scenario(store, sid),
    )
    server.start()

    def make_factory(message_timeout_s=None):
        def client_factory(*, deadline_s=None):
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            client = EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel),
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
    server.stop(0)


_OPP_FIRST = dict(scenario_ids=["s1"], groups_per_step=1, group_size=2, max_retries_per_episode=1,
                  bindings=[_opponent(), _trained()])


def test_a_same_id_retry_after_a_hung_opponent_backs_off_and_succeeds_against_a_real_server(store, same_id_server):
    """设计 §11:同一逻辑 episode id 仍有活动会话时,第二个 CreateSession 以 role_backend_unavailable 被拒、
    客户端按条重跑成功。对手开场那次调用挂过 backend_timeout_s → role_backend_timeout;服务端在 join 里
    最多等 resource_join_timeout_s,其间同 id 的重跑被准入拒,轨迹源退避到旧会话收尾后建成。"""
    env = same_id_server
    threading.Timer(1.5, env.hang.release.set).start()     # 挂住的线程在 join 期内退出
    source = InteractTrajectorySource(
        env.make_factory(), _fixed(ScriptedBackend({"a": ["x"] * 8})), store, _cfg(**_OPP_FIRST),
        run_id="run-1", admission_wait_s=10.0)
    batch = source.collect()
    assert [e.terminated_reason for e in batch.episodes] == ["max_turns", "max_turns"]
    assert [a["reason"] for a in batch.metadata["failed_attempts"]] == ["role_backend_timeout"]
    assert "role_backend_unavailable" in env.rejected                    # 确实撞上了旧会话
    assert batch.episodes[0].id == "run-1-grpo0-s1-r0"


def test_a_same_id_retry_after_a_client_abort_backs_off_and_succeeds_against_a_real_server(store, same_id_server):
    """客户端侧先 abort 的瞬态:相邻消息等待超时(message_timeout_s)让客户端合成 timeout 并 abort,
    旧会话的对手调用还挂着、服务端在 join 里;同 id 立即重跑被准入拒,退避到旧会话收尾后建成。"""
    env = same_id_server
    threading.Timer(1.5, env.hang.release.set).start()
    source = InteractTrajectorySource(
        env.make_factory(message_timeout_s=0.5), _fixed(ScriptedBackend({"a": ["x"] * 8})), store,
        _cfg(**_OPP_FIRST), run_id="run-1", admission_wait_s=10.0)
    batch = source.collect()
    assert [e.terminated_reason for e in batch.episodes] == ["max_turns", "max_turns"]
    assert [a["reason"] for a in batch.metadata["failed_attempts"]] == ["timeout"]
    assert "role_backend_unavailable" in env.rejected


def test_a_transient_memory_failure_of_the_trained_slot_aborts_and_retries_the_rollout(store):
    class Flaky:
        def __init__(self):
            self.calls = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.calls += 1
            if self.calls == 2:
                raise RoleMemoryUnavailableError("memory backend failed for slot 'a': MemoryFailureError")
            return "a", None

    events = []
    factory = _FakeFactory()
    batch = InteractTrajectorySource(
        factory, _Rollouts(Flaky(), events), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(batch.episodes) == 2 and factory.clients[0].aborted
    assert batch.metadata["failed_attempts"][0]["reason"] == "role_memory_unavailable"
    assert batch.metadata["failed_attempts"][0]["detail"] == "RoleMemoryUnavailableError"
    assert events[:3] == [("enter", "run-1-grpo0-s1-r0"), ("exit", "run-1-grpo0-s1-r0"),
                          ("enter", "run-1-grpo0-s1-r0")]


def test_a_deterministic_memory_failure_of_the_trained_slot_passes_through_unwrapped(store):
    class Broken:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RoleMemoryFailureError("memory backend failed for slot 'a': MemoryClosedError")

    events = []
    factory = _FakeFactory()
    with pytest.raises(RoleMemoryFailureError):
        InteractTrajectorySource(
            factory, _Rollouts(Broken(), events), store, _cfg(**_ONE_GROUP), run_id="run-1").collect()
    assert len(factory.clients) == 1 and factory.clients[0].aborted
    assert events == [("enter", "run-1-grpo0-s1-r0"), ("exit", "run-1-grpo0-s1-r0")]
