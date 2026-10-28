"""scheduler.yaml 与 training_defaults.yaml:默认值、严格解析、信封读写、安装不覆盖。"""
import copy

import pytest

from controlplane.config import read_config, write_config
from controlplane.paths import scheduler_config_file, training_defaults_file
from mindtrainer.data_spec import required_stages
from mindtrainer.training_config import TrainingConfig, validate_training_config
from trainscheduler.config import (
    DEFAULT_DATA_SPEC,
    DEFAULT_SCHEDULER_CONFIG,
    DEFAULT_TRAINING_CONFIG,
    SchedulerConfig,
    check_perturbable_key,
    default_training_defaults_payload,
    install_default_configs,
    load_scheduler_config,
    load_training_defaults,
    parse_scheduler_config,
    parse_training_defaults,
)
from trainscheduler.errors import SchedulerConfigError, SchedulerError
from ts_fixtures import make_store


def test_scheduler_defaults():
    c = DEFAULT_SCHEDULER_CONFIG
    assert (c.confirm_directives, c.allow_interact, c.accept_mock_evidence) == (True, False, False)
    assert c.perturbable == ("opd_coefficient", "sft_steps", "grpo_steps", "grpo_config.learning_rate")
    assert (c.min_gain, c.cost_gain_window, c.recent_decision_window, c.max_plan_attempts) == (0.01, 5, 5, 3)
    assert c.template_version == "v1"


def test_an_empty_mapping_parses_to_the_defaults_and_to_dict_round_trips():
    assert parse_scheduler_config({}) == DEFAULT_SCHEDULER_CONFIG
    assert parse_scheduler_config(DEFAULT_SCHEDULER_CONFIG.to_dict()) == DEFAULT_SCHEDULER_CONFIG


