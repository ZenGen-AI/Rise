"""构造指令:载荷形状、data_spec 与按新 run id 写好的清单 id、确认状态、能被 MindTrainer 读回。"""
import copy

import pytest

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.states import can_transition
from mindtrainer.data_spec import data_spec_from_directive, parse_data_spec, stages_to_synthesize
from mindtrainer.training_config import (
    load_config_from_directive,
    validate_pending_interact,
    validate_training_config,
)
from trainplanner.decision import PlannerDecision
from trainscheduler.config import (
    DEFAULT_SCHEDULER_CONFIG,
    DEFAULT_TRAINING_CONFIG,
    SchedulerConfig,
    default_training_defaults_payload,
    parse_training_defaults,
)
from trainscheduler.directive import check_directive, construct_directive, directive_payload, manifest_id
from trainscheduler.errors import DirectiveConstructionError, SchedulerError
from trainscheduler.resolve import ResolvedAction, resolve_action
from ts_fixtures import make_store, recipe_payload, write_directive, write_recipe

DEFAULTS = parse_training_defaults(default_training_defaults_payload())


def _decision(action="new", source=None, perturbation=None):
    return PlannerDecision("tom", action, "batch" if action == "new" else None, source,
                           dict(perturbation or {}), "", "r")


def _resolved(store, action="new", source=None):
    return resolve_action(store, _decision(action, source), config=DEFAULT_SCHEDULER_CONFIG, defaults=DEFAULTS)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe(s, "run-001")
    return s


def test_manifest_ids_follow_the_dataset_rule():
    assert manifest_id("run-004", "grpo_opd") == "dsm-run-004-grpo_opd"


def test_a_new_directive_carries_data_spec_and_its_own_manifest_ids(store):
    path = construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                               planner_decision_id="planner-run-001")
    assert path == "control-plane/runs/run-002/directive.yaml"
    env = store.read_product(path)
    assert (env.id, env.type, env.status) == ("run-002", "directive", "awaiting_confirmation")
    assert set(env.payload) == {"target_dimension", "recipe_ref", "training_config", "planner_decision_ref", "data_spec"}
    assert env.payload["target_dimension"] == "tom" and env.payload["recipe_ref"] == ""
    assert env.payload["planner_decision_ref"] == "planner-run-001"
    assert env.payload["data_spec"] == DEFAULTS.data_spec.to_dict()
    assert env.payload["training_config"]["stage_manifest_refs"] == {
        "sft": "dsm-run-002-sft", "grpo_opd": "dsm-run-002-grpo_opd"}
    assert env.links == ["planner-run-001"]


