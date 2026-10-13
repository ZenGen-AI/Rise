import pytest

from actio.instance import instantiate
from actio.skills import Skill
from actio.trace import build_trace
from inference_bridge import ScriptedBridge
from mindagent.analysis import BehaviorFinding, analyze
from mindagent.errors import MindAgentDataError
from mindagent.proposals import ProposalDraft, propose
from mindagent.verify import METHOD, OVERFIT_NOTE, Verification, replay, verify
from simulator.datamodel import Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord

LV = {"skill": "1", "memory": "0"}
GOAL = Skill("skill-goal", 1, "macro", {"turn_at_least": 0}, ("推进目标",))
ANGRY = "我真的很生气!!"
PERSONAS = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
SCENARIOS = {"s1": Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "deployment_simulation")}


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", None)


def _factory(base_skills=(GOAL,)):
    def make(extra=()):
        return instantiate(Echo(), skills=list(base_skills) + list(extra), memories=[],
                           library_version=LV, profile=None)
    return make


def _episode(eid, opp_lines):
    turns = []
    for line in opp_lines:
        turns.append(TurnRecord(len(turns), "b", line, "opp-thought"))
        trace = build_trace(signals=[], candidates=["skill-goal"], selected="skill-goal",
                            memory_hits=[], experience=None, library_version=LV)
        turns.append(TurnRecord(len(turns), "a", "old reply", None, trace))
    ra = RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
                               RoleBinding("a", "pa", "self_play")], len(turns))
    return Episode(eid, "s1", ra, turns, "max_turns")


def _setup(lines=(ANGRY, "好吧")):
    eps = [_episode(f"e{i}", list(lines)) for i in range(3)]
    findings = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL], signals=("emotion_high",))
    return eps, findings, propose(findings)


def test_the_candidate_fires_on_every_motivating_turn_and_nowhere_else():
    eps, findings, (draft,) = _setup()
    v = verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert isinstance(v, Verification)
    assert v.method == METHOD == "replay_behavior"
    assert v.episodes == ("e0", "e1", "e2")
    assert (v.fired_on_motivating, v.fired_elsewhere) == (1.0, 0.0)
    assert "motivating turns 3, other turns 3" in v.notes
    assert "baseline selected the candidate on 0 motivating turn(s)" in v.notes
    assert OVERFIT_NOTE in v.notes
    assert v.quality_notes is None
    assert set(v.to_dict()) == {"method", "episodes", "fired_on_motivating", "fired_elsewhere", "notes"}


def test_a_candidate_that_also_fires_elsewhere_is_measured_as_such():
    eps, findings, (draft,) = _setup()
    broad = dict(draft.increments[0]["definition"], trigger={"turn_at_least": 0})
    wide = ProposalDraft(draft.finding_id, ({"kind": "skill", "definition": broad},), draft.generator)
    v = verify(wide, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert (v.fired_on_motivating, v.fired_elsewhere) == (1.0, 1.0)


def test_a_candidate_that_loses_routing_does_not_fire():
    eps, findings, (draft,) = _setup()
    weak = dict(draft.increments[0]["definition"], priority=-1)
    low = ProposalDraft(draft.finding_id, ({"kind": "skill", "definition": weak},), draft.generator)
    v = verify(low, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert v.fired_on_motivating == 0.0


def test_replay_keeps_opponent_lines_and_regenerates_agent_turns_without_leaking_thoughts():
    seen = []

    class Recorder(Echo):
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            seen.append([(t.speaker_slot, t.utterance, t.inner_thought) for t in history])
            return super().respond_guided(persona, scenario, history, speaker_slot, guidance)

    inst = instantiate(Recorder(), skills=[GOAL], memories=[], library_version=LV, profile=None)
    turns = replay(inst, _episode("e0", [ANGRY, "好吧"]), PERSONAS["pa"], SCENARIOS["s1"])
    assert [(t.speaker_slot, t.utterance) for t in turns] == [
        ("b", ANGRY), ("a", "[skill:skill-goal] 推进目标"), ("b", "好吧"), ("a", "[skill:skill-goal] 推进目标")]
    assert seen[0] == [("b", ANGRY, None)]
    assert all(t.activation_trace["selected"] == "skill-goal" for t in turns if t.speaker_slot == "a")


def test_a_judge_adds_quality_notes_in_their_own_field():
    eps, findings, (draft,) = _setup()
    judge = ScriptedBridge(["候选回复更体贴"])
    v = verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS,
               scenarios=SCENARIOS, judge=judge, run_id="run-003", task_id="agent-zengen-v1-judge")
    assert v.quality_notes == "候选回复更体贴"
    assert v.to_dict()["quality_notes"] == "候选回复更体贴"
    ((prompt, run_id, task_id),) = judge.calls
    assert (run_id, task_id) == ("run-003", "agent-zengen-v1-judge")
    assert prompt.count("[e") == 3 and "候选:[skill:skill-candidate-emotion_high]" in prompt


def test_a_judge_without_a_run_id_is_rejected_before_any_call():
    eps, findings, (draft,) = _setup()
    judge = ScriptedBridge(["x"])
    with pytest.raises(MindAgentDataError, match="non-empty run_id"):
        verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS,
               scenarios=SCENARIOS, judge=judge)
    assert judge.calls == []


def test_missing_motivating_episodes_and_unknown_findings_are_data_errors():
    eps, findings, (draft,) = _setup()
    with pytest.raises(MindAgentDataError, match="motivating episodes not given"):
        verify(draft, findings, eps[:1], base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    orphan = ProposalDraft("finding-ghost", draft.increments, draft.generator)
    with pytest.raises(MindAgentDataError, match="unknown finding"):
        verify(orphan, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)


def test_verify_does_not_touch_its_inputs():
    eps, findings, (draft,) = _setup()
    before = [e.to_dict() for e in eps]
    verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert [e.to_dict() for e in eps] == before
