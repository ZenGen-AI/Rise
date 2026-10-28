"""MindTrainer 派发器:把调和循环的 pending 指令跑成候选。"""
import subprocess
import time
from contextlib import nullcontext
from types import SimpleNamespace

import grpc
import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, dataset_manifest_file, run_dir
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.resume import NeedsHumanError
from mindtrainer.roll_converter import EXPORT_FORMAT
from mindtrainer.reward_pipeline import AttributionError
from memory_fixtures import STAND_IN_RUNTIME, fake_memory_resolver, resolved_memory, unresolved_memory
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig, TrainingConfig
from realtime_fixtures import plain_factory, plain_hook
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
    disp = make_mindtrainer_dispatcher(executor_factory=lambda run_id: MockExecutor(crash_at_step=1))
    for _ in range(2):
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=disp)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_dispatch_crash_then_retry_resumes_from_the_checkpoint(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            executor_factory=lambda run_id: MockExecutor(crash_at_step=1)))
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


def _corrupt_missing_episode_refs(store, path):
    env = store.read_product(path)
    del env.payload["episode_refs"]
    store.write_product(path, env, "corrupt: drop episode_refs")


def _corrupt_merge_conflict_markers(store, path):
    abs_path = store.repo_root / path
    text = abs_path.read_text(encoding="utf-8")
    abs_path.write_text(
        f"<<<<<<< HEAD\n{text}=======\n{text}>>>>>>> other\n", encoding="utf-8"
    )


def _corrupt_unknown_schema_version(store, path):
    env = store.read_product(path)
    env.schema_version = 99
    store.write_product(path, env, "corrupt: bump schema_version")


@pytest.mark.parametrize(
    "corrupt",
    [_corrupt_missing_episode_refs, _corrupt_merge_conflict_markers, _corrupt_unknown_schema_version],
    ids=["missing_episode_refs", "merge_conflict_markers", "unknown_schema_version"],
)
def test_a_manifest_that_exists_but_cannot_be_read_fails_the_directive(store, corrupt):
    """清单文件在场但读不出来(缺键、YAML 语法错、schema_version 没有迁移器):这同样是
    确定性的数据问题,派发器必须判 failed,而不是让异常逃出 dispatch() 每轮卡在同一处重派。"""
    path = dataset_manifest_file(RUN, "sft")
    corrupt(store, path)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    reason = env.payload["failure"]["reason"]
    assert f"dsm-{RUN}-sft" in reason
    assert any(
        name in reason
        for name in ("KeyError", "ScannerError", "ParserError", "UnknownSchemaVersionError")
    )
    assert reconcile_once(store, dispatcher=make_mindtrainer_dispatcher()) == []


def test_a_manifest_with_a_broken_envelope_fails_the_directive(store):
    """信封校验失败同样是确定性的读不出(gitstore.UNREADABLE_PRODUCT_ERRORS):原先这一处的元组漏了它,
    异常逃出派发器,指令保持 pending、每轮重派。"""
    path = store.repo_root / dataset_manifest_file(RUN, "sft")
    path.write_text(                                       # 字段都在,但载荷不是对象:过不了信封 Schema
        "id: dsm-run-1-sft\ntype: dataset_manifest\nversion: 1\ncreated_at: '2026-10-09T00:00:00Z'\n"
        "created_by: {owner: default, visibility: shared}\nstatus: executed\nschema_version: 1\n"
        "links: []\npayload: [1, 2]\n", encoding="utf-8")
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "EnvelopeValidationError" in env.payload["failure"]["reason"]


