from diagnostic.datamodel import DiagnosticItem
from diagnostic.dynamic_track import run_dynamic_item, DynamicOutcome
from simulator.datamodel import Scenario, RoleSlot, Persona, RoleAssignment, RoleBinding
from simulator.backends import ScriptedBackend


def _dyn_fixture():
    scenario = Scenario("s1", "bg",
        [RoleSlot("candidate", "被测"), RoleSlot("opponent", "对手")],
        "c", ["n"], "persuasion")
    personas = {"pc": Persona("pc", ["t"], "m", "s", ["v"]),
                "po": Persona("po", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment("s1",
        [RoleBinding("candidate", "pc", "self_play"),
         RoleBinding("opponent", "po", "frozen_reference")], max_turns=4)
    backends = {"candidate": ScriptedBackend({"candidate": ["ok", "agreed", "ok"]}),
                "opponent": ScriptedBackend({"opponent": ["no", "no", "no"]})}
    item = DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic",
                          None, {"goal_keyword": "agreed"}, None)
    return item, scenario, assignment, personas, backends


def test_run_dynamic_item_produces_episode_and_outcome():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e1")
    assert isinstance(outcome, DynamicOutcome)
    assert outcome.episode.id == "dyn-e1"
    assert outcome.candidate_slot == "candidate"
    assert outcome.goal_reached is True
    assert outcome.episode.terminated_reason == "goal_reached"


def test_run_dynamic_item_no_goal_runs_full_and_marks_unreached():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    item.rubric = {"goal_keyword": "NEVER_SAID"}
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e2")
    assert outcome.goal_reached is False
    assert outcome.episode.terminated_reason == "max_turns"


def test_candidate_utterances_extracted_for_scoring():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e3")
    assert all("candidate" == t.speaker_slot for t in
               [outcome.episode.turns[i] for i in outcome.candidate_turn_indices])
