from __future__ import annotations

import math

from mindtrainer.datamodel import RewardedTrajectory

# 零方差判定的容差。奖励由同一套确定性计算得出,相同输入得到逐位相同的浮点数;
# 容差只是为了 0.1+0.2 与 0.3 这类等价写法不被误判成"有差异"。
_EPS = 1e-12


class DegenerateGroupError(Exception):
    """有组的成员数不足 min_group_size。一组只有 1 条时优势恒为 0,训练会静默地
    什么都学不到,所以这是错误,不是警告。group_ids 列出全部不合格的组。"""

    def __init__(self, group_ids: list[str], min_group_size: int) -> None:
        super().__init__(
            f"{len(group_ids)} group(s) smaller than min_group_size={min_group_size}: "
            f"{list(group_ids)}"
        )
        self.group_ids = list(group_ids)
        self.min_group_size = min_group_size


def group_by_id(
    rewarded: list[RewardedTrajectory],
) -> dict[str, list[RewardedTrajectory]]:
    """按 group_id 分组。组的顺序是首次出现的顺序,组内顺序保持输入顺序。"""
    groups: dict[str, list[RewardedTrajectory]] = {}
    for r in rewarded:
        groups.setdefault(r.group_id, []).append(r)
    return groups


def _checked(group_id: str, rewards: list[float]) -> list[float]:
    if not rewards:
        raise ValueError(f"group {group_id!r} is empty")
    if not all(math.isfinite(r) for r in rewards):
        raise ValueError(f"group {group_id!r} has a non-finite reward: {rewards}")
    return rewards


def group_advantages(groups: dict[str, list[float]]) -> dict[str, list[float]]:
    """组内优势:基线取该组 combined_reward 的均值,优势 = reward - mean。

    不除标准差——那是真实执行器通过 grpo_config 自己决定的事。各组的基线互相独立。
    零方差组(奖励在 _EPS 内相同)的优势精确为 0.0。
    空组或含 NaN/inf 的组抛 ValueError(NaN 会毒化整组均值)。"""
    out: dict[str, list[float]] = {}
    for group_id, rewards in groups.items():
        _checked(group_id, rewards)
        if max(rewards) - min(rewards) <= _EPS:
            # 零方差组的优势必须精确为 0:三个 0.7 的均值是 0.7+1e-16,
            # 不吸附的话舍入误差会被当成梯度信号喂给执行器。
            out[group_id] = [0.0] * len(rewards)
            continue
        baseline = math.fsum(rewards) / len(rewards)
        out[group_id] = [r - baseline for r in rewards]
    return out


def trajectory_advantages(rewarded: list[RewardedTrajectory]) -> list[float]:
    """与输入顺序逐条对齐的优势列表(不同组的样本可以交错出现)。"""
    groups = group_by_id(rewarded)
    advantages = group_advantages(
        {gid: [r.combined_reward for r in members] for gid, members in groups.items()}
    )
    cursors = {gid: iter(values) for gid, values in advantages.items()}
    return [next(cursors[r.group_id]) for r in rewarded]


def count_degenerate_groups(groups: dict[str, list[float]]) -> int:
    """组内零方差(奖励全相同)的组数,供训练曲线的 n_degenerate_groups 指标。

    这不是错误:成员数够、只是奖励恰好一样,是数据的正常情形,只计数不报错。
    单成员组天然零方差,也会被计入(但 validate_groups 应先把它们拦下)。"""
    return sum(
        1
        for group_id, rewards in groups.items()
        if max(_checked(group_id, rewards)) - min(rewards) <= _EPS
    )


def validate_groups(groups: dict[str, list], min_group_size: int) -> None:
    """有组成员数不足 min_group_size 就抛 DegenerateGroupError(列出全部不合格组 id,
    按字典序)。零方差组不触发它。min_group_size < 2 本身就没有意义,抛 ValueError。"""
    if min_group_size < 2:
        raise ValueError(f"min_group_size must be >= 2, got {min_group_size}")
    too_small = sorted(gid for gid, members in groups.items() if len(members) < min_group_size)
    if too_small:
        raise DegenerateGroupError(too_small, min_group_size)
