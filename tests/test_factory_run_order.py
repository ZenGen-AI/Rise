"""最大 run 的数值序(设计 §3.2;计划 8 的 _latest_numbered_run):plan_next_round 只看 run-<数字> 里数值最大的那个。

直接对计划 8 的纯函数 reconcile(TreeState) 断言,不经处理器:字符串序下这三条都会失败
("run-9" > "run-011","scratch" > "run-002",没有编号 run 时 scratch 也会被规划)。"""
from controlplane.reconcile import RunView, TreeState, reconcile


def _run(run_id, *, candidate="effective", grade="real", decision=None):
    return RunView(run_id=run_id, directive_status="executed", recipe_ref="", candidate_status=candidate,
                   has_diagnostic=True, diagnostic_grade=grade, planner_decision_status=decision,
                   has_nomination_note=True, has_track_note=True)


def _plan_targets(runs):
    state = TreeState(runs=list(runs), enabled=frozenset({"plan_next_round"}))
    return [a.target_id for a in reconcile(state) if a.kind == "plan_next_round"]


def test_run_011_beats_run_9():
    assert _plan_targets([_run("run-9"), _run("run-011")]) == ["run-011"]
    assert _plan_targets([_run("run-011"), _run("run-9")]) == ["run-011"]


def test_a_scratch_directory_does_not_block_planning():
    # "scratch" 在字符串序上大于 "run-002";数值序只看 run-<数字>,它不挡路
    assert _plan_targets([_run("run-001"), _run("run-002"), _run("scratch", candidate=None, grade=None)]) == ["run-002"]


def test_nothing_is_planned_without_any_numbered_run():
    assert _plan_targets([_run("scratch"), _run("exp-a")]) == []
