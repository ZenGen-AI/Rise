import dataclasses
import time
from contextlib import nullcontext
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.observe import trace
from controlplane.paths import candidate_checkpoint_file, training_run_file
from memory_fixtures import unresolved_memory
from mindtrainer.checkpoint import CandidateAlreadyReviewedError, MockConverter
from mindtrainer.datamodel import StepResult, TrainingRun, TrajectoryBatch
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer import orchestrator as orchestrator_module
from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run, run_training
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import AttributionError, build_attribution_backend
from mindtrainer.store_adapter import read_training_run, write_training_run
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import (
    BatchTrajectorySource, InteractTrajectorySource, RolloutFailedError,
)
from realtime_fixtures import plain_factory, plain_hook
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


# ====================== policy_backend:新记录带上,续训保留,noop 不碰 ======================

BACKEND_A = {
    "trained": {"ref": "file:///ckpt/base", "kind": "base", "endpoint": "gpu-1"},
    "opponents": {"b": {"driver": "vllm", "source": "zengen-v1"}},
    "real_inference_with_mock_training": True,
}
BACKEND_B = {
    "trained": {"ref": "file:///ckpt/other", "kind": "adapter", "endpoint": None},
    "opponents": {},
    "real_inference_with_mock_training": True,
}


def test_a_new_run_records_the_policy_backend_it_was_given(store):
    run = _go(store, _batch_cfg(), MockExecutor(), policy_backend=BACKEND_A)
    assert run.status == "executed" and run.policy_backend == BACKEND_A
    assert read_training_run(store, RUN).policy_backend == BACKEND_A


def test_the_backend_is_in_the_very_first_pending_record_not_only_the_final_one(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=0), policy_backend=BACKEND_A)
    pending = read_training_run(store, RUN)
    assert pending.status == "pending" and pending.policy_backend == BACKEND_A


def test_a_new_run_without_a_backend_has_none_and_no_key_in_the_file(store):
    run = _go(store, _batch_cfg(), MockExecutor())
    assert run.policy_backend is None
    assert "policy_backend" not in store.read_product(training_run_file(RUN)).payload


def test_run_training_applies_the_backend_to_a_new_record(store):
    run = run_training(
        _batch_cfg(), store=store, run_id=RUN, make_sources=_batch_sources(store),
        attribution=build_attribution_backend(REWARD), executor=MockExecutor(),
        converter=MockConverter(), policy_backend=BACKEND_A,
    )
    assert run.policy_backend == BACKEND_A


def test_a_config_that_fails_validation_still_leaves_the_backend_on_the_failed_record(store):
    with pytest.raises(InvalidTrainingConfigError):
        _go(store, _batch_cfg(sft_steps=-1), MockExecutor(), policy_backend=BACKEND_A)
    failed = read_training_run(store, RUN)
    assert failed.status == "failed" and failed.policy_backend == BACKEND_A


@pytest.mark.parametrize("passed", [BACKEND_B, None], ids=["a different one", "none"])
def test_resuming_keeps_the_original_backend_whatever_is_passed(store, passed):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3), policy_backend=BACKEND_A)
    resumed = _go(store, _batch_cfg(), MockExecutor(), policy_backend=passed)
    assert resumed.resume_attempts == 1 and resumed.status == "executed"
    assert resumed.policy_backend == BACKEND_A
    assert read_training_run(store, RUN).policy_backend == BACKEND_A


def test_resuming_a_record_that_had_no_backend_does_not_acquire_one(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3))
    resumed = _go(store, _batch_cfg(), MockExecutor(), policy_backend=BACKEND_A)
    assert resumed.policy_backend is None
    assert "policy_backend" not in store.read_product(training_run_file(RUN)).payload


def test_resuming_from_the_start_when_no_checkpoint_survived_also_keeps_the_original(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=0), policy_backend=BACKEND_A)
    assert read_training_run(store, RUN).last_checkpoint_ref is None   # 崩在第一次存点之前:从头重来
    resumed = _go(store, _batch_cfg(), MockExecutor(), policy_backend=BACKEND_B)
    assert resumed.resume_attempts == 1 and resumed.policy_backend == BACKEND_A


def test_a_noop_returns_the_existing_record_and_does_not_rewrite_it(store, tmp_path):
    first = _go(store, _batch_cfg(), MockExecutor(), policy_backend=BACKEND_A)
    before = store.read_product(training_run_file(RUN))
    again = _go(store, _batch_cfg(), MockExecutor(), policy_backend=BACKEND_B)
    assert again == first and again.policy_backend == BACKEND_A
    assert store.read_product(training_run_file(RUN)) == before


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
            plain_factory(factory), port=0, turn_timeout_s=2.0, on_session_closed=plain_hook(closed.append),
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory(*, deadline_s=None):
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
        max_turns_per_episode=4, memory=unresolved_memory(),
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
                env.client_factory, lambda eid: nullcontext(policy), store, cfg, run_id=RUN, start_step=start
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


