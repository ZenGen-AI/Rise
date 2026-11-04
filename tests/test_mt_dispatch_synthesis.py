"""派发器的数据合成分支:带 data_spec 的指令缺本 run 的清单时,先用注入的生产器合成。"""
import copy
import subprocess
from types import SimpleNamespace

import grpc
import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file, dataset_manifest_file, episodes_dir, persona_file, run_dir,
    scenario_file,
)
from memory_fixtures import fake_memory_resolver, interact_dispatcher, resolved_memory, unresolved_memory
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import DataSynthesisError, parse_data_spec
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.training_config import MISSING_MEMORY_REASON, RewardConfig, TrainingConfig
from realtime_fixtures import plain_factory
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.dataset import DatasetIntegrityError, read_manifest, verify_manifest
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.datamodel import Persona, RoleSlot, Scenario
from simulator.store_adapter import (
    list_personas, list_scenarios, read_persona, read_scenario, write_persona, write_scenario,
)

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
        memory=unresolved_memory(),
    )
    _directive(store, cfg=cfg)

    def no_client():
        raise AssertionError("grpo_steps=0: the live server must not be contacted")

    reconcile_once(store, dispatcher=interact_dispatcher(
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
        memory=unresolved_memory(),
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


# ---------------------------------------------------------------------------
# interact 生成路:派发时生成情境与人格,回写进指令,再严格复校
# ---------------------------------------------------------------------------

WRITE_BACK = "mindtrainer: write back interact scenarios"


def _interact_cfg(run_id=RUN, **kw):
    """生成路 interact 指令在回写前的形状:scenario_ids 为空,每个绑定 persona_id 为空串。
    对手绑定带 driver,用来确认回写不丢调度器写进去的键(driver 现在是 RoleBinding 的字段,回写走原始字典,仍要原样留下)。"""
    base = dict(
        mode="interact", trained_slot="a", sft_steps=0, grpo_steps=2, groups_per_step=2,
        group_size=2, scenario_ids=[], max_turns_per_episode=4, stage_manifest_refs={},
        bindings=[
            {"slot_id": "a", "persona_id": "", "model_backend": "self_play"},
            {"slot_id": "b", "persona_id": "", "model_backend": "frozen_reference",
             "model_ref": "zengen-v1", "driver": "scripted"},
        ],
        memory=unresolved_memory(),
    )
    base.update(kw)
    return _cfg_dict(run_id, **base)


def _subjects(store):
    out = subprocess.run(["git", "-C", str(store.repo_root), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def _commit_count(store):
    out = subprocess.run(["git", "-C", str(store.repo_root), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return int(out)


def _training_config(store, run_id=RUN):
    return _status(store, run_id).payload["training_config"]


def _tree_is_empty_of_world(store):
    return list_scenarios(store) == [] and list_personas(store) == []


def _no_client():
    raise AssertionError("the live server must not be contacted")


class WorldSpy:
    """记下 world_for_interact 的调用,再交给真的默认生产器(或抛出给定的异常)。"""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises
        self._inner = TemplateDataProducer()

    def world_for_interact(self, cfg, data_spec):
        self.calls.append((cfg.to_dict(), data_spec.to_dict()))
        if self._raises is not None:
            raise self._raises
        return self._inner.world_for_interact(cfg, data_spec)

    def produce(self, store, run_id, cfg, data_spec):
        self._inner.produce(store, run_id, cfg, data_spec)


@pytest.fixture
def live(store):
    """真实的 loopback gRPC 服务端(127.0.0.1),写法同 tests/test_mt_dispatch.py。"""
    channels, servers = [], []

    def start():
        def backends(scenario, assignment, personas, client_slot):
            return {
                slot: ScriptedBackend({slot: ["b1", "b2", "b3", "b4"]})
                for slot in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            plain_factory(backends), port=0, turn_timeout_s=2.0,
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory(*, deadline_s=None):
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))

        return SimpleNamespace(client_factory=client_factory)

    yield start
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


# 组内要有差异:每个 rollout 说两句,第 0、2、... 个 rollout 的首句含 reward 关键词 agreed
_POLICY_LINES = ["agreed", "x", "no", "x"] * 8


def test_interact_write_back_fills_scenario_ids_and_personas(store, live):
    server = live()
    original = _interact_cfg()
    _directive(store, cfg=copy.deepcopy(original))
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=server.client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=TemplateDataProducer()))

    env = _status(store)
    assert env.status == "executed"
    cfg = env.payload["training_config"]
    # 情境:数量等于 data_spec.scenario_count,id 由 seed 决定,都落在树里
    assert cfg["scenario_ids"] == ["scen-auto-0", "scen-auto-1"]
    assert len(cfg["scenario_ids"]) == SPEC["scenario_count"]
    for sid in cfg["scenario_ids"]:
        assert store.exists(scenario_file(sid))
        scenario = read_scenario(store, sid)
        assert [s.slot_id for s in scenario.role_slots] == ["a", "b"]  # 角色位按绑定顺序
        assert scenario.purpose == SPEC["purpose"]
    # 人格:每个角色位一个,所有情境共用;persona_id 指向树里的人格文件
    assert [b["slot_id"] for b in cfg["bindings"]] == ["a", "b"]
    assert [b["persona_id"] for b in cfg["bindings"]] == ["pers-auto-0", "pers-auto-1"]
    for binding in cfg["bindings"]:
        assert store.exists(persona_file(binding["persona_id"]))
        assert read_persona(store, binding["persona_id"]).id == binding["persona_id"]
    # 树里恰好这几份,没有按 (情境, 角色位) 多生成的人格
    assert len(list_scenarios(store)) == 2 and len(list_personas(store)) == 2
    # 回写只动 scenario_ids 与 persona_id;driver、model_ref 等其余键与字段原样保留
    assert cfg["bindings"][1]["driver"] == "scripted"
    expected = copy.deepcopy(original)
    expected["scenario_ids"] = cfg["scenario_ids"]
    for binding, persona_id in zip(expected["bindings"], ("pers-auto-0", "pers-auto-1")):
        binding["persona_id"] = persona_id
    assert cfg == expected
    assert env.payload["data_spec"] == SPEC
    # 回写之后照常训练到底
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    assert _subjects(store).count(WRITE_BACK) == 1


def test_the_recorded_backend_is_built_from_the_written_back_config(store, live):
    """生成路:persona_id 与 scenario_ids 回写之后才是严格配置;policy_backend 取的是回写后的绑定,
    对手位的 driver(调度器写进去的 scripted)与 model_ref 原样进记录。"""
    server = live()
    _directive(store, cfg=_interact_cfg(memory=unresolved_memory()))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=server.client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=TemplateDataProducer(), policy_endpoint="gpu-1", memory_resolver=fake_memory_resolver))
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert run.status == "executed"
    assert run.payload["policy_backend"] == {
        "trained": {"ref": "file:///ckpt/base", "kind": "base", "endpoint": "gpu-1"},
        "opponents": {"b": {"driver": "scripted", "source": "zengen-v1"}},
        "real_inference_with_mock_training": True,
        # 解析器拿到的是回写后的情境 id(scen-auto-N),摘要按它们记
        "memory": {**resolved_memory(["scen-auto-0", "scen-auto-1"]),
                   "digests": {"scen-auto-0": "digest-scen-auto-0", "scen-auto-1": "digest-scen-auto-1"}},
    }


def test_write_back_happens_once_and_stays_stable(store, live):
    server = live()

    class FlakyOnce:
        """第一次连服务端就断:回写已提交,训练还没开始,指令留在 pending。"""

        def __init__(self):
            self.calls = 0

        def __call__(self, *, deadline_s=None):
            self.calls += 1
            if self.calls == 1:
                raise OSError("server not reachable yet")
            return server.client_factory(deadline_s=deadline_s)

    client_factory = FlakyOnce()
    _directive(store, cfg=_interact_cfg())
    dispatcher = interact_dispatcher(
        client_factory=client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=TemplateDataProducer())

    with pytest.raises(OSError, match="not reachable"):
        reconcile_once(store, dispatcher=dispatcher)
    assert _status(store).status == "pending"
    written_back = copy.deepcopy(_training_config(store))
    assert written_back["scenario_ids"] == ["scen-auto-0", "scen-auto-1"]
    assert [b["persona_id"] for b in written_back["bindings"]] == ["pers-auto-0", "pers-auto-1"]
    assert _subjects(store).count(WRITE_BACK) == 1
    world_commits = [s for s in _subjects(store) if s.startswith(("scenario: ", "persona: "))]
    assert len(world_commits) == 4

    # 第二轮重入:已回写的配置不再算"待回写",原样穿过;不再生成、不再回写
    reconcile_once(store, dispatcher=dispatcher)
    assert _status(store).status == "executed"
    assert _training_config(store) == written_back
    assert _subjects(store).count(WRITE_BACK) == 1
    assert len([s for s in _subjects(store) if s.startswith(("scenario: ", "persona: "))]) == 4

    # 指令已 executed 后再直接派发一次:什么都不写,一个提交都不多
    before = _commit_count(store)
    dispatcher(store, _status(store))
    assert _commit_count(store) == before
    assert _training_config(store) == written_back


def test_a_write_back_that_still_fails_strict_validation_is_t2(store):
    """生产器只给出一个情境而 groups_per_step=2:调度器会拒绝这样的指令,所以手工构造。
    回写之后的严格复校把它判成 T2,原因点名 groups_per_step。"""
    _directive(store, cfg=_interact_cfg(), data_spec=dict(SPEC, scenario_count=1))
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}),
        data_producer=TemplateDataProducer()))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "groups_per_step" in env.payload["failure"]["reason"]
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")


