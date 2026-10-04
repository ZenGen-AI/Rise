import dataclasses
import json

import pytest
import yaml

from controlplane.envelope import BigRef, Envelope, OwnerRef
from mindtrainer.training_config import (
    InvalidTrainingConfigError, RewardConfig, TrainingConfig, config_hash,
    ensure_valid_config, load_config_from_directive, phase_at, total_steps,
    validate_training_config,
)
from simulator.datamodel import RoleBinding


def _ref(ch: str) -> BigRef:
    return BigRef(uri=f"file:///ckpt/{ch}", sha256=ch * 64, size_bytes=1)


def _reward(**kw) -> RewardConfig:
    base = dict(
        components=["goal", "norm"], attribution="outcome_split", combination="sum",
        outcome_rules={"goal": ["agreed"], "norm": ["please"]}, gamma=0.9,
    )
    base.update(kw)
    return RewardConfig(**base)


def _judge_reward(**kw) -> RewardConfig:
    base = dict(
        components=["goal"], attribution="judge", combination="product",
        judge_model_id="external-judge", rubric_version="rubric-v1",
    )
    base.update(kw)
    return RewardConfig(**base)


def _trained() -> RoleBinding:
    return RoleBinding("a", "pa", "self_play")


def _opponent() -> RoleBinding:
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _interact(**kw) -> TrainingConfig:
    base = dict(
        mode="interact", trained_slot="a",
        base_checkpoint_ref=_ref("a"), teacher_model_ref=_ref("b"),
        reward=_reward(),
        sft_steps=0, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=3,
        grpo_config={"learning_rate": 1e-5, "clip": {"low": 0.8, "high": 1.2}},
        opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        scenario_ids=["s1", "s2"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=6,
    )
    base.update(kw)
    return TrainingConfig(**base)


def _batch(**kw) -> TrainingConfig:
    base = dict(
        mode="batch", trained_slot="a",
        base_checkpoint_ref=_ref("a"), teacher_model_ref=_ref("b"),
        reward=_reward(),
        sft_steps=2, grpo_steps=2, sft_batch_size=2, groups_per_step=2, group_size=3,
        grpo_config={"learning_rate": 1e-5}, opd_coefficient=0.1,
        checkpoint_every_steps=2, allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


# ---------------- 合法配置 ----------------

def test_valid_interact_and_batch_configs_pass():
    assert validate_training_config(_interact()) is None
    assert validate_training_config(_batch()) is None


def test_judge_reward_with_model_and_rubric_passes():
    assert validate_training_config(_interact(reward=_judge_reward())) is None


def test_batch_mode_ignores_the_interact_only_fields():
    cfg = _batch(scenario_ids=[], bindings=[], max_turns_per_episode=0)
    assert validate_training_config(cfg) is None


def test_sft_only_batch_needs_no_grpo_manifest():
    cfg = _batch(sft_steps=3, grpo_steps=0, stage_manifest_refs={"sft": "dsm-run-1-sft"})
    assert validate_training_config(cfg) is None


# ---------------- 违规配置:每条一个独立用例 ----------------

_a_frozen = RoleBinding("a", "pa", "frozen_reference", model_ref="x")

INVALID = [
    ("bad_mode", lambda: _interact(mode="online"), "mode must be one of"),
    ("empty_trained_slot", lambda: _interact(trained_slot=""), "trained_slot must be non-empty"),
    ("negative_sft", lambda: _interact(sft_steps=-1), "sft_steps must be >= 0"),
    ("negative_grpo", lambda: _interact(grpo_steps=-1), "grpo_steps must be >= 0"),
    ("no_steps", lambda: _interact(sft_steps=0, grpo_steps=0), "at least one training step"),
    ("sft_batch_zero", lambda: _interact(sft_batch_size=0), "sft_batch_size must be >= 1"),
    ("groups_per_step_zero", lambda: _interact(groups_per_step=0), "groups_per_step must be >= 1"),
    ("min_group_size_one", lambda: _interact(min_group_size=1), "min_group_size must be >= 2"),
    ("group_below_min", lambda: _interact(group_size=2, min_group_size=3), "is below min_group_size"),
    ("checkpoint_every_zero", lambda: _interact(checkpoint_every_steps=0), "checkpoint_every_steps must be >= 1"),
    ("opd_negative", lambda: _interact(opd_coefficient=-0.1), "opd_coefficient must be"),
    ("opd_nan", lambda: _interact(opd_coefficient=float("nan")), "opd_coefficient must be"),
    ("opd_inf", lambda: _interact(opd_coefficient=float("inf")), "opd_coefficient must be"),
    ("negative_retries", lambda: _interact(max_retries_per_episode=-1), "max_retries_per_episode must be >= 0"),
    ("negative_resume_attempts", lambda: _interact(max_resume_attempts=-1), "max_resume_attempts must be >= 0"),
    ("base_sha_not_hex", lambda: _interact(base_checkpoint_ref=BigRef("u", "xyz", 1)), "base_checkpoint_ref.sha256"),
    ("teacher_sha_uppercase", lambda: _interact(teacher_model_ref=BigRef("u", "A" * 64, 1)), "teacher_model_ref.sha256"),
    ("unknown_stage", lambda: _interact(stage_manifest_refs={"sftt": "x"}), "unknown stage"),
    ("sft_without_manifest", lambda: _interact(sft_steps=2, stage_manifest_refs={}), "stage_manifest_refs['sft']"),
    ("batch_grpo_without_manifest", lambda: _batch(stage_manifest_refs={"sft": "m"}), "stage_manifest_refs['grpo_opd']"),
    # interact 专用
    ("no_scenarios", lambda: _interact(scenario_ids=[]), "scenario_ids must be non-empty and unique"),
    ("dup_scenarios", lambda: _interact(scenario_ids=["s1", "s1"]), "scenario_ids must be non-empty and unique"),
    ("groups_exceed_scenarios", lambda: _interact(groups_per_step=3), "exceeds number of scenario_ids"),
    ("max_turns_zero", lambda: _interact(max_turns_per_episode=0), "max_turns_per_episode must be >= 1"),
    ("no_bindings", lambda: _interact(bindings=[]), "bindings must be non-empty"),
    ("dup_slot", lambda: _interact(bindings=[_trained(), _trained()]), "duplicate slot_id"),
    ("trained_not_bound", lambda: _interact(trained_slot="ghost"), "is not among bound slots"),
    ("trained_not_self_play", lambda: _interact(bindings=[_a_frozen, _opponent()]), "must be self_play"),
    ("trained_has_ref", lambda: _interact(bindings=[RoleBinding("a", "pa", "self_play", model_ref="x"), _opponent()]), "must not carry model_ref"),
    ("opp_self_play", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "self_play")]), "must not be self_play"),
    ("opp_no_ref", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference")]), "needs a non-empty model_ref"),
    ("opp_empty_ref", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference", model_ref="")]), "needs a non-empty model_ref"),
    ("opp_unknown_backend", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "magic", model_ref="x")]), "frozen_reference or external_api"),
    # 奖励
    ("no_components", lambda: _interact(reward=_reward(components=[], outcome_rules={})), "reward.components must be non-empty and unique"),
    ("dup_components", lambda: _interact(reward=_reward(components=["goal", "goal"])), "reward.components must be non-empty and unique"),
    ("bad_attribution", lambda: _interact(reward=_reward(attribution="vibes")), "reward.attribution must be one of"),
    ("bad_combination", lambda: _interact(reward=_reward(combination="avg")), "reward.combination must be one of"),
    ("rules_missing_component", lambda: _interact(reward=_reward(outcome_rules={"goal": ["x"]})), "keys must equal components"),
    ("rules_extra_component", lambda: _interact(reward=_reward(outcome_rules={"goal": ["x"], "norm": ["y"], "extra": ["z"]})), "keys must equal components"),
    ("rules_empty_keywords", lambda: _interact(reward=_reward(outcome_rules={"goal": [], "norm": ["y"]})), "needs a non-empty keyword list"),
    ("rules_blank_keyword", lambda: _interact(reward=_reward(outcome_rules={"goal": [""], "norm": ["y"]})), "needs a non-empty keyword list"),
    ("gamma_zero", lambda: _interact(reward=_reward(gamma=0.0)), "reward.gamma must be in (0, 1]"),
    ("gamma_over_one", lambda: _interact(reward=_reward(gamma=1.5)), "reward.gamma must be in (0, 1]"),
    ("gamma_nan", lambda: _interact(reward=_reward(gamma=float("nan"))), "reward.gamma must be in (0, 1]"),
    ("judge_missing_model", lambda: _interact(reward=_judge_reward(judge_model_id=None)), "reward.judge_model_id is required"),
    ("judge_missing_rubric", lambda: _interact(reward=_judge_reward(rubric_version=None)), "reward.rubric_version is required"),
]


