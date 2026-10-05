from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import training_run_file
from controlplane.states import NONREVIEW, assert_transition
from mindtrainer.datamodel import TrainingRun

# 沿用仓库既有的固定时间戳写法:产物内容必须是输入的纯函数,
# 崩溃后重放才会逐字节相同(GitStore.write_product 对此是空操作)。
_NOW = "2026-10-03T00:00:00Z"


def write_training_run(store: GitStore, run: TrainingRun) -> str:
    """写 training_run 产物。状态机沿用非审核类:pending -> executed | failed。
    同状态重写(pending -> pending,更新 checkpoint 进度)允许;其余非法迁移抛
    InvalidTransitionError,所以 executed/failed 的记录不会被悄悄改回 pending。"""
    if run.status == "failed" and not (run.failure and run.failure.get("category")):
        # 失败是数据:failed 记录必须说明为什么失败,否则事后无法追查
        raise ValueError(f"training_run {run.run_id}: a failed run needs failure['category']")
    path = training_run_file(run.run_id)
    if store.exists(path):
        previous = store.read_product(path).status
        if previous != run.status:
            assert_transition(NONREVIEW, previous, run.status)
    env = Envelope(
        id=f"trainrun-{run.run_id}",
        type="training_run",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status=run.status,
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[run.run_id],
        payload=run.to_dict(),
    )
    store.write_product(path, env, f"training_run: {run.run_id} [{run.status}]")
    return path


def read_training_run(store: GitStore, run_id: str) -> TrainingRun | None:
    """不存在返回 None。读取走 read_product(经迁移器链),状态取自信封。"""
    path = training_run_file(run_id)
    if not store.exists(path):
        return None
    env = store.read_product(path)
    return TrainingRun.from_dict(env.payload, status=env.status)
