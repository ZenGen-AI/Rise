from __future__ import annotations

import copy
from dataclasses import dataclass, field

from controlplane.envelope import BigRef
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


@dataclass(frozen=True)
class StepResult:
    """执行器单步的结果。指标名是契约,后续做"训练奖励 vs 诊断分"背离检测靠它们稳定。"""

    step: int  # 全局 step,从 0 起
    phase: str  # "sft" | "grpo_opd"
    metrics: dict[str, float]


@dataclass
class TrainingRun:
    """训练运行记录(产物 type="training_run")。

    status 属于信封而不属于业务字段,这里带上只是为了让 decide_resume 能在一个对象上
    判定:pending(运行中)| executed(成功)| failed(确定性失败,保留)。
    failure 仅在 failed 时有值:{"category": "T2", "point": ..., "detail": ...}。
    逐步指标序列不进 git(补充 D.2),只留 curve_summary 摘要和指标流引用。
    metric_stream_refs 的键名固定,rismforge trace 读的就是它。
    """

    run_id: str
    config_hash: str
    checkpoint_step: int = 0  # 最近一次中间 checkpoint 对应的全局 step;从未存点则为 0
    last_checkpoint_ref: BigRef | None = None
    resume_attempts: int = 0
    curve_summary: dict = field(default_factory=dict)
    metric_stream_refs: list[BigRef] = field(default_factory=list)
    status: str = "pending"
    failure: dict | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "checkpoint_step": self.checkpoint_step,
            "last_checkpoint_ref": (
                self.last_checkpoint_ref.to_dict() if self.last_checkpoint_ref else None
            ),
            "resume_attempts": self.resume_attempts,
            "curve_summary": copy.deepcopy(self.curve_summary),
            "metric_stream_refs": [r.to_dict() for r in self.metric_stream_refs],
            "failure": copy.deepcopy(self.failure),
        }

    @classmethod
    def from_dict(cls, d: dict, *, status: str = "pending") -> "TrainingRun":
        ref = d.get("last_checkpoint_ref")
        return cls(
            run_id=d["run_id"],
            config_hash=d["config_hash"],
            checkpoint_step=d.get("checkpoint_step", 0),
            last_checkpoint_ref=BigRef.from_dict(ref) if ref else None,
            resume_attempts=d.get("resume_attempts", 0),
            curve_summary=copy.deepcopy(d.get("curve_summary", {})),
            metric_stream_refs=[BigRef.from_dict(r) for r in d.get("metric_stream_refs", [])],
            status=status,
            failure=copy.deepcopy(d.get("failure")),
        )
