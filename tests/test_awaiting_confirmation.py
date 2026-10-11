import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir
from controlplane.reconcile import DirectiveView, TreeState, reconcile
from controlplane.states import InvalidTransitionError, assert_transition, can_transition, next_states


def _put(store, run_id, status):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "scheduler: write directive")


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_awaiting_confirmation_goes_to_pending_or_failed_only():
    assert next_states("nonreview", "awaiting_confirmation") == {"pending", "failed"}
    assert not can_transition("nonreview", "awaiting_confirmation", "executed")
    with pytest.raises(InvalidTransitionError):
        assert_transition("nonreview", "awaiting_confirmation", "consumed")


def test_nothing_moves_into_awaiting_confirmation():
    # 它只是初始状态:调度器直接以它写入指令,任何已有状态都迁不进来
    for frm in ("pending", "executed", "consumed", "failed"):
        assert not can_transition("nonreview", frm, "awaiting_confirmation")


def test_existing_nonreview_transitions_are_unchanged():
    assert next_states("nonreview", "pending") == {"executed", "failed"}
    assert next_states("nonreview", "executed") == {"consumed"}
    assert next_states("nonreview", "failed") == set()


def test_reconcile_produces_no_action_for_an_unconfirmed_directive():
    state = TreeState(
        directives=[
            DirectiveView(id="run-001", status="awaiting_confirmation", target_dimension="tom", recipe_ref="")
        ],
    )
    assert reconcile(state) == []


def test_the_loop_leaves_an_unconfirmed_directive_and_the_tree_alone(store):
    _put(store, "run-001", "awaiting_confirmation")
    before = _head(store)
    assert reconcile_once(store) == []
    assert reconcile_once(store) == []
    assert _head(store) == before  # 没有任何提交
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "awaiting_confirmation"
    assert not store.exists(candidate_checkpoint_file("run-001"))


def test_once_confirmed_the_directive_is_dispatched(store):
    _put(store, "run-001", "awaiting_confirmation")
    rel = f"{run_dir('run-001')}/directive.yaml"
    env = store.read_product(rel)
    assert_transition("nonreview", env.status, "pending")  # 命令行 confirm 做的就是这一步
    env.status = "pending"
    store.write_product(rel, env, "human: confirm run-001")
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["dispatch_directive"]
    assert store.read_product(rel).status == "executed"
