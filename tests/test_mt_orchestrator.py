import dataclasses
import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.observe import trace
from controlplane.paths import candidate_checkpoint_file
from mindtrainer.checkpoint import MockConverter
from mindtrainer.datamodel import StepResult, TrainingRun, TrajectoryBatch
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run, run_training
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import AttributionError, build_attribution_backend
from mindtrainer.store_adapter import read_training_run, write_training_run
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource, InteractTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.dataset import DatasetIntegrityError, produce_grouped_dataset, read_manifest
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import (
    read_episode, read_scenario, write_episode, write_persona, write_scenario,
)

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


# ---------------- 世界:数据集、配置、源 ----------------

def _trained():
    return RoleBinding("a", "pa", "self_play")


def _opponent():
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _rollout(scenario, k):
    """k=0 的训练角色位发言含 "agreed"(奖励 1.0),k=1 不含(奖励 0.0):组内有差异。"""
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {
        "a": ScriptedBackend({"a": [first, f"{scenario.id}-a{k}-2"]}),
        "b": ScriptedBackend({"b": [f"{scenario.id}-b{k}-1", f"{scenario.id}-b{k}-2"]}),
    }


def _store(tmp_path, name="repo"):
    s = GitStore(str(tmp_path / name))
    s.init()
    return s


def _populate(store, *, grpo_group_size=2):
    for name, size in (("sft", 1), ("grpo_opd", grpo_group_size)):
        produce_grouped_dataset(
            store, RUN, name,
            scenarios=[
                (_scenario(s), RoleAssignment(s, [_trained(), _opponent()], max_turns=4))
                for s in ("s1", "s2")
            ],
            personas=_personas(), backends_for_rollout=_rollout, group_size=size,
        )
    for sid in ("s1", "s2", "s3"):
        write_scenario(store, _scenario(sid))
    for persona in _personas().values():
        write_persona(store, persona)


@pytest.fixture
def store(tmp_path):
    s = _store(tmp_path)
    _populate(s)
    return s


def _batch_cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=2, grpo_steps=4, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _batch_sources(store, calls=None):
    def make(start):
        if calls is not None:
            calls.append(start)
        return {
            name: BatchTrajectorySource(store, read_manifest(store, RUN, name))
            for name in ("sft", "grpo_opd")
        }
    return make


def _go(store, cfg, executor, *, make_sources=None, converter=None, attribution=None,
        steps=None, **kw):
    return resume_or_run(
        cfg, store=store, run_id=RUN,
        make_sources=make_sources or _batch_sources(store),
        attribution=attribution or build_attribution_backend(cfg.reward),
        executor=executor, converter=converter or MockConverter(),
        on_step=steps.append if steps is not None else None, **kw,
    )


def _candidate(store):
    return store.read_product(candidate_checkpoint_file(RUN))


# ====================== Batch 端到端 ======================

def test_a_batch_run_trains_every_step_and_registers_a_candidate(store):
    ex, steps = MockExecutor(), []
    run = _go(store, _batch_cfg(), ex, steps=steps)
    assert run.status == "executed"
    assert [(s.step, s.phase) for s in steps] == [
        (0, "sft"), (1, "sft"), (2, "grpo_opd"), (3, "grpo_opd"), (4, "grpo_opd"), (5, "grpo_opd"),
    ]
    assert ex.steps_done == 6
    assert (run.checkpoint_step, run.resume_attempts, run.failure) == (6, 0, None)
    assert read_training_run(store, RUN) == run
    cand = _candidate(store)
    assert cand.status == "pending_review"
    assert cand.links == [RUN, "dsm-run-1-sft", "dsm-run-1-grpo_opd"]
    assert BigRef.from_dict(cand.payload["checkpoint_ref"]) == MockConverter().convert(run.last_checkpoint_ref)


