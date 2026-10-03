"""晋级链路端到端：候选的 checkpoint_ref 经调和循环带进 zengen 模型，下一轮基座可解析。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.models import MissingCheckpointRefError, latest_promoted_checkpoint
from controlplane.paths import model_file, run_dir


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _write_directive(store, run_id):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _reach_review(store):
    """下发 directive 产出候选，再跑一轮触发诊断——审核门的证据就位后才能批准。"""
    reconcile_once(store)
    reconcile_once(store)


def _approve(store, run_id):
    path = f"{run_dir(run_id)}/candidate_checkpoint.yaml"
    cand = store.read_product(path)
    cand.status = "approved"
    store.write_product(path, cand, "human: approve")


def test_promotion_carries_the_candidates_checkpoint_ref(store):
    _write_directive(store, "run-001")
    _reach_review(store)
    candidate = store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml")
    _approve(store, "run-001")
    reconcile_once(store)  # 晋级

    model = store.read_product(model_file(1))
    assert model.payload["promoted_from"] == "cand-run-001"
    assert model.payload["checkpoint_ref"] == candidate.payload["checkpoint_ref"]


def test_next_round_base_is_resolvable_after_promotion(store):
    _write_directive(store, "run-001")
    _reach_review(store)
    candidate = store.read_product(f"{run_dir('run-001')}/candidate_checkpoint.yaml")
    _approve(store, "run-001")
    reconcile_once(store)

    base = latest_promoted_checkpoint(store)
    assert base.model_id == "zengen-v1"
    assert base.checkpoint_ref.to_dict() == candidate.payload["checkpoint_ref"]


def test_candidate_without_checkpoint_ref_is_promoted_but_base_resolution_fails_loudly(store):
    # 手写一个没有 checkpoint_ref 的已批准候选：晋级本身不拦（审核门已过），
    # 但绝不能给它编一个引用——下一轮解析基座时必须明确报错。
    path = f"{run_dir('run-x')}/candidate_checkpoint.yaml"
    cand = Envelope(
        id="cand-run-x", type="candidate_checkpoint", version=1,
        created_at="2026-09-26T00:00:00Z", created_by=OwnerRef(),
        status="approved", schema_version=1, links=["run-x"], payload={},
    )
    store.write_product(path, cand, "seed approved candidate without ref")
    # 没有诊断报告就不晋级：先触发诊断，不占 zengen 版本号
    assert [a.kind for a in reconcile_once(store)] == ["request_diagnostic"]
    assert not store.exists(model_file(1))
    assert [a.kind for a in reconcile_once(store)] == ["promote_candidate"]

    model = store.read_product(model_file(1))
    assert "checkpoint_ref" not in model.payload
    with pytest.raises(MissingCheckpointRefError):
        latest_promoted_checkpoint(store)
