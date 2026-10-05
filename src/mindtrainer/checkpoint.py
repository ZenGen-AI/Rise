from __future__ import annotations

import copy
import hashlib
from typing import Protocol

from controlplane.envelope import CURRENT_SCHEMA_VERSION, BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file

# 固定时间戳:产物内容必须是输入的纯函数,崩溃后重放注册才会逐字节相同。
_NOW = "2026-10-03T00:00:00Z"
_HF_SUFFIX = ".hf"


class CandidateAlreadyReviewedError(Exception):
    """候选已离开 pending_review(被批准、拒绝或已生效)。重放注册若悄悄写回
    pending_review,会把人工审核的结论抹掉,所以拒绝。"""


class CheckpointConverter(Protocol):
    """训练格式 -> 推理格式。诊断评测和 MindAgent 要加载的是转换后的那份。"""

    def convert(self, ref: BigRef) -> BigRef: ...


class MockConverter:
    """确定性 mock:只给 uri 加后缀,sha256 = sha256(原哈希 + "hf"),体现转换后是另一份
    制品。真实的 ROLL -> HF/vLLM 转换后置。重复转换同一个引用会让后缀叠加,所以拒绝。"""

    def convert(self, ref: BigRef) -> BigRef:
        if ref.uri.endswith(_HF_SUFFIX):
            raise ValueError(f"{ref.uri!r} is already converted")
        digest = hashlib.sha256((ref.sha256 + "hf").encode("utf-8")).hexdigest()
        return BigRef(
            uri=ref.uri + _HF_SUFFIX,
            sha256=digest,
            size_bytes=ref.size_bytes,
            fetch_hint={"type": "mock", "format": "hf", "source_sha256": ref.sha256},
        )


def register_candidate(
    store: GitStore,
    *,
    run_id: str,
    checkpoint_ref: BigRef,
    base_checkpoint_ref: BigRef,
    config_hash: str,
    curve_summary: dict,
    manifest_ids: list[str],
) -> str:
    """写 candidate_checkpoint.yaml,形状沿用现有 mind_trainer_stub。

    checkpoint_ref 必须是**转换后**的引用。daemon 晋级时会把 payload["checkpoint_ref"]
    原样带进 zengen_model,所以该键名与形状是契约,不能改。
    逐字节相同的重放是空操作(GitStore.write_product 保证);候选已被审核过则拒绝。"""
    if not run_id:
        raise ValueError("run_id must be non-empty")
    path = candidate_checkpoint_file(run_id)
    if store.exists(path):
        status = store.read_product(path).status
        if status != "pending_review":
            raise CandidateAlreadyReviewedError(
                f"candidate for {run_id} is already {status}; refusing to reset it to pending_review"
            )
    env = Envelope(
        id=f"cand-{run_id}",
        type="candidate_checkpoint",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="pending_review",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id, *manifest_ids],
        payload={
            "checkpoint_ref": checkpoint_ref.to_dict(),
            "train_curve_summary": copy.deepcopy(curve_summary),
            "training_run_ref": f"trainrun-{run_id}",
            "base_checkpoint_ref": base_checkpoint_ref.to_dict(),
            "config_hash": config_hash,
        },
    )
    store.write_product(path, env, f"candidate_checkpoint: {env.id}")
    return path
