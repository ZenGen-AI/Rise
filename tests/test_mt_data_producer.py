"""默认数据生产器:模板情境与人格 + 按序号交替关键词的 rollout,产出的数据要一路训得通。"""
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file, episodes_dir
from mindtrainer.checkpoint import MockConverter
from mindtrainer.data_producer import TemplateDataProducer, keyword_alternating_rollouts
from mindtrainer.data_spec import DataSynthesisError, parse_data_spec
from mindtrainer.executor import MockExecutor
from mindtrainer.grouping import count_degenerate_groups, group_by_id
from mindtrainer.orchestrator import resume_or_run
from mindtrainer.reward_pipeline import build_attribution_backend, run_reward_pipeline
from mindtrainer.training_config import RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.dataset import read_manifest, verify_manifest

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=2, grpo_steps=3, sft_batch_size=1, groups_per_step=2,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _spec(**kw):
    raw = {"scenario_count": 3, "seed": 0, "purpose": "training",
           "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}
    raw.update(kw)
    return parse_data_spec(raw)


def _store(tmp_path, name="repo"):
    s = GitStore(str(tmp_path / name))
    s.init()
    return s


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


def _snapshot(store):
    files = store.list_products(episodes_dir(RUN)) + [
        dataset_manifest_file(RUN, n) for n in ("sft", "grpo_opd")
        if store.exists(dataset_manifest_file(RUN, n))
    ]
    return {rel: store.read_product(rel).to_dict() for rel in files}


def _rewarded(store, cfg):
    batch = BatchTrajectorySource(store, read_manifest(store, RUN, "grpo_opd")).collect()
    return run_reward_pipeline(batch, cfg.reward, build_attribution_backend(cfg.reward),
                               trained_slot=cfg.trained_slot)


def test_it_writes_one_manifest_per_stage_under_this_run(store):
    TemplateDataProducer().produce(store, RUN, _cfg(), _spec())
    sft, grpo = read_manifest(store, RUN, "sft"), read_manifest(store, RUN, "grpo_opd")
    assert (sft.id, grpo.id) == (f"dsm-{RUN}-sft", f"dsm-{RUN}-grpo_opd")
    assert (len(sft.episode_refs), len(grpo.episode_refs)) == (3, 6)  # 3 个情境 x 组大小
    assert len(verify_manifest(store, grpo)) == 6


def test_every_grpo_group_has_reward_variance(store):
    cfg = _cfg()
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    groups = group_by_id(_rewarded(store, cfg))
    assert len(groups) == 3
    by_group = {gid: sorted(r.combined_reward for r in members) for gid, members in groups.items()}
    assert all(rewards == [0.0, 1.0] for rewards in by_group.values())
    assert count_degenerate_groups(by_group) == 0


@pytest.mark.parametrize("combination", ["sum", "product"])
def test_variance_holds_with_several_components_and_either_combination(store, combination):
    reward = RewardConfig(["goal", "care"], "outcome_split", combination,
                          {"goal": ["agreed", "deal"], "care": ["sorry"]})
    cfg = _cfg(reward=reward)
    TemplateDataProducer().produce(store, RUN, cfg, _spec(
        stages={"sft": {"group_size": 1}, "grpo_opd": {"group_size": 3}}))
    groups = group_by_id(_rewarded(store, cfg))
    high = 2.0 if combination == "sum" else 1.0
    for members in groups.values():
        assert [r.combined_reward for r in members] == [high, 0.0, high]  # 序号 0、1、2 交替


def test_the_data_trains_end_to_end_through_resume_or_run(store):
    cfg = _cfg()
    TemplateDataProducer().produce(store, RUN, cfg, _spec())

    def make_sources(_start):
        return {n: BatchTrajectorySource(store, read_manifest(store, RUN, n)) for n in ("sft", "grpo_opd")}

    run = resume_or_run(cfg, store=store, run_id=RUN, make_sources=make_sources,
                        attribution=build_attribution_backend(cfg.reward),
                        executor=MockExecutor(), converter=MockConverter())
    assert run.status == "executed"
    assert run.curve_summary["grpo_steps_done"] == 3
    assert run.curve_summary["degenerate_groups_total"] == 0
    assert run.curve_summary["final_mean_reward"] == 0.5


def test_it_is_deterministic(tmp_path):
    a, b = _store(tmp_path, "a"), _store(tmp_path, "b")
    for s in (a, b):
        TemplateDataProducer().produce(s, RUN, _cfg(), _spec())
    assert _snapshot(a) == _snapshot(b)


def test_another_seed_gives_other_scenarios(tmp_path):
    a, b = _store(tmp_path, "a"), _store(tmp_path, "b")
    TemplateDataProducer().produce(a, RUN, _cfg(), _spec(seed=0))
    TemplateDataProducer().produce(b, RUN, _cfg(), _spec(seed=5))
    assert read_manifest(a, RUN, "sft").manifest_hash != read_manifest(b, RUN, "sft").manifest_hash


def test_a_stage_whose_manifest_exists_is_left_alone(store):
    producer = TemplateDataProducer()
    producer.produce(store, RUN, _cfg(grpo_steps=0), _spec())  # 只合成 sft
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    before = store.read_product(dataset_manifest_file(RUN, "sft")).to_dict()
    producer.produce(store, RUN, _cfg(), _spec(seed=9))  # sft 已在,只补 grpo_opd
    assert store.read_product(dataset_manifest_file(RUN, "sft")).to_dict() == before
    assert store.exists(dataset_manifest_file(RUN, "grpo_opd"))


def test_nothing_to_do_writes_nothing(store):
    cfg = _cfg(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": "dsm-run-0-grpo_opd"})
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    assert store.list_products(episodes_dir(RUN)) == []


def test_episodes_bind_the_trained_slot_as_self_play_and_an_opponent_with_a_model_ref(store):
    TemplateDataProducer(opponent_model_ref="ref-x").produce(store, RUN, _cfg(), _spec())
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    bindings = [(b.slot_id, b.model_backend, b.model_ref) for b in episode.role_assignment.bindings]
    assert bindings == [("a", "self_play", None), ("opponent", "frozen_reference", "ref-x")]
    assert episode.turns[0].speaker_slot == "a"


def test_a_trained_slot_called_opponent_gets_a_differently_named_opponent(store):
    cfg = _cfg(trained_slot="opponent")
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    assert [b.slot_id for b in episode.role_assignment.bindings] == ["opponent", "opponent_b"]


def test_interact_bindings_are_followed(store):
    cfg = _cfg(mode="interact", grpo_steps=1, groups_per_step=1, scenario_ids=["s1"],
               max_turns_per_episode=4, stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
               bindings=[RoleBinding("b", "pb", "external_api", model_ref="api-1"),
                         RoleBinding("a", "pa", "self_play")])
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))  # interact 只合成 sft
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    bindings = [(b.slot_id, b.model_backend, b.model_ref) for b in episode.role_assignment.bindings]
    assert bindings == [("a", "self_play", None), ("b", "external_api", "api-1")]


