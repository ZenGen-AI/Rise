from __future__ import annotations

from typing import Protocol

from controlplane.gitstore import GitStore
from mindtrainer.datamodel import TrajectoryBatch
from simulator.dataset import DatasetManifest, verify_manifest


class TrajectorySource(Protocol):
    """下游(奖励管线、分组)只依赖这一个方法,不关心轨迹来自哪里。"""

    def collect(self) -> TrajectoryBatch: ...


def batch_for_step(items: list, step: int, size: int) -> list:
    """按固定顺序轮转取:[items[(step*size + i) % n] for i in range(size)]。

    每个 step 用的数据是 step 的纯函数,续训等价才可测。回绕时同一批内样本可能重复
    (size > len(items) 时)。返回新列表,改它不会影响 items。"""
    if not items:
        raise ValueError("items must be non-empty")
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    if step < 0:
        raise ValueError(f"step must be >= 0, got {step}")
    n = len(items)
    return [items[(step * size + i) % n] for i in range(size)]


class BatchTrajectorySource:
    """BatchTrain 的轨迹源:读带 sha256 清单的数据集。

    直接调 simulator.dataset.verify_manifest:逐条读 episode,用规范化 JSON 重算
    sha256 比对清单,再重算整体 manifest_hash,不依赖 YAML 排版。任何不一致
    (清单被改、episode 被改或丢失、清单为空)抛 DatasetIntegrityError,不静默跳过。

    本类不判断组大小:SFT 数据集每组一条是正常的。GRPO 阶段启动前的组大小校验
    属于 grouping.validate_groups。每次 collect() 都重新读盘并重新校验,
    所以返回的 episode 彼此独立,调用方改动不会影响下一次 collect()。
    """

    def __init__(self, store: GitStore, manifest: DatasetManifest) -> None:
        self._store = store
        self._manifest = manifest

    def collect(self) -> TrajectoryBatch:
        episodes = verify_manifest(self._store, self._manifest)
        return TrajectoryBatch(
            episodes=episodes,
            metadata={
                "manifest_id": self._manifest.id,
                "manifest_hash": self._manifest.manifest_hash,
                "episode_count": len(episodes),
            },
        )
