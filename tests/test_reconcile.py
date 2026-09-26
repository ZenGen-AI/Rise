from controlplane.reconcile import (
    DirectiveView,
    CandidateView,
    TreeState,
    Action,
    reconcile,
)


def test_pending_directive_dispatched():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="pending", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    actions = reconcile(state)
    assert Action(kind="dispatch_directive", target_id="run-001", detail={}) in actions


def test_executed_directive_not_redispatched():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="executed", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    assert reconcile(state) == []


def test_pending_review_candidate_awaits_human():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="pending_review", run_id="run-001")],
        model_count=0,
    )
    actions = reconcile(state)
    assert actions == [Action(kind="await_review", target_id="cand-001", detail={})]


def test_approved_candidate_promoted_with_next_version():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="approved", run_id="run-001")],
        model_count=2,
    )
    actions = reconcile(state)
    assert actions == [
        Action(kind="promote_candidate", target_id="cand-001", detail={"run_id": "run-001", "model_version": 3})
    ]


def test_two_approved_candidates_get_distinct_monotonic_versions():
    state = TreeState(
        directives=[],
        candidates=[
            CandidateView(id="cand-001", status="approved", run_id="run-001"),
            CandidateView(id="cand-002", status="approved", run_id="run-002"),
        ],
        model_count=0,
    )
    actions = reconcile(state)
    assert actions == [
        Action(kind="promote_candidate", target_id="cand-001", detail={"run_id": "run-001", "model_version": 1}),
        Action(kind="promote_candidate", target_id="cand-002", detail={"run_id": "run-002", "model_version": 2}),
    ]
    versions = [a.detail["model_version"] for a in actions]
    assert len(set(versions)) == 2  # 版本号必须互不相同，禁止静默覆盖
    assert state.model_count == 0  # 未被修改（纯函数）


def test_reconcile_is_pure_no_mutation():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="pending", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    actions1 = reconcile(state)
    actions2 = reconcile(state)
    assert actions1 == actions2  # 相同输入两次调用结果一致
    assert state.directives[0].status == "pending"  # 未被修改
    assert state.model_count == 0  # 未被修改
    assert state.candidates == []  # 未被修改
    assert len(state.directives) == 1  # 未被修改
