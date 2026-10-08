"""MindTrainer 派发器:把调和循环的 pending 指令跑成候选。"""
import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, dataset_manifest_file, run_dir
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import AttributionError
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import DatasetIntegrityError, produce_grouped_dataset, read_manifest
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


def _cfg_dict(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE.to_dict(),
        teacher_model_ref=TEACHER.to_dict(), reward=REWARD.to_dict(),
        sft_steps=1, grpo_steps=1, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return base


def _directive(store, run_id=RUN, payload=None):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-07T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload=payload if payload is not None else {"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {"a": ScriptedBackend({"a": [first, "a2"]}), "b": ScriptedBackend({"b": ["b1", "b2"]})}


def _seed_data(store, *, run_id=RUN, group_size=2):
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", group_size)):
        produce_grouped_dataset(
            store, run_id, name,
            scenarios=[
                (_scenario(sid), RoleAssignment(
                    sid,
                    [RoleBinding("a", "pa", "self_play"),
                     RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
                    max_turns=4))
                for sid in ("s1", "s2")
            ],
            personas=personas, backends_for_rollout=_rollout, group_size=size,
        )
    for sid in ("s1", "s2"):
        write_scenario(store, _scenario(sid))
    for p in personas.values():
        write_persona(store, p)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    _seed_data(s)
    return s


def test_a_batch_directive_trains_and_leaves_a_candidate_for_review(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    cand = store.read_product(candidate_checkpoint_file(RUN))
    assert cand.status == "pending_review"
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"


def test_the_directive_is_marked_executed_only_after_the_candidate_exists(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    seen = []

    def watching(s, d):
        seen.append(store.exists(candidate_checkpoint_file(RUN)))
        make_mindtrainer_dispatcher()(s, d)
        seen.append(store.exists(candidate_checkpoint_file(RUN)))

    reconcile_once(store, dispatcher=watching)
    assert seen == [False, True]  # 派发前没有候选,派发后有;状态迁移发生在派发器返回之后


def test_a_second_tick_does_not_train_again(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher()
    reconcile_once(store, dispatcher=disp)
    before = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    actions = reconcile_once(store, dispatcher=disp)
    assert [a.kind for a in actions] == ["request_diagnostic"]  # 候选待审,不再派发
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml") == before


def test_a_missing_manifest_fails_the_directive_instead_of_retrying(store, tmp_path):
    other = GitStore(str(tmp_path / "no-data"))
    other.init()
    _directive(other, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    actions = reconcile_once(other, dispatcher=make_mindtrainer_dispatcher())
    assert [a.kind for a in actions] == ["dispatch_directive"]  # 动作照常返回,但已被判失败
    env = other.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "manifest" in env.payload["failure"]["reason"]


def test_a_directive_without_a_training_config_fails_rather_than_retrying(store):
    _directive(store)  # 只有 target_dimension/recipe_ref
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "training_config" in env.payload["failure"]["reason"]


def test_an_invalid_config_fails_the_directive(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(sft_steps=-1)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "sft_steps" in env.payload["failure"]["reason"]


def test_interact_without_a_client_factory_fails_the_directive(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(mode="interact", scenario_ids=["s1"],
                                                            bindings=[RoleBinding("a", "pa", "self_play").to_dict()],
                                                            max_turns_per_episode=4)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "interact" in env.payload["failure"]["reason"]


def test_transient_failure_keeps_the_directive_pending_for_the_next_tick(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher(executor_factory=lambda: MockExecutor(crash_at_step=1))
    for _ in range(2):
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=disp)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_dispatch_crash_then_retry_resumes_from_the_checkpoint(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            executor_factory=lambda: MockExecutor(crash_at_step=1)))
    pending = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert pending.status == "pending"

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert (run.status, run.payload["resume_attempts"]) == ("executed", 1)
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_a_later_round_can_train_on_a_dataset_that_lives_under_another_run(store):
    """晋级后的下一轮是新的 run(run-2),数据集仍挂在 run-1 下:数据所在的 run 要从清单 id 解出来。"""
    _directive(store, run_id="run-2", payload={"target_dimension": "tom", "recipe_ref": "",
                                               "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir('run-2')}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-2')}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file("run-2")).status == "pending_review"


@pytest.mark.parametrize("ref", ["nonsense", "dsm--sft", f"dsm-{RUN}-grpo_opd"])
def test_a_manifest_id_that_is_not_for_this_stage_fails_the_directive(store, ref):
    cfg = _cfg_dict(stage_manifest_refs={"sft": ref, "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "stage_manifest_refs['sft']" in env.payload["failure"]["reason"]


def test_a_manifest_naming_a_run_that_has_no_data_fails_the_directive(store):
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-9-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "'dsm-run-9-sft'" in env.payload["failure"]["reason"]


def test_a_manifest_file_that_is_not_the_one_the_config_names_fails_the_directive(store):
    path = dataset_manifest_file(RUN, "sft")
    env = store.read_product(path)
    env.payload["id"] = "dsm-someone-else-sft"
    store.write_product(path, env, "tamper: manifest id")
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    failed = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert failed.status == "failed"
    assert "dsm-someone-else-sft" in failed.payload["failure"]["reason"]


def test_a_judge_attribution_directive_fails_because_this_entry_has_no_judge(store):
    reward = dict(REWARD.to_dict(), attribution="judge", judge_model_id="judge-1",
                  rubric_version="r1", outcome_rules={})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(reward=reward)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "judge" in env.payload["failure"]["reason"]
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")  # 一步都没训,也没留半截记录


def test_a_directive_that_keeps_crashing_is_failed_once_the_resume_limit_is_spent(store):
    cfg = _cfg_dict(max_resume_attempts=1)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    crashing = make_mindtrainer_dispatcher(executor_factory=lambda: MockExecutor(crash_at_step=1))
    for expected_attempts in (0, 1):  # 第一次新运行,第二次续训;都崩在第 1 步
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=crashing)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
        assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").payload["resume_attempts"] == expected_attempts

    reconcile_once(store, dispatcher=crashing)  # 次数已满:升级为人工,指令置 failed,不再重派
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "resume attempts exhausted" in env.payload["failure"]["reason"]
    assert reconcile_once(store, dispatcher=crashing) == []


@pytest.mark.parametrize(
    "exc",
    [
        DatasetIntegrityError("x"), DegenerateGroupError(["g"], 2), AttributionError("x"),
        NotEnoughGroupsError("x"), InvalidTrainingConfigError("x"), NeedsHumanError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_every_deterministic_orchestrator_failure_fails_the_directive(store, monkeypatch, exc):
    def raiser(*args, **kwargs):
        raise exc

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert type(exc).__name__ in env.payload["failure"]["reason"]


@pytest.mark.parametrize("exc", [RuntimeError("x"), OSError("disk")], ids=lambda e: type(e).__name__)
def test_any_other_orchestrator_exception_leaves_the_directive_pending(store, monkeypatch, exc):
    def raiser(*args, **kwargs):
        raise exc

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_tampered_episode_fails_the_directive(store):
    episode_id = read_manifest(store, RUN, "grpo_opd").episode_refs[0]["id"]
    episode = read_episode(store, RUN, episode_id)
    episode.scenario_id = "tampered"
    write_episode(store, RUN, episode)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "DatasetIntegrityError" in env.payload["failure"]["reason"]
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status in ("pending", "failed")


@pytest.fixture
def live(store):
    """真实的 loopback gRPC 服务端(127.0.0.1),和编排器测试用的是同一套。"""
    channels, servers = [], []

    def start():
        closed = []

        def backends(scenario, assignment, personas, client_slot):
            return {
                slot: ScriptedBackend({slot: ["b1", "b2", "b3", "b4"]})
                for slot in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            backends, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
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


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_an_interact_directive_trains_against_a_live_server_and_leaves_a_candidate(store, live):
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    assert _wait_until(lambda: len(env.closed) == 4)  # 2 个 GRPO step x 每步一组 2 条 rollout


def test_an_interact_directive_ignores_a_stray_grpo_opd_manifest_ref(store, live):
    """interact 模式的 grpo_opd 来自实时会话,不读清单。即便指令在 stage_manifest_refs 里
    顺手点了一个 grpo_opd 的清单 id(validate_training_config 并不禁止),且那个 id 指向
    一个没有数据的 run,也不该被当成需要校验的阶段去读,否则会被这个用不到的清单拖垮。"""
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": "dsm-run-9-grpo_opd"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_a_rollout_the_server_rejects_for_good_fails_the_directive_in_the_same_tick(store, live):
    """max_turns_per_episode 超过服务端的 max_turns_cap(200):服务端以 invalid_session 拒绝,重试无益。
    编排器把 training_run 写成 failed 再重抛;派发器不能让这个异常逃出调和循环,要当轮判指令 failed。"""
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=1, scenario_ids=["s1"], max_turns_per_episode=1000,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    disp = make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=ScriptedBackend({"a": ["agreed", "x"] * 8}))
    reconcile_once(store, dispatcher=disp)  # 不抛
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed"
    assert "invalid_session" in env_d.payload["failure"]["reason"]
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "failed"
    assert reconcile_once(store, dispatcher=disp) == []


def test_a_stage_with_no_steps_is_not_read_even_if_the_config_names_a_manifest_for_it(store):
    """sft_steps=0:sft 的清单用不到。点名一份不在树里的,不该拖垮指令。"""
    cfg = _cfg_dict(sft_steps=0, stage_manifest_refs={"sft": "dsm-run-9-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_a_batch_directive_without_grpo_steps_does_not_read_the_grpo_manifest(store):
    cfg = _cfg_dict(grpo_steps=0, stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": "dsm-run-9-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