def test_the_directive_reads_back_through_mindtrainer(store):
    construct_directive(store, _resolved(store), new_run_id="run-002", confirm=False,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.status == "pending"
    cfg = load_config_from_directive(env)
    assert validate_training_config(cfg) is None
    assert data_spec_from_directive(env) == DEFAULTS.data_spec
    assert stages_to_synthesize(store, "run-002", cfg) == ["sft", "grpo_opd"]


def test_only_required_stages_get_a_manifest_id(store):
    raw = default_training_defaults_payload()
    raw["training_config"]["sft_steps"] = 0
    defaults = parse_training_defaults(raw)
    resolved = resolve_action(store, _decision(), config=DEFAULT_SCHEDULER_CONFIG, defaults=defaults)
    construct_directive(store, resolved, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.payload["training_config"]["stage_manifest_refs"] == {"grpo_opd": "dsm-run-002-grpo_opd"}


@pytest.mark.parametrize("action", ["clone", "replay"])
def test_clone_and_replay_reuse_the_recipe_manifests_and_carry_no_data_spec(store, action):
    construct_directive(store, _resolved(store, action, "recipe-run-001"), new_run_id="run-002", confirm=True,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert "data_spec" not in env.payload
    assert env.payload["recipe_ref"] == "recipe-run-001"
    assert env.payload["training_config"]["stage_manifest_refs"] == recipe_payload("run-001")["training_config"][
        "stage_manifest_refs"]
    assert env.links == ["planner-run-001", "recipe-run-001"]
    assert validate_training_config(load_config_from_directive(env)) is None
    assert stages_to_synthesize(store, "run-002", load_config_from_directive(env)) == []


def test_awaiting_confirmation_can_only_be_confirmed_or_abandoned():
    assert can_transition("nonreview", "awaiting_confirmation", "pending")
    assert can_transition("nonreview", "awaiting_confirmation", "failed")
    assert not can_transition("nonreview", "awaiting_confirmation", "executed")


def test_an_invalid_config_is_refused_and_nothing_is_written(store):
    bad = ResolvedAction("tom", "replay", "recipe-run-001",
                         {**recipe_payload("run-001")["training_config"], "group_size": 1}, None, None)
    head = store._git("rev-parse", "HEAD")
    with pytest.raises(DirectiveConstructionError, match="group_size 1 is below"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
    assert store._git("rev-parse", "HEAD") == head
    assert issubclass(DirectiveConstructionError, SchedulerError)


def test_a_data_spec_too_small_for_the_config_is_refused(store):
    tiny = parse_data_spec({"scenario_count": 1, "seed": 0, "purpose": "training",
                            "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}})
    r = _resolved(store)
    bad = ResolvedAction(r.target_dimension, r.action, r.recipe_ref, r.training_config, tiny, None)
    with pytest.raises(DirectiveConstructionError, match="below groups_per_step"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")


def test_rewriting_by_the_same_decision_is_a_no_op(store):
    r = _resolved(store)
    construct_directive(store, r, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    head = store._git("rev-parse", "HEAD")
    construct_directive(store, r, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    assert store._git("rev-parse", "HEAD") == head
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "awaiting_confirmation"


def test_someone_elses_directive_is_never_overwritten(store):
    write_directive(store, "run-002", status="pending")
    with pytest.raises(DirectiveConstructionError, match="refusing to overwrite"):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                            planner_decision_id="planner-run-001")
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "pending"


def test_confirm_must_be_a_bool(store):
    with pytest.raises(TypeError):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm="yes",
                            planner_decision_id="planner-run-001")


# ---- new + interact:点名路不带 data_spec,生成路带 data_spec 且回写前情境与绑定为空 ----

def _bindings():
    return [{"slot_id": "a", "persona_id": "p1", "model_backend": "self_play"},
            {"slot_id": "b", "persona_id": "p2", "model_backend": "frozen_reference", "model_ref": "zengen-v1"}]


def _interact_cfg(**kw):
    cfg = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    cfg.update(mode="interact", trained_slot="a", max_turns_per_episode=4, **kw)
    return cfg


def _named_cfg(**kw):
    return _interact_cfg(**{"scenario_ids": ["s1", "s2"], "bindings": _bindings(), "sft_steps": 0, **kw})


def _pending_cfg(**kw):
    return _interact_cfg(**{"scenario_ids": [], "bindings": [], **kw})


def _spec():
    return parse_data_spec({"scenario_count": 2, "seed": 7, "purpose": "training", "stages": {"sft": {"group_size": 1}}})


def _head(store):
    return store._git("rev-parse", "HEAD")


def _directive_env(payload):
    return Envelope(id="run-002", type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status="pending", schema_version=CURRENT_SCHEMA_VERSION,
                    links=["planner-run-001"], payload=payload)


def test_directive_construction_allows_a_pending_interact_config():
    resolved = ResolvedAction("tom", "new", "", _pending_cfg(), _spec(), None)
    payload = directive_payload(resolved, new_run_id="run-002", planner_decision_id="planner-run-001")
    assert payload["data_spec"] == _spec().to_dict()
    assert payload["training_config"]["scenario_ids"] == []  # 回写前为空,放行
    assert payload["training_config"]["bindings"] == []
    assert payload["training_config"]["stage_manifest_refs"] == {"sft": "dsm-run-002-sft"}


def test_named_path_omits_data_spec_instead_of_crashing():
    resolved = ResolvedAction("tom", "new", "", _named_cfg(), None, None)
    payload = directive_payload(resolved, new_run_id="run-002", planner_decision_id="planner-run-001")
    assert "data_spec" not in payload
    assert payload["training_config"]["scenario_ids"] == ["s1", "s2"]
    assert payload["training_config"]["stage_manifest_refs"] == {}  # interact 不读 grpo_opd 清单,sft_steps 为 0 也没有 sft


def test_a_generated_path_directive_is_written_and_reads_back(store):
    resolved = ResolvedAction("tom", "new", "", _pending_cfg(), _spec(), None)
    path = construct_directive(store, resolved, new_run_id="run-002", confirm=False,
                               planner_decision_id="planner-run-001")
    env = store.read_product(path)
    cfg = load_config_from_directive(env)
    assert (cfg.mode, cfg.scenario_ids, cfg.bindings) == ("interact", [], [])
    assert cfg.stage_manifest_refs == {"sft": "dsm-run-002-sft"}
    assert validate_pending_interact(cfg) is None
    assert validate_training_config(cfg) is not None  # 严格口径仍拒绝:回写前不能直接训练
    assert data_spec_from_directive(env) == _spec()


def test_a_named_path_directive_goes_through_construct_directive_and_reads_back(store):
    resolved = ResolvedAction("tom", "new", "", _named_cfg(), None, None)
    path = construct_directive(store, resolved, new_run_id="run-002", confirm=True,
                               planner_decision_id="planner-run-001")
    assert path == "control-plane/runs/run-002/directive.yaml"
    env = store.read_product(path)
    assert (env.status, env.links) == ("awaiting_confirmation", ["planner-run-001"])
    assert set(env.payload) == {"target_dimension", "recipe_ref", "training_config", "planner_decision_ref"}
    cfg = load_config_from_directive(env)
    assert validate_training_config(cfg) is None  # 点名路本来就满足严格口径
    assert (cfg.mode, cfg.scenario_ids, [b.slot_id for b in cfg.bindings]) == ("interact", ["s1", "s2"], ["a", "b"])
    assert data_spec_from_directive(env) is None
    assert stages_to_synthesize(store, "run-002", cfg) == []


def _interact_section(kind):
    section = {"trained_slot": "a",
               "slots": [{"slot_id": "a", "model_backend": "self_play"},
                         {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1"}],
               "max_turns_per_episode": 4}
    if kind == "named":
        section.update(scenario_ids=["s1", "s2"], persona_ids={"a": "p1", "b": "p2"})
    else:
        section["data_spec"] = _spec().to_dict()
    return section


@pytest.mark.parametrize("kind", ["named", "generated"])
def test_a_resolved_interact_action_from_the_real_resolver_is_written(store, kind):
    raw = default_training_defaults_payload()
    raw["interact"] = _interact_section(kind)
    decision = PlannerDecision("tom", "new", "interact", None, {}, "", "r")
    resolved = resolve_action(store, decision, config=SchedulerConfig(allow_interact=True),
                              defaults=parse_training_defaults(raw))
    path = construct_directive(store, resolved, new_run_id="run-002", confirm=False,
                               planner_decision_id="planner-run-001")
    env = store.read_product(path)
    assert ("data_spec" in env.payload) == (kind == "generated")
    assert load_config_from_directive(env).mode == "interact"


def test_a_directive_without_data_spec_but_with_empty_scenario_ids_is_refused_and_nothing_is_written(store):
    """手写指令(没有 data_spec)走严格口径:空情境不能靠宽口径混过去。"""
    bad = ResolvedAction("tom", "new", "", _pending_cfg(), None, None)
    head = _head(store)
    with pytest.raises(DirectiveConstructionError, match="scenario_ids must be non-empty and unique"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
    assert _head(store) == head


def test_the_lenient_path_is_not_a_bypass_a_pending_config_with_a_bad_field_is_refused(store):
    bad = ResolvedAction("tom", "new", "", _pending_cfg(sft_steps=-1), _spec(), None)
    head = _head(store)
    with pytest.raises(DirectiveConstructionError, match="sft_steps must be >= 0"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
    assert _head(store) == head


def test_check_directive_uses_the_lenient_check_only_when_the_payload_carries_data_spec(store):
    resolved = ResolvedAction("tom", "new", "", _pending_cfg(), _spec(), None)
    payload = directive_payload(resolved, new_run_id="run-002", planner_decision_id="planner-run-001")
    check_directive(_directive_env(payload))  # 带 data_spec:放行
    del payload["data_spec"]
    with pytest.raises(DirectiveConstructionError, match="scenario_ids must be non-empty and unique"):
        check_directive(_directive_env(payload))


def test_a_pending_config_still_has_its_data_spec_checked_against_the_config(store):
    no_sft = parse_data_spec({"scenario_count": 2, "seed": 0, "purpose": "training",
                              "stages": {"grpo_opd": {"group_size": 2}}})
    bad = ResolvedAction("tom", "new", "", _pending_cfg(), no_sft, None)  # sft_steps=2,必须能合成 sft
    with pytest.raises(DirectiveConstructionError, match="has no entry for stage 'sft'"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