@pytest.mark.parametrize("build,needle", [(b, n) for _, b, n in INVALID], ids=[i for i, _, _ in INVALID])
def test_invalid_config_is_rejected_with_the_specific_reason(build, needle):
    reason = validate_training_config(build())
    assert reason is not None, "应当被拒绝"
    assert needle in reason, reason


def test_judge_fields_are_not_required_for_outcome_split():
    assert validate_training_config(_interact(reward=_reward(judge_model_id=None, rubric_version=None))) is None


def test_ensure_valid_config_returns_the_config_or_raises_with_the_reason():
    cfg = _interact()
    assert ensure_valid_config(cfg) is cfg
    with pytest.raises(InvalidTrainingConfigError, match="trained_slot 'ghost'"):
        ensure_valid_config(_interact(trained_slot="ghost"))


# ---------------- config_hash ----------------

def test_config_hash_is_64_hex_and_stable():
    h = config_hash(_interact())
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == config_hash(_interact())


def test_config_hash_ignores_dict_key_order():
    one = _interact(grpo_config={"a": 1, "b": 2}, reward=_reward(outcome_rules={"goal": ["x"], "norm": ["y"]}))
    two = _interact(grpo_config={"b": 2, "a": 1}, reward=_reward(outcome_rules={"norm": ["y"], "goal": ["x"]}))
    assert config_hash(one) == config_hash(two)


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _interact(opd_coefficient=0.2),
        lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v2")]),
        lambda: _interact(reward=_reward(gamma=0.8)),
        lambda: _interact(teacher_model_ref=_ref("c")),
        lambda: _interact(scenario_ids=["s2", "s1"]),
        lambda: _interact(stage_manifest_refs={"sft": "other"}),
    ],
    ids=["opd", "opponent_model_ref", "gamma", "teacher", "scenario_order", "manifests"],
)
def test_config_hash_changes_when_anything_that_matters_changes(changed):
    assert config_hash(changed()) != config_hash(_interact())


