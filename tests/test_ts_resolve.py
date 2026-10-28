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


def test_new_interact_is_refused_when_allowed_but_defaults_have_no_interact_section(store):
    allowed = SchedulerConfig(allow_interact=True)
    for defaults in (None, DEFAULTS):
        reason = validate_decision(store, _d(mode="interact"), config=allowed, defaults=defaults)
        assert "interact section" in reason and "allow_interact" not in reason
    # 没传 defaults 的老调用方式同样拒,不会因为缺参数而放行
    assert "interact section" in validate_decision(store, _d(mode="interact"), config=allowed)


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


def test_cloning_an_interact_recipe_from_before_memory_support_is_refused(store):
    payload = recipe_payload("run-004", recipe_type="interact")
    del payload["training_config"]["memory"]
    write_recipe(store, "run-004", payload=payload)
    with pytest.raises(ResolveError, match="no memory section"):
        resolve_action(store, _d("replay", source="recipe-run-004"), config=ALLOW, defaults=DEFAULTS)


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


def test_a_replay_with_a_perturbation_is_a_violation(store):
    d = PlannerDecision(
        target_dimension="tom", action="replay", mode=None, source_recipe_id="recipe-run-001",
        perturbation={"opd_coefficient": 0.2}, data_synthesis_hint="", rationale="r",
    )
    reason = validate_decision(store, d, config=CFG)
    assert "replay" in reason and "perturbation" in reason


def test_resolve_refuses_a_replay_with_a_perturbation(store):
    d = PlannerDecision(
        target_dimension="tom", action="replay", mode=None, source_recipe_id="recipe-run-001",
        perturbation={"opd_coefficient": 0.2}, data_synthesis_hint="", rationale="r",
    )
    with pytest.raises(ResolveError):
        resolve_action(store, d, config=CFG, defaults=DEFAULTS)


def test_validation_and_resolution_only_read(store):
    head = store._git("rev-parse", "HEAD")
    resolve_action(store, _d("clone", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    validate_decision(store, _d("replay", source="recipe-run-404"), config=CFG)
    assert store._git("rev-parse", "HEAD") == head


# ---- new + interact:按 training_defaults 的 interact 段放行与解析 ----

ALLOW = SchedulerConfig(allow_interact=True)


def _named_section():
    return {
        "trained_slot": "a",
        "slots": [
            {"slot_id": "a", "model_backend": "self_play"},
            {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1"},
        ],
        "max_turns_per_episode": 4,
        "memory": {"seed": None},
        "scenario_ids": ["s1", "s2"],
        "persona_ids": {"a": "p1", "b": "p2"},
    }


def _generated_section():
    section = _named_section()
    del section["scenario_ids"], section["persona_ids"]
    section["data_spec"] = {"scenario_count": 2, "seed": 7, "purpose": "training", "stages": {"sft": {"group_size": 1}}}
    return section


def _defaults_with(section):
    raw = default_training_defaults_payload()
    raw["interact"] = section
    return parse_training_defaults(raw)


@pytest.fixture
def named():
    return _defaults_with(_named_section())


@pytest.fixture
def generated():
    return _defaults_with(_generated_section())


def test_new_interact_is_refused_without_the_section_when_resolving(store):
    with pytest.raises(ResolveError, match="interact section"):
        resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=DEFAULTS)


def test_new_interact_still_needs_allow_interact_even_with_the_section(store, named):
    reason = validate_decision(store, _d(mode="interact"), config=CFG, defaults=named)
    assert "allow_interact" in reason
    with pytest.raises(ResolveError, match="allow_interact"):
        resolve_action(store, _d(mode="interact"), config=CFG, defaults=named)


def test_new_interact_is_valid_with_either_kind_of_section(store, named, generated):
    assert validate_decision(store, _d(mode="interact"), config=ALLOW, defaults=named) is None
    assert validate_decision(store, _d(mode="interact"), config=ALLOW, defaults=generated) is None


def test_new_interact_resolves_the_named_path_from_the_section(store, named):
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=named)
    cfg = r.training_config
    assert (cfg["mode"], cfg["trained_slot"], cfg["max_turns_per_episode"]) == ("interact", "a", 4)
    assert cfg["scenario_ids"] == ["s1", "s2"]
    assert cfg["bindings"] == [
        {"slot_id": "a", "persona_id": "p1", "model_backend": "self_play"},
        {"slot_id": "b", "persona_id": "p2", "model_backend": "frozen_reference", "model_ref": "zengen-v1"},
    ]
    assert (r.action, r.recipe_ref, r.target_dimension, r.data_spec) == ("new", "", "tom", None)


def test_named_path_forces_sft_steps_to_zero_and_keeps_the_other_defaults(store, named):
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=named).training_config
    assert DEFAULT_TRAINING_CONFIG["sft_steps"] == 2 and cfg["sft_steps"] == 0
    for key in ("grpo_steps", "group_size", "groups_per_step", "opd_coefficient", "reward", "teacher_model_ref"):
        assert cfg[key] == DEFAULT_TRAINING_CONFIG[key]


@pytest.mark.parametrize("make", [_named_section, _generated_section], ids=["named", "generated"])
@pytest.mark.parametrize("seed", [None, "seed-1"])
def test_new_interact_carries_the_memory_seed_and_leaves_the_rest_to_the_dispatcher(store, make, seed):
    section = make()
    section["memory"] = {"seed": seed}
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section))
    assert r.training_config["memory"] == {"seed": seed, "digests": {}, "runtime": {}}


