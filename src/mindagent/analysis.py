"""经验分析器:跨 episode 聚合"信号触发了,被选中的技能却不针对它"的模式(闭环集成设计 §8.3)。

纯函数。核心原则"不基于单一个案":受影响的 episode 数不足 min_support,或占比低于 min_ratio,
都不产生发现。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from simple_actio.errors import SimpleActioDataError
from simple_actio.signals import SignalRegistry, default_registry
from simple_actio.skills import Skill
from simple_actio.trace import validate_trace
from simple_actio.triggers import referenced_signals
from mindagent.errors import MindAgentDataError
from simulator.datamodel import Episode, TurnRecord

# 读"对方最后一句"的内置信号。first_turn 看的是说话方自己有没有说过话,不在此列。
OPPONENT_SIGNALS: tuple[str, ...] = ("emotion_high", "no_new_information")


@dataclass(frozen=True)
class BehaviorFinding:
    id: str
    signal: str
    pattern: str
    support: int
    ratio: float
    episode_refs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal": self.signal,
            "pattern": self.pattern,
            "support": self.support,
            "ratio": self.ratio,
            "episode_refs": list(self.episode_refs),
        }


def finding_id(signal: str, episode_refs: list[str] | tuple[str, ...]) -> str:
    """由信号名与(排序后的)episode 集合推出,同输入同 id。"""
    digest = hashlib.sha256("\n".join([signal, *sorted(episode_refs)]).encode("utf-8")).hexdigest()
    return f"finding-{signal}-{digest[:12]}"


def visible_history(turns: list[TurnRecord], index: int, slot: str) -> list[TurnRecord]:
    """第 index 轮发言前,slot 能看到的历史:与 runner 给后端的那份同一条规则(对方的内心独白与轨迹抹掉)。"""
    return [
        t if t.speaker_slot == slot else replace(t, inner_thought=None, activation_trace=None)
        for t in turns[:index]
    ]


def agent_slot_of_episode(episode: Episode) -> str:
    slots = [b.slot_id for b in episode.role_assignment.bindings if b.model_backend == "self_play"]
    if len(slots) != 1:
        raise MindAgentDataError(
            f"episode {episode.id}: expected exactly one self_play binding, got {slots}"
        )
    return slots[0]


def signal_turns(episode: Episode, signal: str, registry: SignalRegistry) -> list[int]:
    """该 episode 里 signal 在 agent 发言前为真的那些 agent 轮次的 turn_index。"""
    return _signal_turns(episode, agent_slot_of_episode(episode), signal, registry)


def _signal_turns(episode: Episode, slot: str, signal: str, registry: SignalRegistry) -> list[int]:
    fn = registry.get(signal)
    return [
        t.turn_index
        for i, t in enumerate(episode.turns)
        if t.speaker_slot == slot and fn(visible_history(episode.turns, i, slot), slot)
    ]


def analyze(
    episodes: list[Episode] | tuple[Episode, ...],
    *,
    min_support: int,
    min_ratio: float,
    skills: list[Skill] | tuple[Skill, ...] = (),
    registry: SignalRegistry | None = None,
    signals: tuple[str, ...] | None = None,
) -> list[BehaviorFinding]:
    """对每个信号,统计受影响的 episode:至少有一个 agent 轮次,信号在它发言前为真,而轨迹里
    被选中的技能不为空且其触发条件不引用该信号。ratio = 受影响数 / 有 agent 轮次的 episode 数。

    - 轨迹为 None(裸模型)或 selected 为 None 的轮次不算受影响:没有路由,谈不上"路由错了"。
    - skills 是这批 episode 所用的技能库;轨迹选中了库里没有的技能抛 MindAgentDataError。
    - 结果按信号名排序。
    """
    if isinstance(min_support, bool) or not isinstance(min_support, int) or min_support < 1:
        raise ValueError(f"min_support must be an int >= 1, got {min_support!r}")
    if isinstance(min_ratio, bool) or not isinstance(min_ratio, (int, float)) or not 0.0 <= min_ratio <= 1.0:
        raise ValueError(f"min_ratio must be a number in [0, 1], got {min_ratio!r}")
    reg = registry if registry is not None else default_registry()
    # 去重:同名信号重复给出会产出 id 相同的两条发现
    names = list(dict.fromkeys(s for s in (signals if signals is not None else OPPONENT_SIGNALS) if s in reg))
    targets = {s.id: referenced_signals(s.trigger) for s in skills}

    counted: list[tuple[Episode, str]] = []
    for e in episodes:
        slot = agent_slot_of_episode(e)
        if any(t.speaker_slot == slot for t in e.turns):
            counted.append((e, slot))
    if not counted:
        return []
    findings: list[BehaviorFinding] = []
    for signal in sorted(names):
        affected: list[str] = []
        for episode, slot in counted:
            turns_by_index = {t.turn_index: t for t in episode.turns}
            for idx in _signal_turns(episode, slot, signal, reg):
                trace = turns_by_index[idx].activation_trace
                if trace is not None:
                    try:
                        validate_trace(trace)  # 计划 6:消费方读落盘 episode 时校验轨迹
                    except SimpleActioDataError as exc:
                        raise MindAgentDataError(f"episode {episode.id} turn {idx}: {exc}") from None
                selected = trace["selected"] if trace is not None else None
                if selected is None:
                    continue
                if selected not in targets:
                    raise MindAgentDataError(
                        f"episode {episode.id} turn {idx}: selected skill {selected!r} is not in the library"
                    )
                if signal not in targets[selected]:
                    affected.append(episode.id)
                    break
        support = len(affected)
        ratio = support / len(counted)
        if support < min_support or ratio < min_ratio:
            continue
        refs = tuple(sorted(affected))
        findings.append(BehaviorFinding(
            id=finding_id(signal, refs),
            signal=signal,
            pattern=f"signal {signal} fired but the selected skill did not target it",
            support=support,
            ratio=ratio,
            episode_refs=refs,
        ))
    return findings