def test_given_keys_override_the_defaults():
    c = parse_scheduler_config({"confirm_directives": False, "min_gain": 0, "perturbable": ["reward.gamma"]})
    assert (c.confirm_directives, c.min_gain, c.perturbable) == (False, 0.0, ("reward.gamma",))
    assert isinstance(c.min_gain, float)


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"confirm_directive": True}, "unknown key"),
        ({"confirm_directives": "yes"}, "scheduler.confirm_directives must be a bool"),
        ({"allow_interact": 1}, "scheduler.allow_interact must be a bool"),
        ({"accept_mock_evidence": None}, "scheduler.accept_mock_evidence must be a bool"),
        ({"min_gain": "0.01"}, "scheduler.min_gain must be a finite number"),
        ({"min_gain": True}, "scheduler.min_gain must be a finite number"),
        ({"min_gain": float("nan")}, "scheduler.min_gain must be a finite number"),
        ({"cost_gain_window": 0}, "scheduler.cost_gain_window must be in [1, 100]"),
        ({"cost_gain_window": 2.0}, "scheduler.cost_gain_window must be an int"),
        ({"recent_decision_window": 101}, "scheduler.recent_decision_window must be in [1, 100]"),
        ({"max_plan_attempts": 0}, "scheduler.max_plan_attempts must be in [1, 10]"),
        ({"max_plan_attempts": True}, "scheduler.max_plan_attempts must be an int"),
        ({"template_version": ""}, "scheduler.template_version must be a non-empty str"),
        ({"perturbable": "opd_coefficient"}, "scheduler.perturbable must be a list"),
        ({"perturbable": ["opd_coefficient", "opd_coefficient"]}, "duplicate"),
        ({"perturbable": ["nope"]}, "not a training_config field"),
        ({"perturbable": ["grpo_config..lr"]}, "dotted path"),
        ({"perturbable": [3]}, "dotted path"),
        ({"perturbable": ["sft_steps.x"]}, "can be descended into"),
        ({"perturbable": ["base_checkpoint_ref"]}, "never perturbable"),
        ({"perturbable": ["mode"]}, "never perturbable"),
        ({"perturbable": ["stage_manifest_refs"]}, "never perturbable"),
    ],
)
def test_bad_scheduler_config_names_the_field(raw, fragment):
    with pytest.raises(SchedulerConfigError, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        parse_scheduler_config(raw)


def test_a_non_mapping_is_rejected():
    with pytest.raises(SchedulerConfigError, match="must be a mapping"):
        parse_scheduler_config(["confirm_directives"])


def test_config_errors_are_not_scheduler_errors():
    # 配置错不该把某个 run 的规划永久判 failed:处理器把 SchedulerError 记成 T3,SchedulerConfigError 原样上抛
    assert not issubclass(SchedulerConfigError, SchedulerError)


@pytest.mark.parametrize("key", ["grpo_config.learning_rate", "grpo_config.anything", "reward.gamma",
                                 "opd_coefficient", "max_resume_attempts"])
def test_accepted_perturbable_keys(key):
    assert check_perturbable_key(key) is None


def test_the_default_training_config_validates_with_per_run_manifest_ids():
    cfg = TrainingConfig.from_dict({**DEFAULT_TRAINING_CONFIG,
                                    "stage_manifest_refs": {"sft": "dsm-run-001-sft", "grpo_opd": "dsm-run-001-grpo_opd"}})
    assert validate_training_config(cfg) is None
    assert required_stages(cfg) == ["sft", "grpo_opd"]


def test_the_default_training_defaults_parse():
    d = parse_training_defaults(default_training_defaults_payload())
    assert d.training_config == DEFAULT_TRAINING_CONFIG
    assert d.data_spec.to_dict() == DEFAULT_DATA_SPEC
    assert d.to_dict() == default_training_defaults_payload()


def _defaults(**cfg_changes):
    raw = default_training_defaults_payload()
    raw["training_config"].update(cfg_changes)
    return raw


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"training_config": {}}, "missing key"),
        ({**default_training_defaults_payload(), "extra": 1}, "unknown key"),
        ({"training_config": [], "data_spec": DEFAULT_DATA_SPEC}, "training_config must be a mapping"),
        (_defaults(stage_manifest_refs={"sft": "dsm-run-001-sft"}), "stage_manifest_refs must not be set"),
        (_defaults(learning_rate=1e-5), "unknown field"),
        (_defaults(mode="interact"), "mode must be 'batch'"),
        (_defaults(sft_steps="2"), "sft_steps must be an int"),
        (_defaults(group_size=1), "group_size 1 is below min_group_size 2"),
        (_defaults(reward={"components": ["goal"], "attribution": "judge", "combination": "sum"}),
         "judge_model_id is required"),
    ],
)
def test_bad_training_defaults_name_the_problem(raw, fragment):
    with pytest.raises(SchedulerConfigError, match=fragment):
        parse_training_defaults(raw)


def test_a_missing_required_field_is_named():
    raw = default_training_defaults_payload()
    del raw["training_config"]["teacher_model_ref"]
    with pytest.raises(SchedulerConfigError, match="missing field 'teacher_model_ref'"):
        parse_training_defaults(raw)


def test_a_bad_data_spec_is_named():
    raw = default_training_defaults_payload()
    raw["data_spec"]["scenario_count"] = "4"
    with pytest.raises(SchedulerConfigError, match="training_defaults.data_spec.scenario_count must be an int"):
        parse_training_defaults(raw)


def test_a_data_spec_too_small_for_the_config_is_rejected():
    raw = default_training_defaults_payload()
    raw["data_spec"]["scenario_count"] = 1  # groups_per_step 是 2
    with pytest.raises(SchedulerConfigError, match="below groups_per_step 2"):
        parse_training_defaults(raw)


def test_parse_does_not_alias_the_input():
    raw = default_training_defaults_payload()
    d = parse_training_defaults(raw)
    raw["training_config"]["sft_steps"] = 99
    assert d.training_config["sft_steps"] == 2


