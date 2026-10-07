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


# ---------------- 配置类型缺口:剩余字段的类型错误也必须返回原因,不能抛、不能静默通过 ----------------

_CIRCULAR: dict = {}
_CIRCULAR["self"] = _CIRCULAR


def _bad_ref(**kw) -> BigRef:
    base = dict(uri="file:///ckpt/x", sha256="a" * 64, size_bytes=1, fetch_hint=None)
    base.update(kw)
    return BigRef(**base)


TYPE_GAPS = [
    ("components_is_an_int", lambda: _interact(reward=_reward(components=5)), "reward.components"),
    ("components_is_a_string", lambda: _interact(reward=_reward(components="goal")), "reward.components"),
    ("components_has_a_non_string_entry",
     lambda: _interact(reward=_reward(components=["goal", 5], outcome_rules={"goal": ["a"]})), "reward.components"),
    ("outcome_rules_is_a_list",
     lambda: _interact(reward=_reward(outcome_rules=["goal", "norm"])), "reward.outcome_rules"),
    ("outcome_rules_value_is_a_string",
     lambda: _interact(reward=_reward(outcome_rules={"goal": "agreed", "norm": ["please"]})), "reward.outcome_rules"),
    ("outcome_rules_keyword_is_an_int",
     lambda: _interact(reward=_reward(outcome_rules={"goal": [1], "norm": ["please"]})), "reward.outcome_rules"),
    ("outcome_rules_key_is_an_int",
     lambda: _interact(reward=_reward(components=["goal"], outcome_rules={5: ["x"]})), "reward.outcome_rules"),
    ("judge_model_id_is_an_int", lambda: _interact(reward=_reward(judge_model_id=5)), "reward.judge_model_id"),
    ("rubric_version_is_an_int", lambda: _interact(reward=_reward(rubric_version=5)), "reward.rubric_version"),
    ("reward_is_none", lambda: _interact(reward=None), "reward"),
    ("reward_is_a_dict", lambda: _interact(reward={"components": ["goal"]}), "reward"),
    ("bindings_is_an_int", lambda: _interact(bindings=5), "bindings"),
    ("bindings_has_a_non_binding_entry", lambda: _interact(bindings=[_trained(), 5]), "bindings"),
    ("binding_slot_id_is_an_int",
     lambda: _interact(bindings=[RoleBinding(5, "pa", "self_play"), _opponent()]), "bindings"),
    ("binding_model_ref_is_an_int",
     lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference", model_ref=5)]), "bindings"),
    ("base_checkpoint_ref_is_a_string", lambda: _interact(base_checkpoint_ref="base"), "base_checkpoint_ref"),
    ("teacher_model_ref_is_none", lambda: _interact(teacher_model_ref=None), "teacher_model_ref"),
    ("ref_uri_is_an_int", lambda: _interact(base_checkpoint_ref=_bad_ref(uri=5)), "base_checkpoint_ref.uri"),
    ("ref_size_bytes_is_a_string",
     lambda: _interact(teacher_model_ref=_bad_ref(size_bytes="1")), "teacher_model_ref.size_bytes"),
    ("ref_size_bytes_is_a_bool",
     lambda: _interact(base_checkpoint_ref=_bad_ref(size_bytes=True)), "base_checkpoint_ref.size_bytes"),
    ("ref_fetch_hint_is_a_string",
     lambda: _interact(base_checkpoint_ref=_bad_ref(fetch_hint="x")), "base_checkpoint_ref.fetch_hint"),
    ("stage_manifest_refs_value_is_an_int",
     lambda: _batch(stage_manifest_refs={"sft": "a", "grpo_opd": 5}), "stage_manifest_refs"),
    ("stage_manifest_refs_key_is_an_int",
     lambda: _batch(stage_manifest_refs={1: "a", "sft": "b"}), "stage_manifest_refs"),
    ("grpo_config_is_circular", lambda: _interact(grpo_config=_CIRCULAR), "grpo_config"),
    ("opd_coefficient_overflows_a_float", lambda: _interact(opd_coefficient=10 ** 400), "opd_coefficient"),
]


@pytest.mark.parametrize(
    "build,field", [(b, f) for _, b, f in TYPE_GAPS], ids=[i for i, _, _ in TYPE_GAPS],
)
def test_the_remaining_wrong_field_types_are_rejected_with_a_reason_and_never_raise(build, field):
    reason = validate_training_config(build())
    assert reason is not None, "应当被拒绝"
    assert field in reason, reason
    assert "must be" in reason, reason


# ---------------- config_hash:序列化不了时点名真正的字段,而且永远是具名异常 ----------------

HASH_FAILS = [
    ("components_is_an_int", lambda: _interact(reward=_reward(components=5)), "reward.components"),
    ("outcome_rules_is_a_list", lambda: _interact(reward=_reward(outcome_rules=["goal"])), "reward.outcome_rules"),
    ("bindings_is_an_int", lambda: _interact(bindings=5), "bindings"),
    ("bindings_has_a_non_binding_entry", lambda: _interact(bindings=[5]), "bindings"),
    ("reward_is_none", lambda: _interact(reward=None), "reward"),
    ("base_checkpoint_ref_is_a_string", lambda: _interact(base_checkpoint_ref="base"), "base_checkpoint_ref"),
    ("grpo_config_is_circular", lambda: _interact(grpo_config=_CIRCULAR), "grpo_config"),
]


@pytest.mark.parametrize(
    "build,field", [(b, f) for _, b, f in HASH_FAILS], ids=[i for i, _, _ in HASH_FAILS],
)
def test_config_hash_names_the_field_that_cannot_be_serialized(build, field):
    with pytest.raises(InvalidTrainingConfigError) as exc:
        config_hash(build())
    message = str(exc.value)
    assert field in message, message
    if field != "grpo_config":
        assert "grpo_config" not in message, message  # 不能把别的字段的毛病栽给 grpo_config


def test_config_hash_still_hashes_a_serializable_config_that_only_fails_validation():
    # 编排器靠它:能算出哈希,才有 training_run 记录可写,配置失败才会留下 failed 记录
    assert len(config_hash(_interact(sft_steps="3"))) == 64


# ---------------- 外部输入(directive):容器类型不能被悄悄转换成别的东西 ----------------

def _load_or_reason(training_config: dict):
    try:
        cfg = load_config_from_directive(_directive({"training_config": training_config}))
    except InvalidTrainingConfigError as exc:
        return str(exc)
    return validate_training_config(cfg)


def _raw(build, mutate) -> dict:
    d = build().to_dict()
    mutate(d)
    return d


EXTERNAL_NAMED = [
    ("scenario_ids_is_a_string", _interact, lambda d: d.__setitem__("scenario_ids", "s1"), "scenario_ids"),
    ("components_is_a_string", _interact, lambda d: d["reward"].__setitem__("components", "goal"), "reward.components"),
    ("components_is_an_int", _interact, lambda d: d["reward"].__setitem__("components", 5), "reward.components"),
    ("outcome_rules_is_a_list", _interact,
     lambda d: d["reward"].__setitem__("outcome_rules", ["goal"]), "reward.outcome_rules"),
    ("outcome_rules_value_is_a_string", _interact,
     lambda d: d["reward"]["outcome_rules"].__setitem__("goal", "agreed"), "reward.outcome_rules"),
    ("stage_manifest_refs_is_a_string", _batch,
     lambda d: d.__setitem__("stage_manifest_refs", "ab"), "stage_manifest_refs"),
    ("stage_manifest_refs_is_a_list_of_pairs", _batch,
     lambda d: d.__setitem__("stage_manifest_refs", [["sft", "x"], ["grpo_opd", "y"]]), "stage_manifest_refs"),
    ("bindings_is_a_dict", _interact, lambda d: d.__setitem__("bindings", {"a": 1}), "bindings"),
]


@pytest.mark.parametrize(
    "build,mutate,field", [(b, m, f) for _, b, m, f in EXTERNAL_NAMED], ids=[i for i, _, _, _ in EXTERNAL_NAMED],
)
def test_a_wrong_container_type_in_a_directive_is_reported_by_field_not_silently_converted(build, mutate, field):
    reason = _load_or_reason(_raw(build, mutate))
    assert reason is not None, "错误类型的输入不能当作合法配置通过"
    assert field in reason, reason


EXTERNAL_REJECTED = [
    ("bindings_has_a_non_object_entry", _interact, lambda d: d.__setitem__("bindings", [5])),
    ("reward_is_none", _interact, lambda d: d.__setitem__("reward", None)),
    ("reward_is_a_list", _interact, lambda d: d.__setitem__("reward", ["goal"])),
    ("base_checkpoint_ref_is_a_string", _interact, lambda d: d.__setitem__("base_checkpoint_ref", "base")),
    ("teacher_model_ref_is_none", _interact, lambda d: d.__setitem__("teacher_model_ref", None)),
]


@pytest.mark.parametrize(
    "build,mutate", [(b, m) for _, b, m in EXTERNAL_REJECTED], ids=[i for i, _, _ in EXTERNAL_REJECTED],
)
def test_a_malformed_object_in_a_directive_is_rejected_with_the_named_error(build, mutate):
    assert _load_or_reason(_raw(build, mutate)) is not None


# ---------------- 变异检查补洞:根对象类型、最终序列化兜底、超大数字、空容器 ----------------

def test_validate_rejects_something_that_is_not_a_training_config_at_all():
    for bogus in (None, {}, "config", 5):
        reason = validate_training_config(bogus)
        assert reason is not None and "TrainingConfig" in reason and "must be" in reason, reason


def test_a_fetch_hint_with_an_unserializable_value_is_caught_by_the_final_serialization_check():
    # fetch_hint 本身是 dict,逐项检查都放行;只有最后一道"整份配置能不能序列化"能抓住它
    cfg = _interact(base_checkpoint_ref=_bad_ref(fetch_hint={"url": {1, 2, 3}}))
    reason = validate_training_config(cfg)
    assert reason is not None and "JSON serializable" in reason and "must be" in reason, reason
    with pytest.raises(InvalidTrainingConfigError, match="JSON serializable"):
        config_hash(cfg)


@pytest.mark.parametrize(
    "build,field",
    [
        (lambda: _interact(opd_coefficient=10 ** 400), "opd_coefficient"),
        (lambda: _interact(reward=_reward(gamma=10 ** 400)), "reward.gamma"),
    ],
    ids=["opd_coefficient", "gamma"],
)
def test_an_absurdly_large_number_does_not_flood_the_failure_reason(build, field):
    reason = validate_training_config(build())
    assert reason is not None and field in reason, reason
    assert len(reason) < 130, len(reason)  # 401 位数字原样写进去会有 400 多个字符


def test_a_number_too_large_to_print_inside_a_container_still_yields_a_reason():
    import sys

    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("this interpreter has no int-to-str digit limit")
    huge = 10 ** (limit + 50)  # repr 这个数会抛 ValueError
    cfg = _interact(reward=_reward(components=[huge], outcome_rules={"goal": ["a"]}))
    reason = validate_training_config(cfg)
    assert reason is not None and "reward.components" in reason, reason


EXTERNAL_EMPTY = [
    ("bindings_is_an_empty_dict", _batch, lambda d: d.__setitem__("bindings", {}), "bindings"),
    ("scenario_ids_is_an_empty_dict", _batch, lambda d: d.__setitem__("scenario_ids", {}), "scenario_ids"),
    ("stage_manifest_refs_is_an_empty_list", _interact,
     lambda d: d.__setitem__("stage_manifest_refs", []), "stage_manifest_refs"),
]


@pytest.mark.parametrize(
    "build,mutate,field", [(b, m, f) for _, b, m, f in EXTERNAL_EMPTY], ids=[i for i, _, _, _ in EXTERNAL_EMPTY],
)
def test_an_empty_container_of_the_wrong_type_in_a_directive_is_not_silently_converted(build, mutate, field):
    reason = _load_or_reason(_raw(build, mutate))
    assert reason is not None, "空容器也不能因为类型不对被悄悄转成合法的空值"
    assert field in reason, reason


# ---------------- 评审补洞:validate_training_config 与 config_hash 对"能不能序列化"必须判得一致 ----------------

class _BadRepr:
    def __repr__(self):
        raise ZeroDivisionError("repr is broken")


HASH_ONLY_FAILS = [
    # 不排序键能序列化、排序键不能:config_hash 排序,所以校验也必须排序
    ("grpo_config_keys_cannot_be_sorted", lambda: _interact(grpo_config={1: "a", "b": 2}), "grpo_config"),
    # 孤立代理字符:json.dumps 能写出来,编码成 UTF-8 才失败
    ("grpo_config_has_a_lone_surrogate", lambda: _interact(grpo_config={"x": "\ud800"}), "grpo_config"),
    ("fetch_hint_keys_cannot_be_sorted",
     lambda: _interact(base_checkpoint_ref=_bad_ref(fetch_hint={1: "a", "b": 2})), "JSON serializable"),
    ("trained_slot_has_a_lone_surrogate", lambda: _batch(trained_slot="\ud800"), "JSON serializable"),
]


@pytest.mark.parametrize(
    "build,needle", [(b, n) for _, b, n in HASH_ONLY_FAILS], ids=[i for i, _, _ in HASH_ONLY_FAILS],
)
def test_a_config_that_cannot_be_hashed_is_rejected_by_validate_with_the_same_reason(build, needle):
    cfg = build()
    with pytest.raises(InvalidTrainingConfigError) as exc:
        config_hash(cfg)
    reason = validate_training_config(cfg)
    assert reason is not None, "config_hash 抛了,validate 却放行"
    assert str(exc.value) == reason
    assert needle in reason, reason
    assert "must be" in reason, reason


def test_a_value_whose_repr_raises_still_yields_a_reason():
    cfg = _interact(reward=_reward(components=[_BadRepr()], outcome_rules={"goal": ["a"]}))
    reason = validate_training_config(cfg)
    assert reason is not None and "reward.components" in reason, reason


# ---------------- 哈希公式:已落盘的 training_run 与候选里存着旧哈希,公式一变,续训就会被判成"配置变了" ----------------

def test_the_canonical_form_is_sorted_compact_utf8_and_keeps_non_ascii_as_is():  # CANONICAL_FORM
    from mindtrainer.training_config import _canonical_json

    assert _canonical_json({"b": 1, "a": "é", "c": [1, 2]}) == b'{"a":"\xc3\xa9","b":1,"c":[1,2]}'


@pytest.mark.parametrize(
    "build", [_interact, _batch, lambda: _batch(trained_slot="角色")], ids=["interact", "batch", "non_ascii"],
)
def test_config_hash_keeps_the_formula_existing_records_were_hashed_with(build):
    import hashlib

    cfg = build()
    legacy = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert config_hash(cfg) == hashlib.sha256(legacy.encode("utf-8")).hexdigest()