class SharedPersonaProducer(WorldSpy):
    """违约的生产器:给每个角色位同一份人格,回写出来的绑定就会有重复人格。"""

    def world_for_interact(self, cfg, data_spec):
        scenarios, personas = super().world_for_interact(cfg, data_spec)
        shared = next(iter(personas.values()))
        return scenarios, {slot: shared for slot in personas}


def test_a_synthesized_persona_on_more_than_one_slot_fails_the_directive_before_memory_is_resolved(store):
    """回写后的严格复校(派发器里记忆解析之前的那一次)拒绝人格重复的绑定:T2,原因同训练配置那一侧,
    记忆解析器与服务端都没被碰。"""
    resolver_calls = []

    def resolver(seed, scenario_ids):
        resolver_calls.append(scenario_ids)
        return fake_memory_resolver(seed, scenario_ids)

    _directive(store, cfg=_interact_cfg())
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}),
        data_producer=SharedPersonaProducer(), memory_resolver=resolver))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "bindings: persona 'pers-auto-0' is bound to more than one slot" in env.payload["failure"]["reason"]
    assert resolver_calls == []
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")


def test_a_pending_interact_directive_without_a_producer_fails_and_names_it(store):
    _directive(store, cfg=_interact_cfg())
    before = copy.deepcopy(_training_config(store))
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({})))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "data_producer" in env.payload["failure"]["reason"]
    assert _tree_is_empty_of_world(store)
    assert env.payload["training_config"] == before
    assert WRITE_BACK not in _subjects(store)


