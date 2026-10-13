"""晋级基座解析（纯读）：下一轮训练的 base_checkpoint_ref 从这里来，不靠人手填。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT

_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")


class MissingCheckpointRefError(Exception):
    """最新的已晋级模型产物里没有 checkpoint_ref，无法作为下一轮的训练基座。"""


@dataclass(frozen=True)
class PromotedBase:
    model_id: str  # 如 "zengen-v3"
    version: int
    checkpoint_ref: BigRef


def latest_promoted_checkpoint(store: GitStore) -> PromotedBase | None:
    """返回版本号最大、且状态为 effective 的 zengen 模型及其 checkpoint 引用。

    - 版本按**数字**比较（字符串序会把 v10 排在 v9 前面）。
    - deprecated/superseded 的模型不算"当前基座"，跳过。
    - 没有任何已晋级模型 → None，由调用方自己提供初始基座（如开源底座权重）。
    - 最新的 effective 模型缺 checkpoint_ref → 抛 MissingCheckpointRefError，
      **不**静默回退到更早的版本：悄悄换基座会让训练结果无法追溯。
    """
    candidates: list[tuple[int, str]] = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = _MODEL_FILE.search(rel)
        if match:
            candidates.append((int(match.group(1)), rel))

    for version, rel in sorted(candidates, reverse=True):
        env = store.read_product(rel)
        if env.status != "effective":
            continue
        raw = env.payload.get("checkpoint_ref")
        if raw is None:
            raise MissingCheckpointRefError(
                f"{env.id} is the latest effective model but its payload has no "
                f"checkpoint_ref; refusing to fall back to an earlier version"
            )
        return PromotedBase(model_id=env.id, version=version, checkpoint_ref=BigRef.from_dict(raw))
    return None


def max_model_version(store: GitStore) -> int:
    """models/ 下最大的 zengen 版本号;没有模型返回 0。

    新模型的版本号是它加一,不数文件个数:手工删掉中间的版本会留下空洞,但新号绝不会撞上
    已经用过的号。"""
    versions = [
        int(match.group(1))
        for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models")
        if (match := _MODEL_FILE.search(rel))
    ]
    return max(versions, default=0)