# ---------------- 阶段 ----------------

def test_phase_at_switches_exactly_at_sft_steps():
    cfg = _batch(sft_steps=2, grpo_steps=2)
    assert total_steps(cfg) == 4
    assert [phase_at(s, cfg) for s in range(4)] == ["sft", "sft", "grpo_opd", "grpo_opd"]


def test_phase_at_with_no_sft_starts_in_grpo():
    cfg = _interact(sft_steps=0, grpo_steps=3)
    assert [phase_at(s, cfg) for s in range(3)] == ["grpo_opd"] * 3


@pytest.mark.parametrize("step", [-1, 4])
def test_phase_at_rejects_out_of_range_steps(step):
    with pytest.raises(ValueError, match="out of range"):
        phase_at(step, _batch(sft_steps=2, grpo_steps=2))


# ---------------- 序列化 ----------------

@pytest.mark.parametrize("build", [_interact, _batch, lambda: _interact(reward=_judge_reward())])
def test_to_dict_round_trip(build):
    cfg = build()
    assert TrainingConfig.from_dict(cfg.to_dict()) == cfg


def test_to_dict_is_plain_data_that_survives_json_and_yaml():
    cfg = _interact()
    d = cfg.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d
    assert TrainingConfig.from_dict(yaml.safe_load(yaml.safe_dump(d))) == cfg


