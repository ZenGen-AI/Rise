"""构造指令:载荷形状、data_spec 与按新 run id 写好的清单 id、确认状态、能被 MindTrainer 读回。"""
import pytest

from controlplane.states import can_transition
from mindtrainer.data_spec import data_spec_from_directive, stages_to_synthesize
from mindtrainer.training_config import load_config_from_directive, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import (
    DEFAULT_SCHEDULER_CONFIG,
    default_training_defaults_payload,
    parse_training_defaults,
)
from trainscheduler.directive import construct_directive, manifest_id
from trainscheduler.errors import DirectiveConstructionError, SchedulerError
from trainscheduler.resolve import ResolvedAction, resolve_action
from ts_fixtures import make_store, recipe_payload, write_directive, write_recipe

DEFAULTS = parse_training_defaults(default_training_defaults_payload())


def _decision(action="new", source=None, perturbation=None):
    return PlannerDecision("tom", action, "batch" if action == "new" else None, source,
                           dict(perturbation or {}), "", "r")


def _resolved(store, action="new", source=None):
    return resolve_action(store, _decision(action, source), config=DEFAULT_SCHEDULER_CONFIG, defaults=DEFAULTS)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe(s, "run-001")
    return s


def test_manifest_ids_follow_the_dataset_rule():
    assert manifest_id("run-004", "grpo_opd") == "dsm-run-004-grpo_opd"


def test_a_new_directive_carries_data_spec_and_its_own_manifest_ids(store):
    path = construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                               planner_decision_id="planner-run-001")
    assert path == "control-plane/runs/run-002/directive.yaml"
    env = store.read_product(path)
    assert (env.id, env.type, env.status) == ("run-002", "directive", "awaiting_confirmation")
    assert set(env.payload) == {"target_dimension", "recipe_ref", "training_config", "planner_decision_ref", "data_spec"}
    assert env.payload["target_dimension"] == "tom" and env.payload["recipe_ref"] == ""
    assert env.payload["planner_decision_ref"] == "planner-run-001"
    assert env.payload["data_spec"] == DEFAULTS.data_spec.to_dict()
    assert env.payload["training_config"]["stage_manifest_refs"] == {
        "sft": "dsm-run-002-sft", "grpo_opd": "dsm-run-002-grpo_opd"}
    assert env.links == ["planner-run-001"]


def test_the_directive_reads_back_through_mindtrainer(store):
    construct_directive(store, _resolved(store), new_run_id="run-002", confirm=False,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.status == "pending"
    cfg = load_config_from_directive(env)
    assert validate_training_config(cfg) is None
    assert data_spec_from_directive(env) == DEFAULTS.data_spec
    assert stages_to_synthesize(store, "run-002", cfg) == ["sft", "grpo_opd"]


def test_only_required_stages_get_a_manifest_id(store):
    raw = default_training_defaults_payload()
    raw["training_config"]["sft_steps"] = 0
    defaults = parse_training_defaults(raw)
    resolved = resolve_action(store, _decision(), config=DEFAULT_SCHEDULER_CONFIG, defaults=defaults)
    construct_directive(store, resolved, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.payload["training_config"]["stage_manifest_refs"] == {"grpo_opd": "dsm-run-002-grpo_opd"}


@pytest.mark.parametrize("action", ["clone", "replay"])
def test_clone_and_replay_reuse_the_recipe_manifests_and_carry_no_data_spec(store, action):
    construct_directive(store, _resolved(store, action, "recipe-run-001"), new_run_id="run-002", confirm=True,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert "data_spec" not in env.payload
    assert env.payload["recipe_ref"] == "recipe-run-001"
    assert env.payload["training_config"]["stage_manifest_refs"] == recipe_payload("run-001")["training_config"][
        "stage_manifest_refs"]
    assert env.links == ["planner-run-001", "recipe-run-001"]
    assert validate_training_config(load_config_from_directive(env)) is None
    assert stages_to_synthesize(store, "run-002", load_config_from_directive(env)) == []


def test_awaiting_confirmation_can_only_be_confirmed_or_abandoned():
    assert can_transition("nonreview", "awaiting_confirmation", "pending")
    assert can_transition("nonreview", "awaiting_confirmation", "failed")
    assert not can_transition("nonreview", "awaiting_confirmation", "executed")


def test_an_invalid_config_is_refused_and_nothing_is_written(store):
    bad = ResolvedAction("tom", "replay", "recipe-run-001",
                         {**recipe_payload("run-001")["training_config"], "group_size": 1}, None, None)
    head = store._git("rev-parse", "HEAD")
    with pytest.raises(DirectiveConstructionError, match="group_size 1 is below"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
    assert store._git("rev-parse", "HEAD") == head
    assert issubclass(DirectiveConstructionError, SchedulerError)


def test_a_data_spec_too_small_for_the_config_is_refused(store):
    from mindtrainer.data_spec import parse_data_spec

    tiny = parse_data_spec({"scenario_count": 1, "seed": 0, "purpose": "training",
                            "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}})
    r = _resolved(store)
    bad = ResolvedAction(r.target_dimension, r.action, r.recipe_ref, r.training_config, tiny, None)
    with pytest.raises(DirectiveConstructionError, match="below groups_per_step"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")


def test_rewriting_by_the_same_decision_is_a_no_op(store):
    r = _resolved(store)
    construct_directive(store, r, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    head = store._git("rev-parse", "HEAD")
    construct_directive(store, r, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    assert store._git("rev-parse", "HEAD") == head
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "awaiting_confirmation"


def test_someone_elses_directive_is_never_overwritten(store):
    write_directive(store, "run-002", status="pending")
    with pytest.raises(DirectiveConstructionError, match="refusing to overwrite"):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                            planner_decision_id="planner-run-001")
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "pending"


def test_confirm_must_be_a_bool(store):
    with pytest.raises(TypeError):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm="yes",
                            planner_decision_id="planner-run-001")
