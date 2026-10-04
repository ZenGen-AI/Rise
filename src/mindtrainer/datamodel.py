from __future__ import annotations

import copy
from dataclasses import dataclass, field

from simulator.datamodel import Episode


@dataclass
class TrajectoryBatch:
    """统一轨迹格式(spec §3.3)。两种轨迹源产出同一格式,下游不关心来源。

    metadata 约定键:
    - batch:    manifest_id、manifest_hash、episode_count
    - interact: episode_ids、failed_attempts
    """

    episodes: list[Episode]
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "episodes": [e.to_dict() for e in self.episodes],
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrajectoryBatch":
        return cls(
            episodes=[Episode.from_dict(e) for e in d["episodes"]],
            metadata=copy.deepcopy(d.get("metadata", {})),
        )


@dataclass
class RewardedTrajectory:
    """带奖励轨迹(spec §3.3)。奖励管线的输出。

    per_turn_rewards 与 episode.turns 逐轮对齐;None 表示该轮不是被训练角色位的发言。
    combined_reward 是训练轮逐轮奖励的均值(不取总和,免得奖励拖长对话)。
    """

    episode: Episode
    group_id: str  # = simulator.dataset.episode_group_id(episode)
    per_turn_rewards: list[float | None]
    combined_reward: float

    def __post_init__(self) -> None:
        if len(self.per_turn_rewards) != len(self.episode.turns):
            raise ValueError(
                f"per_turn_rewards has {len(self.per_turn_rewards)} entries but episode "
                f"{self.episode.id} has {len(self.episode.turns)} turns"
            )

    def to_dict(self) -> dict:
        return {
            "episode": self.episode.to_dict(),
            "group_id": self.group_id,
            "per_turn_rewards": list(self.per_turn_rewards),
            "combined_reward": self.combined_reward,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RewardedTrajectory":
        return cls(
            episode=Episode.from_dict(d["episode"]),
            group_id=d["group_id"],
            per_turn_rewards=list(d["per_turn_rewards"]),
            combined_reward=d["combined_reward"],
        )