def test_a_transient_error_while_reading_a_manifest_leaves_the_directive_pending(store, monkeypatch):
    """读清单那段只接确定性的读取失败。OSError 是瞬态的,必须原样上抛、指令保持 pending,
    下一轮才有机会重派;若被当成确定性失败接住,一次磁盘抖动就会把指令永久判 failed。"""
    def flaky(*args, **kwargs):
        raise OSError("disk hiccup")

    monkeypatch.setattr("mindtrainer.dispatch.read_manifest", flaky)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(OSError, match="disk hiccup"):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_poisoned_directive_does_not_block_the_one_after_it(store, tmp_path):
    """reconcile 按 run id 排序派发;一条读不出清单的指令若排在前面,不能把排在它后面的
    好指令一起卡住(_iter_run_ids 按 id 排序,'a-bad' 必排在 'z-good' 之前)。"""
    bad_path = dataset_manifest_file(RUN, "sft")
    _corrupt_missing_episode_refs(store, bad_path)
    _directive(store, run_id="a-bad",
               payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})

    _seed_data(store, run_id="run-2")
    good_cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-2-sft", "grpo_opd": "dsm-run-2-grpo_opd"})
    _directive(store, run_id="z-good",
               payload={"target_dimension": "tom", "recipe_ref": "", "training_config": good_cfg})

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())  # 不抛

    assert store.read_product(f"{run_dir('a-bad')}/directive.yaml").status == "failed"
    assert store.read_product(f"{run_dir('z-good')}/directive.yaml").status == "executed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sft_steps": "3"},
        {"grpo_steps": None},
        {"stage_manifest_refs": {"sft": 5, "grpo_opd": f"dsm-{RUN}-grpo_opd"}},
    ],
    ids=["sft_steps_str", "grpo_steps_none", "stage_manifest_refs_int_value"],
)
def test_a_wrong_typed_config_field_fails_the_directive_before_touching_the_tree(store, overrides):
    """validate_training_config 的类型检查是承重的:没有它,错类型的字段会一路滑到
    _required_stages / _data_run_id 才炸,抛出点名字段以外的裸 TypeError/AttributeError。
    这里只证明派发器确实先用上了那条校验。"""
    cfg = _cfg_dict(**overrides)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())  # 不抛
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    field_name = next(iter(overrides))
    assert field_name in env.payload["failure"]["reason"]


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
    crashing = make_mindtrainer_dispatcher(executor_factory=lambda run_id: MockExecutor(crash_at_step=1))
    for expected_attempts in (0, 1):  # 第一次新运行,第二次续训;都崩在第 1 步
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=crashing)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
        assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").payload["resume_attempts"] == expected_attempts

    reconcile_once(store, dispatcher=crashing)  # 次数已满:升级为人工,指令置 failed,不再重派
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "resume attempts exhausted" in env.payload["failure"]["reason"]
    assert env.payload["failure"]["category"] == "T3"  # NeedsHumanError 是该人工介入,不是 T2
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
    expected = "T3" if isinstance(exc, NeedsHumanError) else "T2"  # 只有该人工介入的才记 T3
    assert env.payload["failure"]["category"] == expected


@pytest.mark.parametrize("exc", [RuntimeError("x"), OSError("disk")], ids=lambda e: type(e).__name__)
def test_any_other_orchestrator_exception_leaves_the_directive_pending(store, monkeypatch, exc):
    def raiser(*args, **kwargs):
        raise exc

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_an_unreadable_record_in_the_fallback_branch_fails_the_directive(store, monkeypatch):
    """兜底分支要重读训练记录判它是不是终态。记录在而读不出(手改进树的坏引用,I8)时
    read_training_run 抛 NeedsHumanError;它在 except 分支里抛出,兄弟分支接不住,会一路逃出
    调和循环。所以那一处自己转成 DirectiveFailedError,类别 T3,原异常留在原因里。"""
    def raiser(*args, **kwargs):
        raise RuntimeError("rollout went sideways")

    def unreadable(store_, run_id):
        raise NeedsHumanError(f"training_run {run_id} holds an unusable reference")

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    monkeypatch.setattr("mindtrainer.dispatch.read_training_run", unreadable)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())   # 不抛
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T3"
    assert "rollout went sideways" in env.payload["failure"]["reason"]
    assert "unusable reference" in env.payload["failure"]["reason"]


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
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert run.status == "failed"
    assert run.payload["failure"]["point"] == "grpo precheck"


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
            plain_factory(backends), port=0, turn_timeout_s=2.0, on_session_closed=plain_hook(closed.append),
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


def test_an_interact_directive_names_its_episodes_with_the_run_id(store, live):
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})))
    assert _wait_until(lambda: len(env.closed) == 4)
    assert sorted(e.id for e in env.closed) == [
        f"{RUN}-grpo0-s1-r0", f"{RUN}-grpo0-s1-r1", f"{RUN}-grpo1-s2-r0", f"{RUN}-grpo1-s2-r1"]


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


