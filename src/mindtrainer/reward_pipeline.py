from __future__ import annotations

from typing import Protocol

from mindtrainer.training_config import RewardConfig
from simulator.datamodel import Episode


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
