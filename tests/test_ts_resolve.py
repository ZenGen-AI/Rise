"""领域校验与动作解析:配方存在且有效、扰动白名单、interact 开关;new / clone / replay 各自取什么。"""
import copy

import pytest

from mindtrainer.training_config import TrainingConfig, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import (
    DEFAULT_SCHEDULER_CONFIG,
    DEFAULT_TRAINING_CONFIG,
    SchedulerConfig,
    default_training_defaults_payload,
    parse_training_defaults,
)
from trainscheduler.errors import ResolveError, SchedulerError
from trainscheduler.resolve import apply_perturbation, resolve_action, validate_decision
from ts_fixtures import make_store, recipe_payload, ref, write_model, write_recipe

CFG = DEFAULT_SCHEDULER_CONFIG
DEFAULTS = parse_training_defaults(default_training_defaults_payload())


def _d(action="new", *, dimension="tom", mode="batch", source=None, perturbation=None):
    return PlannerDecision(
        target_dimension=dimension, action=action, mode=mode if action == "new" else None,
        source_recipe_id=source, perturbation=dict(perturbation or {}), data_synthesis_hint="",
        rationale="r",
    )


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe(s, "run-001")
    return s


def test_a_new_batch_decision_is_valid(store):
    assert validate_decision(store, _d(), config=CFG) is None


def test_new_interact_is_refused_while_allow_interact_is_off(store):
    reason = validate_decision(store, _d(mode="interact"), config=CFG)
    assert "allow_interact" in reason


def test_new_interact_is_refused_even_when_allowed_because_defaults_have_no_bindings(store):
    reason = validate_decision(store, _d(mode="interact"), config=SchedulerConfig(allow_interact=True))
    assert "scenario_ids and bindings" in reason


def test_clone_of_an_effective_recipe_is_valid(store):
    assert validate_decision(store, _d("clone", source="recipe-run-001"), config=CFG) is None
    assert validate_decision(store, _d("replay", source="recipe-run-001"), config=CFG) is None


@pytest.mark.parametrize("action", ["clone", "replay"])
def test_a_missing_recipe_is_a_violation(store, action):
    assert "does not exist" in validate_decision(store, _d(action, source="recipe-run-404"), config=CFG)
    assert "does not exist" in validate_decision(store, _d(action, source="not-a-recipe"), config=CFG)


def test_a_deprecated_recipe_is_a_violation(store):
    write_recipe(store, "run-002", status="deprecated")
    assert "deprecated" in validate_decision(store, _d("clone", source="recipe-run-002"), config=CFG)


def test_an_agent_rsi_recipe_cannot_be_reused(store):
    payload = recipe_payload("run-003")
    payload["recipe_type"] = "agent_rsi"
    write_recipe(store, "run-003", payload=payload)
    assert "'agent_rsi'" in validate_decision(store, _d("clone", source="recipe-run-003"), config=CFG)


def test_an_interact_recipe_needs_allow_interact(store):
    write_recipe(store, "run-004", payload=recipe_payload("run-004", recipe_type="interact"))
    assert "allow_interact is off" in validate_decision(store, _d("replay", source="recipe-run-004"), config=CFG)
    allowed = SchedulerConfig(allow_interact=True)
    assert validate_decision(store, _d("replay", source="recipe-run-004"), config=allowed) is None


def test_the_recipe_must_target_the_decided_dimension(store):
    reason = validate_decision(store, _d("clone", dimension="empathy", source="recipe-run-001"), config=CFG)
    assert "targets 'tom', not 'empathy'" in reason


def test_perturbation_keys_must_be_whitelisted(store):
    ok = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": 0.2, "grpo_config.learning_rate": 2e-5})
    assert validate_decision(store, ok, config=CFG) is None
    bad = _d("clone", source="recipe-run-001", perturbation={"group_size": 4})
    assert "'group_size' is not perturbable" in validate_decision(store, bad, config=CFG)


def test_a_whitelisted_perturbation_that_breaks_the_config_is_a_violation(store):
    bad = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": -1})
    assert "opd_coefficient must be a finite number >= 0" in validate_decision(store, bad, config=CFG)
    bad_type = _d("clone", source="recipe-run-001", perturbation={"sft_steps": "2"})
    assert "sft_steps must be an int" in validate_decision(store, bad_type, config=CFG)


def test_a_perturbation_into_a_non_mapping_is_a_violation(store):
    cfg = SchedulerConfig(perturbable=("reward.gamma.x",))
    bad = _d("clone", source="recipe-run-001", perturbation={"reward.gamma.x": 1})
    assert "training_config.reward.gamma is not a mapping" in validate_decision(store, bad, config=cfg)