def test_optional_keys_fall_back_to_defaults():
    d = _interact().to_dict()
    for key in ("max_retries_per_episode", "min_group_size", "max_resume_attempts"):
        del d[key]
    cfg = TrainingConfig.from_dict(d)
    assert (cfg.max_retries_per_episode, cfg.min_group_size, cfg.max_resume_attempts) == (2, 2, 3)


def test_to_dict_does_not_alias_mutable_state():
    cfg = _interact()
    cfg.to_dict()["grpo_config"]["clip"]["low"] = 999
    assert cfg.grpo_config["clip"]["low"] == 0.8


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _interact().mode = "batch"


# ---------------- 从 directive 读取 ----------------

def _directive(payload):
    return Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-03T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )


def test_load_config_from_directive_reads_the_training_config_key():
    cfg = _interact()
    env = _directive({"target_dimension": "tom", "recipe_ref": "", "training_config": cfg.to_dict()})
    assert load_config_from_directive(env) == cfg


def test_directive_without_training_config_is_rejected():
    with pytest.raises(InvalidTrainingConfigError, match="training_config"):
        load_config_from_directive(_directive({"target_dimension": "tom"}))


def test_directive_with_a_missing_field_names_the_field():
    d = _interact().to_dict()
    del d["opd_coefficient"]
    with pytest.raises(InvalidTrainingConfigError, match="opd_coefficient"):
        load_config_from_directive(_directive({"training_config": d}))


# ---------------- F1:字段类型校验 ----------------
# 下半段编排器靠异常类型区分"确定性失败写 failed"和"其余保持 pending 等续训",
# 类型错误绝不能以裸 TypeError 或静默通过的形式逃出 validate_training_config。