# ====================== 终审修复 C1:invalid_session 是确定性拒绝,写 failed 而不是续训重试 ======================

class _RejectedSource:
    """替身 Interact 源:每次 collect 都以给定 reason 抛 RolloutFailedError,并记下被调用的次数。"""

    def __init__(self, reason):
        self.reason, self.collects = reason, 0

    def collect(self):
        self.collects += 1
        raise RolloutFailedError(
            scenario_id="s1", rollout=0, reason=self.reason, detail="detail-x",
            attempts=1, failed_attempts=[{"reason": self.reason}],
        )


@pytest.mark.parametrize("reason", ["invalid_session", "role_backend_rejected", "role_memory_failure"])
def test_a_deterministic_rejection_fails_the_run_and_is_never_retried(store, reason):
    """invalid_session(请求本身有问题)、role_backend_rejected(对手 API 明确拒绝)与 role_memory_failure
    (对手位记忆确定性失败)同属确定性:写 failed,不续训。"""
    source = _RejectedSource(reason)
    with pytest.raises(RolloutFailedError):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["category"], failed.failure["point"]) == (
        "failed", "T2", "step 1 (grpo_opd)",
    )
    assert reason in failed.failure["detail"]
    with pytest.raises(NeedsHumanError, match="failed"):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    assert source.collects == 1  # 第二次进入被 escalate 挡住,没有再去开会话


class _MemoryBrokenSource:
    """替身 Interact 源:collect 时训练位策略抛确定性记忆失败(轨迹源不接它,原样穿出)。"""

    def __init__(self):
        self.collects = 0

    def collect(self):
        from simulator.realtime.backend_timeout import RoleMemoryFailureError
        self.collects += 1
        raise RoleMemoryFailureError("memory backend failed for slot 'a': MemoryClosedError")


def test_a_deterministic_memory_failure_of_the_trained_slot_fails_the_run_instead_of_leaving_it_pending(store):
    from simulator.realtime.backend_timeout import RoleMemoryFailureError
    source = _MemoryBrokenSource()
    with pytest.raises(RoleMemoryFailureError):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["category"], failed.failure["point"]) == (
        "failed", "T2", "step 1 (grpo_opd)",
    )
    assert failed.failure["detail"].startswith("RoleMemoryFailureError: ")
    with pytest.raises(NeedsHumanError, match="failed"):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    assert source.collects == 1


@pytest.mark.parametrize("reason", ["timeout", "role_backend_failure"])
def test_other_rollout_failures_stay_pending_for_the_attempt_limit_to_bound(store, reason):
    source = _RejectedSource(reason)
    with pytest.raises(RolloutFailedError):
        _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.failure) == ("pending", None)


# ====================== 终审修复 I1:收尾窗口内候选被批准,续训直接收尾 ======================

def _crash_between_registration_and_executed(store, cfg):
    """让训练做完、候选注册完,但在写 executed 记录时崩溃。"""
    real = orchestrator_module.write_training_run

    def boom(st, run):
        if run.status == "executed":
            raise SimulatedCrashError("crash before the executed record")
        return real(st, run)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator_module, "write_training_run", boom)
        with pytest.raises(SimulatedCrashError):
            _go(store, cfg, MockExecutor())
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.checkpoint_step) == ("pending", 6)
    assert _candidate(store).status == "pending_review"


def _review(store, status, **payload_edits):
    path = candidate_checkpoint_file(RUN)
    cand = store.read_product(path)
    cand.status = status
    cand.payload.update(payload_edits)
    store.write_product(path, cand, "human: review")


def test_a_candidate_approved_in_the_crash_window_does_not_stop_the_run_from_finishing(store):
    cfg = _batch_cfg()
    _crash_between_registration_and_executed(store, cfg)
    _review(store, "approved")
    run = _go(store, cfg, MockExecutor())
    assert (run.status, run.resume_attempts) == ("executed", 1)
    assert read_training_run(store, RUN).status == "executed"
    assert _candidate(store).status == "approved"  # 人工结论原样保留,没有被重置


