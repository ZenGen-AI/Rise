"""晋级基座解析（纯读）：下一轮训练的 base_checkpoint_ref 从这里来，不靠人手填。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from controlplane.envelope import BigRef, InvalidBigRefError
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT

_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")
MODEL_ID = re.compile(r"^zengen-v(\d+)$")


def version_of_file(rel: str) -> int | None:
    """从模型产物路径里取版本号;不是模型文件返回 None。

    模型文件名与 id 的正则原先在四个包里各有一份拷贝,改一处漏三处,所以收到这里。
    """
    match = _MODEL_FILE.search(rel)
    return int(match.group(1)) if match else None


def version_of_id(model_id: str) -> int | None:
    """从 `zengen-vN` 形式的 id 里取版本号;形状不对返回 None。"""
    match = MODEL_ID.match(model_id)
    return int(match.group(1)) if match else None


class MissingCheckpointRefError(Exception):
    """最新的已晋级模型产物里没有可用的 checkpoint_ref(缺失或不合规),无法作为下一轮的训练基座。"""


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
        try:
            ref = BigRef.from_dict(raw)
        except (InvalidBigRefError, KeyError, AttributeError) as exc:
            # 手改进树的坏引用(I8):与缺失同样处理,同样不回退到更早的版本。调用方已把
            # MissingCheckpointRefError 当成确定性失败,InvalidBigRefError 却会从它们那里漏出去。
            raise MissingCheckpointRefError(
                f"{env.id} is the latest effective model but its checkpoint_ref is unusable "
                f"({type(exc).__name__}: {exc}); refusing to fall back to an earlier version"
            ) from exc
        return PromotedBase(model_id=env.id, version=version, checkpoint_ref=ref)
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