def test_a_pending_interact_directive_without_data_spec_fails_and_names_it(store):
    _directive(store, cfg=_interact_cfg(), data_spec=None)
    spy = WorldSpy()
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}), data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "data_spec" in env.payload["failure"]["reason"]
    assert spy.calls == []
    assert _tree_is_empty_of_world(store)
    assert WRITE_BACK not in _subjects(store)


def test_a_pending_interact_directive_with_a_malformed_data_spec_fails(store):
    _directive(store, cfg=_interact_cfg(), data_spec=dict(SPEC, seed="0"))
    spy = WorldSpy()
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}), data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "data_spec.seed must be an int" in env.payload["failure"]["reason"]
    assert spy.calls == [] and _tree_is_empty_of_world(store)


def test_a_producer_that_cannot_make_an_interact_world_fails_and_names_the_method(store):
    """Spy 只实现 produce:生成路要的 world_for_interact 它没有,指令判 failed 并写明缺什么。"""
    _directive(store, cfg=_interact_cfg())
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}), data_producer=Spy()))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "world_for_interact" in env.payload["failure"]["reason"]
    assert _tree_is_empty_of_world(store)


_HALF = [
    dict(scenario_ids=[], personas=["pa", ""], fragment="scenario_ids must be non-empty"),
    dict(scenario_ids=[], personas=["pa", "pb"], fragment="scenario_ids must be non-empty"),
    dict(scenario_ids=["s1", "s2"], personas=["pa", ""], fragment="bindings[1].persona_id must be non-empty"),
    dict(scenario_ids=["s1", "s2"], personas=["", ""], fragment="bindings[0].persona_id must be non-empty"),
    dict(scenario_ids=[], personas=None, fragment="scenario_ids must be non-empty"),
    dict(scenario_ids=["s1", "s2"], personas=None, fragment="bindings must be non-empty"),
]


