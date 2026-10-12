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
