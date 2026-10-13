"""reconcile 扩展(闭环集成设计 §3.1、§3.2):六个新动作的触发与不触发。全部是纯函数测试。"""
import dataclasses

import pytest

from controlplane.reconcile import (
    CLOSED_LOOP_ACTIONS,
    Action,
    CandidateView,
    DirectiveView,
    GateView,
    ModelView,
    RunView,
    TreeState,
    has_round_in_flight,
    reconcile,
)

ALL = frozenset(CLOSED_LOOP_ACTIONS)
PLAN = frozenset({"plan_next_round"})  # 规划自己的触发条件;不启用提名,所以不涉及配方入库门(CP-11)


def _run(run_id="run-001", **kw) -> RunView:
    base = dict(
        run_id=run_id, directive_status="executed", recipe_ref="", candidate_status="effective",
        has_diagnostic=True, diagnostic_grade="real", planner_decision_status=None,
        has_nomination_note=False, has_track_note=False,
    )
    base.update(kw)
    return RunView(**base)


def _kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


def _plan(state):
    return _kinds(reconcile(state), "plan_next_round")


# ---- 视图与默认值 ----

def test_new_tree_state_fields_all_default_to_empty():
    s = TreeState()
    assert (s.runs, s.models, s.gates, s.enabled, s.accept_mock_evidence) == ([], [], [], frozenset(), False)


def test_the_six_action_names_are_fixed():
    assert CLOSED_LOOP_ACTIONS == {
        "plan_next_round", "nominate_recipe", "record_track",
        "start_agent_round", "admit_recipe", "admit_proposal",
    }


def test_views_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _run().run_id = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ModelView("zengen-v1", 1, "run-001", "effective", None).status = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        GateView("recipe-run-001", "recipe_candidate", "approved").status = "x"


@pytest.mark.parametrize("round_status,has_round", [(None, False), ("pending", True), ("executed", True), ("failed", True)])
def test_has_agent_round_is_derived_from_the_round_status(round_status, has_round):
    assert ModelView("zengen-v1", 1, "run-001", "effective", round_status).has_agent_round is has_round


def test_without_enabled_names_the_views_produce_nothing():
    state = TreeState(
        runs=[_run()],
        models=[ModelView("zengen-v1", 1, "run-001", "effective", None)],
        gates=[GateView("recipe-run-001", "recipe_candidate", "approved"),
               GateView("proposal-r-1", "skill_memory_proposal", "approved")],
    )
    assert reconcile(state) == []


def test_an_awaiting_confirmation_directive_is_never_dispatched():
    state = TreeState(directives=[DirectiveView("run-002", "awaiting_confirmation", "tom", "")], enabled=ALL)
    assert reconcile(state) == []


# ---- 在途轮(R4) ----

@pytest.mark.parametrize("directive_status", ["pending", "awaiting_confirmation"])
def test_a_pending_or_unconfirmed_directive_is_a_round_in_flight(directive_status):
    assert has_round_in_flight(TreeState(runs=[_run("run-002", directive_status=directive_status, candidate_status=None)]))


@pytest.mark.parametrize("candidate_status", ["pending_review", "approved"])
def test_an_unreviewed_or_unpromoted_candidate_is_a_round_in_flight(candidate_status):
    assert has_round_in_flight(TreeState(runs=[_run("run-002", candidate_status=candidate_status)]))


@pytest.mark.parametrize("directive_status", ["executed", "failed", "consumed", None])
@pytest.mark.parametrize("candidate_status", ["effective", "rejected", None])
def test_finished_rounds_are_not_in_flight(directive_status, candidate_status):
    assert not has_round_in_flight(
        TreeState(runs=[_run(directive_status=directive_status, candidate_status=candidate_status)])
    )


# ---- plan_next_round ----

@pytest.mark.parametrize("candidate_status", ["effective", "rejected"])
def test_plan_triggers_on_a_terminal_candidate_with_real_evidence(candidate_status):
    state = TreeState(runs=[_run(candidate_status=candidate_status)], enabled=frozenset({"plan_next_round"}))
    assert reconcile(state) == [
        Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})
    ]


@pytest.mark.parametrize("candidate_status", ["pending_review", "approved", "draft", "deprecated", None])
def test_plan_does_not_trigger_before_the_candidate_is_terminal(candidate_status):
    state = TreeState(runs=[_run(candidate_status=candidate_status)], enabled=PLAN)
    assert _plan(state) == []


def test_a_failed_directive_does_not_trigger_planning():
    # R5:指令失败,没有候选,闭环停住等人
    state = TreeState(runs=[_run(directive_status="failed", candidate_status=None,
                                 has_diagnostic=False, diagnostic_grade=None)], enabled=PLAN)
    assert reconcile(state) == []


