import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once, run_daemon, stub_dispatcher
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir


def _put(store, run_id, payload=None):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload=payload if payload is not None else {"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _directive(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


def _tree(store, run_id):
    return {rel: store.read_product(rel).to_dict() for rel in store.list_products(run_dir(run_id))}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_without_a_dispatcher_the_stub_still_runs(store):
    _put(store, "run-1")
    reconcile_once(store)
    assert store.read_product(candidate_checkpoint_file("run-1")).id == "cand-run-1"
    assert _directive(store, "run-1").status == "executed"


def test_passing_the_stub_explicitly_changes_nothing(tmp_path):
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
        _put(s, "run-1")
    reconcile_once(a)
    reconcile_once(b, dispatcher=stub_dispatcher)
    assert _tree(a, "run-1") == _tree(b, "run-1")


def test_an_injected_dispatcher_replaces_the_stub_and_gets_the_directive(store):
    _put(store, "run-1", {"target_dimension": "tom", "recipe_ref": "", "note": "x"})
    seen = []
    reconcile_once(
        store, dispatcher=lambda s, d: seen.append((s, d.id, d.status, d.payload["note"]))
    )
    assert seen == [(store, "run-1", "pending", "x")]
    assert not store.exists(candidate_checkpoint_file("run-1"))  # 桩没有跑
    assert _directive(store, "run-1").status == "executed"


def test_a_dispatcher_cannot_change_the_state_by_mutating_what_it_was_given(store):
    _put(store, "run-1")

    def meddle(s, d):
        d.status = "failed"

    reconcile_once(store, dispatcher=meddle)
    assert _directive(store, "run-1").status == "executed"


def test_an_ordinary_exception_propagates_and_the_directive_stays_pending(store):
    _put(store, "run-1")
    calls = []

    def boom(s, d):
        calls.append(d.id)
        raise RuntimeError("transient")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="transient"):
            reconcile_once(store, dispatcher=boom)
        assert _directive(store, "run-1").status == "pending"
    assert calls == ["run-1", "run-1"]  # 下一轮重新派发


def test_a_directive_failure_is_recorded_and_never_retried(store):
    _put(store, "run-1")
    calls = []

    def refuse(s, d):
        calls.append(d.id)
        raise DirectiveFailedError("config is nonsense")

    first = reconcile_once(store, dispatcher=refuse)
    assert [a.kind for a in first] == ["dispatch_directive"]
    env = _directive(store, "run-1")
    assert env.status == "failed"
    assert env.payload["failure"] == {"category": "T2", "reason": "config is nonsense"}
    assert env.payload["target_dimension"] == "tom"  # 人写的内容原样保留
    assert reconcile_once(store, dispatcher=refuse) == []
    assert calls == ["run-1"]


def test_a_failed_directive_does_not_stop_the_rest_of_the_tick(store):
    _put(store, "run-1")
    _put(store, "run-2")

    def pick(s, d):
        if d.id == "run-1":
            raise DirectiveFailedError("no", category="T3")
        stub_dispatcher(s, d)

    reconcile_once(store, dispatcher=pick)
    assert _directive(store, "run-1").status == "failed"
    assert _directive(store, "run-1").payload["failure"]["category"] == "T3"
    assert _directive(store, "run-2").status == "executed"
    assert store.exists(candidate_checkpoint_file("run-2"))


def test_the_dispatcher_is_keyword_only(store):
    _put(store, "run-1")
    with pytest.raises(TypeError):
        reconcile_once(store, stub_dispatcher)


def test_run_daemon_hands_the_dispatcher_to_the_ticks(store):
    _put(store, "run-1")
    seen = []
    run_daemon(store, 0, max_ticks=2, dispatcher=lambda s, d: seen.append(d.id))
    assert seen == ["run-1"]  # 第二轮时 directive 已 executed,不再派发


def test_run_daemon_without_a_dispatcher_uses_the_stub(store):
    _put(store, "run-1")
    run_daemon(store, 0, max_ticks=1)
    assert store.exists(candidate_checkpoint_file("run-1"))


def test_the_failure_error_carries_its_reason_and_category():
    exc = DirectiveFailedError("why", category="T3")
    assert (exc.reason, exc.category, str(exc)) == ("why", "T3", "why")
    assert DirectiveFailedError("x").category == "T2"