def test_a_deferred_error_from_the_executor_leaves_the_directive_pending_and_is_absorbed(store):
    """延后异常(资源暂时不可用)不是指令的错:守护进程接住,指令保持 pending,下一次再派发。"""
    from controlplane.daemon import DeferralBook
    from controlplane.deferred import DeferredError

    class BusyExecutor(MockExecutor):
        def load(self, checkpoint_ref):
            raise DeferredError("gpu busy")

    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher(executor_factory=lambda run_id: BusyExecutor())
    reconcile_once(store, dispatcher=disp, deferrals=DeferralBook())  # 不抛
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
    assert not store.exists(candidate_checkpoint_file(RUN))


def test_a_deferred_error_is_never_rewrapped_as_a_permanent_failure(store, monkeypatch):
    """兜底分支见到"记录已 failed"会把异常包成 DirectiveFailedError(终态)。DeferredError 不在此列:
    它说的是"稍后再试",不论记录当时是什么状态,都要原样上抛。"""
    from controlplane.deferred import DeferredError
    from mindtrainer import dispatch as dispatch_module
    from mindtrainer.store_adapter import read_training_run, write_training_run

    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})

    def fake_resume_or_run(cfg, *, store, run_id, **kwargs):
        from mindtrainer.datamodel import TrainingRun
        write_training_run(store, TrainingRun(run_id=run_id, config_hash="h", status="failed",
                                              failure={"category": "T2", "point": "x", "detail": "y"}))
        raise DeferredError("busy")

    monkeypatch.setattr(dispatch_module, "resume_or_run", fake_resume_or_run)
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    with pytest.raises(DeferredError):
        make_mindtrainer_dispatcher()(store, directive)
    assert read_training_run(store, RUN).status == "failed"  # 记录没人动,指令也没被判死


def test_the_executor_factory_is_called_with_the_directive_id(store):
    """真实执行器把 run_id 编进作业身份与远端输出目录,所以工厂必须拿到它;装配期不知道
    run_id,它是每条指令来了才有的。"""
    seen = []

    def factory(run_id):
        seen.append(run_id)
        return MockExecutor()

    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(executor_factory=factory))
    assert seen == [RUN]
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"


# ---------------- policy_factory:按 run 现造的策略,返回上下文管理器 ----------------

def _interact_directive(store, **overrides):
    fields = dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
        memory=unresolved_memory(),
    )
    fields.update(overrides)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict(**fields)})


class _Scope:
    """policy_factory 给出的上下文:记下进出,进入时交出策略;enter_raises 非空时进入就抛。"""

    def __init__(self, trace, policy, enter_raises=None):
        self.trace, self.policy, self.enter_raises = trace, policy, enter_raises

    def __enter__(self):
        self.trace.append("enter")
        if self.enter_raises is not None:
            raise self.enter_raises
        return lambda episode_id: nullcontext(self.policy)

    def __exit__(self, exc_type, exc, tb):
        self.trace.append(("exit", exc_type))
        return False


class _TracingPolicy(ScriptedBackend):
    """每说一句就往 trace 里记一笔,用来确认训练发生在上下文之内。"""

    def __init__(self, trace):
        super().__init__({"a": ["agreed", "x", "no", "x"] * 8})
        self._trace = trace

    def respond(self, persona, scenario, history, speaker_slot):
        self._trace.append("respond")
        return super().respond(persona, scenario, history, speaker_slot)


def test_the_policy_factory_context_is_entered_once_with_the_run_id_and_the_validated_config(store, live):
    env = live()
    _interact_directive(store)
    trace, calls = [], []

    def policy_factory(run_id, cfg):
        calls.append((run_id, cfg))
        return _Scope(trace, _TracingPolicy(trace))

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy_factory=policy_factory, memory_resolver=fake_memory_resolver))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    (run_id, cfg), = calls                                  # 每次派发只造一次
    assert run_id == RUN
    assert isinstance(cfg, TrainingConfig) and cfg.mode == "interact"
    assert cfg.scenario_ids == ["s1", "s2"] and cfg.trained_slot == "a"
    assert [b.slot_id for b in cfg.bindings] == ["a", "b"]
    assert trace[0] == "enter" and trace[-1] == ("exit", None)
    assert trace.count("enter") == 1 and trace.count(("exit", None)) == 1
    assert "respond" in trace[1:-1]                         # 训练全程在上下文之内


