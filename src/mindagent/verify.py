"""提案验证器(闭环集成设计 §8.3、R13):离线重放动机 episode,度量"行为是否按预期改变"。

用保存下来的对方原话重放:对方轮次照抄,agent 轮次由实例重新生成(不起服务端)。分别用基线实例
与加载候选技能后的实例驱动,度量候选技能在动机轮次(动机 episode 里该信号为真的 agent 轮次)上被
选中的比例 fired_on_motivating,以及在动机 episode 其余 agent 轮次上被选中的比例 fired_elsewhere。
它不度量对话质量;可选传入 judge 桥接做质量评分,结果单列在 quality_notes。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from actio.skills import Skill
from mindagent.analysis import BehaviorFinding, agent_slot_of_episode, visible_history
from mindagent.errors import MindAgentDataError
from mindagent.proposals import ProposalDraft
from simulator.datamodel import Episode, Persona, Scenario, TurnRecord

METHOD = "replay_behavior"
OVERFIT_NOTE = (
    "verified on the same episodes that motivated the proposal (no held-out set); "
    "overfitting risk, keep monitoring in later rounds"
)


@dataclass(frozen=True)
class Verification:
    method: str
    episodes: tuple[str, ...]
    fired_on_motivating: float
    fired_elsewhere: float
    notes: str
    quality_notes: str | None = None

    def to_dict(self) -> dict:
        d = {
            "method": self.method,
            "episodes": list(self.episodes),
            "fired_on_motivating": self.fired_on_motivating,
            "fired_elsewhere": self.fired_elsewhere,
            "notes": self.notes,
        }
        if self.quality_notes is not None:
            d["quality_notes"] = self.quality_notes
        return d


def replay(instance, episode: Episode, persona: Persona, scenario: Scenario) -> list[TurnRecord]:
    """按保存下来的对方原话重放一条 episode,返回重放出的完整轮次(agent 轮次带新轨迹)。"""
    slot = agent_slot_of_episode(episode)
    turns: list[TurnRecord] = []
    for saved in episode.turns:
        if saved.speaker_slot != slot:
            turns.append(TurnRecord(saved.turn_index, saved.speaker_slot, saved.utterance))
            continue
        history = visible_history(turns, len(turns), slot)
        utterance, thought, trace = instance.respond(persona, scenario, history, slot)
        turns.append(TurnRecord(saved.turn_index, slot, utterance, thought, trace))
    return turns


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def verify(
    draft: ProposalDraft,
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    episodes: list[Episode] | tuple[Episode, ...],
    *,
    base_instance_factory: Callable[[tuple[Skill, ...]], object],
    personas: dict[str, Persona],
    scenarios: dict[str, Scenario],
    judge=None,
    run_id: str = "",
    task_id: str = "mindagent-judge",
) -> Verification:
    """base_instance_factory(extra_skills) 返回一个新实例:extra_skills 为空是基线,
    为草案的技能是候选。episodes 里必须包含发现引用的全部动机 episode。
    传入 judge 时 run_id 必须非空(judge 是桥接,会记账),否则抛 MindAgentDataError。"""
    if judge is not None and not run_id:
        raise MindAgentDataError("verify with a judge needs a non-empty run_id")
    finding = next((f for f in findings if f.id == draft.finding_id), None)
    if finding is None:
        raise MindAgentDataError(f"draft refers to unknown finding {draft.finding_id!r}")
    by_id = {e.id: e for e in episodes}
    missing = [ref for ref in finding.episode_refs if ref not in by_id]
    if missing:
        raise MindAgentDataError(f"finding {finding.id}: motivating episodes not given: {missing}")
    candidate = tuple(Skill.from_dict(d) for d in draft.skill_definitions())
    candidate_ids = {s.id for s in candidate}
    baseline = base_instance_factory(())
    loaded = base_instance_factory(candidate)

    on_hits = on_total = else_hits = else_total = base_on_hits = 0
    pairs: list[tuple[str, str, str]] = []
    for ref in finding.episode_refs:
        episode = by_id[ref]
        slot = agent_slot_of_episode(episode)
        persona_id = next(b.persona_id for b in episode.role_assignment.bindings if b.slot_id == slot)
        persona, scenario = personas[persona_id], scenarios[episode.scenario_id]
        base_turns = replay(baseline, episode, persona, scenario)
        cand_turns = replay(loaded, episode, persona, scenario)
        signal_fn = loaded.registry.get(finding.signal)
        for i, (b, c) in enumerate(zip(base_turns, cand_turns)):
            if c.speaker_slot != slot:
                continue
            fired = c.activation_trace["selected"] in candidate_ids
            if signal_fn(visible_history(cand_turns, i, slot), slot):
                on_total += 1
                on_hits += fired
                base_on_hits += b.activation_trace["selected"] in candidate_ids
                pairs.append((ref, b.utterance, c.utterance))
            else:
                else_total += 1
                else_hits += fired
    notes = (
        f"motivating turns {on_total}, other turns {else_total}; "
        f"baseline selected the candidate on {base_on_hits} motivating turn(s); {OVERFIT_NOTE}"
    )
    quality = None
    if judge is not None:
        prompt = "对比每组里基线回复与候选回复的对话质量,给出简短评价:\n" + "\n".join(
            f"[{ref}] 基线:{b} | 候选:{c}" for ref, b, c in pairs
        )
        quality = judge.complete(prompt, run_id=run_id, task_id=task_id).text
    return Verification(
        method=METHOD,
        episodes=tuple(finding.episode_refs),
        fired_on_motivating=_ratio(on_hits, on_total),
        fired_elsewhere=_ratio(else_hits, else_total),
        notes=notes,
        quality_notes=quality,
    )