def test_the_curve_summary_records_what_the_orchestrator_computed(store):
    summary = _go(store, _batch_cfg(), MockExecutor()).curve_summary
    assert summary["sft_steps_done"] == 2 and summary["grpo_steps_done"] == 4
    assert summary["first_mean_reward"] == 0.5 and summary["final_mean_reward"] == 0.5
    assert summary["final_opd_weight"] == 0.1
    assert (summary["degenerate_groups_total"], summary["failed_rollout_attempts"]) == (0, 0)


def test_metric_stream_refs_are_kept_and_visible_to_trace(store, tmp_path):
    stream = tmp_path / "metrics.jsonl"
    stream.write_text("{}\n")
    ref = BigRef(stream.as_uri(), "d" * 64, 3)
    run = _go(store, _batch_cfg(), MockExecutor(), metric_stream_refs=[ref])
    assert run.metric_stream_refs == [ref]
    assert f"metric_ref [ok] {stream.as_uri()}" in trace(store, RUN)


# ====================== 续训等价(核心) ======================

CRASH_START = {0: 0, 1: 0, 2: 2, 3: 2, 4: 4, 5: 4}  # 崩在哪一步 -> 续训从哪一步起


@pytest.mark.parametrize("crash_at", sorted(CRASH_START))
def test_crash_then_resume_reaches_exactly_the_result_of_an_uninterrupted_run(tmp_path, crash_at):
    straight_store = _store(tmp_path, "straight")
    _populate(straight_store)
    straight_ex = MockExecutor()
    straight = _go(straight_store, _batch_cfg(), straight_ex)

    store = _store(tmp_path, "crashy")
    _populate(store)
    calls, steps = [], []
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=crash_at),
            make_sources=_batch_sources(store, calls))
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.failure) == ("pending", None)
    assert pending.checkpoint_step == CRASH_START[crash_at]

    fresh = MockExecutor()  # 新进程:没有任何内存状态
    resumed = _go(store, _batch_cfg(), fresh, make_sources=_batch_sources(store, calls), steps=steps)

    assert [s.step for s in steps] == list(range(CRASH_START[crash_at], 6))
    assert calls == [0, max(0, CRASH_START[crash_at] - 2)]
    assert fresh.state_hash == straight_ex.state_hash
    assert resumed.resume_attempts == 1
    assert resumed.curve_summary == straight.curve_summary
    assert _candidate(store).payload["checkpoint_ref"] == _candidate(straight_store).payload["checkpoint_ref"]


class _FlakyConverter(MockConverter):
    def __init__(self):
        self.calls = 0

    def convert(self, ref):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("converter down")
        return super().convert(ref)


def test_a_failure_after_the_final_checkpoint_resumes_straight_into_registration(store):
    flaky = _FlakyConverter()
    with pytest.raises(RuntimeError, match="converter down"):
        _go(store, _batch_cfg(), MockExecutor(), converter=flaky)
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.checkpoint_step) == ("pending", 6)
    assert not store.exists(candidate_checkpoint_file(RUN))

    steps = []
    run = _go(store, _batch_cfg(), MockExecutor(), converter=flaky, steps=steps)
    assert steps == []  # 没有重训任何一步
    assert run.status == "executed" and store.exists(candidate_checkpoint_file(RUN))


# ====================== 升级(T3)与幂等 ======================

def test_resume_attempts_are_counted_and_eventually_escalate(store):
    cfg = _batch_cfg(max_resume_attempts=1)
    with pytest.raises(SimulatedCrashError):
        _go(store, cfg, MockExecutor(crash_at_step=3))
    with pytest.raises(SimulatedCrashError):  # 第 1 次续训,又崩在同一步
        _go(store, cfg, MockExecutor(crash_at_step=3))
    assert read_training_run(store, RUN).resume_attempts == 1  # 次数在动执行器之前就落盘了
    with pytest.raises(NeedsHumanError, match="exhausted"):
        _go(store, cfg, MockExecutor())


def test_a_config_that_forbids_resuming_escalates(store):
    cfg = _batch_cfg(allow_resume=False)
    with pytest.raises(SimulatedCrashError):
        _go(store, cfg, MockExecutor(crash_at_step=3))
    with pytest.raises(NeedsHumanError, match="allow_resume"):
        _go(store, cfg, MockExecutor())