@pytest.mark.parametrize(
    "bindings",
    [[RoleBinding("b", "pb", "frozen_reference", model_ref="r")],
     [RoleBinding("a", "pa", "self_play")],
     [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference", model_ref="r"),
      RoleBinding("b", "pc", "frozen_reference", model_ref="r")]],
    ids=["no_trained_slot", "no_opponent", "duplicate_slot"],
)
def test_bindings_that_cannot_make_an_episode_are_a_synthesis_error(store, bindings):
    with pytest.raises(DataSynthesisError, match="bind"):
        TemplateDataProducer().produce(store, RUN, _cfg(bindings=bindings), _spec())
    assert store.list_products(episodes_dir(RUN)) == []


def test_the_rollout_factory_is_injectable(store):
    seen = []

    def factory(cfg, slots, max_turns):
        seen.append((slots, max_turns))
        return lambda scenario, k: {s: ScriptedBackend({s: [f"{s}-{k}-agreed"] * max_turns}) for s in slots}

    TemplateDataProducer(rollout_factory=factory, max_turns=2).produce(store, RUN, _cfg(), _spec())
    assert seen == [(["a", "opponent"], 2)]
    episode = verify_manifest(store, read_manifest(store, RUN, "grpo_opd"))[1]
    assert [t.utterance for t in episode.turns] == ["a-1-agreed", "opponent-1-agreed"]


def test_keyword_rollouts_refuse_judge_attribution():
    reward = RewardConfig(["goal"], "judge", "sum", judge_model_id="j", rubric_version="r1")
    with pytest.raises(DataSynthesisError, match="outcome_split"):
        keyword_alternating_rollouts(_cfg(reward=reward), ["a", "opponent"], 4)


def test_keyword_rollouts_refuse_keywords_that_swallow_every_neutral_line():
    reward = RewardConfig(["goal"], "outcome_split", "sum",
                          {"goal": ["嗯", "(", ".", "-", "~", "|"]})
    with pytest.raises(DataSynthesisError, match="neutral"):
        keyword_alternating_rollouts(_cfg(reward=reward), ["a", "opponent"], 4)


def test_the_neutral_line_skips_a_keyword_that_it_would_contain(store):
    reward = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["嗯"]})
    cfg = _cfg(reward=reward)
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    groups = group_by_id(_rewarded(store, cfg))
    assert all(sorted(r.combined_reward for r in m) == [0.0, 1.0] for m in groups.values())


@pytest.mark.parametrize("max_turns, exc", [(0, ValueError), (201, ValueError), ("4", TypeError), (True, TypeError)])
def test_max_turns_is_checked_at_construction(max_turns, exc):
    with pytest.raises(exc):
        TemplateDataProducer(max_turns=max_turns)


@pytest.mark.parametrize("ref", ["", None])
def test_the_opponent_model_ref_must_be_non_empty(ref):
    with pytest.raises(ValueError):
        TemplateDataProducer(opponent_model_ref=ref)
