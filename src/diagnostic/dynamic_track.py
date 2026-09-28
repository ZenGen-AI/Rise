from __future__ import annotations

from dataclasses import dataclass

from simulator.datamodel import Episode
from simulator.runner import run_episode
from diagnostic.datamodel import DiagnosticItem


@dataclass
class DynamicOutcome:
    episode: Episode
    candidate_slot: str
    goal_reached: bool
    candidate_turn_indices: list[int]

    def to_dict(self) -> dict:
        return {
            "episode": self.episode.to_dict(),
            "candidate_slot": self.candidate_slot,
            "goal_reached": self.goal_reached,
            "candidate_turn_indices": list(self.candidate_turn_indices),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DynamicOutcome:
        return cls(
            episode=Episode.from_dict(d["episode"]),
            candidate_slot=d["candidate_slot"],
            goal_reached=d["goal_reached"],
            candidate_turn_indices=list(d["candidate_turn_indices"]),
        )


def run_dynamic_item(
    item: DiagnosticItem,
    scenario,
    assignment,
    personas,
    backends,
    episode_id: str,
) -> DynamicOutcome:
    candidate_slot = next(
        b.slot_id for b in assignment.bindings if b.model_backend == "self_play"
    )

    goal_keyword = None
    if item.rubric and "goal_keyword" in item.rubric:
        goal_keyword = item.rubric["goal_keyword"]

    stop_predicate = None
    if goal_keyword is not None:
        def stop_predicate(turns):
            last = turns[-1]
            return (
                last.speaker_slot == candidate_slot
                and goal_keyword in last.utterance
            )

    episode = run_episode(
        scenario, assignment, personas, backends, episode_id,
        stop_predicate=stop_predicate,
    )

    goal_reached = episode.terminated_reason == "goal_reached"
    candidate_turn_indices = [
        i for i, t in enumerate(episode.turns) if t.speaker_slot == candidate_slot
    ]

    return DynamicOutcome(
        episode=episode,
        candidate_slot=candidate_slot,
        goal_reached=goal_reached,
        candidate_turn_indices=candidate_turn_indices,
    )
