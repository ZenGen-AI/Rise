"""派发器的数据合成分支:带 data_spec 的指令缺本 run 的清单时,先用注入的生产器合成。"""
import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, dataset_manifest_file, episodes_dir, run_dir
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import DataSynthesisError, parse_data_spec
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.training_config import RewardConfig, TrainingConfig
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.dataset import DatasetIntegrityError, read_manifest, verify_manifest

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})
SPEC = {"scenario_count": 2, "seed": 0, "purpose": "training",
        "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}


def _cfg_dict(run_id=RUN, **kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE.to_dict(),
        teacher_model_ref=TEACHER.to_dict(), reward=REWARD.to_dict(),
        sft_steps=1, grpo_steps=2, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"},
    )
    base.update(kw)
    return base


def _directive(store, run_id=RUN, *, cfg=None, data_spec=SPEC):
    payload = {"target_dimension": "tom", "recipe_ref": "",
               "training_config": cfg if cfg is not None else _cfg_dict(run_id)}
    if data_spec is not None:
        payload["data_spec"] = data_spec
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "scheduler: write directive")


def _status(store, run_id=RUN):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


class Spy:
    """记下调用,再交给真的默认生产器(或抛出给定的异常)。"""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises
        self._inner = TemplateDataProducer()

    def produce(self, store, run_id, cfg, data_spec):
        self.calls.append((run_id, data_spec.to_dict()))
        if self._raises is not None:
            raise self._raises
        self._inner.produce(store, run_id, cfg, data_spec)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_a_directive_with_data_spec_synthesizes_its_data_and_trains(store):
    _directive(store)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == [(RUN, SPEC)]
    assert _status(store).status == "executed"
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert run.status == "executed"
    assert run.payload["curve_summary"]["degenerate_groups_total"] == 0
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    for stage in ("sft", "grpo_opd"):
        manifest = read_manifest(store, RUN, stage)
        assert manifest.id == f"dsm-{RUN}-{stage}"
        verify_manifest(store, manifest)


def test_without_a_producer_the_directive_fails_and_says_what_is_missing(store):
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    reason = env.payload["failure"]["reason"]
    assert "data_producer" in reason and "['sft', 'grpo_opd']" in reason
    assert store.list_products(episodes_dir(RUN)) == []


def test_without_data_spec_a_missing_manifest_still_fails_and_the_producer_is_not_called(store):
    _directive(store, data_spec=None)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    env = _status(store)
    assert env.status == "failed"
    assert "is not in the tree" in env.payload["failure"]["reason"]


def test_a_missing_manifest_of_another_run_is_never_synthesized_and_nothing_is_written(store):
    """sft 点名 run-0 的清单且它不在树里:指令反正要判 failed,所以本 run 的 grpo_opd 也不先合成,
    树里不留孤儿 episode。"""
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    assert not store.exists(dataset_manifest_file("run-0", "sft"))
    env = _status(store)
    assert env.status == "failed"
    assert "'dsm-run-0-sft'" in env.payload["failure"]["reason"]


def test_a_malformed_manifest_id_for_another_stage_fails_before_synthesizing(store):
    cfg = _cfg_dict(stage_manifest_refs={"sft": "nonsense", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []
    env = _status(store)
    assert env.status == "failed"
    assert "stage_manifest_refs['sft']" in env.payload["failure"]["reason"]


def test_a_present_manifest_of_another_run_lets_this_runs_stage_be_synthesized(store):
    """另一阶段的清单在别的 run 下且确实在场:只为本 run 缺的阶段合成,然后照常训练。"""
    TemplateDataProducer().produce(store, "run-0", TrainingConfig.from_dict(_cfg_dict("run-0")), parse_data_spec(SPEC))
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == [(RUN, SPEC)]
    assert store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    assert not store.exists(dataset_manifest_file(RUN, "sft"))  # 不替别的 run 的清单在本 run 下造一份
    assert _status(store).status == "executed"


def test_without_a_producer_a_valid_spec_with_every_manifest_present_trains_normally(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert _status(store).status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_manifests_already_in_the_tree_are_not_synthesized_again(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert _status(store).status == "executed"


def test_a_second_tick_does_not_synthesize_again(store):
    _directive(store)
    spy = Spy()
    disp = make_mindtrainer_dispatcher(data_producer=spy)
    reconcile_once(store, dispatcher=disp)
    reconcile_once(store, dispatcher=disp)
    assert len(spy.calls) == 1


@pytest.mark.parametrize(
    "data_spec, field",
    [
        ("lots", "data_spec must be a dict"),
        (dict(SPEC, scenario_count="2"), "data_spec.scenario_count must be an int"),
        (dict(SPEC, scenario_count=0), "data_spec.scenario_count must be in"),
        (dict(SPEC, seed=1.5), "data_spec.seed must be an int"),
        (dict(SPEC, seed=-1), "data_spec.seed must be in"),
        (dict(SPEC, purpose=None), "data_spec.purpose must be a str"),
        (dict(SPEC, purpose="eval"), "data_spec.purpose must be one of"),
        (dict(SPEC, stages=["sft"]), "data_spec.stages must be a dict"),
        (dict(SPEC, stages={"sft": {"group_size": "1"}}), "group_size must be an int"),
        (dict(SPEC, stages={"grpo_opd": {"group_size": 1}}), "group_size must be in"),
        (dict(SPEC, extra=1), "unknown key"),
    ],
    ids=["not_dict", "count_str", "count_zero", "seed_float", "seed_negative", "purpose_none",
         "purpose_unknown", "stages_list", "group_size_str", "grpo_group_size_one", "extra_key"],
)
def test_a_malformed_data_spec_fails_the_directive_before_touching_the_tree(store, data_spec, field):
    _directive(store, data_spec=data_spec)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))  # 不抛
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert field in env.payload["failure"]["reason"]
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []


def test_an_integer_too_long_to_print_in_data_spec_is_a_directive_failure_not_a_value_error(store):
    """YAML 的 0xfff... 能写出超过 4300 位的整数;拼错误信息时若对它做 f-string,裸 ValueError 会逃出派发器。
    这里直接调派发器而不经 reconcile_once:调和循环把指令置 failed 时要把整份 payload 写回 YAML,
    而 yaml.safe_dump 写这个整数本身就抛 ValueError(controlplane 的既有限制,见已知限制)。"""
    payload = {"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict(),
               "data_spec": dict(SPEC, scenario_count=int("f" * 4000, 16))}
    directive = Envelope(
        id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )
    spy = Spy()
    with pytest.raises(DirectiveFailedError, match="data_spec.scenario_count must be in") as info:
        make_mindtrainer_dispatcher(data_producer=spy)(store, directive)
    assert "<int>" in info.value.reason
    assert spy.calls == []


def test_a_malformed_data_spec_fails_even_when_every_manifest_is_present(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store, data_spec=dict(SPEC, seed="0"))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = _status(store)
    assert env.status == "failed"
    assert "data_spec.seed" in env.payload["failure"]["reason"]


def test_a_data_spec_that_cannot_feed_the_config_fails_before_writing_episodes(store):
    _directive(store, cfg=_cfg_dict(groups_per_step=3), data_spec=dict(SPEC, scenario_count=2))
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert "scenario_count 2 is below groups_per_step 3" in env.payload["failure"]["reason"]
    assert spy.calls == []


def test_a_data_spec_missing_a_needed_stage_fails(store):
    _directive(store, data_spec=dict(SPEC, stages={"sft": {"group_size": 1}}))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy()))
    env = _status(store)
    assert env.status == "failed"
    assert "no entry for stage 'grpo_opd'" in env.payload["failure"]["reason"]


