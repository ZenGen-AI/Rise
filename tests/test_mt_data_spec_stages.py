"""data_spec 与训练配置的交叉校验、要合成哪些阶段。"""
import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.data_spec import (
    check_data_spec_against_config, manifest_data_run, parse_data_spec, required_stages,
    stages_to_synthesize,
)
from mindtrainer.training_config import RewardConfig, TrainingConfig
from simulator.datamodel import RoleBinding

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=1, grpo_steps=1, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1,
        allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _interact(**kw):
    base = dict(mode="interact", scenario_ids=["s1"], max_turns_per_episode=4,
                bindings=[RoleBinding("a", "pa", "self_play"),
                          RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
                stage_manifest_refs={"sft": f"dsm-{RUN}-sft"})
    base.update(kw)
    return _cfg(**base)


def _spec(**kw):
    raw = {"scenario_count": 2, "seed": 0, "purpose": "training",
           "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}
    raw.update(kw)
    return parse_data_spec(raw)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _put_manifest_file(store, run_id, stage):
    env = Envelope(id=f"dsm-{run_id}-{stage}", type="dataset_manifest", version=1,
                   created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(), status="executed",
                   schema_version=1, payload={})
    store.write_product(dataset_manifest_file(run_id, stage), env, "seed manifest")


def test_required_stages_follow_the_steps_and_the_mode():
    assert required_stages(_cfg()) == ["sft", "grpo_opd"]
    assert required_stages(_cfg(sft_steps=0)) == ["grpo_opd"]
    assert required_stages(_cfg(grpo_steps=0)) == ["sft"]
    assert required_stages(_interact(grpo_steps=2)) == ["sft"]  # interact 的 grpo_opd 来自实时会话


@pytest.mark.parametrize(
    "ref, stage, expected",
    [("dsm-run-1-sft", "sft", "run-1"), ("dsm-a-b-c-grpo_opd", "grpo_opd", "a-b-c"),
     ("dsm--sft", "sft", None), ("dsm-run-1-sft", "grpo_opd", None), ("nonsense", "sft", None)],
)
def test_manifest_data_run_parses_the_build_manifest_id_format(ref, stage, expected):
    assert manifest_data_run(ref, stage) == expected


def test_every_missing_stage_of_this_run_is_synthesized(store):
    assert stages_to_synthesize(store, RUN, _cfg()) == ["sft", "grpo_opd"]


def test_a_stage_whose_manifest_is_already_in_the_tree_is_not_synthesized_again(store):
    _put_manifest_file(store, RUN, "sft")
    assert stages_to_synthesize(store, RUN, _cfg()) == ["grpo_opd"]
    _put_manifest_file(store, RUN, "grpo_opd")
    assert stages_to_synthesize(store, RUN, _cfg()) == []


def test_a_manifest_whose_data_lives_under_another_run_is_never_synthesized(store):
    cfg = _cfg(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["grpo_opd"]


def test_a_malformed_manifest_id_is_left_to_the_dispatcher(store):
    cfg = _cfg(stage_manifest_refs={"sft": "nonsense", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["grpo_opd"]


def test_a_stage_without_steps_is_not_synthesized_even_if_it_is_named(store):
    assert stages_to_synthesize(store, RUN, _cfg(grpo_steps=0)) == ["sft"]


def test_interact_only_ever_synthesizes_sft(store):
    cfg = _interact(grpo_steps=2, stage_manifest_refs={"sft": f"dsm-{RUN}-sft",
                                                       "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["sft"]


def test_a_spec_that_fits_the_config_passes():
    assert check_data_spec_against_config(_spec(), _cfg(), ["sft", "grpo_opd"]) is None


def test_a_spec_must_cover_every_stage_to_synthesize():
    spec = _spec(stages={"sft": {"group_size": 1}})
    reason = check_data_spec_against_config(spec, _cfg(), ["sft", "grpo_opd"])
    assert "no entry for stage 'grpo_opd'" in reason
    assert check_data_spec_against_config(spec, _cfg(), ["sft"]) is None


def test_extra_stages_in_the_spec_are_ignored():
    assert check_data_spec_against_config(_spec(), _interact(), ["sft"]) is None


def test_grpo_groups_must_reach_min_group_size():
    cfg = _cfg(min_group_size=3, group_size=3)
    reason = check_data_spec_against_config(_spec(), cfg, ["grpo_opd"])
    assert "group_size 2 is below min_group_size 3" in reason


def test_there_must_be_a_scenario_per_group_in_a_step():
    reason = check_data_spec_against_config(_spec(scenario_count=1), _cfg(groups_per_step=2), ["grpo_opd"])
    assert "scenario_count 1 is below groups_per_step 2" in reason
    assert check_data_spec_against_config(_spec(scenario_count=1), _cfg(groups_per_step=2), ["sft"]) is None