def test_a_changed_config_cannot_resume_the_run_and_nothing_is_modified(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3))
    before = read_training_run(store, RUN)
    with pytest.raises(NeedsHumanError, match="config changed"):
        _go(store, _batch_cfg(opd_coefficient=0.2), MockExecutor())
    assert read_training_run(store, RUN) == before


def test_a_finished_run_is_returned_untouched_and_the_executor_never_loaded(store):
    first = _go(store, _batch_cfg(), MockExecutor())
    ex, steps = MockExecutor(), []
    again = _go(store, _batch_cfg(), ex, steps=steps)
    assert again == first and steps == [] and ex.state_hash is None


# ====================== 确定性失败 ======================

def test_a_tampered_sft_episode_fails_the_run_for_good(store):
    target = read_manifest(store, RUN, "sft").episode_refs[0]["id"]
    episode = read_episode(store, RUN, target)
    episode.turns[0].utterance = "rewritten after the manifest was built"
    write_episode(store, RUN, episode)

    ex = MockExecutor()
    with pytest.raises(DatasetIntegrityError, match="sha256 mismatch"):
        _go(store, _batch_cfg(), ex)
    failed = read_training_run(store, RUN)
    assert failed.status == "failed"
    assert (failed.failure["category"], failed.failure["point"]) == ("T2", "step 0 (sft)")
    assert "DatasetIntegrityError" in failed.failure["detail"]
    assert ex.steps_done == 0 and not store.exists(candidate_checkpoint_file(RUN))
    with pytest.raises(NeedsHumanError, match="failed"):  # 失败被保留,不自动重跑
        _go(store, _batch_cfg(), MockExecutor())


def test_undersized_groups_fail_before_a_single_training_step(tmp_path):
    store = _store(tmp_path)
    _populate(store, grpo_group_size=1)
    ex = MockExecutor()
    with pytest.raises(DegenerateGroupError) as exc:
        _go(store, _batch_cfg(), ex)
    assert len(exc.value.group_ids) == 2
    assert ex.steps_done == 0  # 连 SFT 也没有跑:数据有问题时一步都不浪费
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "grpo precheck")


def test_asking_for_more_groups_per_step_than_exist_fails(store):
    with pytest.raises(NotEnoughGroupsError, match="3 exceeds the 2 group"):
        _go(store, _batch_cfg(groups_per_step=3), MockExecutor())
    assert read_training_run(store, RUN).status == "failed"


def test_an_invalid_config_is_recorded_as_failed_and_touches_nothing_else(store):
    calls, ex = [], MockExecutor()
    with pytest.raises(InvalidTrainingConfigError, match="below min_group_size"):
        _go(store, _batch_cfg(group_size=1), ex, make_sources=_batch_sources(store, calls))
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "config")
    assert calls == [] and ex.state_hash is None


class _ConstJudge:
    model_id = "external-judge"

    def score_turn(self, episode, turn_index, component):
        return 1.5  # 越界


def test_an_unusable_attribution_fails_the_run(store):
    reward = RewardConfig(["goal"], "judge", "sum", judge_model_id="external-judge", rubric_version="r1")
    attribution = build_attribution_backend(reward, judge=_ConstJudge(), trained_model_id="zengen-v0")
    with pytest.raises(AttributionError, match="not a finite number in"):
        _go(store, _batch_cfg(reward=reward), MockExecutor(), attribution=attribution)
    assert read_training_run(store, RUN).failure["point"] == "grpo precheck"


# ====================== 不吞异常、不替调用方掩盖 bug ======================

class _SkewedExecutor(MockExecutor):
    def sft_step(self, episodes, *, trained_slot):
        real = super().sft_step(episodes, trained_slot=trained_slot)
        return StepResult(real.step + 1, real.phase, real.metrics)


