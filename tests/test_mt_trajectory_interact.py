import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from mindtrainer.training_config import (
    InvalidTrainingConfigError, RewardConfig, TrainingConfig,
)
from mindtrainer.trajectory_source import (
    InteractTrajectorySource, RolloutFailedError,
)
from simulator.backends import ScriptedBackend
from simulator.dataset import episode_group_id
from simulator.datamodel import (
    Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord,
)
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
        max_turns_per_episode=4,
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
            factory, port=0, turn_timeout_s=turn_timeout_s, on_session_closed=closed.append,
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory():
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
    batch = InteractTrajectorySource(env.client_factory, policy, store, cfg).collect()

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
    batch = InteractTrajectorySource(env.client_factory, policy, store, _cfg(group_size=3)).collect()
    assert [e.scenario_id for e in batch.episodes] == ["s1"] * 3 + ["s2"] * 3
    gids = [episode_group_id(e) for e in batch.episodes]
    assert len(set(gids[:3])) == 1 and len(set(gids[3:])) == 1 and gids[0] != gids[3]
    assert len({e.id for e in batch.episodes}) == 6
    assert batch.metadata["episode_ids"] == [e.id for e in batch.episodes]
    assert batch.metadata["failed_attempts"] == []


def test_the_server_persists_exactly_the_episodes_returned(store, live):
    env = live()
    policy = ScriptedBackend({"a": [f"a{i}" for i in range(20)]})
    batch = InteractTrajectorySource(env.client_factory, policy, store, _cfg()).collect()
    assert _wait_until(lambda: len(env.closed) == 4)
    assert {e.id for e in env.closed} == {e.id for e in batch.episodes}


def test_trained_slot_speaking_second_sees_the_opponents_opening_in_its_history(store, live):
    env = live()
    calls, seen = [], []

    class Recorder:
        def respond(self, persona, scenario, history, speaker_slot):
            calls.append((persona.id, scenario.id, speaker_slot))
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return f"a{len(seen)}", None

    cfg = _cfg(bindings=[_opponent(), _trained()], scenario_ids=["s1"], groups_per_step=1)
    batch = InteractTrajectorySource(env.client_factory, Recorder(), store, cfg).collect()
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
    InteractTrajectorySource(env.client_factory, Thinker(), store, cfg).collect()
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

    def tracked():
        client = env.client_factory()
        clients.append(client)
        return client

    with pytest.raises(_Boom, match="model bug"):
        InteractTrajectorySource(tracked, Exploding(), store, _cfg()).collect()
    env.server.stop(2).wait()  # 等在途会话收尾,再断言"没有落盘"
    assert env.closed == []
    assert len(clients) == 1 and clients[0].closed


def test_a_stopped_server_is_a_transient_transport_failure_retried_then_reported(store, live):
    env = live()
    env.server.stop(0).wait()
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, max_retries_per_episode=1)
    source = InteractTrajectorySource(
        env.client_factory, ScriptedBackend({"a": ["x"] * 4}), store, cfg
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

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(
            scenario_id=scenario_id, role_assignment=role_assignment, personas=personas,
            purpose=purpose, slot=client_driven_slot,
        )
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        return "sess"

    def submit_turn(self, utterance, inner_thought=None):
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

    def __call__(self):
        plan = self._plans.pop(0) if self._plans else {}
        client = _FakeClient(f"ep-fake-{len(self.clients)}", **plan)
        self.clients.append(client)
        return client


def _source(store, factory, policy=None, **cfg_kw):
    policy = policy or ScriptedBackend({"a": [f"a{i}" for i in range(50)]})
    start_step = cfg_kw.pop("start_step", 0)
    return InteractTrajectorySource(factory, policy, store, _cfg(**cfg_kw), start_step=start_step)


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
        ({"end_reason": "persistence_failed"}, "persistence_failed"),
        ({"create_error": _Unavailable()}, "transport_unavailable"),
        ({"submit_error": _Unavailable()}, "transport_unavailable"),
    ],
    ids=["timeout", "role_backend_timeout", "persistence_failed", "unavailable_on_create", "unavailable_on_submit"],
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
        ({"end_reason": "client_disconnected"}, "client_disconnected"),
        ({"create_error": _Exhausted()}, "transport_resource_exhausted"),
        ({"submit_error": RuntimeError("server closed the stream without sending EndSession")}, "client_protocol_error"),
    ],
    ids=["invalid_session", "factory_fault", "backend_failure", "disconnected", "exhausted", "protocol_error"],
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
        scenario_ids=[], bindings=[], max_turns_per_episode=0,
    )
    with pytest.raises(ValueError, match="mode='interact'"):
        _source(store, _FakeFactory(), **batch_cfg)
    with pytest.raises(InvalidTrainingConfigError, match="trained_slot 'ghost'"):
        _source(store, _FakeFactory(), trained_slot="ghost")
    with pytest.raises(ValueError, match="start_step must be >= 0"):
        _source(store, _FakeFactory(), start_step=-1)
