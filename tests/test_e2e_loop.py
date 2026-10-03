from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir, model_file, diagnostic_report_file
from controlplane.daemon import reconcile_once


def _write_directive(store: GitStore, run_id: str, dimension: str) -> None:
    env = Envelope(
        id=run_id,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
        payload={"target_dimension": dimension, "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _approve_like_cli(store: GitStore, run_id: str) -> None:
    cand = store.read_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml")
    assert cand.status == "pending_review"
    cand.status = "approved"
    store.write_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml", cand, "human: approve cand")


def test_full_fake_rsi_loop(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()

    # 1. 人工写训练指令（决策先人工写死，spec §7）
    _write_directive(store, "run-001", "high_order_belief")

    # 2. 调和 tick 1：下发 → stub 产出 scenario + candidate(pending_review)，directive→executed
    reconcile_once(store)
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-001')}/scenario_pool.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "pending_review"

    # 3. 调和 tick 2：候选待审但还没有诊断 → 先触发诊断（审核门的证据）；
    #    tick 3：证据就位，仅 await_review，不越过审核门自动晋级
    assert [a.kind for a in reconcile_once(store)] == ["request_diagnostic"]
    assert store.exists(diagnostic_report_file("run-001"))
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["await_review"]
    assert not store.exists(model_file(1))  # 未经批准不得晋级

    # 4. 人工审核门①批准（等价 CLI approve）
    _approve_like_cli(store, "run-001")

    # 5. 调和 tick 4：晋级 → 登记 zengen-v1 + 写 outcome + 候选→effective
    reconcile_once(store)
    assert store.exists(model_file(1))
    model = store.read_product(model_file(1))
    assert model.id == "zengen-v1"
    assert model.type == "zengen_model"
    outcome = store.read_product(f"{run_dir('run-001')}/outcome.yaml")
    assert outcome.payload["promoted_model"] == "zengen-v1"
    assert store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml").status == "effective"

    # 6. 调和 tick 5：闭环稳定，无剩余动作
    assert reconcile_once(store) == []


def test_rejected_candidate_never_promoted(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _write_directive(store, "run-002", "emotion")
    reconcile_once(store)
    # 人工拒绝（等价 CLI reject）
    cand = store.read_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml")
    cand.status = "rejected"
    cand.payload["reject_reason"] = "reward hacking suspected"
    store.write_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml", cand, "human: reject")
    # 调和：被拒候选不产生任何晋级动作，记录保留
    assert reconcile_once(store) == []
    assert not store.exists(model_file(1))
    assert store.read_product(f"{run_dir('run-002')}/candidate_checkpoint.yaml").status == "rejected"
