"""注入缝(闭环集成设计 §3.3):handlers 与 evaluator。不注入时与现在逐字节相同。"""
import pytest

from controlplane.daemon import apply_action, reconcile_once, run_daemon, stub_dispatcher
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, nomination_file, run_dir
from controlplane.reconcile import Action


def _put_directive(store, run_id):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _approve(store, run_id):
    cand = store.read_product(candidate_checkpoint_file(run_id))
    cand.status = "approved"
    store.write_product(candidate_checkpoint_file(run_id), cand, "human: approve")


def _whole_tree(store):
    root = store.repo_root / "control-plane"
    return {
        str(p.relative_to(store.repo_root)): store.read_product(str(p.relative_to(store.repo_root))).to_dict()
        for p in sorted(root.rglob("*.yaml"))
    }


def _git_log(store):
    return store._git("log", "--format=%s").splitlines()


def _mock_report(run_id, candidate_id):
    return Envelope(
        id=f"diag-{run_id}", type="diagnostic_report", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, links=[run_id],
        payload={"candidate_ref": candidate_id, "backends": "mock",
                 "dimension_scores": [{"dimension": "tom", "level": "l1", "score": 0.6}],
                 "regression_flags": {"flags": []}},
    )


def _fake_evaluator(calls):
    def evaluate(store, run_id, candidate_id):
        calls.append((run_id, candidate_id))
        store.write_product(diagnostic_report_file(run_id), _mock_report(run_id, candidate_id), "fake: diag")
    return evaluate


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _drive_to_promotion(store, **kw):
    """派发 → 诊断 → 待审 → 批准 → 晋级 → 稳定,每一 tick 的动作种类。"""
    _put_directive(store, "run-001")
    kinds = [[a.kind for a in reconcile_once(store, **kw)] for _ in range(3)]
    _approve(store, "run-001")
    kinds += [[a.kind for a in reconcile_once(store, **kw)] for _ in range(2)]
    return kinds


def test_explicit_empty_injection_leaves_tree_log_and_actions_identical(tmp_path):
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
    ka = _drive_to_promotion(a)
    kb = _drive_to_promotion(b, dispatcher=stub_dispatcher, handlers={}, evaluator=None,
                             accept_mock_evidence=False)
    assert ka == kb == [["dispatch_directive"], ["request_diagnostic"], ["await_review"],
                        ["promote_candidate"], []]
    assert _whole_tree(a) == _whole_tree(b)
    assert _git_log(a) == _git_log(b)


def test_handlers_for_actions_that_never_trigger_change_nothing(tmp_path):
    """有处理器但触发条件不满足(桩诊断是 stub 证据):树与提交日志都与不注入时一致。"""
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
    calls = []
    handlers = {k: (lambda s, act, k=k: calls.append(k)) for k in ("plan_next_round", "nominate_recipe")}
    ka = _drive_to_promotion(a)
    kb = _drive_to_promotion(b, handlers=handlers)
    assert ka == kb
    assert calls == []
    assert _whole_tree(a) == _whole_tree(b)
    assert _git_log(a) == _git_log(b)


def test_an_injected_evaluator_replaces_only_the_diagnostic_step(store):
    calls = []
    kinds = _drive_to_promotion(store, evaluator=_fake_evaluator(calls))
    assert kinds == [["dispatch_directive"], ["request_diagnostic"], ["await_review"],
                     ["promote_candidate"], []]
    assert calls == [("run-001", "cand-run-001")]
    assert store.read_product(diagnostic_report_file("run-001")).payload["backends"] == "mock"
    assert "stub" not in store.read_product(diagnostic_report_file("run-001")).payload
    assert store.exists(model_file(1))  # 晋级路径不变


def test_the_evaluator_also_covers_an_approved_candidate_without_evidence(store):
    _put_directive(store, "run-001")
    reconcile_once(store)
    _approve(store, "run-001")
    calls = []
    assert [a.kind for a in reconcile_once(store, evaluator=_fake_evaluator(calls))] == ["request_diagnostic"]
    assert calls == [("run-001", "cand-run-001")]


