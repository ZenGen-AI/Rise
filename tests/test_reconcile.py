import pytest

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
        candidates=[CandidateView(id="cand-001", status="pending_review", run_id="run-001", has_diagnostic=True)],
        model_count=0,
    )
    actions = reconcile(state)
    assert actions == [Action(kind="await_review", target_id="cand-001", detail={})]


def test_approved_candidate_promoted_with_next_version():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="approved", run_id="run-001", has_diagnostic=True)],
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
            CandidateView(id="cand-001", status="approved", run_id="run-001", has_diagnostic=True),
            CandidateView(id="cand-002", status="approved", run_id="run-002", has_diagnostic=True),
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


def test_pending_review_without_diagnostic_requests_it_instead_of_awaiting_human():
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status="pending_review", run_id="run-001")],
        model_count=0,
    )
    assert reconcile(state) == [
        Action(kind="request_diagnostic", target_id="cand-001", detail={"run_id": "run-001"})
    ]


def test_has_diagnostic_defaults_to_false_so_missing_evidence_never_opens_the_gate():
    assert CandidateView(id="c", status="pending_review", run_id="r").has_diagnostic is False


def test_approved_without_diagnostic_is_withheld_and_does_not_consume_a_version():
    state = TreeState(
        directives=[],
        candidates=[
            CandidateView(id="cand-001", status="approved", run_id="run-001"),  # 无诊断
            CandidateView(id="cand-002", status="approved", run_id="run-002", has_diagnostic=True),
        ],
        model_count=0,
    )
    assert reconcile(state) == [
        Action(kind="request_diagnostic", target_id="cand-001", detail={"run_id": "run-001"}),
        # 被拦下的 cand-001 不占版本号，cand-002 拿到 v1 而不是 v2
        Action(kind="promote_candidate", target_id="cand-002",
               detail={"run_id": "run-002", "model_version": 1}),
    ]


@pytest.mark.parametrize("status", ["draft", "rejected", "effective", "deprecated", "superseded"])
def test_candidates_in_other_statuses_emit_nothing_even_without_a_diagnostic(status):
    state = TreeState(
        directives=[],
        candidates=[CandidateView(id="cand-001", status=status, run_id="run-001")],
        model_count=0,
    )
    assert reconcile(state) == []


# ---- CP-17:有模型读不出时诊断与晋级也暂停 ----

def _candidates():
    return [
        CandidateView("cand-run-001", "pending_review", "run-001", has_diagnostic=False),
        CandidateView("cand-run-002", "approved", "run-002", has_diagnostic=True),
    ]


def test_an_unreadable_model_pauses_diagnosis_and_promotion_but_not_dispatch():
    """真实评估器找基线、晋级的重放判定都要读模型;派发与等待审核不读。"""
    state = TreeState(
        directives=[DirectiveView("run-003", "pending", "tom", "")],
        candidates=_candidates() + [CandidateView("cand-run-004", "pending_review", "run-004", has_diagnostic=True)],
        max_model_version=2,
        unreadable=("control-plane/models/zengen-v2.yaml",),
    )
    assert [(a.kind, a.target_id) for a in reconcile(state)] == [
        ("dispatch_directive", "run-003"),
        ("await_review", "cand-run-004"),
    ]


def test_an_unreadable_non_model_product_does_not_pause_diagnosis_or_promotion():
    state = TreeState(candidates=_candidates(), max_model_version=1,
                      unreadable=("control-plane/runs/run-009/directive.yaml",))
    assert [a.kind for a in reconcile(state)] == ["request_diagnostic", "promote_candidate"]
