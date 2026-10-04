from __future__ import annotations

import math
from typing import Protocol

from mindtrainer.datamodel import RewardedTrajectory, TrajectoryBatch
from mindtrainer.training_config import COMBINATIONS, RewardConfig
from simulator.datamodel import Episode
from simulator.dataset import episode_group_id


class AttributionError(ValueError):
    """归因结果不合法,或无法产生归因。确定性失败:不静默跳过、不重试。"""


class SelfEvalBiasError(Exception):
    """judge 与被训模型相同或互为前缀。训练奖励比诊断更危险:模型会被直接优化去
    讨好自己的 judge,所以构造时就拒绝。"""


class AttributionBackend(Protocol):
    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        """返回每个分量逐轮的分数,取值 [0,1];非训练角色位的轮次为 None。"""
        ...


class TurnJudge(Protocol):
    model_id: str

    def score_turn(self, episode: Episode, turn_index: int, component: str) -> float: ...


# ---------------- 规则归因 ----------------

class OutcomeSplitAttribution:
    """规则式归因。每个分量先算一个结果分 o:训练角色位任一发言含任一关键词则 1.0,
    否则 0.0(纯子串、区分大小写)。只看训练角色位的 utterance,对手的话和
    inner_thought 都不算。

    第 k 个训练轮(共 K 个)的得分是 o * gamma ** (K-1-k),即越靠近结尾越高;
    gamma=1.0 时各训练轮同分。**不归一化**,所以仍在 [0,1]:归一化后的份额相乘
    没有意义,逐轮分数相乘才是门控。

    刻意不做 terminated_reason == goal_reached 的规则:Interact 的 episode 永远不会
    以 goal_reached 结束(driver 接管了 stop_predicate),那条规则恒为 0。"""

    def __init__(self, rules: dict[str, list[str]], gamma: float = 1.0) -> None:
        if not (0 < gamma <= 1):  # NaN 也会落到这里
            raise ValueError(f"gamma must be in (0, 1], got {gamma}")
        self._rules = {name: list(words) for name, words in rules.items()}
        self._gamma = gamma

    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        trained = [i for i, t in enumerate(episode.turns) if t.speaker_slot == trained_slot]
        last = len(trained) - 1
        out: dict[str, list[float | None]] = {}
        for name in components:
            keywords = self._rules.get(name)
            if not keywords:
                raise AttributionError(f"no outcome rule for component {name!r}")
            hit = any(kw in episode.turns[i].utterance for i in trained for kw in keywords)
            outcome = 1.0 if hit else 0.0
            scores: list[float | None] = [None] * len(episode.turns)
            for k, i in enumerate(trained):
                scores[i] = outcome * self._gamma ** (last - k)
            out[name] = scores
        return out


# ---------------- judge 归因 ----------------

def _is_near_relative(a: str, b: str) -> bool:
    # 双向前缀:"x-sft" 评 "x" 与 "x" 评 "x-sft" 同样有偏。相同的情形被它包含。
    # 这只是保守的前缀匹配,不是真正的模型血缘判断。故意不 import
    # diagnostic.scorer 的私有函数:MindTrainer 不依赖 diagnostic,两份各自测试。
    return a.startswith(b) or b.startswith(a)


class MockTurnJudge:
    """固定表 judge。表缺键直接 KeyError,与 Plan 3 的 mock 一致:漏配就响,不给默认分。
    键是 (episode_id, turn_index, component)。"""

    def __init__(self, model_id: str, table: dict[tuple[str, int, str], float]) -> None:
        self.model_id = model_id
        self._table = dict(table)

    def score_turn(self, episode: Episode, turn_index: int, component: str) -> float:
        return self._table[(episode.id, turn_index, component)]


class JudgeAttribution:
    """judge 归因:只对训练角色位的轮次调用 judge,其余填 None。judge 返回的分数
    不在这里校验——统一由 validate_attribution 把关。"""

    def __init__(self, judge: TurnJudge, trained_model_id: str) -> None:
        if not judge.model_id or not trained_model_id:
            raise ValueError("judge.model_id and trained_model_id must be non-empty")
        if _is_near_relative(judge.model_id, trained_model_id):
            raise SelfEvalBiasError(
                f"judge model {judge.model_id!r} is too close to the trained model "
                f"{trained_model_id!r} (self-eval bias risk)"
            )
        self._judge = judge

    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        return {
            name: [
                self._judge.score_turn(episode, t.turn_index, name)
                if t.speaker_slot == trained_slot
                else None
                for t in episode.turns
            ]
            for name in components
        }