def test_a_handler_receives_the_store_and_the_action(store):
    seen = []
    handlers = {"nominate_recipe": lambda s, a: seen.append((s, a))}
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))  # 跑到晋级,mock 证据
    actions = reconcile_once(store, handlers=handlers, accept_mock_evidence=True)
    assert actions == [Action(kind="nominate_recipe", target_id="run-001", detail={"run_id": "run-001"})]
    assert seen == [(store, actions[0])]


def test_mock_evidence_does_not_trigger_without_the_switch(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    seen = []
    assert reconcile_once(store, handlers={"nominate_recipe": lambda s, a: seen.append(a)}) == []
    assert seen == []


def test_a_handler_that_writes_its_marker_runs_once(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    calls = []

    def nominate(s, action):
        calls.append(action.target_id)
        note = Envelope(id=f"nomination-{action.target_id}", type="nomination_note", version=1,
                        created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(), status="executed",
                        schema_version=1, links=[action.target_id], payload={"result": "skipped"})
        s.write_product(nomination_file(action.target_id), note, "fake: nomination")

    for _ in range(3):
        reconcile_once(store, handlers={"nominate_recipe": nominate}, accept_mock_evidence=True)
    assert calls == ["run-001"]


def test_handler_exceptions_propagate_unchanged(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))

    class Boom(Exception):
        pass

    def boom(s, a):
        raise Boom("transient")

    for _ in range(2):  # 不吞、不记;下一 tick 再次触发
        with pytest.raises(Boom, match="transient"):
            reconcile_once(store, handlers={"nominate_recipe": boom}, accept_mock_evidence=True)


def test_an_action_kind_without_a_handler_never_appears(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    actions = reconcile_once(store, handlers={"start_agent_round": lambda s, a: None},
                             accept_mock_evidence=True)
    # nominate_recipe / plan_next_round 的条件都满足,但没有处理器,所以不出现
    assert [a.kind for a in actions] == ["start_agent_round"]


def test_unknown_handler_keys_are_rejected_before_anything_runs(store):
    _put_directive(store, "run-001")
    with pytest.raises(ValueError, match="dispatch_directive"):
        reconcile_once(store, handlers={"dispatch_directive": lambda s, a: None})
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "pending"
    with pytest.raises(ValueError, match=r"\['nominate'\]"):
        run_daemon(store, 0, max_ticks=1, handlers={"nominate": lambda s, a: None})
    with pytest.raises(ValueError):
        apply_action(store, Action("await_review", "x", {}), handlers={"promote_candidate": lambda s, a: None})


def test_apply_action_routes_closed_loop_kinds_to_their_handler(store):
    seen = []
    action = Action(kind="admit_recipe", target_id="recipe-run-001", detail={})
    apply_action(store, action, handlers={"admit_recipe": lambda s, a: seen.append(a)})
    assert seen == [action]


def test_apply_action_ignores_a_closed_loop_kind_without_a_handler(store):
    _put_directive(store, "run-001")
    before = _git_log(store)
    apply_action(store, Action(kind="admit_recipe", target_id="recipe-run-001", detail={}))
    assert _git_log(store) == before


def test_run_daemon_passes_handlers_evaluator_and_switch_to_every_tick(store):
    _put_directive(store, "run-001")
    calls, nominated = [], []
    run_daemon(store, 0, max_ticks=3, evaluator=_fake_evaluator(calls))
    _approve(store, "run-001")
    run_daemon(store, 0, max_ticks=2, evaluator=_fake_evaluator(calls),
               handlers={"nominate_recipe": lambda s, a: nominated.append(a.target_id)},
               accept_mock_evidence=True)
    assert calls == [("run-001", "cand-run-001")]
    assert nominated == ["run-001"]  # 第一 tick 晋级,第二 tick 才看到 effective 候选


@pytest.mark.parametrize("fn,args", [
    (reconcile_once, (None,)),
    (apply_action, (Action("noop", "x", {}), None)),
    (run_daemon, (0, 1, None)),
])
def test_the_new_parameters_are_keyword_only(store, fn, args):
    with pytest.raises(TypeError):
        fn(store, *args, {})