def test_install_writes_both_config_envelopes(tmp_path):
    store = make_store(tmp_path)
    assert install_default_configs(store) == [scheduler_config_file(), training_defaults_file()]
    for rel in (scheduler_config_file(), training_defaults_file()):
        e = store.read_product(rel)
        assert (e.type, e.status) == ("config", "effective")
    assert load_scheduler_config(store) == DEFAULT_SCHEDULER_CONFIG
    assert load_training_defaults(store).training_config == DEFAULT_TRAINING_CONFIG


def test_install_never_overwrites_a_config_a_human_changed(tmp_path):
    store = make_store(tmp_path)
    write_config(store, scheduler_config_file(), "scheduler-config", {"confirm_directives": False}, "human")
    assert install_default_configs(store) == [training_defaults_file()]
    assert read_config(store, scheduler_config_file()) == {"confirm_directives": False}
    assert install_default_configs(store) == []
    assert load_scheduler_config(store).confirm_directives is False


def test_a_missing_scheduler_config_means_the_defaults(tmp_path):
    assert load_scheduler_config(make_store(tmp_path)) == DEFAULT_SCHEDULER_CONFIG


def test_a_missing_training_defaults_is_an_error_that_says_how_to_fix_it(tmp_path):
    with pytest.raises(SchedulerConfigError, match="install_default_configs"):
        load_training_defaults(make_store(tmp_path))


def test_load_parses_strictly(tmp_path):
    store = make_store(tmp_path)
    write_config(store, scheduler_config_file(), "scheduler-config", {"min_gain": "high"}, "human")
    with pytest.raises(SchedulerConfigError, match="scheduler.min_gain"):
        load_scheduler_config(store)


def test_the_config_dataclass_is_frozen():
    with pytest.raises(Exception):
        SchedulerConfig().min_gain = 0.5  # type: ignore[misc]


def test_the_defaults_dict_is_not_mutated_by_callers():
    before = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    payload = default_training_defaults_payload()
    payload["training_config"]["reward"]["components"].append("x")
    assert DEFAULT_TRAINING_CONFIG == before


# ---- interact 与 opponent 段(都可选;缺省时文件与今天逐字节相同) ----


def _defaults_with_interact():
    """点名路的 interact 段:两个角色位,两个情境,人格按角色位给。每次返回新的深拷贝。"""
    raw = default_training_defaults_payload()
    raw["interact"] = {
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
    return raw


def _opponent():
    return {"base_url": "https://api.example.com", "model": "opp-1", "api_key_env": "FORGE_OPP_KEY"}


def test_interact_section_parses_two_sources_and_rejects_halves():
    full = {"training_config": copy.deepcopy(DEFAULT_TRAINING_CONFIG),
            "data_spec": copy.deepcopy(DEFAULT_DATA_SPEC),
            "interact": {"trained_slot": "a",
                         "slots": [{"slot_id": "a", "model_backend": "self_play"},
                                   {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1"}],
                         "max_turns_per_episode": 4, "memory": {"seed": None},
                         "scenario_ids": ["s1", "s2"], "persona_ids": {"a": "p1", "b": "p2"}}}
    d = parse_training_defaults(full)
    assert d.interact.scenario_ids == ("s1", "s2") and d.interact.data_spec is None

    generated = copy.deepcopy(full)
    del generated["interact"]["scenario_ids"]
    del generated["interact"]["persona_ids"]
    generated["interact"]["data_spec"] = {"scenario_count": 2, "seed": 0, "purpose": "training",
                                          "stages": {"sft": {"group_size": 1}}}
    assert parse_training_defaults(generated).interact.data_spec.scenario_count == 2


@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["interact"].pop("persona_ids"), "persona_ids"),
    (lambda s: s["interact"].update(persona_ids={"a": "p1"}), "same slots"),
    (lambda s: s["interact"].update(scenario_ids=["s1"]), "at least"),
    (lambda s: s["interact"].update(unknown=1), "unknown"),
])
def test_interact_section_is_strictly_parsed(mutate, reason):
    raw = _defaults_with_interact()
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)


