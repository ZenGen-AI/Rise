from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir, model_file, diagnostic_report_file
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
    # 第二轮：候选待审但还没有诊断报告 → 先触发诊断（诊断是审核门的证据）
    assert [a.kind for a in reconcile_once(store)] == ["request_diagnostic"]
    assert store.exists(diagnostic_report_file("run-001"))
    # 第三轮：证据就位，候选待审，等待人工，不推进
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["await_review"]


def test_reconcile_once_promotes_after_approval(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)
    reconcile_once(store)  # 触发诊断：审批人拿到证据之后才批准
    # 模拟人工审核门①批准（CLI 做的事：flip 状态 + commit）
    cand = store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml")
    cand.status = "approved"
    store.write_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml", cand, "human: approve")
    # 下一轮调和：登记模型 + 写 outcome + 候选置 effective
    reconcile_once(store)
    assert store.exists(model_file(1))
    assert store.read_product(f"{run_dir('run-001')}/outcome.yaml").status == "effective"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "effective"


def _candidate_view(store, run_id):
    return next(c for c in read_tree_state(store).candidates if c.run_id == run_id)


def test_read_tree_state_reports_whether_a_diagnostic_exists(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)  # 下发：产出候选，但还没有诊断
    assert _candidate_view(store, "run-001").has_diagnostic is False
    reconcile_once(store)  # 触发诊断
    assert _candidate_view(store, "run-001").has_diagnostic is True


def test_daemon_written_diagnostic_is_flagged_as_stub(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)
    reconcile_once(store)
    report = store.read_product(diagnostic_report_file("run-001"))
    assert report.type == "diagnostic_report"
    assert report.payload["stub"] is True  # 审核门据此标出 [STUB]，不会被当成真诊断
    assert report.payload["candidate_ref"] == "cand-run-001"


def test_approved_without_diagnostic_is_withheld_until_the_report_exists(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-001")
    reconcile_once(store)  # 只下发：候选待审，无诊断
    # 绕过 CLI 直接改状态（CLI 不是唯一的写入方）
    path = f"{run_dir('run-001')}/candidate_checkpoint.yaml"
    cand = store.read_product(path)
    cand.status = "approved"
    store.write_product(path, cand, "bypass: approve without diagnostic")

    assert [a.kind for a in reconcile_once(store)] == ["request_diagnostic"]
    assert not store.exists(model_file(1))  # 没有证据，不晋级
    assert store.read_product(path).status == "approved"  # 状态不动，也不卡死
    assert [a.kind for a in reconcile_once(store)] == ["promote_candidate"]
    assert store.exists(model_file(1))
