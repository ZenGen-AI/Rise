# tests/test_se_loop.py
"""端到端:落账先于装配,提示里出现前几轮的有效扰动值(设计 6.1、6.2)。"""
import pytest

from controlplane.reconcile import Action
from trainscheduler.assemble import assemble_context
from trainscheduler.config import SchedulerConfig
from trainscheduler.experience_port import record_and_recompute
from trainscheduler.plan_handler import make_plan_handler
from trainplanner.backends import HeuristicBackend
from ts_fixtures import (
    make_store,
    write_decision,
    write_directive,
    write_report,
    write_round,
    write_trace,
)

BENCH = "bench-v1"
BASE = "zengen-v1"
ON = SchedulerConfig(strategy_experience=True, template_version="v2")


def _previous_run(run_id: str) -> str:
    """产出这一轮的那次规划的来源 run:run-005 -> run-004。测试里编号都是整数。"""
    number = int(run_id.split("-")[1])
    return f"run-{number - 1:03d}"


def _baseline(store):
    """基线轮必须在 real 级:compute_gain 拒绝"real 候选对非 real 基线"的跨级比较(R17),
    基线若是默认的 mock,增益恒为空,条目一条也写不出来。"""
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1, backends="real")


def _link_directive(store, run_id, dimension="tom"):
    """把指令的 planner_decision_ref 指回产出这一轮的那次规划。

    write_round 写的指令没有这个引用(人写指令的形状),而落账靠它找到本轮策略。
    """
    write_directive(store, run_id, dimension=dimension,
                    extra={"planner_decision_ref": f"planner-{_previous_run(run_id)}"})


def _round(store, run_id, *, score=0.45, dimension="tom", perturbation=None, backends="real"):
    """一整轮加一条已执行的规划决策。基线固定是 zengen-v1,所以增益 = score - 0.40。"""
    write_round(store, run_id, {dimension: score, "empathy": 0.70}, backends=backends)
    _link_directive(store, run_id, dimension=dimension)
    # 决策属于产出这一轮的那次规划:source 是上一个 run,next_run 是本 run
    write_decision(store, _previous_run(run_id), run_id, action="clone", dimension=dimension,
                   perturbation={"opd_coefficient": 0.2} if perturbation is None else perturbation,
                   source_recipe_id="recipe-run-002")
    write_report(store, run_id, {dimension: score, "empathy": 0.70}, baseline_ref=BASE,
                 benchmark_version=BENCH, backends=backends)
    write_trace(store, run_id, [{"task_id": "train", "cost_type": "gpu", "calls": 6}])


def test_off_keeps_the_context_seventh_section_empty(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    ctx = assemble_context(store, "run-003", config=SchedulerConfig())
    assert ctx.strategy_experience == []


def test_on_injects_matching_entries(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    for run_id, score in (("run-003", 0.45), ("run-005", 0.43)):
        _round(store, run_id, score=score)
        record_and_recompute(store, run_id, config=ON)
    ctx = assemble_context(store, "run-005", config=ON)
    assert ctx.strategy_experience
    assert "opd_coefficient" in ctx.strategy_experience[0]
    assert "0.2" in ctx.strategy_experience[0]


def test_handler_records_before_it_assembles(tmp_path):
    """落账在装配之前:即使后面规划失败,这一轮的记录也已经写下。

    run-005 自己的决策必须还不存在,否则处理器走 (c) 分支直接返回,什么都不做。
    """
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-005")
    handler = make_plan_handler(
        backend=HeuristicBackend(),
        config_loader=lambda _store: ON,
        defaults_loader=lambda _store: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        handler(store, Action(kind="plan_next_round", target_id="run-005",
                              detail={"run_id": "run-005"}))
    from controlplane.paths import strategy_experience_note_file
    assert store.exists(strategy_experience_note_file("run-005"))