@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["interact"].pop("memory"), "missing key\\(s\\) \\['memory'\\]"),
    (lambda s: s["interact"].update(memory=None), "memory must be a mapping"),
    (lambda s: s["interact"].update(memory={}), "memory: missing key\\(s\\) \\['seed'\\]"),
    (lambda s: s["interact"].update(memory={"seed": None, "digests": {}}), "memory: missing key\\(s\\) \\[\\], unknown key\\(s\\)"),
    (lambda s: s["interact"].update(memory={"seed": ""}), "memory.seed must be null or a non-empty str"),
    (lambda s: s["interact"].update(memory={"seed": 7}), "memory.seed must be null or a non-empty str"),
])
def test_interact_section_requires_a_memory_seed(mutate, reason):
    raw = _defaults_with_interact()
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)


@pytest.mark.parametrize("seed", [None, "seed-1"])
def test_the_memory_seed_parses_and_round_trips(seed):
    raw = _defaults_with_interact()
    raw["interact"]["memory"] = {"seed": seed}
    d = parse_training_defaults(raw)
    assert d.interact.memory_seed == seed
    assert d.to_dict() == raw and parse_training_defaults(d.to_dict()) == d


def test_memory_anchors_the_run_and_is_never_perturbable():
    assert check_perturbable_key("memory") == "perturbable key 'memory': 'memory' anchors the run and is never perturbable"


def _to_generated(raw):
    section = raw["interact"]
    del section["scenario_ids"], section["persona_ids"]
    section["data_spec"] = {"scenario_count": 2, "seed": 0, "purpose": "training",
                            "stages": {"sft": {"group_size": 1}}}
    return raw


@pytest.mark.parametrize("mutate, reason", [
    # 来源二选一:同时写、都缺都抛
    (lambda s: s["interact"].update(data_spec={"scenario_count": 2, "seed": 0, "purpose": "training",
                                               "stages": {"sft": {"group_size": 1}}}),
     "exactly one of"),
    (lambda s: (s["interact"].pop("scenario_ids"), s["interact"].pop("persona_ids")), "exactly one of"),
    # 成对:只写一半
    (lambda s: s["interact"].pop("scenario_ids"), "written together"),
    (lambda s: s["interact"].pop("persona_ids"), "written together"),
    # 必填键
    (lambda s: s["interact"].pop("trained_slot"), "missing key"),
    (lambda s: s["interact"].pop("slots"), "missing key"),
    (lambda s: s["interact"].pop("max_turns_per_episode"), "missing key"),
    # 段本身
    (lambda s: s.update(interact=[]), "interact must be a mapping"),
    (lambda s: s.update(interact=None), "interact must be a mapping"),
    # trained_slot 与 slots
    (lambda s: s["interact"].update(trained_slot="z"), "trained_slot 'z' is not among"),
    (lambda s: s["interact"].update(trained_slot=""), "trained_slot must be non-empty"),
    (lambda s: s["interact"].update(slots=[]), "slots must be a non-empty list"),
    (lambda s: s["interact"].update(slots="a"), "slots must be a non-empty list"),
    (lambda s: s["interact"]["slots"].append({"slot_id": "a", "model_backend": "self_play"}),
     "duplicate slot_id"),
    (lambda s: s["interact"]["slots"][1].update(extra=1), "slots\\[1\\] has unknown key"),
    (lambda s: s["interact"]["slots"][1].pop("model_backend"), "slots\\[1\\] is missing key"),
    (lambda s: s["interact"]["slots"][1].update(model_ref=3), "slots\\[1\\].model_ref must be a str"),
    (lambda s: s["interact"]["slots"].__setitem__(1, "b"), "slots\\[1\\] must be a mapping"),
    (lambda s: s["interact"]["slots"][1].update(driver="magic"), "slots\\[1\\].driver must be one of"),
    (lambda s: s["interact"]["slots"][1].update(driver="VLLM"), "slots\\[1\\].driver must be one of"),
    # max_turns_per_episode
    (lambda s: s["interact"].update(max_turns_per_episode=0), "max_turns_per_episode must be >= 1"),
    (lambda s: s["interact"].update(max_turns_per_episode=True), "max_turns_per_episode must be an int"),
    (lambda s: s["interact"].update(max_turns_per_episode="4"), "max_turns_per_episode must be an int"),
    # scenario_ids
    (lambda s: s["interact"].update(scenario_ids=[]), "scenario_ids must be a non-empty list"),
    (lambda s: s["interact"].update(scenario_ids=["s1", "s1"]), "scenario_ids has duplicate"),
    (lambda s: s["interact"].update(scenario_ids=["s1", 2]), "scenario_ids\\[1\\] must be a str"),
    (lambda s: s["interact"].update(scenario_ids="s1"), "scenario_ids must be a non-empty list"),
    # persona_ids
    (lambda s: s["interact"].update(persona_ids=["p1", "p2"]), "persona_ids must be a mapping"),
    (lambda s: s["interact"].update(persona_ids={"a": "p1", "b": ""}), "persona_ids\\['b'\\] must be non-empty"),
    (lambda s: s["interact"].update(persona_ids={"a": "p1", "b": "p2", "c": "p3"}), "same slots"),
])
def test_interact_section_rejects_each_malformed_shape(mutate, reason):
    raw = _defaults_with_interact()
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)


