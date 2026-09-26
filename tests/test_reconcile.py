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


def test_reconcile_is_pure_no_mutation():
    state = TreeState(
        directives=[DirectiveView(id="run-001", status="pending", target_dimension="tom", recipe_ref="")],
        candidates=[],
        model_count=0,
    )
    reconcile(state)
    reconcile(state)
    assert state.directives[0].status == "pending"  # 未被修改
