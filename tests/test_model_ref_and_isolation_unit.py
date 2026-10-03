"""model_ref 贯穿数据模型/codec/校验，以及 run_episode 对 inner_thought 的隔离（纯函数层，无 gRPC）。"""
from simulator.datamodel import (
    Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord,
)
from simulator.realtime.codec import role_binding_from_proto, role_binding_to_proto
from simulator.realtime.validation import validate_create_session
from simulator.runner import run_episode


# ---------------- model_ref：数据模型 ----------------

def test_binding_without_model_ref_serializes_exactly_as_before():
    # 不带 model_ref 时 to_dict 不得多出键，否则既有 Episode 的序列化/哈希会漂移
    b = RoleBinding("a", "pa", "self_play")
    assert b.to_dict() == {"slot_id": "a", "persona_id": "pa", "model_backend": "self_play"}
    assert RoleBinding.from_dict(b.to_dict()) == b


def test_binding_with_model_ref_round_trips():
    b = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
    assert b.to_dict()["model_ref"] == "zengen-v1"
    assert RoleBinding.from_dict(b.to_dict()) == b


# ---------------- model_ref：codec ----------------

def test_codec_preserves_unset_vs_set_model_ref():
    unset = RoleBinding("a", "pa", "self_play")
    proto = role_binding_to_proto(unset)
    assert proto.HasField("model_ref") is False
    assert role_binding_from_proto(proto).model_ref is None

    ref = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
    proto = role_binding_to_proto(ref)
    assert proto.HasField("model_ref") is True
    assert role_binding_from_proto(proto) == ref


# ---------------- model_ref：校验 ----------------

def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _assignment(b_binding):
    return RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"), b_binding], max_turns=4)


def test_frozen_reference_with_model_ref_is_valid():
    ra = _assignment(RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"))
    assert validate_create_session(ra, _personas(), "a", max_turns_cap=200) is None


def test_self_play_must_not_carry_model_ref():
    ra = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play", model_ref="zengen-v1"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=4,
    )
    reason = validate_create_session(ra, _personas(), "a", max_turns_cap=200)
    assert reason is not None and "self_play" in reason and "'a'" in reason


def test_empty_model_ref_rejected():
    ra = _assignment(RoleBinding("b", "pb", "frozen_reference", model_ref=""))
    reason = validate_create_session(ra, _personas(), "a", max_turns_cap=200)
    assert reason is not None and "empty model_ref" in reason


# ---------------- inner_thought 隔离：run_episode ----------------

class _RecordingBackend:
    """记录每次 respond 收到的 history 快照，并返回带 inner_thought 的发言。"""

    def __init__(self, slot, thoughts):
        self._slot = slot
        self._thoughts = list(thoughts)
        self.seen: list[list[TurnRecord]] = []

    def respond(self, persona, scenario, history, speaker_slot):
        self.seen.append([TurnRecord(**t.__dict__) for t in history])
        i = len(self.seen) - 1
        return f"{self._slot}-say-{i}", self._thoughts[i]


def _run(max_turns=4):
    scenario = Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")
    assignment = RoleAssignment(
        "s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    a = _RecordingBackend("a", ["a-secret-0", "a-secret-1"])
    b = _RecordingBackend("b", ["b-secret-0", "b-secret-1"])
    episode = run_episode(scenario, assignment, _personas(), {"a": a, "b": b}, "ep-1")
    return episode, a, b


def test_opponent_never_sees_other_roles_inner_thought():
    _, a, b = _run()
    for snapshot in b.seen:
        for turn in snapshot:
            if turn.speaker_slot == "a":
                assert turn.inner_thought is None and turn.activation_trace is None
    # b 第 1 次发言时 history 里已有 a 的第 0 轮——泄漏会让这里出现 "a-secret-0"
    assert [t.utterance for t in b.seen[0]] == ["a-say-0"]


def test_a_role_still_sees_its_own_inner_thought():
    _, a, _ = _run()
    own = [t for t in a.seen[1] if t.speaker_slot == "a"]
    assert [t.inner_thought for t in own] == ["a-secret-0"]


def test_episode_keeps_full_inner_thoughts_for_training_data():
    episode, _, _ = _run()
    assert [t.inner_thought for t in episode.turns] == [
        "a-secret-0", "b-secret-0", "a-secret-1", "b-secret-1",
    ]


def test_mutating_history_cannot_corrupt_the_episode():
    class Vandal:
        def respond(self, persona, scenario, history, speaker_slot):
            for t in history:
                t.utterance = "VANDALIZED"
            history.clear()
            return "ok", None

    scenario = Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")
    assignment = RoleAssignment(
        "s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    episode = run_episode(scenario, assignment, _personas(), {"a": Vandal(), "b": Vandal()}, "ep-v")
    assert [t.utterance for t in episode.turns] == ["ok"] * 4
