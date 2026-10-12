import copy

import pytest

from recipestore.validate import (
    INTERACT_WARNING,
    ReproReport,
    is_relative_ref,
    validate_reproducibility,
)
from rs_fixtures import agent_rsi_payload, batch_payload, interact_payload


def _without(payload, *path):
    p = copy.deepcopy(payload)
    node = p
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return p


def _with(payload, value, *path):
    p = copy.deepcopy(payload)
    node = p
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return p


# ---- batch ----

def test_a_complete_batch_recipe_has_no_errors_and_no_warnings():
    assert validate_reproducibility(batch_payload()) == ReproReport(errors=(), warnings=())


@pytest.mark.parametrize("path, needle", [
    (("training_config",), "training_config: missing"),
    (("training_config", "sft_steps"), "training_config.sft_steps: missing"),
    (("training_config", "reward"), "training_config.reward: missing"),
    (("stage_manifests",), "stage_manifests: must be a non-empty mapping"),
    (("stage_manifests", "sft", "manifest_hash"), "stage_manifests.sft.manifest_hash"),
    (("stage_manifests", "sft", "id"), "stage_manifests.sft.id: missing"),
    (("code_version",), "code_version: missing"),
    (("code_version", "repo_commit"), "code_version.repo_commit"),
    (("eval_anchor", "taxonomy_version"), "eval_anchor.taxonomy_version: missing"),
    (("eval_anchor", "item_pool_version"), "eval_anchor.item_pool_version: missing"),
])
def test_a_batch_recipe_missing_an_anchor_is_an_error(path, needle):
    report = validate_reproducibility(_without(batch_payload(), *path))
    assert not report.ok
    assert any(needle in e for e in report.errors), report.errors


def test_a_batch_stage_the_config_names_but_the_recipe_does_not_anchor_is_an_error():
    p = _without(batch_payload(), "stage_manifests", "grpo_opd")
    errors = validate_reproducibility(p).errors
    assert "stage_manifests.grpo_opd: training_config names it but it is not anchored" in errors


def test_a_batch_stage_whose_anchored_id_differs_from_the_config_is_an_error():
    p = _with(batch_payload(), "dsm-run-001-sft", "stage_manifests", "sft", "id")
    assert any("does not match training_config.stage_manifest_refs.sft" in e
               for e in validate_reproducibility(p).errors)


def test_a_batch_recipe_whose_mode_disagrees_with_its_type_is_an_error():
    p = _with(batch_payload(), "interact", "training_config", "mode")
    assert any("does not match recipe_type 'batch'" in e for e in validate_reproducibility(p).errors)


def test_a_checkpoint_ref_without_a_real_digest_is_an_error():
    p = _with(batch_payload(), "latest", "training_config", "base_checkpoint_ref", "sha256")
    errors = validate_reproducibility(p).errors
    assert "training_config.base_checkpoint_ref.sha256: must be a 64-char lowercase hex digest" in errors


@pytest.mark.parametrize("commit", ["HEAD", "main", "abc", "", None])
def test_a_branch_name_or_short_junk_is_not_a_commit(commit):
    p = _with(batch_payload(), commit, "code_version", "repo_commit")
    assert any(e.startswith("code_version.repo_commit") for e in validate_reproducibility(p).errors)


# ---- interact ----

def test_a_complete_interact_recipe_has_no_errors_and_one_warning():
    report = validate_reproducibility(interact_payload())
    assert report.errors == ()
    assert report.warnings == (INTERACT_WARNING,)


@pytest.mark.parametrize("path, needle", [
    (("training_config",), "training_config: missing"),
    (("scenario_ids",), "scenario_ids: missing"),
    (("code_version",), "code_version: missing"),
    (("bindings",), "bindings: must be a non-empty list"),
])
def test_an_interact_recipe_missing_an_anchor_is_an_error(path, needle):
    errors = validate_reproducibility(_without(interact_payload(), *path)).errors
    assert any(needle in e for e in errors), errors


def test_an_opponent_without_a_concrete_model_ref_is_an_error():
    p = _without(interact_payload(), "bindings", 1, "model_ref")
    assert "bindings[1].model_ref: opponent must name a concrete model id" in validate_reproducibility(p).errors


def test_an_opponent_pinned_to_latest_is_an_error():
    p = _with(interact_payload(), "latest", "bindings", 1, "model_ref")
    errors = validate_reproducibility(p).errors
    assert "bindings[1].model_ref: opponent must name a concrete model id" in errors
    assert "bindings[1].model_ref: relative reference 'latest' is not anchored" in errors