@pytest.mark.parametrize(
    "case", _HALF,
    ids=["empty_ids_half_personas", "empty_ids_full_personas", "ids_half_personas",
         "ids_no_personas", "no_ids_no_bindings", "ids_no_bindings"],
)
def test_a_config_that_is_not_pending_but_incomplete_is_rejected_by_the_strict_check(store, case):
    """待回写只指"情境为空且全部 persona_id 为空串"。半成品与 bindings 为空不回写,什么都不碰,
    由紧随其后的严格校验判 T2。"""
    bindings = _interact_cfg()["bindings"]
    if case["personas"] is None:
        bindings = []
    else:
        for binding, persona_id in zip(bindings, case["personas"]):
            binding["persona_id"] = persona_id
    _directive(store, cfg=_interact_cfg(scenario_ids=case["scenario_ids"], bindings=bindings))
    before = copy.deepcopy(_training_config(store))
    spy = WorldSpy()
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}), data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert case["fragment"] in env.payload["failure"]["reason"]
    assert spy.calls == []
    assert _tree_is_empty_of_world(store)
    assert env.payload["training_config"] == before
    assert WRITE_BACK not in _subjects(store)


def test_a_named_interact_directive_is_dispatched_without_a_write_back(store, live):
    """点名路:指令自带情境与人格,不带 data_spec,派发时不生成也不回写。"""
    server = live()
    for sid in ("s1", "s2"):
        write_scenario(store, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")],
                                       "c", ["n"], "training"))
    for pid in ("pa", "pb"):
        write_persona(store, Persona(pid, ["t"], "m", "s", ["v"]))
    cfg = _interact_cfg(
        scenario_ids=["s1", "s2"],
        bindings=[{"slot_id": "a", "persona_id": "pa", "model_backend": "self_play"},
                  {"slot_id": "b", "persona_id": "pb", "model_backend": "frozen_reference",
                   "model_ref": "zengen-v1"}],
    )
    _directive(store, cfg=copy.deepcopy(cfg), data_spec=None)
    spy = WorldSpy()
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=server.client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=spy))
    env = _status(store)
    assert env.status == "executed"
    assert spy.calls == []
    assert WRITE_BACK not in _subjects(store)
    assert env.payload["training_config"] == cfg
    assert sorted(list_scenarios(store)) == ["control-plane/scenarios/s1.yaml",
                                             "control-plane/scenarios/s2.yaml"]


def test_a_deterministic_world_failure_fails_the_directive_and_writes_nothing(store):
    _directive(store, cfg=_interact_cfg())
    before = copy.deepcopy(_training_config(store))
    disp = interact_dispatcher(
        client_factory=_no_client, policy=ScriptedBackend({}),
        data_producer=WorldSpy(raises=DataSynthesisError("no template for this purpose")))
    reconcile_once(store, dispatcher=disp)  # 不抛
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "data synthesis failed (DataSynthesisError" in env.payload["failure"]["reason"]
    assert env.payload["training_config"] == before
    assert _tree_is_empty_of_world(store)
    assert reconcile_once(store, dispatcher=disp) == []