def test_an_executor_that_loses_count_of_steps_is_not_silently_accepted(store):
    with pytest.raises(RuntimeError, match="out of sync"):
        _go(store, _batch_cfg(), _SkewedExecutor())
    assert read_training_run(store, RUN).status == "pending"  # 不是确定性失败,不写 failed


def test_a_missing_stage_source_is_a_caller_bug_and_writes_nothing(store):
    only_sft = lambda start: {  # noqa: E731
        "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft"))
    }
    with pytest.raises(ValueError, match="no 'grpo_opd' source"):
        _go(store, _batch_cfg(), MockExecutor(), make_sources=only_sft)
    assert read_training_run(store, RUN) is None


def test_run_training_validates_the_record_it_is_asked_to_resume(store):
    cfg = _batch_cfg()
    common = dict(
        store=store, run_id=RUN, make_sources=_batch_sources(store),
        attribution=build_attribution_backend(cfg.reward), executor=MockExecutor(),
        converter=MockConverter(),
    )
    with pytest.raises(ValueError, match="belongs to 'other'"):
        run_training(cfg, resume_from=TrainingRun("other", "c" * 64), **common)
    with pytest.raises(ValueError, match="only a pending run"):
        run_training(cfg, resume_from=TrainingRun(RUN, "c" * 64, status="executed"), **common)


# ====================== Interact 端到端(真实 loopback 服务端) ======================

def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start():
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {
                s: ScriptedBackend({s: ["b1", "b2", "b3", "b4"]})
                for s in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            factory, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))

        return SimpleNamespace(closed=closed, client_factory=client_factory)

    yield start
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


def _interact_cfg(**kw):
    base = dict(
        mode="interact", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=1, grpo_steps=3, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True, stage_manifest_refs={"sft": "dsm-run-1-sft"},
        scenario_ids=["s1", "s2", "s3"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=4,
    )
    base.update(kw)
    return TrainingConfig(**base)


def _interact_sources(store, env, cfg, calls):
    def make(start):
        calls.append(start)
        # 每个 rollout 训练角色位说两句:偶数 rollout 含 "agreed"(奖励 1.0),奇数不含(0.0)
        policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
        return {
            "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft")),
            "grpo_opd": InteractTrajectorySource(
                env.client_factory, policy, store, cfg, start_step=start
            ),
        }
    return make


def test_an_interact_run_collects_fresh_rollouts_for_every_grpo_step(store, live):
    env, cfg, calls, steps = live(), _interact_cfg(), [], []
    run = _go(store, cfg, MockExecutor(), steps=steps,
              make_sources=_interact_sources(store, env, cfg, calls))
    assert run.status == "executed" and calls == [0]
    assert [s.phase for s in steps] == ["sft", "grpo_opd", "grpo_opd", "grpo_opd"]
    for s in steps[1:]:
        assert (s.metrics["n_groups"], s.metrics["n_degenerate_groups"], s.metrics["mean_reward"]) == (1.0, 0.0, 0.5)
    assert _wait_until(lambda: len(env.closed) == 6)
    assert [e.scenario_id for e in env.closed] == ["s1", "s1", "s2", "s2", "s3", "s3"]
    assert run.curve_summary["grpo_steps_done"] == 3
    assert store.exists(candidate_checkpoint_file(RUN))


def test_an_interact_resume_continues_the_scenario_rotation(store, live):
    env, cfg, calls = live(), _interact_cfg(), []
    with pytest.raises(SimulatedCrashError):  # 全局第 3 步 = 第 3 个 GRPO step,收集之后、训练之时崩
        _go(store, cfg, MockExecutor(crash_at_step=3),
            make_sources=_interact_sources(store, env, cfg, calls))
    assert _wait_until(lambda: len(env.closed) == 6)
    assert read_training_run(store, RUN).checkpoint_step == 2

    run = _go(store, cfg, MockExecutor(), make_sources=_interact_sources(store, env, cfg, calls))
    assert calls == [0, 1]  # 续训从第 2 个 GRPO step(序号 1)接着轮转
    assert _wait_until(lambda: len(env.closed) == 10)
    assert [e.scenario_id for e in env.closed[6:]] == ["s2", "s2", "s3", "s3"]
    assert (run.status, run.resume_attempts, run.curve_summary["grpo_steps_done"]) == ("executed", 1, 3)