def test_an_interact_recipe_that_claims_to_be_deterministic_is_an_error():
    p = _with(interact_payload(), False, "nondeterministic")
    assert any(e.startswith("nondeterministic:") for e in validate_reproducibility(p).errors)


# ---- agent_rsi ----

def test_a_complete_agent_rsi_recipe_without_a_bridge_has_no_errors():
    assert validate_reproducibility(agent_rsi_payload()) == ReproReport(errors=(), warnings=())


@pytest.mark.parametrize("key", [
    "bound_model_id", "scenario_ids", "persona_ids", "analyzer_config", "verifier_config", "generator_version",
])
def test_an_agent_rsi_recipe_missing_an_anchor_is_an_error(key):
    errors = validate_reproducibility(_without(agent_rsi_payload(), key)).errors
    assert f"{key}: missing" in errors


def test_using_the_bridge_without_a_prompt_template_version_is_an_error():
    p = _with(agent_rsi_payload(), True, "uses_bridge")
    assert "prompt_template_version: missing" in validate_reproducibility(p).errors
    p["prompt_template_version"] = "proposer-v1"
    assert validate_reproducibility(p).errors == ()


def test_uses_bridge_must_be_a_boolean():
    p = _with(agent_rsi_payload(), "yes", "uses_bridge")
    assert "uses_bridge: must be a boolean" in validate_reproducibility(p).errors


def test_a_bound_model_of_current_best_is_an_error():
    p = _with(agent_rsi_payload(), "current_best", "bound_model_id")
    assert "bound_model_id: relative reference 'current_best' is not anchored" in validate_reproducibility(p).errors


# ---- 通用 ----

@pytest.mark.parametrize("value", ["latest", "LATEST", "current_best", "head", "zengen:latest", "model-latest"])
def test_relative_refs_are_recognised(value):
    assert is_relative_ref(value)


@pytest.mark.parametrize("value", ["zengen-v3", "dsm-run-003-sft", "latestness", "", 3, None])
def test_concrete_values_are_not_relative(value):
    assert not is_relative_ref(value)


def test_a_relative_ref_anywhere_in_the_payload_is_an_error():
    p = _with(batch_payload(), "latest", "training_config", "grpo_config", "scheduler")
    assert "training_config.grpo_config.scheduler: relative reference 'latest' is not anchored" in (
        validate_reproducibility(p).errors
    )


def test_the_repro_field_itself_is_not_scanned():
    p = _with(batch_payload(), {"errors": ["x: relative reference 'latest'"], "warnings": []}, "repro")
    assert validate_reproducibility(p).errors == ()


def test_outcome_rule_keywords_are_not_scanned():
    # reward.outcome_rules 是结局匹配关键词,不是引用(Ruling 3 的例外)
    p = _with(batch_payload(), {"outcome": ["best", "current", "previous"]}, "training_config", "reward", "outcome_rules")
    assert validate_reproducibility(p).errors == ()


def test_the_rest_of_the_reward_is_still_scanned():
    p = _with(batch_payload(), {"outcome": ["best"]}, "training_config", "reward", "outcome_rules")
    p["training_config"]["reward"]["combination"] = "latest"
    assert validate_reproducibility(p).errors == (
        "training_config.reward.combination: relative reference 'latest' is not anchored",
    )


def test_stage_keys_of_mixed_types_do_not_raise():
    p = batch_payload()
    p["stage_manifests"][3] = {"id": "dsm-x", "manifest_hash": "d" * 64}
    p["training_config"]["stage_manifest_refs"][7] = "dsm-y"
    errors = validate_reproducibility(p).errors
    assert "stage_manifests.7: training_config names it but it is not anchored" in errors


@pytest.mark.parametrize("bad", [None, "", "rl", 3])
def test_an_unknown_recipe_type_is_an_error(bad):
    p = _with(batch_payload(), bad, "recipe_type")
    assert validate_reproducibility(p).errors[0].startswith("recipe_type: must be one of")


def test_a_payload_that_is_not_a_mapping_is_an_error():
    assert validate_reproducibility(["x"]).errors == ("payload: must be a mapping",)


def test_validation_does_not_mutate_its_input():
    p = batch_payload()
    snapshot = copy.deepcopy(p)
    validate_reproducibility(p)
    assert p == snapshot


def test_the_report_serialises_as_lists():
    assert ReproReport(errors=("a",), warnings=("b",)).to_dict() == {"errors": ["a"], "warnings": ["b"]}