@pytest.mark.parametrize(
    "exc",
    [DatasetIntegrityError("x"), DataSynthesisError("x"), NotEnoughGroupsError("x")],
    ids=lambda e: type(e).__name__,
)
def test_a_deterministic_producer_failure_fails_the_directive(store, exc):
    _directive(store)
    disp = make_mindtrainer_dispatcher(data_producer=Spy(raises=exc))
    reconcile_once(store, dispatcher=disp)  # 不抛
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert f"data synthesis failed ({type(exc).__name__}" in env.payload["failure"]["reason"]
    assert reconcile_once(store, dispatcher=disp) == []


@pytest.mark.parametrize("exc", [OSError("disk hiccup"), RuntimeError("flaky")], ids=lambda e: type(e).__name__)
def test_a_transient_producer_failure_propagates_and_the_directive_stays_pending(store, exc):
    _directive(store)
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy(raises=exc)))
    assert _status(store).status == "pending"
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy()))
    assert _status(store).status == "executed"


def test_a_crash_after_the_episodes_but_before_the_manifest_heals_on_the_next_tick(store, monkeypatch):
    """清单最后写:episode 写完、grpo_opd 清单没写就崩,下一轮只重跑缺清单的阶段并覆盖成同样的内容。"""
    import simulator.dataset as dataset_module

    real_write = dataset_module.write_manifest

    def crash_on_grpo(s, manifest):
        if manifest.name == "grpo_opd":
            raise OSError("crash before the grpo_opd manifest")
        return real_write(s, manifest)

    _directive(store)
    monkeypatch.setattr(dataset_module, "write_manifest", crash_on_grpo)
    with pytest.raises(OSError, match="crash before"):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    assert _status(store).status == "pending"
    assert store.exists(dataset_manifest_file(RUN, "sft"))
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    leftover = store.list_products(episodes_dir(RUN))
    assert len(leftover) == 2 + 4  # sft 2 条 + grpo_opd 2 组 x 2 条,都已落盘
    sft_before = store.read_product(dataset_manifest_file(RUN, "sft")).to_dict()

    monkeypatch.setattr(dataset_module, "write_manifest", real_write)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert len(spy.calls) == 1
    assert _status(store).status == "executed"
    assert store.read_product(dataset_manifest_file(RUN, "sft")).to_dict() == sft_before
    assert store.list_products(episodes_dir(RUN)) == leftover  # 覆盖,不多出 episode
    verify_manifest(store, read_manifest(store, RUN, "grpo_opd"))


def test_an_interact_directive_synthesizes_only_its_sft_manifest(store):
    """interact 只从清单读 sft;grpo_steps=0 时不需要实时服务端,client_factory 不会被调用。"""
    cfg = _cfg_dict(
        mode="interact", grpo_steps=0, scenario_ids=["s1"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, cfg=cfg)

    def no_client():
        raise AssertionError("grpo_steps=0: the live server must not be contacted")

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=no_client, policy=ScriptedBackend({}), data_producer=TemplateDataProducer()))
    assert _status(store).status == "executed"
    assert store.exists(dataset_manifest_file(RUN, "sft"))
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    assert [b.slot_id for b in episode.role_assignment.bindings] == ["a", "b"]


def test_an_interact_directive_without_a_client_fails_before_synthesizing(store):
    cfg = _cfg_dict(
        mode="interact", scenario_ids=["s1"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert "client_factory" in env.payload["failure"]["reason"]
    assert spy.calls == []


def test_a_later_round_reuses_an_earlier_runs_data_without_synthesizing(store):
    """clone/replay 的指令点名前一轮的清单,不带 data_spec,也不会触发合成。"""
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    _directive(store, "run-2", cfg=_cfg_dict(RUN), data_spec=None)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert _status(store, "run-2").status == "executed"