@pytest.mark.parametrize("exc", [OSError("disk hiccup"), RuntimeError("flaky")], ids=lambda e: type(e).__name__)
def test_a_transient_world_failure_propagates_and_the_directive_stays_pending(store, exc):
    _directive(store, cfg=_interact_cfg())
    before = copy.deepcopy(_training_config(store))
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=interact_dispatcher(
            client_factory=_no_client, policy=ScriptedBackend({}),
            data_producer=WorldSpy(raises=exc)))
    env = _status(store)
    assert env.status == "pending"
    assert env.payload["training_config"] == before
    assert _tree_is_empty_of_world(store)


def test_a_crash_between_the_tree_writes_and_the_write_back_heals_on_the_next_tick(store, live, monkeypatch):
    """情境与人格先落树、指令最后回写。回写前崩:重入时重新生成同样的 id、写同样的内容(空操作),
    再回写;树里不多出文件。"""
    server = live()
    real_write = store.write_product
    armed = {"on": True}

    def crash_on_write_back(path, env, message):
        if armed["on"] and message == WRITE_BACK:
            armed["on"] = False
            raise OSError("crash before the directive write-back")
        return real_write(path, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_write_back)
    _directive(store, cfg=_interact_cfg())
    dispatcher = interact_dispatcher(
        client_factory=server.client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=TemplateDataProducer())

    with pytest.raises(OSError, match="crash before the directive write-back"):
        reconcile_once(store, dispatcher=dispatcher)
    assert _status(store).status == "pending"
    assert _training_config(store)["scenario_ids"] == []  # 指令一个字节都没动
    scenarios_before, personas_before = list_scenarios(store), list_personas(store)
    assert len(scenarios_before) == 2 and len(personas_before) == 2

    reconcile_once(store, dispatcher=dispatcher)
    assert _status(store).status == "executed"
    assert _training_config(store)["scenario_ids"] == ["scen-auto-0", "scen-auto-1"]
    assert list_scenarios(store) == scenarios_before
    assert list_personas(store) == personas_before
    assert _subjects(store).count(WRITE_BACK) == 1
    assert len([s for s in _subjects(store) if s.startswith(("scenario: ", "persona: "))]) == 4


def test_the_sft_synthesis_after_a_write_back_sees_the_written_back_config(store, live):
    """回写之后的 _synthesize_missing 与生产器的 produce 拿到的是回写后的配置,不是回写前的。"""
    server = live()
    seen = []

    class Recording(WorldSpy):
        def produce(self, store, run_id, cfg, data_spec):
            seen.append(cfg)
            super().produce(store, run_id, cfg, data_spec)

    _directive(store, cfg=_interact_cfg(
        sft_steps=1, stage_manifest_refs={"sft": f"dsm-{RUN}-sft"}))
    reconcile_once(store, dispatcher=interact_dispatcher(
        client_factory=server.client_factory, policy=ScriptedBackend({"a": _POLICY_LINES}),
        data_producer=Recording()))
    assert _status(store).status == "executed"
    assert len(seen) == 1
    assert seen[0].scenario_ids == ["scen-auto-0", "scen-auto-1"]
    assert [b.persona_id for b in seen[0].bindings] == ["pers-auto-0", "pers-auto-1"]
    assert store.exists(dataset_manifest_file(RUN, "sft"))
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))


def test_a_generated_interact_directive_from_before_memory_support_writes_nothing_back(store):
    cfg = _interact_cfg()
    del cfg["memory"]
    _directive(store, cfg=cfg)                                     # data_spec 缺省就是本文件的 SPEC
    before = _commit_count(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed" and MISSING_MEMORY_REASON in env.payload["failure"]["reason"]
    assert env.payload["training_config"]["scenario_ids"] == []      # 判在回写之前:情境与人格没有生成
    assert _commit_count(store) == before + 1                        # 只有判 failed 那一笔