def test_a_new_batch_round_has_no_memory(store):
    r = resolve_action(store, _d(), config=ALLOW, defaults=DEFAULTS)
    assert "memory" not in r.training_config


def test_a_named_path_config_passes_the_strict_validation(store, named):
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=named).training_config
    assert validate_training_config(TrainingConfig.from_dict(cfg)) is None
    assert cfg["bindings"][0]["model_backend"] == "self_play" and "model_ref" not in cfg["bindings"][0]


def test_generated_path_leaves_scenarios_empty_keeps_the_slot_layout_and_carries_the_data_spec(store, generated):
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=generated)
    cfg = r.training_config
    assert (cfg["mode"], cfg["trained_slot"], cfg["max_turns_per_episode"]) == ("interact", "a", 4)
    assert cfg["scenario_ids"] == []
    # 角色位布局随指令走(派发器读不到调度器的 interact 段);人格留空,等派发时配上
    assert cfg["bindings"] == [
        {"slot_id": "a", "persona_id": "", "model_backend": "self_play"},
        {"slot_id": "b", "persona_id": "", "model_backend": "frozen_reference", "model_ref": "zengen-v1"},
    ]
    assert r.data_spec == generated.interact.data_spec and r.data_spec.scenario_count == 2


def test_generated_and_named_paths_expand_the_same_slot_layout(store, named, generated):
    def layout(defaults):
        bindings = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=defaults).training_config["bindings"]
        return [{k: v for k, v in b.items() if k != "persona_id"} for b in bindings]

    assert layout(generated) == layout(named)


def test_a_generated_path_binding_carries_driver_only_when_the_slot_has_one(store):
    section = _generated_section()
    section["slots"][1]["driver"] = "scripted"
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section)).training_config
    assert "driver" not in cfg["bindings"][0]
    assert cfg["bindings"][1]["driver"] == "scripted"
    assert [b["persona_id"] for b in cfg["bindings"]] == ["", ""]


def test_generated_path_bindings_follow_the_slot_order_of_the_section(store):
    section = _generated_section()
    section["slots"].reverse()
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section)).training_config
    assert [b["slot_id"] for b in cfg["bindings"]] == ["b", "a"]


def test_generated_path_keeps_the_default_sft_steps(store, generated):
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=generated).training_config
    assert cfg["sft_steps"] == DEFAULT_TRAINING_CONFIG["sft_steps"] == 2


def test_a_binding_carries_driver_only_when_the_slot_has_one(store):
    section = _named_section()
    section["slots"][1]["driver"] = "scripted"
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section)).training_config
    assert "driver" not in cfg["bindings"][0]
    assert cfg["bindings"][1]["driver"] == "scripted"


def test_bindings_follow_the_slot_order_of_the_section(store):
    section = _named_section()
    section["slots"].reverse()
    cfg = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section)).training_config
    assert [b["slot_id"] for b in cfg["bindings"]] == ["b", "a"]
    assert [b["persona_id"] for b in cfg["bindings"]] == ["p2", "p1"]


@pytest.mark.parametrize("path", ["named", "generated"])
def test_new_interact_takes_the_latest_promoted_checkpoint(store, request, path):
    defaults = request.getfixturevalue(path)
    write_model(store, 1, "run-001")
    write_model(store, 2, "run-002")
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=defaults)
    assert r.training_config["base_checkpoint_ref"] == ref("cand-run-002")
    assert r.base_model_id == "zengen-v2"


@pytest.mark.parametrize("path", ["named", "generated"])
def test_new_interact_without_a_promoted_model_uses_the_initial_base(store, request, path):
    defaults = request.getfixturevalue(path)
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=defaults)
    assert r.training_config["base_checkpoint_ref"] == DEFAULT_TRAINING_CONFIG["base_checkpoint_ref"]
    assert r.base_model_id is None


@pytest.mark.parametrize("path", ["named", "generated"])
def test_resolving_new_interact_does_not_alias_or_change_the_defaults(store, request, path):
    defaults = request.getfixturevalue(path)
    before = defaults.to_dict()
    r = resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=defaults)
    r.training_config["scenario_ids"].append("x")
    r.training_config["bindings"].append({"slot_id": "x"})
    r.training_config["reward"]["components"].append("x")
    for binding in r.training_config["bindings"][:-1]:
        binding["persona_id"] = "changed"
    assert defaults.to_dict() == before


def test_a_new_batch_decision_is_unaffected_by_the_interact_section(store, named):
    r = resolve_action(store, _d(), config=ALLOW, defaults=named)
    assert r.training_config == DEFAULT_TRAINING_CONFIG and r.data_spec == named.data_spec


def test_a_named_path_section_with_a_non_self_play_trained_slot_is_a_resolve_error(store):
    section = _named_section()
    section["slots"][0] = {"slot_id": "a", "model_backend": "frozen_reference", "model_ref": "zengen-v1"}
    with pytest.raises(ResolveError, match="trained slot 'a' must be self_play"):
        resolve_action(store, _d(mode="interact"), config=ALLOW, defaults=_defaults_with(section))