def test_the_policy_factory_context_is_exited_when_training_raises(store, live):
    env = live()
    _interact_directive(store)
    trace = []
    disp = make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, _TracingPolicy(trace)),
        executor_factory=lambda run_id: MockExecutor(crash_at_step=1), memory_resolver=fake_memory_resolver)
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=disp)
    assert trace[0] == "enter" and trace[-1] == ("exit", SimulatedCrashError)
    assert trace.count("enter") == trace.count(("exit", SimulatedCrashError)) == 1
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"   # 瞬态:留待下一轮


def test_a_deferred_error_on_entering_the_policy_context_defers_without_writing_a_training_run(store, live):
    """服务起不来(租约被占、启动超时)是延后通道的事:指令保持 pending,训练记录一个字节都没写。"""
    from controlplane.daemon import DeferralBook
    from controlplane.deferred import DeferredError

    class LeaseBusy(DeferredError):
        pass

    env = live()
    _interact_directive(store)
    trace = []
    disp = make_mindtrainer_dispatcher(
        client_factory=env.client_factory,
        policy_factory=lambda run_id, cfg: _Scope(trace, None, enter_raises=LeaseBusy("gpu lease is held")),
        memory_resolver=fake_memory_resolver)
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    with pytest.raises(LeaseBusy):
        disp(store, directive)                               # 原样上抛,没有被包成 DirectiveFailedError
    reconcile_once(store, dispatcher=disp, deferrals=DeferralBook())   # 守护进程接住,不抛
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")
    assert not store.exists(candidate_checkpoint_file(RUN))
    assert trace.count("enter") == 2                         # 两次派发各进一次;进入失败所以没有退出


def test_a_deterministic_error_on_entering_the_policy_context_fails_the_directive(store, live):
    env = live()
    _interact_directive(store)
    disp = make_mindtrainer_dispatcher(
        client_factory=env.client_factory,
        policy_factory=lambda run_id, cfg: _Scope([], None, enter_raises=DatasetIntegrityError("bad data")),
        memory_resolver=fake_memory_resolver)
    reconcile_once(store, dispatcher=disp)
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed" and env_d.payload["failure"]["category"] == "T2"
    assert "bad data" in env_d.payload["failure"]["reason"]
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")


def test_a_missing_manifest_fails_the_directive_before_the_policy_context_is_entered(store, live):
    """进上下文要起 vLLM、占 GPU 租约;清单不在场这种确定性的错不该先付这笔代价。"""
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": "dsm-run-9-sft"},        # run-9 没有数据
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy_factory=lambda run_id, c: _Scope(trace, _TracingPolicy(trace))))
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed" and "manifest" in env_d.payload["failure"]["reason"]
    assert trace == []                                       # 上下文一次也没进
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")


def test_a_plain_policy_still_works_and_needs_no_factory(store, live):
    env = live()
    _interact_directive(store)
    policy = _TracingPolicy(trace := [])
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy, memory_resolver=fake_memory_resolver))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert "respond" in trace


def test_a_policy_and_a_policy_factory_together_are_refused_at_construction():
    with pytest.raises(ValueError, match="policy_factory") as info:
        make_mindtrainer_dispatcher(policy=ScriptedBackend({}), policy_factory=lambda run_id, cfg: None)
    assert "policy" in str(info.value) and "both" in str(info.value)


def test_interact_with_neither_a_policy_nor_a_factory_fails_with_the_new_message(store, live):
    env = live()
    _interact_directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(client_factory=env.client_factory))
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed" and env_d.payload["failure"]["category"] == "T2"
    reason = env_d.payload["failure"]["reason"]
    assert "interact mode needs a client_factory and a policy or policy_factory" in reason
    assert "--interact" in reason          # 原因点名开关,操作者不用去翻代码


def test_a_policy_factory_without_a_client_factory_still_fails_the_directive(store):
    _interact_directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        policy_factory=lambda run_id, cfg: _Scope([], ScriptedBackend({}))))
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed"
    assert "client_factory" in env_d.payload["failure"]["reason"]