@pytest.mark.parametrize("candidate_status", ["effective", "rejected"])
@pytest.mark.parametrize("grade", ["stub", None])
def test_plan_never_triggers_on_stub_or_missing_evidence(grade, candidate_status):
    for accept in (False, True):
        state = TreeState(runs=[_run(diagnostic_grade=grade, has_diagnostic=grade is not None,
                                     candidate_status=candidate_status)],
                          enabled=PLAN, accept_mock_evidence=accept)
        assert _plan(state) == []


def test_mock_evidence_needs_the_explicit_switch():
    run = _run(diagnostic_grade="mock")
    assert _plan(TreeState(runs=[run], enabled=PLAN)) == []
    assert [a.target_id for a in _plan(TreeState(runs=[run], enabled=PLAN, accept_mock_evidence=True))] == ["run-001"]


@pytest.mark.parametrize("decision", ["executed", "failed"])
def test_a_finished_planner_decision_is_the_idempotency_key(decision):
    state = TreeState(runs=[_run(planner_decision_status=decision)], enabled=PLAN)
    assert _plan(state) == []


def test_a_pending_planner_decision_still_triggers():
    state = TreeState(runs=[_run(planner_decision_status="pending")], enabled=PLAN)
    assert _plan(state) == [Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})]


def test_only_the_largest_run_id_is_planned():
    state = TreeState(runs=[_run("run-003"), _run("run-001"), _run("run-002")], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-003"]


def test_an_earlier_finished_run_is_not_planned_when_a_larger_run_exists():
    """较早 run 的候选已终态、证据可接受,但存在 run id 更大的 run:不产生 plan_next_round。"""
    newer_without_evidence = _run("run-002", candidate_status="rejected", has_diagnostic=False, diagnostic_grade=None)
    newer_already_planned = _run("run-002", planner_decision_status="executed")
    for newer in (newer_without_evidence, newer_already_planned):
        assert _plan(TreeState(runs=[_run("run-001"), newer], enabled=PLAN)) == []


def test_the_largest_run_is_chosen_by_numeric_order():
    # 数值序:run-1000 大于 run-999(字符串序会选 run-999)
    state = TreeState(runs=[_run("run-999"), _run("run-1000"), _run("run-010")], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-1000"]


def test_an_unpadded_run_id_does_not_shadow_a_larger_padded_one():
    # 计划 5 允许不补零的 run-9,并按数值分配 run-011;字符串序下 run-9 永远"最大",规划会静默停住
    state = TreeState(runs=[_run("run-9", planner_decision_status="executed"), _run("run-011")], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-011"]


@pytest.mark.parametrize("order", [("run-7", "run-007"), ("run-007", "run-7")])
def test_a_tie_on_the_run_number_is_broken_by_run_id_regardless_of_order(order):
    # 手工命名才会出现的同编号:按 run id 字典序取最大("run-7" > "run-007"),不看输入顺序
    state = TreeState(runs=[_run(r) for r in order], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-7"]


def test_a_non_numbered_directory_does_not_block_planning():
    state = TreeState(runs=[_run("run-001"), _run("scratch", candidate_status=None, has_diagnostic=False,
                                                  diagnostic_grade=None)], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_no_numbered_run_means_no_planning():
    # 刻意失败关闭:树里一个 run-<数字> 都没有(只可能是手工造的目录)时不规划
    state = TreeState(runs=[_run("scratch"), _run("run-abc"), _run("run-")], enabled=PLAN)
    assert _plan(state) == []


def test_a_non_numbered_directory_still_counts_as_a_round_in_flight():
    # R4 仍看全部 run:不参与"最大"的比较,不等于不算在途
    state = TreeState(runs=[_run("run-001"), _run("scratch", directive_status="pending", candidate_status=None,
                                                  has_diagnostic=False, diagnostic_grade=None)], enabled=PLAN)
    assert _plan(state) == []


def test_a_failed_largest_directive_without_a_candidate_stops_planning():
    """R5:最大 run 的指令 failed、没有候选 → 不触发,即使较早的 run 满足全部条件。"""
    failed = _run("run-002", directive_status="failed", candidate_status=None,
                  has_diagnostic=False, diagnostic_grade=None)
    assert _plan(TreeState(runs=[_run("run-001"), failed], enabled=PLAN)) == []


def test_a_round_in_flight_blocks_planning_anywhere():
    state = TreeState(
        runs=[_run("run-001"), _run("run-002", directive_status="pending", candidate_status=None,
                                    has_diagnostic=False, diagnostic_grade=None)],
        enabled=PLAN,
    )
    assert _plan(state) == []


def test_reentry_after_a_crash_before_the_directive_retriggers_the_source_run():
    """规划处理器先写 planner_decision(pending, next_run_id),再写指令,最后置 executed。
    崩在"决策 pending、指令未写":来源 run 仍是最大 run,决策 pending → 再次触发;
    处理器复用已记下的 next_run_id 续写指令(对外契约,见计划正文)。"""
    src = _run("run-001", planner_decision_status="pending")
    assert _plan(TreeState(runs=[_run("run-000", planner_decision_status="executed"), src], enabled=PLAN)) == [
        Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})
    ]


def test_reentry_after_a_crash_after_the_directive_does_not_retrigger_the_source_run():
    """崩在"指令已写、决策仍 pending":新指令所在 run 成为最大 run。
    1) 它在途(awaiting_confirmation)时,不规划;
    2) 它走完后,规划落在新 run 上,来源 run 不再触发;装配上下文时把那条 pending 决策按 executed 对待。"""
    src = _run("run-001", planner_decision_status="pending")
    in_flight = _run("run-002", directive_status="awaiting_confirmation", candidate_status=None,
                     has_diagnostic=False, diagnostic_grade=None)
    assert _plan(TreeState(runs=[src, in_flight], enabled=PLAN)) == []

    finished = _run("run-002", candidate_status="rejected")
    assert _plan(TreeState(runs=[src, finished], enabled=PLAN)) == [
        Action(kind="plan_next_round", target_id="run-002", detail={"run_id": "run-002"})
    ]


def test_plan_needs_its_own_name_enabled():
    state = TreeState(runs=[_run()], enabled=ALL - {"plan_next_round"})
    assert _plan(state) == []


def test_reconcile_with_closed_loop_views_is_still_pure():
    state = TreeState(runs=[_run("run-002"), _run("run-001")], enabled=PLAN)
    first, second = reconcile(state), reconcile(state)
    assert first == second
    assert [a.target_id for a in _plan(state)] == ["run-002"]
    assert [r.run_id for r in state.runs] == ["run-002", "run-001"]  # 入参未被排序


# ---- 规划等配方入库门(设计 CP-11) ----

PLAN_AND_NOMINATE = frozenset({"plan_next_round", "nominate_recipe"})


def _gate(status, run_id="run-001", kind="recipe_candidate"):
    return GateView(f"recipe-{run_id}", kind, status)


@pytest.mark.parametrize("gate_status", ["pending_review", "approved"])
def test_planning_waits_while_the_recipe_candidate_is_unfinished(gate_status):
    state = TreeState(runs=[_run(has_nomination_note=True)], gates=[_gate(gate_status)], enabled=PLAN_AND_NOMINATE)
    assert _plan(state) == []


@pytest.mark.parametrize("gate_status", ["effective", "rejected"])
def test_planning_goes_ahead_once_the_recipe_candidate_has_left_the_gate(gate_status):
    state = TreeState(runs=[_run(has_nomination_note=True)], gates=[_gate(gate_status)], enabled=PLAN_AND_NOMINATE)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_planning_goes_ahead_when_the_run_nominated_nothing():
    # 提名写了 skipped 标记,没有配方候选:规划不等
    state = TreeState(runs=[_run(has_nomination_note=True)], gates=[], enabled=PLAN_AND_NOMINATE)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_planning_waits_for_the_nomination_note():
    state = TreeState(runs=[_run(has_nomination_note=False)], enabled=PLAN_AND_NOMINATE)
    assert _plan(state) == []
    later = dataclasses.replace(state, runs=[_run(has_nomination_note=True)])
    assert [a.target_id for a in _plan(later)] == ["run-001"]


def test_a_rejected_candidate_is_never_nominated_so_planning_does_not_wait():
    state = TreeState(runs=[_run(candidate_status="rejected", has_nomination_note=False)], enabled=PLAN_AND_NOMINATE)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_planning_does_not_wait_when_nomination_is_not_enabled():
    state = TreeState(runs=[_run(has_nomination_note=False)], gates=[_gate("pending_review")], enabled=PLAN)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_another_runs_unfinished_recipe_candidate_does_not_block_this_run():
    state = TreeState(runs=[_run("run-002", has_nomination_note=True)],
                      gates=[_gate("pending_review", "run-001")], enabled=PLAN_AND_NOMINATE)
    assert [a.target_id for a in _plan(state)] == ["run-002"]


def test_a_skill_proposal_with_the_same_id_does_not_count_as_the_recipe_gate():
    state = TreeState(runs=[_run(has_nomination_note=True)],
                      gates=[_gate("pending_review", kind="skill_memory_proposal")], enabled=PLAN_AND_NOMINATE)
    assert [a.target_id for a in _plan(state)] == ["run-001"]