@pytest.mark.parametrize(
    "edit",
    [
        {"config_hash": "f" * 64},
        {"training_run_ref": "trainrun-other"},
        {"checkpoint_ref": {"uri": "mock://x.hf", "sha256": "f" * 64, "size_bytes": 0, "fetch_hint": None}},
    ],
    ids=["other_config", "other_run", "other_weights"],
)
def test_a_reviewed_candidate_that_is_not_this_runs_is_left_for_a_human(store, edit):
    cfg = _batch_cfg()
    _crash_between_registration_and_executed(store, cfg)
    _review(store, "approved", **edit)  # 候选被改成不属于本 run、本配置、同一份权重
    # 交给人是 NeedsHumanError(派发器把它判成 T3 的 failed);原样漏出 CandidateAlreadyReviewedError
    # 会让守护进程退出,重启后每次都重做最后一段,直到 resume_attempts 耗尽才停。
    with pytest.raises(NeedsHumanError, match="not this run") as caught:
        _go(store, cfg, MockExecutor())
    assert isinstance(caught.value.__cause__, CandidateAlreadyReviewedError)
    assert read_training_run(store, RUN).status == "pending"


class TestStagedExecutor:
    """阶段执行器协议:run_sft_stage 成批返回、stage_end 切段、prior_batches 是前缀。

    覆盖:
    - 无 run_sft_stage 时走单步路径,on_step 逐步到达(与改动前一致)
    - 有 run_sft_stage 时成批返回,段内每步都被记账
    - stage_end 返回更短的段,编排器按子段衔接,prior_batches 随段增长
    - stage_end 不前进(first_step <= end)抛 RuntimeError
    - 结果的 step 字段与预期不符抛 RuntimeError
    """

    def test_single_step_path_unchanged(self, store):
        """MockExecutor 无 run_sft_stage:单步路径逐步回调,run 正常收尾。"""
        steps = []
        cfg = _batch_cfg(sft_steps=3, grpo_steps=0, checkpoint_every_steps=100)
        run = _go(store, cfg, MockExecutor(), steps=steps)
        assert [r.step for r in steps] == [0, 1, 2]
        assert run.status == "executed"

    def test_staged_path_records_every_step(self, store):
        """有 run_sft_stage:一次成批返回,段内每步都进 on_step 与摘要。"""

        class StagedExecutor(MockExecutor):
            def __init__(self):
                super().__init__()
                self.calls = []

            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                self.calls.append((first_step, len(batches), len(prior_batches)))
                return [
                    self.sft_step(batch, trained_slot=trained_slot) for batch in batches
                ]

        steps = []
        cfg = _batch_cfg(sft_steps=5, grpo_steps=0, checkpoint_every_steps=100)
        executor = StagedExecutor()
        run = _go(store, cfg, executor, steps=steps)
        assert executor.calls == [(0, 5, 0)], executor.calls
        assert [r.step for r in steps] == [0, 1, 2, 3, 4]
        assert run.curve_summary["sft_steps_done"] == 5

    def test_stage_end_cuts_finer_and_prefix_grows(self, store):
        """stage_end 每段只吃 2 步:段依次衔接,prior_batches 随已训步数增长。"""

        class ChunkingExecutor(MockExecutor):
            def __init__(self):
                super().__init__()
                self.calls = []

            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                self.calls.append((first_step, len(batches), len(prior_batches)))
                return [
                    self.sft_step(batch, trained_slot=trained_slot) for batch in batches
                ]

            def stage_end(self, batches, *, first_step, limit):
                return min(first_step + 2, limit)

        cfg = _batch_cfg(sft_steps=5, grpo_steps=0, checkpoint_every_steps=100)
        executor = ChunkingExecutor()
        _go(store, cfg, executor)
        assert executor.calls == [(0, 2, 0), (2, 2, 2), (4, 1, 4)], executor.calls

    def test_stage_end_without_forward_progress_fails_loudly(self, store):
        class StuckExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return []

            def stage_end(self, batches, *, first_step, limit):
                return first_step

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"violating the guarantee of forward progress"):
            _go(store, cfg, StuckExecutor())

    def test_stage_end_beyond_limit_is_rejected(self, store):
        class OverrunExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return []

            def stage_end(self, batches, *, first_step, limit):
                return limit + 1

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"violating the guarantee of forward progress"):
            _go(store, cfg, OverrunExecutor())

    def test_results_step_mismatch_is_rejected(self, store):
        """结果的 step 字段错开时,记账处抛 RuntimeError(step 校验没有被废掉)。"""

        class MismatchExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return [
                    StepResult(first_step + i + 100, "sft", {"sft_loss": 0.1})
                    for i in range(len(batches))
                ]

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"executor returned step \d+ at index \d+, expected \d+"):
            _go(store, cfg, MismatchExecutor())