# ====================== 接线:每步数据来自 batch_for_step;Interact 的失败尝试与组校验 ======================

class _Recording(MockExecutor):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sft_ids, self.grpo_gids = [], []

    def sft_step(self, episodes, *, trained_slot):
        self.sft_ids.append([e.id for e in episodes])
        return super().sft_step(episodes, trained_slot=trained_slot)

    def grpo_opd_step(self, rewarded, *, opd_coefficient, teacher_model_ref):
        self.grpo_gids.append(sorted({r.group_id for r in rewarded}))
        return super().grpo_opd_step(
            rewarded, opd_coefficient=opd_coefficient, teacher_model_ref=teacher_model_ref
        )


def test_each_sft_step_takes_the_next_episode_of_the_manifest(store):
    ex = _Recording()
    _go(store, _batch_cfg(), ex)
    ids = [r["id"] for r in read_manifest(store, RUN, "sft").episode_refs]
    assert ex.sft_ids == [[ids[0]], [ids[1]]]  # 每步换一条,不是反复训同一条


def test_each_grpo_step_takes_the_next_group_and_wraps_around(store):
    ex = _Recording()
    _go(store, _batch_cfg(), ex)  # 数据集里 2 个组,每步 1 个组,共 4 个 GRPO step
    g = ex.grpo_gids
    assert [len(x) for x in g] == [1, 1, 1, 1]
    assert g[0] != g[1] and g[0] == g[2] and g[1] == g[3]


class _StubSource:
    """替身 Interact 源:每次 collect 返回固定的 episode 与元数据,用来精确控制源的输出。"""

    def __init__(self, episodes, failed_attempts=()):
        self._episodes = list(episodes)
        self._failed = list(failed_attempts)

    def collect(self):
        return TrajectoryBatch(
            list(self._episodes),
            {
                "episode_ids": [e.id for e in self._episodes],
                "failed_attempts": [dict(a) for a in self._failed],
            },
        )


def _stub_sources(store, grpo_source):
    def make(start):
        return {
            "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft")),
            "grpo_opd": grpo_source,
        }
    return make


def _grpo_episodes(store):
    return BatchTrajectorySource(store, read_manifest(store, RUN, "grpo_opd")).collect().episodes


def test_failed_rollout_attempts_reported_by_the_source_reach_the_curve_summary(store):
    failed = [{"reason": "timeout"}, {"reason": "timeout"}]
    source = _StubSource(_grpo_episodes(store), failed)
    run = _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    assert run.curve_summary["failed_rollout_attempts"] == 6  # 3 个 GRPO step,每步 2 次


def test_an_undersized_group_from_the_source_fails_the_run_at_that_step(store):
    source = _StubSource(_grpo_episodes(store)[:3])  # s1 两条,s2 只有一条
    ex = MockExecutor()
    with pytest.raises(DegenerateGroupError) as exc:
        _go(store, _interact_cfg(), ex, make_sources=_stub_sources(store, source))
    assert len(exc.value.group_ids) == 1
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "step 1 (grpo_opd)")
    assert ex.steps_done == 1  # SFT 那一步已跑,出问题的 GRPO 步没有训练


def test_a_corrupt_checkpoint_in_the_record_escalates_instead_of_being_loaded(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3))
    pending = read_training_run(store, RUN)
    assert pending.last_checkpoint_ref is not None
    broken = dataclasses.replace(pending.last_checkpoint_ref, sha256="f" * 64)
    tampered = dataclasses.replace(pending, last_checkpoint_ref=broken)
    write_training_run(store, tampered)

    with pytest.raises(NeedsHumanError, match="integrity"):
        _go(store, _batch_cfg(), MockExecutor())
    assert read_training_run(store, RUN) == tampered  # 升级时不改动任何记录
