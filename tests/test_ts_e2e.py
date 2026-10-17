"""调度器一侧端到端:装配上下文 → 脚本化规划 → 指令 → 人确认 → MindTrainer 派发器(带默认数据生产器)
训练出候选。clone 的下一轮复用前一轮的清单,不再合成。只用 MockExecutor,不接外部服务。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.paths import (
    candidate_checkpoint_file,
    dataset_manifest_file,
    nomination_file,
    planner_decision_file,
    recipe_candidate_file,
    run_dir,
)
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.executor import MockExecutor
from recipestore.admit import handle_admit_recipe
from trainplanner.backends import ScriptedDecisionBackend
from trainplanner.templates import install_default_template
from trainscheduler.config import SchedulerConfig, install_default_configs
from trainscheduler.nominate import make_nominate_handler
from trainscheduler.plan_handler import make_plan_handler
from ts_fixtures import decision_json, make_store, write_model, write_report, write_round


def _confirm(store, run_id):
    """命令行 confirm 的等价操作(计划 8):awaiting_confirmation -> pending。"""
    path = f"{run_dir(run_id)}/directive.yaml"
    d = store.read_product(path)
    assert_transition("nonreview", d.status, "pending")
    d.status = "pending"
    store.write_product(path, d, f"human: confirm {run_id}")


def _train(store, run_id):
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        executor_factory=lambda run_id: MockExecutor(), data_producer=TemplateDataProducer()))
    assert store.read_product(f"{run_dir(run_id)}/directive.yaml").status == "executed"
    return store.read_product(candidate_checkpoint_file(run_id))


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    install_default_configs(s)
    install_default_template(s)
    write_round(s, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    return s


def test_a_new_batch_round_is_planned_confirmed_synthesized_and_trained(store):
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(
        store, Action("plan_next_round", "run-001", {}))
    assert store.read_product(planner_decision_file("run-001")).status == "executed"
    assert reconcile_once(store) == []  # awaiting_confirmation:调和循环不碰它
    _confirm(store, "run-002")
    cand = _train(store, "run-002")
    assert cand.status == "pending_review"
    for stage in ("sft", "grpo_opd"):
        assert store.exists(dataset_manifest_file("run-002", stage))
    curve = cand.payload["train_curve_summary"]
    assert curve["degenerate_groups_total"] == 0
    assert cand.payload["base_checkpoint_ref"]["uri"].endswith("cand-run-001.safetensors")


def test_promote_nominate_admit_then_clone_reuses_the_manifests(store):
    plan = make_plan_handler(backend=ScriptedDecisionBackend([
        decision_json(),
        decision_json(action="clone", source_recipe_id="recipe-run-002", perturbation={"opd_coefficient": 0.2}),
    ]))
    plan(store, Action("plan_next_round", "run-001", {}))
    _confirm(store, "run-002")
    _train(store, "run-002")

    # 审核门①与晋级、诊断用写状态模拟(与既有端到端测试同一手法)
    cand = store.read_product(candidate_checkpoint_file("run-002"))
    cand.status = "effective"
    store.write_product(candidate_checkpoint_file("run-002"), cand, "human+reconcile: promote")
    write_model(store, 2, "run-002")
    write_report(store, "run-002", {"tom": 0.48, "empathy": 0.70}, baseline_ref="zengen-v1")

    make_nominate_handler(config_loader=lambda s: SchedulerConfig(accept_mock_evidence=True),
                          repo_commit="0385004")(store, Action("nominate_recipe", "run-002", {}))
    rc = store.read_product(recipe_candidate_file("run-002"))
    assert rc.status == "pending_review"
    assert rc.payload["stage_manifests"]["sft"]["manifest_hash"] == store.read_product(
        dataset_manifest_file("run-002", "sft")).payload["manifest_hash"]
    assert store.read_product(nomination_file("run-002")).payload["result"] == "nominated"

    rc.status = "approved"  # 审核门③
    store.write_product(recipe_candidate_file("run-002"), rc, "human: approve recipe")
    handle_admit_recipe(store, Action("admit_recipe", "recipe-run-002", {}))

    plan(store, Action("plan_next_round", "run-002", {}))
    directive = store.read_product(f"{run_dir('run-003')}/directive.yaml")
    assert directive.payload["recipe_ref"] == "recipe-run-002"
    assert "data_spec" not in directive.payload
    assert directive.payload["training_config"]["stage_manifest_refs"]["sft"] == "dsm-run-002-sft"
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-002.safetensors")
    _confirm(store, "run-003")
    cand3 = _train(store, "run-003")
    assert cand3.status == "pending_review"
    assert not store.exists(dataset_manifest_file("run-003", "sft"))  # 没有重新合成
