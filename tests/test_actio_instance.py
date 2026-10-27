import ast
from pathlib import Path

import pytest
from google.protobuf import json_format, struct_pb2

from actio.errors import ActioDataError
from actio.experience import ReasoningExperience
from actio.instance import IgnoreGuidance, as_guided, instantiate, render_guidance
from actio.memory import MemoryEntry
from actio.signals import default_registry
from actio.skills import Skill
from actio.trace import (
    TIE_BREAK_RULE, TRACE_SCHEMA_V1, TRACE_SCHEMA_V2, build_trace, normalize_library_version, validate_trace,
)
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.runner import run_episode

SRC = Path(__file__).resolve().parents[1] / "src" / "actio"


class EchoGuidance:
    """罐头带指导语后端:把指导语原样拼进话语,证明技能确实改变了输出。"""

    def __init__(self):
        self.calls = []

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        self.calls.append(list(guidance))
        return ("|".join(guidance) or "plain", None)


def _persona():
    return Persona("pa", ["t"], "m", "s", ["v"])


def _scenario():
    return Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _calm_skill(**kw):
    base = dict(id="skill-calm", version=1, level="micro", trigger={"signal": "emotion_high"},
                policy_instructions=("先回应对方的情绪",), priority=1)
    base.update(kw)
    return Skill(**base)


LV = {"skill": "lib-2", "memory": "lib-1"}


# ---------------- 轨迹模式 ----------------

def test_build_trace_exact_shape_with_a_conflict():
    t = build_trace(signals=["emotion_high"], candidates=["skill-a", "skill-b"], selected="skill-a",
                    memory_hits=["mem-1"], experience=("e1", "primary"), library_version=LV)
    assert t == {
        "schema": TRACE_SCHEMA_V1, "signals": ["emotion_high"], "candidates": ["skill-a", "skill-b"],
        "selected": "skill-a", "conflict": {"skills": ["skill-a", "skill-b"], "rule": TIE_BREAK_RULE},
        "memory_hits": ["mem-1"], "experience": {"id": "e1", "tier": "primary"},
        "library_version": {"skill": "lib-2", "memory": "lib-1"},
    }
    assert TRACE_SCHEMA_V1 == "actio.trace.v1" and TIE_BREAK_RULE == "priority_desc,version_desc,id_asc"


def test_build_trace_without_hits():
    t = build_trace(signals=[], candidates=[], selected=None, memory_hits=[], experience=None,
                    library_version=LV)
    assert (t["selected"], t["conflict"], t["experience"]) == (None, None, None)


def _walk_leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk_leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_leaves(v)
    else:
        yield node


def test_a_trace_has_no_numbers_anywhere():
    t = build_trace(signals=["first_turn"], candidates=["skill-a", "skill-b"], selected="skill-a", memory_hits=["mem-m"],
                    experience=("e", "fallback"), library_version=normalize_library_version({"skill": 3, "memory": 0}))
    assert all(v is None or type(v) in (str, bool) for v in _walk_leaves(t))


@pytest.mark.parametrize("patch", [
    {"schema": "v0"}, {"selected": "skill-b"}, {"selected": None}, {"conflict": None},
    {"memory_hits": [3]}, {"experience": {"id": "e", "tier": "fourth"}},
    {"library_version": {"skill": 3, "memory": "1"}}, {"extra": 1},
    {"signals": ["first_turn", "emotion_high"]}, {"signals": ["first_turn", "first_turn"]},
    {"memory_hits": ["m1"]}, {"memory_hits": ["mem-1", "mem-1"]},
])
def test_validate_trace_rejects_off_schema(patch):
    good = build_trace(signals=[], candidates=["skill-a", "skill-b"], selected="skill-a", memory_hits=[],
                       experience=None, library_version=LV)
    with pytest.raises(ActioDataError):
        validate_trace({**good, **patch})


def test_validate_trace_requires_prefixed_candidates():
    good = build_trace(signals=[], candidates=["skill-a"], selected="skill-a", memory_hits=[],
                       experience=None, library_version=LV)
    with pytest.raises(ActioDataError, match="must start with 'skill-'"):
        validate_trace({**good, "candidates": ["a"], "selected": "a"})


def test_normalize_library_version():
    assert normalize_library_version({"skill": 3, "memory": "v1"}) == {"skill": "3", "memory": "v1"}
    for bad in ({"skill": 1}, {"skill": -1, "memory": 0}, {"skill": True, "memory": 0},
                {"skill": "", "memory": 0}, {"skill": 1.0, "memory": 0}, [1, 2]):
        with pytest.raises(ActioDataError):
            normalize_library_version(bad)


MEM = {"digest_before": "ab" * 32, "remembered": "2", "extraction_failed": "0"}


def test_build_trace_with_memory_is_v2_with_nine_keys():
    t = build_trace(signals=[], candidates=[], selected=None, memory_hits=["mem-a"], experience=None,
                    library_version=LV, memory=MEM)
    assert TRACE_SCHEMA_V2 == "actio.trace.v2"
    assert t["schema"] == TRACE_SCHEMA_V2 and len(t) == 9 and t["memory"] == MEM
    assert t["memory"] is not MEM
    assert all(v is None or type(v) in (str, bool) for v in _walk_leaves(t))
    validate_trace(t)