def build_attribution_backend(
    config: RewardConfig,
    *,
    judge: TurnJudge | None = None,
    trained_model_id: str | None = None,
) -> AttributionBackend:
    """按配置组装归因后端,并核对"配置里写的 judge"与"实际传入的 judge"一致——
    配置被哈希锚定,实际用了别的 judge 就无法按配方复现。"""
    if config.attribution == "outcome_split":
        if judge is not None:
            raise ValueError("a judge was supplied but reward.attribution is outcome_split")
        return OutcomeSplitAttribution(config.outcome_rules, config.gamma)
    if config.attribution == "judge":
        if judge is None:
            raise ValueError("reward.attribution is judge but the call requires a judge")
        if trained_model_id is None:
            raise ValueError("judge attribution needs trained_model_id for the self-eval guard")
        if judge.model_id != config.judge_model_id:
            raise ValueError(
                f"judge.model_id {judge.model_id!r} does not match "
                f"reward.judge_model_id {config.judge_model_id!r}"
            )
        return JudgeAttribution(judge, trained_model_id)
    raise ValueError(f"unknown attribution {config.attribution!r}")


# ---------------- 校验与组合 ----------------

def validate_attribution(
    episode: Episode,
    trained_slot: str,
    components: list[str],
    scores: dict[str, list[float | None]],
) -> None:
    """纯函数,不通过抛 AttributionError。训练角色位一个发言都没有的 episode 抛错,
    不静默跳过——静默跳过会让数据集悄悄变小。"""
    eid = episode.id
    if set(scores) != set(components):
        raise AttributionError(
            f"episode {eid}: attribution components {sorted(scores)} "
            f"!= configured {sorted(components)}"
        )
    if trained_slot not in episode.role_assignment.slot_ids():
        raise AttributionError(f"episode {eid}: slot {trained_slot!r} is not bound")
    trained = [t.speaker_slot == trained_slot for t in episode.turns]
    if not any(trained):
        raise AttributionError(f"episode {eid}: no turns from trained slot {trained_slot!r}")
    for name in components:
        values = scores[name]
        if len(values) != len(episode.turns):
            raise AttributionError(
                f"episode {eid}: component {name!r} has {len(values)} scores "
                f"for {len(episode.turns)} turns"
            )
        for i, (value, is_trained) in enumerate(zip(values, trained)):
            if not is_trained:
                if value is not None:
                    raise AttributionError(
                        f"episode {eid}: component {name!r} turn {i}: "
                        f"non-trained turn must be None, got {value!r}"
                    )
                continue
            if value is None:
                raise AttributionError(
                    f"episode {eid}: component {name!r} turn {i}: trained turn has no score"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not (0.0 <= value <= 1.0)
            ):
                raise AttributionError(
                    f"episode {eid}: component {name!r} turn {i}: "
                    f"score {value!r} is not a finite number in [0, 1]"
                )


def combine(
    scores: dict[str, list[float | None]], components: list[str], combination: str
) -> list[float | None]:
    """逐轮把各分量按 sum 或 product 合成。不做权重——分量都在 [0,1] 上,保持最小。
    按 components 的顺序累加,结果与字典顺序无关。"""
    if combination not in COMBINATIONS:
        raise ValueError(f"combination must be one of {list(COMBINATIONS)}, got {combination!r}")
    if not components:
        raise ValueError("components must be non-empty")
    lengths = {len(scores[name]) for name in components}
    if len(lengths) != 1:
        raise ValueError(f"components have different lengths: {sorted(lengths)}")
    out: list[float | None] = []
    for i in range(lengths.pop()):
        values = [scores[name][i] for name in components]
        if all(v is None for v in values):
            out.append(None)
        elif any(v is None for v in values):
            raise ValueError(f"turn {i}: components disagree on whether it is a trained turn")
        elif combination == "sum":
            out.append(sum(values))
        else:
            out.append(math.prod(values))
    return out


# ---------------- 入口 ----------------

def run_reward_pipeline(
    batch: TrajectoryBatch,
    config: RewardConfig,
    backend: AttributionBackend,
    *,
    trained_slot: str,
) -> list[RewardedTrajectory]:
    """批到批的变换,每次训练迭代都可重跑。combined_reward 取训练轮的**均值**,
    不取总和:总和随 episode 变长而变大,会奖励拖长对话。"""
    components = list(config.components)
    out: list[RewardedTrajectory] = []
    for episode in batch.episodes:
        scores = backend.attribute(episode, trained_slot, components)
        validate_attribution(episode, trained_slot, components, scores)
        per_turn = combine(scores, components, config.combination)
        trained_rewards = [r for r in per_turn if r is not None]
        out.append(
            RewardedTrajectory(
                episode=episode,
                group_id=episode_group_id(episode),
                per_turn_rewards=per_turn,
                combined_reward=math.fsum(trained_rewards) / len(trained_rewards),
            )
        )
    return out


def summarize_rewards(rewarded: list[RewardedTrajectory]) -> dict:
    """写进训练曲线摘要。"训练奖励 vs 诊断分背离"的检测靠这条曲线,
    MindTrainer 只记录,不读诊断。"""
    if not rewarded:
        raise ValueError("cannot summarize zero trajectories")
    return {
        "mean": math.fsum(r.combined_reward for r in rewarded) / len(rewarded),
        "n": len(rewarded),
    }