def test_apply_perturbation_copies_and_sets_nested_keys():
    cfg = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    out = apply_perturbation(cfg, {"grpo_config.learning_rate": 3e-5, "grpo_config.beta": 0.1, "sft_steps": 4})
    assert out["grpo_config"] == {"learning_rate": 3e-5, "beta": 0.1} and out["sft_steps"] == 4
    assert cfg == DEFAULT_TRAINING_CONFIG


def test_apply_perturbation_refuses_unknown_top_level_fields():
    with pytest.raises(ResolveError, match="has no field 'learning_rate'"):
        apply_perturbation(copy.deepcopy(DEFAULT_TRAINING_CONFIG), {"learning_rate": 1})


def test_new_without_a_promoted_model_uses_the_initial_base(store):
    r = resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)
    assert (r.action, r.recipe_ref, r.target_dimension, r.base_model_id) == ("new", "", "tom", None)
    assert r.training_config == DEFAULT_TRAINING_CONFIG
    assert r.data_spec == DEFAULTS.data_spec


def test_new_takes_the_latest_promoted_checkpoint(store):
    write_model(store, 1, "run-001")
    write_model(store, 2, "run-002")
    r = resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)
    assert r.training_config["base_checkpoint_ref"] == ref("cand-run-002")
    assert r.base_model_id == "zengen-v2"


def test_clone_takes_the_recipe_config_plus_perturbation_on_the_latest_base(store):
    write_model(store, 1, "run-005")
    d = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": 0.25})
    r = resolve_action(store, d, config=CFG, defaults=DEFAULTS)
    expected = copy.deepcopy(recipe_payload("run-001")["training_config"])
    expected["opd_coefficient"] = 0.25
    expected["base_checkpoint_ref"] = ref("cand-run-005")
    assert r.training_config == expected
    assert (r.action, r.recipe_ref, r.data_spec, r.base_model_id) == ("clone", "recipe-run-001", None, "zengen-v1")
    # 清单仍是配方锚定的那两份
    assert r.training_config["stage_manifest_refs"] == {"sft": "dsm-run-001-sft", "grpo_opd": "dsm-run-001-grpo_opd"}


def test_clone_without_a_promoted_model_keeps_the_recipe_base(store):
    r = resolve_action(store, _d("clone", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    assert r.training_config == recipe_payload("run-001")["training_config"]


def test_replay_takes_the_recipe_verbatim_even_with_a_newer_base(store):
    write_model(store, 3, "run-009")
    r = resolve_action(store, _d("replay", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    assert r.training_config == recipe_payload("run-001")["training_config"]
    assert (r.recipe_ref, r.data_spec, r.base_model_id) == ("recipe-run-001", None, None)


def test_resolved_configs_validate(store):
    write_model(store, 1, "run-001")
    for d in (_d("clone", source="recipe-run-001"), _d("replay", source="recipe-run-001")):
        cfg = resolve_action(store, d, config=CFG, defaults=DEFAULTS).training_config
        assert validate_training_config(TrainingConfig.from_dict(cfg)) is None


def test_a_violation_is_raised_as_a_resolve_error(store):
    with pytest.raises(ResolveError, match="does not exist"):
        resolve_action(store, _d("clone", source="recipe-run-404"), config=CFG, defaults=DEFAULTS)
    assert issubclass(ResolveError, SchedulerError)


def test_a_recipe_whose_config_does_not_validate_cannot_be_replayed(store):
    payload = recipe_payload("run-006")
    payload["training_config"]["group_size"] = 1
    write_recipe(store, "run-006", payload=payload)
    with pytest.raises(ResolveError, match="group_size 1 is below min_group_size 2"):
        resolve_action(store, _d("replay", source="recipe-run-006"), config=CFG, defaults=DEFAULTS)


def test_a_promoted_model_without_checkpoint_ref_is_a_resolve_error(store):
    from controlplane.paths import model_file
    from ts_fixtures import env

    store.write_product(model_file(1), env("zengen-v1", "zengen_model", "effective", {"promoted_from": "x"}), "t")
    with pytest.raises(ResolveError, match="no checkpoint_ref"):
        resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)


def test_validation_and_resolution_only_read(store):
    head = store._git("rev-parse", "HEAD")
    resolve_action(store, _d("clone", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    validate_decision(store, _d("replay", source="recipe-run-404"), config=CFG)
    assert store._git("rev-parse", "HEAD") == head