def test_a_v2_trace_survives_a_struct_round_trip():
    t = build_trace(signals=["first_turn"], candidates=["skill-a"], selected="skill-a", memory_hits=[],
                    experience=None, library_version=LV, memory={**MEM, "remembered": "12"})
    s = struct_pb2.Struct()
    s.update(t)
    back = json_format.MessageToDict(s)
    assert back == t and back["memory"]["remembered"] == "12"
    validate_trace(back)


V2_GOOD = dict(signals=[], candidates=[], selected=None, memory_hits=[], experience=None, library_version=LV)


@pytest.mark.parametrize("patch", [
    {"memory": None}, {"memory": {}}, {"memory": {**MEM, "extra": "1"}},
    {"memory": {k: v for k, v in MEM.items() if k != "remembered"}},
    {"memory": {**MEM, "digest_before": ""}}, {"memory": {**MEM, "remembered": 2}},
    {"memory": {**MEM, "remembered": "2.0"}}, {"memory": {**MEM, "extraction_failed": "-1"}},
    {"memory": {**MEM, "remembered": "\u0663"}},          # 阿拉伯-印度数字:str.isdecimal() 为真,但不是 ASCII
    {"memory": {**MEM, "remembered": "02"}}, {"memory": {**MEM, "remembered": ""}},
])
def test_validate_trace_rejects_a_bad_v2_memory_block(patch):
    good = build_trace(**V2_GOOD, memory=MEM)
    with pytest.raises(ActioDataError, match=r"trace\.memory"):
        validate_trace({**good, **patch})


def test_the_schema_decides_the_key_set():
    v1 = build_trace(**V2_GOOD)
    v2 = build_trace(**V2_GOOD, memory=MEM)
    with pytest.raises(ActioDataError, match="expected exactly keys"):
        validate_trace({**v1, "memory": MEM})                      # v1 带 memory 键
    with pytest.raises(ActioDataError, match="expected exactly keys"):
        validate_trace({k: v for k, v in v2.items() if k != "memory"})  # v2 缺 memory 键
    with pytest.raises(ActioDataError, match=r"trace\.schema"):
        validate_trace({**v2, "schema": "actio.trace.v3"})


def test_build_trace_rejects_a_bad_memory_block():
    with pytest.raises(ActioDataError, match=r"trace\.memory"):
        build_trace(**V2_GOOD, memory={**MEM, "remembered": 3})


# ---------------- 指导语与适配器 ----------------

def test_render_guidance_order_and_format():
    exp = ReasoningExperience.from_dict({
        "id": "e1", "version": 1, "pattern": "p", "applies_when": {"turn_at_least": 0},
        "tiers": {"primary": {"when": {"signal": "emotion_high"}, "instructions": ["共情"]},
                  "secondary": {"when": {"signal": "first_turn"}, "instructions": ["提问"]},
                  "fallback": {"when": None, "instructions": ["复述"]}},
    })
    from actio.experience import select_tier
    act = select_tier(exp, [], "a", default_registry())
    mem = MemoryEntry("mem-1", 1, "pb", "担心预算", "2026-01-01T00:00:00Z", 0.5)
    assert render_guidance(_calm_skill(), act, [mem]) == [
        "[skill:skill-calm] 先回应对方的情绪",
        "[experience:e1/secondary] 提问",
        "[memory:mem-1] pb: 担心预算",
    ]
    assert render_guidance(None, None, []) == []


def test_plain_backends_are_wrapped_and_ignore_guidance():
    plain = ScriptedBackend({"a": ["hello"]})
    wrapped = as_guided(plain)
    assert isinstance(wrapped, IgnoreGuidance)
    assert wrapped.respond_guided(_persona(), _scenario(), [], "a", ["ignored"]) == ("hello", None)
    echo = EchoGuidance()
    assert as_guided(echo) is echo
    with pytest.raises(TypeError):
        as_guided(object())


# ---------------- 实例 ----------------

def test_a_selected_skill_changes_the_output():
    angry = [TurnRecord(0, "b", "我很生气")]
    with_skill = instantiate(EchoGuidance(), skills=[_calm_skill()], memories=[], library_version=LV, profile=None)
    without = instantiate(EchoGuidance(), skills=[], memories=[], library_version=LV, profile=None)
    u1, _, t1 = with_skill.respond(_persona(), _scenario(), angry, "a")
    u0, _, t0 = without.respond(_persona(), _scenario(), angry, "a")
    assert u1 == "[skill:skill-calm] 先回应对方的情绪"
    assert u0 == "plain"
    assert (t1["selected"], t0["selected"]) == ("skill-calm", None)
    assert t1["signals"] == ["emotion_high", "first_turn"]


def test_the_skill_does_not_fire_when_its_trigger_is_false():
    inst = instantiate(EchoGuidance(), skills=[_calm_skill()], memories=[], library_version=LV, profile=None)
    u, _, t = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "你好")], "a")
    assert (u, t["candidates"], t["selected"]) == ("plain", [], None)


