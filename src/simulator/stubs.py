from __future__ import annotations

from controlplane.envelope import Envelope, OwnerRef, BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from simulator.backends import TemplateGenerationBackend
from simulator.generators import generate_scenario

_NOW = "2026-09-26T00:00:00Z"


def social_simulator_stub(run_id: str, target_dimension: str) -> Envelope:
    """SocialSimulator 批量模块:用真实情境生成器产出一个情境,再包装成
    scenario_pool 信封。target_dimension → purpose 的完整映射留待 recipe 计划;
    V1 固定 purpose="training"。保留计划 1 既有 payload 键以兼容 daemon 消费。"""
    scenario = generate_scenario(TemplateGenerationBackend(), seed=0, purpose="training")
    return Envelope(
        id=f"scen-{run_id}",
        type="scenario_pool",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=1,
        links=[run_id],
        payload={
            "target_dimension": target_dimension,
            "episode_count": 0,
            "scenario_id": scenario.id,
        },
    )


def mind_trainer_stub(run_id: str, scenario_id: str) -> Envelope:
    """MindTrainer stub：假装训练出一个候选 checkpoint，置为待审。"""
    ckpt = BigRef(
        uri=f"file:///compute/checkpoints/{run_id}.safetensors",
        sha256="0" * 64,
        size_bytes=0,
        fetch_hint=None,
    )
    return Envelope(
        id=f"cand-{run_id}",
        type="candidate_checkpoint",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="pending_review",
        schema_version=1,
        links=[run_id, scenario_id],
        payload={"checkpoint_ref": ckpt.to_dict(), "train_curve_summary": {"final_loss": 0.0}},
    )


def diagnostic_evaluator_stub(run_id: str, candidate_id: str) -> Envelope:
    """DiagnosticEvaluator stub:占位诊断评测产出信封,真实实现见 diagnostic.orchestrator。"""
    return Envelope(
        id=f"diag-{run_id}",
        type="diagnostic_report",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=1,
        links=[run_id],
        payload={
            "candidate_ref": candidate_id,
            "benchmark_version": "bench-v1",
            "dimension_count": 0,
        },
    )


def execute_dispatch(store: GitStore, run_id: str, target_dimension: str) -> None:
    """动作执行器：本骨架用 stub 顶替计算层。真实实现会经 SSH 下发到 GPU。"""
    scenario = social_simulator_stub(run_id, target_dimension)
    store.write_product(
        f"{run_dir(run_id)}/scenario_pool.yaml", scenario, f"stub: scenario for {run_id}"
    )
    candidate = mind_trainer_stub(run_id, scenario.id)
    store.write_product(
        f"{run_dir(run_id)}/candidate_checkpoint.yaml",
        candidate,
        f"stub: candidate checkpoint for {run_id}",
    )
