"""接线入口:调和循环 + MindTrainer 派发器,端到端跑一条 batch 指令。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, model_file, run_dir
from mindtrainer.training_config import RewardConfig
from mindtrainer.wire import main, run
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import produce_grouped_dataset
from simulator.store_adapter import write_persona, write_scenario

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {"a": ScriptedBackend({"a": [first, "a2"]}), "b": ScriptedBackend({"b": ["b1", "b2"]})}


@pytest.fixture
def repo(tmp_path):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", 2)):
        produce_grouped_dataset(
            store, RUN, name,
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
    env = Envelope(
        id=RUN, type="directive", version=1, created_at="2026-10-07T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={
            "target_dimension": "tom", "recipe_ref": "",
            "training_config": {
                "mode": "batch", "trained_slot": "a",
                "base_checkpoint_ref": BASE.to_dict(), "teacher_model_ref": TEACHER.to_dict(),
                "reward": REWARD.to_dict(), "sft_steps": 1, "grpo_steps": 1, "sft_batch_size": 1,
                "groups_per_step": 1, "group_size": 2, "grpo_config": {}, "opd_coefficient": 0.1,
                "checkpoint_every_steps": 1, "allow_resume": True,
                "stage_manifest_refs": {"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
            },
        },
    )
    store.write_product(f"{run_dir(RUN)}/directive.yaml", env, "human: write directive")
    return store


def test_one_tick_trains_the_directive_and_leaves_a_candidate(repo, tmp_path):
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    assert repo.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    assert repo.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert repo.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"


def test_main_parses_arguments_and_can_run_a_single_tick(repo, tmp_path):
    assert main([str(tmp_path / "cp"), "--interval-s", "0", "--max-ticks", "1"]) == 0
    assert repo.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_the_wired_loop_still_reaches_promotion(repo, tmp_path):
    """端到端:一条 batch 指令经调和循环 → 候选 → 诊断 → 人工批准 → 晋级。"""
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    reconcile_once(repo)  # request_diagnostic
    reconcile_once(repo)  # await_review
    cand = repo.read_product(candidate_checkpoint_file(RUN))
    cand.status = "approved"
    repo.write_product(candidate_checkpoint_file(RUN), cand, "human: approve")
    reconcile_once(repo)  # promote_candidate
    model = repo.read_product(model_file(1))
    assert (model.id, model.payload["promoted_from"]) == ("zengen-v1", "cand-run-1")


def test_a_second_tick_over_a_finished_run_does_nothing(repo, tmp_path):
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    before = repo.read_product(f"{run_dir(RUN)}/training_run.yaml")
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    assert repo.read_product(f"{run_dir(RUN)}/training_run.yaml") == before