def test_a_batch_directive_never_calls_the_policy_factory(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    calls = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        policy_factory=lambda run_id, cfg: calls.append(run_id)))
    assert calls == []
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"


# ---------------- policy_backend:interact 路径把后端写进 training_run,batch 路径什么都不传 ----------------

def _commit_count(store):
    out = subprocess.run(["git", "-C", str(store.repo_root), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


def _record(store):
    return store.read_product(f"{run_dir(RUN)}/training_run.yaml")


def _run_interact(store, env, **dispatcher_kw):
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
    dispatcher_kw.setdefault("memory_resolver", fake_memory_resolver)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy, **dispatcher_kw))


def test_an_interact_run_records_which_backend_produced_its_data(store, live):
    _interact_directive(store)
    _run_interact(store, live(), policy_endpoint="gpu-1")
    assert _record(store).status == "executed"
    assert _record(store).payload["policy_backend"] == {
        "trained": {"ref": "file:///ckpt/base", "kind": "base", "endpoint": "gpu-1"},
        "opponents": {"b": {"driver": "vllm", "source": "zengen-v1"}},
        "real_inference_with_mock_training": True,
        "memory": {"seed": None, "digests": {"pa": "digest-pa", "pb": "digest-pb"}, "runtime": STAND_IN_RUNTIME},
    }


def test_the_endpoint_is_none_when_the_dispatcher_was_given_none(store, live):
    _interact_directive(store)
    _run_interact(store, live())
    assert _record(store).payload["policy_backend"]["trained"]["endpoint"] is None


@pytest.mark.parametrize(
    "hint, kind",
    [
        (None, "base"),
        ({"type": "mock", "tag": "t"}, "base"),
        ({"type": "roll_ssh", "format": EXPORT_FORMAT}, "adapter"),
        # roll_executor 写的训练存点:type 同为 roll_ssh,但它不是适配器导出,训练位并没有挂 LoRA 在说话
        ({"type": "roll_ssh", "format": "roll_lora_legacy"}, "base"),
        ({"type": "roll_ssh"}, "base"),
        ({"format": EXPORT_FORMAT}, "base"),
        ({"type": "mock", "format": EXPORT_FORMAT}, "base"),
    ],
    ids=["no hint", "mock hint", "roll_ssh export", "roll_ssh training checkpoint",
         "roll_ssh without format", "export format without type", "export format on another type"],
)
def test_the_trained_kind_follows_the_base_checkpoint_fetch_hint(store, live, hint, kind):
    ref = BigRef("file:///ckpt/base", "a" * 64, 1, hint)
    _interact_directive(store, base_checkpoint_ref=ref.to_dict())
    _run_interact(store, live())
    trained = _record(store).payload["policy_backend"]["trained"]
    assert (trained["ref"], trained["kind"]) == ("file:///ckpt/base", kind)


@pytest.mark.parametrize(
    "backend, driver, expected",
    [
        ("frozen_reference", None, "vllm"),
        ("external_api", None, "external_api"),
        ("frozen_reference", "scripted", "scripted"),
        ("frozen_reference", "external_api", "external_api"),
        ("external_api", "vllm", "vllm"),
    ],
)
def test_the_opponent_driver_is_the_explicit_one_else_the_backend_default(store, live, backend, driver, expected):
    binding = RoleBinding("b", "pb", backend, model_ref="zengen-v7", driver=driver)
    _interact_directive(store, bindings=[RoleBinding("a", "pa", "self_play").to_dict(), binding.to_dict()])
    _run_interact(store, live())
    assert _record(store).payload["policy_backend"]["opponents"] == {
        "b": {"driver": expected, "source": "zengen-v7"},
    }


def test_the_backend_is_json_safe_plain_data(store, live):
    import json
    _interact_directive(store)
    _run_interact(store, live(), policy_endpoint="gpu-1")
    backend = _record(store).payload["policy_backend"]
    assert json.loads(json.dumps(backend)) == backend


def test_the_policy_factory_route_records_the_backend_too(store, live):
    env = live()
    _interact_directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy_endpoint="gpu-2", memory_resolver=fake_memory_resolver,
        policy_factory=lambda run_id, cfg: _Scope([], ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8}))))
    assert _record(store).payload["policy_backend"]["trained"]["endpoint"] == "gpu-2"


