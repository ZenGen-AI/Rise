"""装配规划上下文:形状照计划 4 的契约,stub 证据拒绝,成本未记录为 None,窗口取最近 K 轮。"""
import json

import pytest

from controlplane.config import write_config
from controlplane.paths import scheduler_config_file
from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainscheduler.assemble import (
    assemble_context,
    decision_is_executed,
    drop_no_error_label,
    regression_mark,
)
from trainscheduler.config import SchedulerConfig
from trainscheduler.errors import ContextAssemblyError, SchedulerError
from ts_fixtures import (
    make_store,
    write_candidate,
    write_decision,
    write_directive,
    write_model,
    write_recipe,
    write_report,
    write_round,
    write_trace,
)


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


# 这两个字段是"名字 → 数"的映射,键是数据不是结构
_DATA_MAPS = ("l1_scores", "error_type_distribution")


def _keys(d):
    """嵌套字典的键结构(列表取第一个元素),用来与计划 4 的示例比对形状。"""
    if isinstance(d, dict):
        return {k: (None if k in _DATA_MAPS else _keys(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_keys(d[0])] if d else []
    return None


def _full_tree(store):
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    write_recipe(store, "run-001", entries=[{"run_id": "run-002", "gain": 0.02, "promoted": True,
                                             "regressed": False, "used_as": "clone"}])
    write_decision(store, "run-001", "run-002", action="clone", source_recipe_id="recipe-run-001", mode=None)
    write_round(store, "run-002", {"tom": 0.45, "empathy": 0.70}, baseline_ref="zengen-v1",
                flags=[{"dimension": "pragmatics", "kind": "social_regression", "drop": 0.06,
                        "baseline_version": "zengen-v1", "beyond_ci": True, "level": "l1", "track": "static"}])
    write_report(store, "run-002", {"tom": 0.45, "empathy": 0.70}, baseline_ref="zengen-v1",
                 flags=[{"dimension": "pragmatics", "kind": "social_regression", "drop": 0.06}],
                 error_profiles=[{"l2_dimension": "tom.false_belief", "track": "static",
                                  "error_type_distribution": {"literal_reading": 1.0}}])
    write_trace(store, "run-002", [{"run_id": "run-002", "task_id": "plan-1", "cost_type": "external_inference",
                                    "tokens": 1200, "calls": 1}])


def test_the_assembled_context_has_exactly_the_contract_shape(store):
    _full_tree(store)
    ctx = assemble_context(store, "run-002")
    d = ctx.to_dict()
    assert _keys(d) == _keys(example_context_dict())
    assert PlanningContext.from_dict(json.loads(json.dumps(d))) == ctx


def test_candidate_and_diagnostic_are_read_from_the_source_run(store):
    _full_tree(store)
    ctx = assemble_context(store, "run-002")
    assert ctx.run_id == "run-002"
    assert ctx.candidate.to_dict() == {"id": "cand-run-002", "status": "effective", "reject_reason": None}
    assert ctx.diagnostic.l1_scores == {"tom": 0.45, "empathy": 0.70}  # l2 分数不进 l1_scores
    assert ctx.diagnostic.grade == "mock"
    assert ctx.diagnostic.benchmark_version == "bench-v1"
    assert [f.to_dict() for f in ctx.diagnostic.regression_flags] == [
        {"kind": "social_regression", "subject": "pragmatics", "magnitude": 0.06}]
    assert [p.to_dict() for p in ctx.diagnostic.error_profiles] == [
        {"l2_dimension": "tom.false_belief", "error_type_distribution": {"literal_reading": 1.0}}]


def test_a_rejected_candidate_carries_its_reason(store):
    write_round(store, "run-001", {"tom": 0.4}, candidate_status="rejected")
    write_candidate(store, "run-001", "rejected", reject_reason="reward hacking suspected")
    assert assemble_context(store, "run-001").candidate.reject_reason == "reward hacking suspected"


def test_a_real_report_is_graded_real(store):
    write_round(store, "run-001", {"tom": 0.4}, backends="real")
    assert assemble_context(store, "run-001").diagnostic.grade == "real"


def test_recipes_are_the_effective_ones_in_track_order_without_payload(store):
    _full_tree(store)
    write_recipe(store, "run-000", status="deprecated")
    ctx = assemble_context(store, "run-002")
    assert [r.to_dict() for r in ctx.recipes] == [
        {"id": "recipe-run-001", "recipe_type": "batch", "dimension": "tom", "status": "effective",
         "mean_gain": 0.02, "n_reuse": 1}]


def test_recent_decisions_carry_the_outcome_of_the_round_they_produced(store):
    _full_tree(store)
    [d] = assemble_context(store, "run-002").recent_decisions
    assert d.to_dict() == {
        "decision_id": "planner-run-001", "next_run_id": "run-002", "target_dimension": "tom",
        "action": "clone", "source_recipe_id": "recipe-run-001", "candidate_status": "effective",
        "gain": pytest.approx(0.05),
    }


def test_a_pending_decision_whose_directive_exists_counts_as_executed(store):
    _full_tree(store)
    write_decision(store, "run-001", "run-002", status="pending", action="clone",
                   source_recipe_id="recipe-run-001", mode=None)
    assert decision_is_executed(store, store.read_product("control-plane/runs/run-001/planner_decision.yaml"))
    assert len(assemble_context(store, "run-002").recent_decisions) == 1


def test_pending_without_directive_and_failed_decisions_are_left_out(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_decision(store, "run-001", "run-002", status="pending")
    assert assemble_context(store, "run-001").recent_decisions == []
    write_decision(store, "run-001", None, status="failed")
    assert assemble_context(store, "run-001").recent_decisions == []


def test_an_executed_decision_without_next_run_id_is_a_context_error(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_decision(store, "run-001", "run-002", action="new")
    env = store.read_product("control-plane/runs/run-001/planner_decision.yaml")
    del env.payload["next_run_id"]
    store.write_product("control-plane/runs/run-001/planner_decision.yaml", env, "test: strip next_run_id")
    with pytest.raises(ContextAssemblyError, match="next_run_id"):
        assemble_context(store, "run-001")


def test_a_decision_whose_round_has_no_candidate_yet_has_null_outcome(store):
    write_directive(store, "run-001")
    write_decision(store, "run-001", "run-002", action="new")
    write_directive(store, "run-002")  # 下一轮已有指令,还没有候选
    write_round(store, "run-003", {"tom": 0.4})
    out = [d.to_dict() for d in assemble_context(store, "run-003").recent_decisions]
    assert [(d["next_run_id"], d["candidate_status"], d["gain"]) for d in out] == [("run-002", None, None)]


def test_decisions_recorded_after_the_source_run_are_left_out(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_directive(store, "run-002")
    write_decision(store, "run-002", "run-003", action="new")
    write_directive(store, "run-003")
    assert assemble_context(store, "run-001").recent_decisions == []


def test_unrecorded_costs_are_none_not_zero(store):
    _full_tree(store)
    entries = [c.to_dict() for c in assemble_context(store, "run-002").cost_gain]
    assert entries[0] == {"run_id": "run-001", "target_dimension": "tom", "gpu_hours": None, "tokens": None,
                          "gain": None}
    assert entries[1]["tokens"] == 1200 and entries[1]["gpu_hours"] is None
    assert entries[1]["gain"] == pytest.approx(0.05)


def test_costs_sum_over_records_and_only_known_keys_count(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_trace(store, "run-001", [
        {"task_id": "a", "cost_type": "gpu", "gpu_hours": 1.5},
        {"task_id": "b", "cost_type": "external_inference", "tokens": 100},
        {"task_id": "c", "cost_type": "external_inference", "tokens": 50, "gpu_hours": 0.5},
    ])
    [c] = assemble_context(store, "run-001").cost_gain
    assert (c.gpu_hours, c.tokens) == (2.0, 150)
    assert isinstance(c.tokens, int)


def test_cost_gain_keeps_only_the_last_k_runs_up_to_the_source(store):
    for n in range(1, 9):
        write_round(store, f"run-{n:03d}", {"tom": 0.4})
    write_directive(store, "hand-named")  # 非 run-<数字> 的排在最前,窗口足够大时也会出现
    write_config(store, scheduler_config_file(), "scheduler-config", {"cost_gain_window": 3}, "test")
    ctx = assemble_context(store, "run-007")
    assert [c.run_id for c in ctx.cost_gain] == ["run-005", "run-006", "run-007"]


def test_an_explicit_config_overrides_the_tree(store):
    for n in range(1, 4):
        write_round(store, f"run-{n:03d}", {"tom": 0.4})
    ctx = assemble_context(store, "run-003", config=SchedulerConfig(cost_gain_window=1))
    assert [c.run_id for c in ctx.cost_gain] == ["run-003"]


def test_runs_without_a_directive_are_left_out_of_cost_gain(store):
    write_round(store, "run-002", {"tom": 0.4})
    write_candidate(store, "run-001")
    assert [c.run_id for c in assemble_context(store, "run-002").cost_gain] == ["run-002"]


@pytest.mark.parametrize(
    "setup, fragment",
    [
        (lambda s: write_directive(s, "run-001"), "has no candidate"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001", "pending_review")),
         "pending_review; planning needs"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001")), "has no diagnostic report"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001"),
                    write_report(s, "run-001", stub=True)), "graded stub"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001"),
                    write_report(s, "run-001", backends="weird")), "graded stub"),
    ],
)
def test_missing_or_stub_evidence_is_a_deterministic_error(store, setup, fragment):
    setup(store)
    with pytest.raises(ContextAssemblyError, match=fragment):
        assemble_context(store, "run-001")


def test_assembly_errors_are_scheduler_errors():
    assert issubclass(ContextAssemblyError, SchedulerError)


def test_a_report_without_l1_scores_is_rejected(store):
    write_round(store, "run-001", {"tom": 0.4})
    payload = store.read_product("control-plane/runs/run-001/diagnostic_report.yaml")
    payload.payload["dimension_scores"] = [s for s in payload.payload["dimension_scores"] if s["level"] != "l1"]
    store.write_product("control-plane/runs/run-001/diagnostic_report.yaml", payload, "test")
    with pytest.raises(ContextAssemblyError, match="no l1 scores"):
        assemble_context(store, "run-001")


@pytest.mark.parametrize(
    "flag, expected",
    [
        ({"kind": "social_regression", "dimension": "tom", "drop": 0.1}, ("social_regression", "tom", 0.1)),
        ({"kind": "g_probe_failure", "probe": "g1", "score": 0.3}, ("g_probe_failure", "g1", 0.3)),
        ({"kind": "reward_hacking_suspected", "diag_delta": -0.1, "reward_delta": 0.2},
         ("reward_hacking_suspected", "reward", pytest.approx(0.3))),
        ({"kind": "something_new", "dimension": "tom"}, ("something_new", "tom", 0.0)),
    ],
)
def test_regression_flags_are_normalized(flag, expected):
    m = regression_mark(flag)
    assert (m.kind, m.subject, m.magnitude) == expected


@pytest.mark.parametrize("flag", [{"dimension": "tom"}, {"kind": "social_regression", "dimension": "tom"},
                                  {"kind": "g_probe_failure", "score": 0.1}])
def test_malformed_regression_flags_are_rejected(flag):
    with pytest.raises(ContextAssemblyError):
        regression_mark(flag)


def test_assembly_only_reads(store):
    _full_tree(store)
    write_model(store, 2, "run-002")
    head = store._git("rev-parse", "HEAD")
    assemble_context(store, "run-002")
    assert store._git("rev-parse", "HEAD") == head
    assert store._git("status", "--porcelain") == ""


# ---- 设计 R21:空错误标签("没有错误")在装配时丢掉,不重新归一化 ----

@pytest.mark.parametrize(
    "dist, expected",
    [
        ({"": 1.0}, {}),
        ({"": 0.5, "surface_match": 0.5}, {"surface_match": 0.5}),
        ({"surface_match": 0.25, "reasoning_break": 0.75}, {"surface_match": 0.25, "reasoning_break": 0.75}),
    ],
)
def test_the_no_error_label_is_dropped_without_renormalizing(dist, expected):
    before = dict(dist)
    assert drop_no_error_label(dist) == expected
    assert dist == before  # 不改入参


def test_the_real_classifier_output_for_an_all_correct_family_is_accepted(store):
    """真实形状:aggregate_error_profile 对全答对的族给 {"": 1.0}。装配后是空映射,计划 4 接受。"""
    from diagnostic.error_classifier import aggregate_error_profile

    profile = aggregate_error_profile("l2.x", [""], "static")
    assert profile.error_type_distribution == {"": 1.0}
    write_round(store, "run-001", {"tom": 0.9})
    write_report(store, "run-001", {"tom": 0.9},
                 error_profiles=[{"l2_dimension": profile.l2_dimension, "track": profile.track,
                                  "error_type_distribution": profile.error_type_distribution}])
    ctx = assemble_context(store, "run-001")
    assert [p.to_dict() for p in ctx.diagnostic.error_profiles] == [
        {"l2_dimension": "l2.x", "error_type_distribution": {}}]
    assert PlanningContext.from_dict(json.loads(json.dumps(ctx.to_dict()))) == ctx


def test_regression_marks_keep_the_level_so_one_drop_on_three_levels_is_recognizable():
    """一次 L3 下降会在 L2、L1 各再产出一条标记(诊断设计 §6):level 丢了,规划方就分不出
    这是同一次退化的三层证据还是三次退化。没有层级的标记 level 为 None。"""
    social = regression_mark({"kind": "social_regression", "dimension": "tom", "drop": 0.1, "level": "l1"})
    probe = regression_mark({"kind": "g_probe_failure", "probe": "g1", "score": 0.3})
    assert (social.level, probe.level) == ("l1", None)
    assert social.to_dict() == {"kind": "social_regression", "subject": "tom", "magnitude": 0.1, "level": "l1"}
