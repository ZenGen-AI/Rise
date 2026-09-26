from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir, model_file
from controlplane.daemon import read_tree_state, reconcile_once


def _write_directive(store: GitStore, run_id: str) -> None:
    env = Envelope(
        id=run_id,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def test_read_tree_state_sees_pending_directive(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    state = read_tree_state(store)
    assert any(d.id == "run-001" and d.status == "pending" for d in state.directives)


def test_reconcile_once_dispatches_then_awaits(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    # 第一轮：下发 → stub 产出 scenario + candidate(pending_review)，directive 置 executed
    reconcile_once(store)
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "pending_review"
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "executed"
    # 第二轮：候选待审，等待人工，不推进
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["await_review"]


def test_reconcile_once_promotes_after_approval(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)
    # 模拟人工审核门①批准（CLI 做的事：flip 状态 + commit）
    cand = store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml")
    cand.status = "approved"
    store.write_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml", cand, "human: approve")
    # 下一轮调和：登记模型 + 写 outcome + 候选置 effective
    reconcile_once(store)
    assert store.exists(model_file(1))
    assert store.read_product(f"{run_dir('run-001')}/outcome.yaml").status == "effective"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "effective"