def test_a_resume_after_a_crash_keeps_the_backend_the_first_attempt_wrote(store, live):
    env = live()
    _interact_directive(store)
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 16})
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            client_factory=env.client_factory, policy=policy, policy_endpoint="gpu-1",
            executor_factory=lambda run_id: MockExecutor(crash_at_step=1), memory_resolver=fake_memory_resolver))
    assert _record(store).status == "pending"
    assert _record(store).payload["policy_backend"]["trained"]["endpoint"] == "gpu-1"

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy, policy_endpoint="gpu-2",
        memory_resolver=fake_memory_resolver))
    run = _record(store)
    assert (run.status, run.payload["resume_attempts"]) == ("executed", 1)
    assert run.payload["policy_backend"]["trained"]["endpoint"] == "gpu-1"   # 第二次换了端点,记录不改


def test_a_noop_redispatch_does_not_rewrite_the_recorded_backend(store, live):
    env = live()
    _interact_directive(store)
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 16})
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy, policy_endpoint="gpu-1",
        memory_resolver=fake_memory_resolver)(store, directive)
    before, commits = _record(store), _commit_count(store)
    make_mindtrainer_dispatcher(                             # 记录已 executed:resume_or_run 走 noop
        client_factory=env.client_factory, policy=policy, policy_endpoint="gpu-2",
        memory_resolver=fake_memory_resolver)(store, directive)
    assert _record(store) == before
    assert _record(store).payload["policy_backend"]["trained"]["endpoint"] == "gpu-1"
    assert _commit_count(store) == commits


def test_a_batch_run_passes_no_backend_to_the_orchestrator_and_records_none(store, monkeypatch):
    from mindtrainer import dispatch as dispatch_module

    seen = []
    real = dispatch_module.resume_or_run

    def spy(cfg, **kwargs):
        seen.append(kwargs)
        return real(cfg, **kwargs)

    monkeypatch.setattr(dispatch_module, "resume_or_run", spy)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(policy_endpoint="gpu-1"))
    (kwargs,) = seen
    assert "policy_backend" not in kwargs
    assert _record(store).status == "executed"
    assert "policy_backend" not in _record(store).payload


def test_an_interact_run_hands_the_orchestrator_the_backend_as_a_keyword(store, live, monkeypatch):
    from mindtrainer import dispatch as dispatch_module

    seen = []
    real = dispatch_module.resume_or_run

    def spy(cfg, **kwargs):
        seen.append(kwargs)
        return real(cfg, **kwargs)

    monkeypatch.setattr(dispatch_module, "resume_or_run", spy)
    _interact_directive(store)
    _run_interact(store, live(), policy_endpoint="gpu-1")
    (kwargs,) = seen
    assert kwargs["policy_backend"]["trained"]["endpoint"] == "gpu-1"


def test_the_backend_has_one_opponent_entry_per_non_trained_slot_and_none_for_a_missing_source():
    from mindtrainer.dispatch import _policy_backend
    from mindtrainer.training_config import TrainingConfig

    cfg = TrainingConfig(
        mode="interact", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=0, grpo_steps=1, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        scenario_ids=["s1"], max_turns_per_episode=4,
        bindings=[
            RoleBinding("a", "pa", "self_play"),
            RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
            RoleBinding("c", "pc", "external_api"),   # 没有 model_ref:不合规的配置,但映射不应抛错
            RoleBinding("d", "pd", "frozen_reference", model_ref="zengen-v2", driver="scripted"),
        ],
        memory=resolved_memory(["pa", "pb"]),
    )
    assert _policy_backend(cfg, None) == {
        "trained": {"ref": "file:///ckpt/base", "kind": "base", "endpoint": None},
        "opponents": {
            "b": {"driver": "vllm", "source": "zengen-v1"},
            "c": {"driver": "external_api", "source": None},
            "d": {"driver": "scripted", "source": "zengen-v2"},
        },
        "real_inference_with_mock_training": True,
        "memory": resolved_memory(["pa", "pb"]),
    }


# ---------------- memory_resolver:进策略上下文之前解出起点摘要与运行时身份 ----------------