@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["interact"]["data_spec"].update(scenario_count="2"),
     "training_defaults.interact.data_spec.scenario_count must be an int"),
    (lambda s: s["interact"]["data_spec"].update(scenario_count=1), "at least"),  # groups_per_step 是 2
    (lambda s: s["interact"]["data_spec"].update(extra=1), "interact.data_spec"),
    (lambda s: s["interact"].update(data_spec=[]), "interact.data_spec must be a dict"),
])
def test_interact_generated_path_checks_the_data_spec(mutate, reason):
    raw = _to_generated(_defaults_with_interact())
    mutate(raw)
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)


def test_interact_scenario_floor_follows_groups_per_step():
    raw = _defaults_with_interact()
    raw["training_config"]["groups_per_step"] = 3
    with pytest.raises(SchedulerConfigError, match="at least groups_per_step 3"):
        parse_training_defaults(raw)
    raw["interact"]["scenario_ids"] = ["s1", "s2", "s3"]
    assert parse_training_defaults(raw).interact.scenario_ids == ("s1", "s2", "s3")


def test_interact_parses_slot_fields_and_optional_driver():
    raw = _defaults_with_interact()
    raw["interact"]["slots"][1]["driver"] = "scripted"
    d = parse_training_defaults(raw)
    assert d.interact.trained_slot == "a" and d.interact.max_turns_per_episode == 4
    assert d.interact.slots == (
        {"slot_id": "a", "model_backend": "self_play"},
        {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1", "driver": "scripted"},
    )
    assert d.interact.persona_ids == {"a": "p1", "b": "p2"}


@pytest.mark.parametrize("driver", ["vllm", "external_api", "scripted"])
def test_interact_accepts_each_of_the_three_drivers(driver):
    raw = _defaults_with_interact()
    raw["interact"]["slots"][1]["driver"] = driver
    assert parse_training_defaults(raw).interact.slots[1]["driver"] == driver


def test_a_file_without_the_new_sections_is_unchanged():
    d = parse_training_defaults(default_training_defaults_payload())
    assert d.interact is None and d.opponent is None
    assert d.to_dict() == default_training_defaults_payload()
    assert set(d.to_dict()) == {"training_config", "data_spec"}
    assert set(default_training_defaults_payload()) == {"training_config", "data_spec"}


def test_interact_and_opponent_round_trip_through_to_dict():
    raw = _defaults_with_interact()
    raw["opponent"] = _opponent()
    d = parse_training_defaults(raw)
    assert d.to_dict() == raw
    assert parse_training_defaults(d.to_dict()) == d

    generated = _to_generated(_defaults_with_interact())
    g = parse_training_defaults(generated)
    assert g.to_dict() == generated
    assert parse_training_defaults(g.to_dict()) == g


def test_interact_does_not_alias_the_input_or_the_output():
    raw = _defaults_with_interact()
    raw["opponent"] = _opponent()
    d = parse_training_defaults(raw)
    raw["interact"]["slots"][0]["slot_id"] = "zzz"
    raw["interact"]["scenario_ids"].append("s9")
    raw["interact"]["persona_ids"]["a"] = "zzz"
    raw["opponent"]["model"] = "zzz"
    assert d.interact.slots[0]["slot_id"] == "a"
    assert d.interact.scenario_ids == ("s1", "s2")
    assert d.interact.persona_ids["a"] == "p1"
    assert d.opponent["model"] == "opp-1"
    out = d.to_dict()
    out["interact"]["slots"][0]["slot_id"] = "zzz"
    out["opponent"]["model"] = "zzz"
    assert d.interact.slots[0]["slot_id"] == "a" and d.opponent["model"] == "opp-1"


def test_the_interact_dataclass_is_frozen():
    d = parse_training_defaults(_defaults_with_interact())
    with pytest.raises(Exception):
        d.interact.trained_slot = "b"  # type: ignore[misc]
    with pytest.raises(Exception):
        d.opponent = {}  # type: ignore[misc]


def test_opponent_section_parses_alone():
    raw = default_training_defaults_payload()
    raw["opponent"] = _opponent()
    d = parse_training_defaults(raw)
    assert d.opponent == _opponent() and d.interact is None


@pytest.mark.parametrize("mutate, reason", [
    (lambda o: o.pop("base_url"), "missing key"),
    (lambda o: o.pop("api_key_env"), "missing key"),
    (lambda o: o.update(extra=1), "unknown key"),
    (lambda o: o.update(model=3), "opponent.model must be a str"),
    (lambda o: o.update(base_url=""), "opponent.base_url must be non-empty"),
    (lambda o: o.update(api_key_env=""), "opponent.api_key_env must be non-empty"),
    (lambda o: o.update(api_key_env="not a name"), "opponent.api_key_env must be an environment variable name"),
    (lambda o: o.update(api_key_env="FORGE_OPP_KEY\n"), "opponent.api_key_env must be an environment variable name"),
    (lambda o: o.update(api_key_env="1KEY"), "opponent.api_key_env must be an environment variable name"),
])
def test_opponent_section_is_strictly_parsed(mutate, reason):
    raw = default_training_defaults_payload()
    raw["opponent"] = _opponent()
    mutate(raw["opponent"])
    with pytest.raises(SchedulerConfigError, match=reason):
        parse_training_defaults(raw)


@pytest.mark.parametrize("bad", [None, [], "FORGE_OPP_KEY"])
def test_opponent_section_must_be_a_mapping(bad):
    raw = default_training_defaults_payload()
    raw["opponent"] = bad
    with pytest.raises(SchedulerConfigError, match="opponent must be a mapping"):
        parse_training_defaults(raw)


def test_a_rejected_api_key_env_never_echoes_its_value():
    # api_key_env 只该存变量名;写成密钥本身时,报错不能把它原样吐出来
    raw = default_training_defaults_payload()
    raw["opponent"] = {**_opponent(), "api_key_env": "sk-live-0123456789abcdef"}
    with pytest.raises(SchedulerConfigError) as info:
        parse_training_defaults(raw)
    assert "sk-live" not in str(info.value)


def test_install_still_writes_a_defaults_file_without_the_new_sections(tmp_path):
    store = make_store(tmp_path)
    install_default_configs(store)
    assert set(read_config(store, training_defaults_file())) == {"training_config", "data_spec"}
    d = load_training_defaults(store)
    assert d.interact is None and d.opponent is None