class TestRollExecutorIntegration:
    """RollExecutor 与编排器合在一起:预检时机、阶段路径、跨模式与 supported_phases。

    用 FakeClient,不碰真实 SSH,所以不标 slow。
    """

    def _executor(self, client):
        from compute_fixtures import fake_endpoint
        from mindtrainer.roll_executor import RollExecutor

        return RollExecutor(client=client, endpoint=fake_endpoint(), run_id=RUN)

    def test_preflight_runs_before_the_run_record_is_written(self, store):
        """预检在 run_training 之前:被占时 run 记录一个字节都不写(设计 §7.3)。"""
        from compute.errors import ComputeBusyError
        from compute_fixtures import FakeClient
        from controlplane.paths import training_run_file

        cfg = _batch_cfg(sft_steps=2, grpo_steps=0)
        with pytest.raises(ComputeBusyError):
            _go(store, cfg, self._executor(FakeClient(busy=True)))
        assert not store.exists(training_run_file(RUN))

    def test_grpo_steps_are_refused_as_a_config_error(self, store):
        """supported_phases 只有 sft,配置却要 GRPO:确定性失败,run 记 failed(设计 §5.1)。"""
        from compute_fixtures import FakeClient

        cfg = _batch_cfg(sft_steps=1, grpo_steps=2)
        with pytest.raises(InvalidTrainingConfigError, match=r"supports only \['sft'\]"):
            _go(store, cfg, self._executor(FakeClient()))
        run = read_training_run(store, RUN)
        assert run.status == "failed" and run.failure["category"] == "T2"

    def test_resuming_a_mock_run_with_the_real_executor_needs_a_human(self, store):
        """跨模式续训要在 checkpoint_intact 之前挡住:T3、理由是后端不一致、记录不改(设计 §6.1)。"""
        from compute_fixtures import FakeClient

        cfg = _batch_cfg(sft_steps=4, grpo_steps=0, checkpoint_every_steps=2)
        with pytest.raises(SimulatedCrashError):
            _go(store, cfg, MockExecutor(crash_at_step=3), converter=MockConverter())  # 第 2 步存点后崩在第 3 步
        pending = read_training_run(store, RUN)
        assert pending.status == "pending" and pending.last_checkpoint_ref is not None

        with pytest.raises(NeedsHumanError, match=r"resuming across backend modes"):
            _go(store, cfg, self._executor(FakeClient()))
        assert read_training_run(store, RUN) == pending  # 记录一个字节都没动


class TestRollConverterIntegration:
    """编排器调真实转换器:候选产物里落的是适配器导出引用。"""

    def test_the_candidate_carries_the_adapter_export_reference(self, store):
        """run_training 收尾时调 converter.convert(final_ref),产出进 candidate_checkpoint.yaml。"""
        from compute_fixtures import FakeClient, fake_endpoint, training_artifacts
        from mindtrainer.roll_converter import EXPORT_FORMAT, RollLoraConverter
        from mindtrainer.roll_executor import RollExecutor

        client = FakeClient()
        endpoint = fake_endpoint()

        def hook(job_id, spec, lease=True):
            if "ROLL_EXPORT_OUT" in spec.env:  # 转换作业
                out = spec.env["ROLL_EXPORT_OUT"]
                client.files[f"{out}/adapter_model.safetensors"] = b"w"
            else:  # 训练作业:留下真实布局的产物(completion.json 与存点里的 log_history)
                out = spec.argv[spec.argv.index("--output-dir") + 1]
                client.files.update(training_artifacts(out, 2))

        client.job_hook = hook
        client.seed_dir(endpoint.base_model_path)
        executor = RollExecutor(client=client, endpoint=endpoint, run_id=RUN)
        converter = RollLoraConverter(client=client, endpoint=endpoint)

        cfg = _batch_cfg(sft_steps=2, grpo_steps=0, checkpoint_every_steps=100,
                         base_checkpoint_ref=BigRef(
                             uri=f"ssh://{endpoint.endpoint_id}{endpoint.base_model_path}",
                             sha256=client.hash_tree(endpoint.base_model_path, "sampled"),
                             size_bytes=0, fetch_hint={"type": "roll_ssh"}))
        run = _go(store, cfg, executor, converter=converter)
        assert run.status == "executed"

        candidate = _candidate(store).payload
        ref = BigRef.from_dict(candidate["checkpoint_ref"])
        assert "/exports/" in ref.uri
        assert ref.fetch_hint["format"] == EXPORT_FORMAT
        assert ref.fetch_hint["type"] == "roll_ssh"