def _recording_resolver(trace, digests=None):
    def resolve(seed, persona_ids):
        trace.append(("resolve", seed, persona_ids))
        return (digests if digests is not None else {p: f"digest-{p}" for p in persona_ids}), dict(STAND_IN_RUNTIME)
    return resolve


def test_the_memory_is_resolved_before_the_policy_context_is_entered_and_recorded(store, live):
    _interact_directive(store, memory=unresolved_memory())
    trace, seen = [], []

    def factory(run_id, cfg):
        seen.append(cfg.memory)
        return _Scope(trace, ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8}))

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=factory, memory_resolver=_recording_resolver(trace)))
    assert trace[0] == ("resolve", None, ("pa", "pb")) and trace[1] == "enter"
    resolved = {"seed": None, "digests": {"pa": "digest-pa", "pb": "digest-pb"}, "runtime": STAND_IN_RUNTIME}
    assert seen == [resolved]
    assert _record(store).payload["policy_backend"]["memory"] == resolved
    # 解出的值只进这次派发的配置与记录,指令原样不动(续训时重新解)
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert directive.payload["training_config"]["memory"] == unresolved_memory()


def test_the_resolved_memory_is_part_of_the_config_hash(store, live):
    _interact_directive(store, memory=unresolved_memory())
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            executor_factory=lambda run_id: MockExecutor(crash_at_step=1), client_factory=live().client_factory,
            policy=ScriptedBackend({"a": ["agreed", "x"] * 16}), memory_resolver=fake_memory_resolver))
    assert _record(store).status == "pending"
    changed = _recording_resolver([], digests={"pa": "digest-pa", "pb": "other"})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({"a": ["agreed", "x"] * 16}),
        memory_resolver=changed))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T3"
    assert "config changed" in env.payload["failure"]["reason"]


def test_an_interact_directive_without_a_memory_resolver_fails_naming_the_interact_switch(store, live):
    _interact_directive(store, memory=unresolved_memory())
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, None)))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "memory_resolver" in env.payload["failure"]["reason"] and "--interact" in env.payload["failure"]["reason"]
    assert trace == [] and not store.exists(f"{run_dir(RUN)}/training_run.yaml")


def test_a_resolver_exception_fails_the_directive_with_its_class_name_only(store, live):
    def broken(seed, persona_ids):
        raise RuntimeError("cannot open https://memory.example/secret")

    _interact_directive(store, memory=unresolved_memory())
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, None),
        memory_resolver=broken))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    reason = env.payload["failure"]["reason"]
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "memory resolver failed: RuntimeError" in reason and "memory.example" not in reason
    assert trace == []


def test_a_resolver_that_refuses_with_a_reason_keeps_that_reason(store, live):
    def refuses(seed, persona_ids):
        raise DirectiveFailedError("memory.seed 'seed-1' needs a memory config")

    _interact_directive(store, memory=unresolved_memory("seed-1"))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({}), memory_resolver=refuses))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and "memory.seed 'seed-1' needs a memory config" in env.payload["failure"]["reason"]


def test_a_resolver_that_misses_a_persona_fails_the_directive(store, live):
    _interact_directive(store, memory=unresolved_memory())
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy=ScriptedBackend({}),
        memory_resolver=_recording_resolver([], digests={"pa": "digest-pa"})))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and "memory.digests must name exactly the bound personas" in env.payload["failure"]["reason"]


def test_a_batch_directive_never_calls_the_resolver(store):
    trace = []
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(memory_resolver=_recording_resolver(trace)))
    assert trace == [] and store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"


def test_a_resolver_that_returns_malformed_digests_fails_the_directive_with_the_class_name_only(store, live):
    """返回值形状不对是确定性的错:不能让转换时的 TypeError 逃出 dispatch,否则指令留在 pending 里每轮重派。"""
    def malformed(seed, persona_ids):
        return None, dict(STAND_IN_RUNTIME)

    _interact_directive(store, memory=unresolved_memory())
    trace = []
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=live().client_factory, policy_factory=lambda run_id, cfg: _Scope(trace, None),
        memory_resolver=malformed))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and env.payload["failure"]["category"] == "T2"
    assert "memory resolver failed: TypeError" in env.payload["failure"]["reason"]
    assert trace == [] and not store.exists(f"{run_dir(RUN)}/training_run.yaml")