TYPE_INVALID = [
    ("mode_not_str", lambda: _interact(mode=123), "mode"),
    ("trained_slot_not_str", lambda: _interact(trained_slot=123), "trained_slot"),
    ("sft_steps_is_a_string", lambda: _interact(sft_steps="3"), "sft_steps"),
    ("sft_steps_is_a_bool", lambda: _interact(sft_steps=True), "sft_steps"),
    ("grpo_steps_is_a_float", lambda: _interact(grpo_steps=2.5), "grpo_steps"),
    ("sft_batch_size_is_a_string", lambda: _interact(sft_batch_size="2"), "sft_batch_size"),
    ("groups_per_step_is_a_bool", lambda: _interact(groups_per_step=True), "groups_per_step"),
    ("group_size_is_a_float", lambda: _interact(group_size=2.5), "group_size"),
    ("checkpoint_every_steps_is_a_float", lambda: _interact(checkpoint_every_steps=1.5), "checkpoint_every_steps"),
    ("max_turns_per_episode_is_a_bool", lambda: _interact(max_turns_per_episode=True), "max_turns_per_episode"),
    ("max_retries_per_episode_is_a_bool", lambda: _interact(max_retries_per_episode=True), "max_retries_per_episode"),
    ("min_group_size_is_a_bool", lambda: _interact(min_group_size=True), "min_group_size"),
    ("max_resume_attempts_is_a_bool", lambda: _interact(max_resume_attempts=True), "max_resume_attempts"),
    ("allow_resume_is_the_string_false", lambda: _interact(allow_resume="false"), "allow_resume"),
    ("allow_resume_is_an_int", lambda: _interact(allow_resume=1), "allow_resume"),
    ("opd_coefficient_is_a_string", lambda: _interact(opd_coefficient="0.1"), "opd_coefficient"),
    ("opd_coefficient_is_a_bool", lambda: _interact(opd_coefficient=True), "opd_coefficient"),
    ("gamma_is_a_string", lambda: _interact(reward=_reward(gamma="0.9")), "reward.gamma"),
    ("gamma_is_a_bool", lambda: _interact(reward=_reward(gamma=True)), "reward.gamma"),
    ("grpo_config_is_not_a_dict", lambda: _interact(grpo_config=["lr"]), "grpo_config"),
    ("grpo_config_has_an_unserializable_value", lambda: _interact(grpo_config={"x": {1, 2, 3}}), "grpo_config"),
    ("base_checkpoint_sha256_is_not_a_string", lambda: _interact(base_checkpoint_ref=BigRef("u", 123, 1)), "base_checkpoint_ref.sha256"),
    ("teacher_sha256_is_not_a_string", lambda: _interact(teacher_model_ref=BigRef("u", 123, 1)), "teacher_model_ref.sha256"),
    ("scenario_ids_is_not_a_list", lambda: _interact(scenario_ids="s1"), "scenario_ids"),
    ("scenario_ids_has_a_non_string_entry", lambda: _interact(scenario_ids=["s1", 2]), "scenario_ids"),
    ("scenario_ids_has_an_empty_string_entry", lambda: _interact(scenario_ids=["s1", ""]), "scenario_ids"),
    ("stage_manifest_refs_is_not_a_dict", lambda: _interact(stage_manifest_refs=["sft"]), "stage_manifest_refs"),
]


@pytest.mark.parametrize(
    "build,field", [(b, f) for _, b, f in TYPE_INVALID], ids=[i for i, _, _ in TYPE_INVALID],
)
def test_wrong_field_types_are_rejected_with_a_reason_not_an_exception(build, field):
    reason = validate_training_config(build())
    assert reason is not None, "应当被拒绝"
    assert field in reason, reason
    assert "must be" in reason, reason


def test_sft_steps_as_a_string_does_not_raise_type_error():
    # 曾经的裸 TypeError 来源:字符串和整数比较(cfg.sft_steps < 0)
    cfg = _interact(sft_steps="3")
    reason = validate_training_config(cfg)
    assert reason is not None and "sft_steps" in reason


def test_opd_coefficient_as_a_string_does_not_raise_type_error():
    # 曾经的裸 TypeError 来源:math.isfinite(str)
    cfg = _interact(opd_coefficient="0.1")
    reason = validate_training_config(cfg)
    assert reason is not None and "opd_coefficient" in reason


def test_ensure_valid_config_raises_invalid_training_config_error_not_type_error_for_bad_types():
    with pytest.raises(InvalidTrainingConfigError, match="sft_steps"):
        ensure_valid_config(_interact(sft_steps="3"))


# ---------------- F1:config_hash 遇到不可序列化字段 ----------------

def test_config_hash_rejects_unserializable_grpo_config_with_a_named_error_not_a_bare_type_error():
    cfg = _interact(grpo_config={"x": {1, 2, 3}})
    with pytest.raises(InvalidTrainingConfigError, match="grpo_config"):
        config_hash(cfg)


# ---------------- F1:load_config_from_directive 遇到错误类型的 training_config ----------------

@pytest.mark.parametrize(
    "bad_training_config",
    [["a", "b"], "oops", 42],
    ids=["list", "str", "int"],
)
def test_directive_with_a_non_dict_training_config_is_rejected_not_a_bare_type_error(bad_training_config):
    with pytest.raises(InvalidTrainingConfigError, match="training_config"):
        load_config_from_directive(_directive({"training_config": bad_training_config}))
