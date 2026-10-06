"""MindTrainer 产出的候选,不改一行调和循环就能走完 审核 -> 晋级 -> 下一轮基座。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.models import latest_promoted_checkpoint
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file
from mindtrainer.checkpoint import MockConverter
from mindtrainer.executor import MockExecutor
from mindtrainer.orchestrator import resume_or_run
from mindtrainer.reward_pipeline import build_attribution_backend
from mindtrainer.training_config import RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.dataset import produce_grouped_dataset, read_manifest
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario

DATA_RUN = "run-1"  # 数据集挂在这个 run 下;两轮训练共用同一份清单
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {
        "a": ScriptedBackend({"a": [first, "a2"]}),
        "b": ScriptedBackend({"b": ["b1", "b2"]}),
    }


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", 2)):
        produce_grouped_dataset(
            s, DATA_RUN, name,
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
    return s


def _cfg(base_ref):
    return TrainingConfig(
        mode="batch", trained_slot="a", base_checkpoint_ref=base_ref, teacher_model_ref=TEACHER,
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=1, grpo_steps=2, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )


def _train(store, run_id, cfg, executor):
    def make_sources(start):
        return {
            name: BatchTrajectorySource(store, read_manifest(store, DATA_RUN, name))
            for name in ("sft", "grpo_opd")
        }

    return resume_or_run(
        cfg, store=store, run_id=run_id, make_sources=make_sources,
        attribution=build_attribution_backend(cfg.reward),
        executor=executor, converter=MockConverter(),
    )


def _approve(store, run_id):
    path = candidate_checkpoint_file(run_id)
    cand = store.read_product(path)
    cand.status = "approved"
    store.write_product(path, cand, "human: approve")


class _Recording(MockExecutor):
    def __init__(self):
        super().__init__()
        self.loads = []

    def load(self, checkpoint_ref):
        self.loads.append(checkpoint_ref)
        super().load(checkpoint_ref)


def test_a_mindtrainer_candidate_walks_the_unmodified_reconcile_loop_to_promotion(store):
    _train(store, "run-1", _cfg(BASE), MockExecutor())
    candidate = store.read_product(candidate_checkpoint_file("run-1"))

    first = reconcile_once(store)
    assert [(a.kind, a.target_id) for a in first] == [("request_diagnostic", "cand-run-1")]
    assert store.exists(diagnostic_report_file("run-1"))

    second = reconcile_once(store)
    assert [a.kind for a in second] == ["await_review"]
    assert store.read_product(candidate_checkpoint_file("run-1")).status == "pending_review"

    _approve(store, "run-1")
    third = reconcile_once(store)
    assert [a.kind for a in third] == ["promote_candidate"]

    model = store.read_product(model_file(1))
    assert model.payload["promoted_from"] == "cand-run-1"
    assert model.payload["checkpoint_ref"] == candidate.payload["checkpoint_ref"]
    assert store.read_product(candidate_checkpoint_file("run-1")).status == "effective"


def test_the_promoted_checkpoint_becomes_the_base_of_the_next_training_round(store):
    _train(store, "run-1", _cfg(BASE), MockExecutor())
    reconcile_once(store)
    reconcile_once(store)
    _approve(store, "run-1")
    reconcile_once(store)

    promoted = latest_promoted_checkpoint(store)
    assert promoted.model_id == "zengen-v1"

    executor = _Recording()
    run = _train(store, "run-2", _cfg(promoted.checkpoint_ref), executor)

    assert run.status == "executed"
    assert executor.loads[0] == promoted.checkpoint_ref  # 起点正是晋级的那份权重
    second = store.read_product(candidate_checkpoint_file("run-2"))
    assert second.payload["base_checkpoint_ref"] == promoted.checkpoint_ref.to_dict()
    assert second.status == "pending_review"