def test_conflicts_and_memory_hits_are_in_the_trace():
    skills = [_calm_skill(), _calm_skill(id="skill-firm", priority=5, policy_instructions=("坚持立场",))]
    mem = MemoryEntry("mem-angry", 1, "pb", "对方容易生气", "2026-01-01T00:00:00Z", 0.9)
    echo = EchoGuidance()
    inst = instantiate(echo, skills=skills, memories=[mem], library_version={"skill": 7, "memory": 2}, profile={"name": "x"})
    _, _, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")
    assert trace["candidates"] == ["skill-firm", "skill-calm"]
    assert trace["selected"] == "skill-firm"
    assert trace["conflict"] == {"skills": ["skill-firm", "skill-calm"], "rule": TIE_BREAK_RULE}
    assert trace["memory_hits"] == ["mem-angry"]
    assert trace["library_version"] == {"skill": "7", "memory": "2"}
    assert echo.calls == [["[skill:skill-firm] 坚持立场", "[memory:mem-angry] pb: 对方容易生气"]]
    validate_trace(trace)


def test_a_plain_backend_still_gets_a_trace():
    inst = instantiate(ScriptedBackend({"a": ["hi"]}), skills=[_calm_skill()], memories=[], library_version=LV, profile=None)
    u, thought, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")
    assert (u, thought, trace["selected"]) == ("hi", None, "skill-calm")


def test_the_instance_runs_as_a_runner_backend():
    inst = instantiate(EchoGuidance(), skills=[_calm_skill()], memories=[], library_version=LV, profile=None)
    assignment = RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference"), RoleBinding("a", "pa", "self_play")], 4)
    personas = {"pa": _persona(), "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    ep = run_episode(_scenario(), assignment, personas,
                     {"a": inst, "b": ScriptedBackend({"b": ["我很生气", "好吧"]})}, "ep-1")
    assert [t.utterance for t in ep.turns] == ["我很生气", "[skill:skill-calm] 先回应对方的情绪", "好吧", "plain"]
    assert ep.turns[0].activation_trace is None
    assert ep.turns[1].activation_trace["selected"] == "skill-calm"
    assert ep.turns[3].activation_trace["selected"] is None


def test_the_backend_gets_a_copy_of_history():
    class Meddler:
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            history.clear()
            return "x", None

    history = [TurnRecord(0, "b", "你好")]
    instantiate(Meddler(), skills=[], memories=[], library_version=LV, profile=None).respond(_persona(), _scenario(), history, "a")
    assert len(history) == 1


def test_a_guided_backend_returning_a_triple_is_rejected():
    class Bad:
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            return "x", None, {}

    with pytest.raises(TypeError, match="guided backend"):
        instantiate(Bad(), skills=[], memories=[], library_version=LV, profile=None).respond(_persona(), _scenario(), [], "a")


def test_instantiate_validates_its_inputs():
    ok = dict(skills=[], memories=[], library_version=LV, profile=None)
    with pytest.raises(ActioDataError, match="duplicate skill id"):
        instantiate(EchoGuidance(), **{**ok, "skills": [_calm_skill(), _calm_skill()]})
    with pytest.raises(ActioDataError, match="unknown signal"):
        instantiate(EchoGuidance(), **{**ok, "skills": [_calm_skill(trigger={"signal": "custom"})]})
    with pytest.raises(ActioDataError):
        instantiate(EchoGuidance(), **{**ok, "memories": [{"id": "m"}]})
    with pytest.raises(ActioDataError):
        instantiate(EchoGuidance(), **{**ok, "library_version": {"skill": 1}})
    with pytest.raises(ActioDataError):
        instantiate(EchoGuidance(), **{**ok, "profile": "x"})
    with pytest.raises(ActioDataError):
        instantiate(EchoGuidance(), **ok, memory_limit=-1)
    with pytest.raises(TypeError):
        instantiate(EchoGuidance(), [], [], LV, None)  # 除后端外全部 keyword-only


def test_a_custom_registry_signal_can_drive_a_skill():
    reg = default_registry()
    reg.register("mentions_money", lambda h, s: any("钱" in t.utterance for t in h if t.speaker_slot != s))
    inst = instantiate(EchoGuidance(), skills=[_calm_skill(trigger={"signal": "mentions_money"})],
                       memories=[], library_version=LV, profile=None, registry=reg)
    u, _, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "钱不够")], "a")
    assert trace["selected"] == "skill-calm"
    assert "mentions_money" in trace["signals"]


# ---------------- 依赖边界 ----------------

def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


_ALLOWED = ("actio", "simulator.datamodel", "__future__", "copy", "dataclasses", "datetime", "hashlib", "json",
            "pathlib", "re", "typing")


def _allowed(mod):
    return any(mod == a or mod.startswith(a + ".") for a in _ALLOWED)


def test_actio_only_imports_itself_simulator_datamodel_and_the_stdlib():
    for path in sorted(SRC.glob("*.py")):
        for mod in _imports(path):
            assert _allowed(mod), (path.name, mod)


def test_every_actio_source_starts_with_future_annotations():
    for path in sorted(SRC.glob("*.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        assert body, path.name
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
