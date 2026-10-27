import ast
from pathlib import Path

import pytest
from google.protobuf import json_format, struct_pb2

from actio.errors import ActioDataError
from actio.experience import ReasoningExperience
from actio.instance import IgnoreGuidance, as_guided, instantiate, render_guidance
from actio.memory import FileTupleStore, MemoryEntry, TupleMemory, content_id
from actio.memory_port import MemoryClosedError, MemoryFailureError, MemoryHit, RememberOutcome
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


class RecordingPort:
    """记录调用顺序的假端口。log 可与 LoggingBackend 共用,于是能断言 recall/后端/remember 的先后。
    fail_keys:key -> 还要以 timeout 失败的次数;raise_on:方法名 -> 要抛的异常。"""

    def __init__(self, log=None, *, hits=(), fail_keys=None, raise_on=None):
        self.log = [] if log is None else log
        self.hits = list(hits)
        self.fail_keys = dict(fail_keys or {})
        self.raise_on = dict(raise_on or {})
        self.stored = 0
        self.closed = 0

    def _call(self, entry):
        self.log.append(entry)
        if entry[0] in self.raise_on:
            raise self.raise_on[entry[0]]

    def recall(self, query, *, limit):
        self._call(("recall", query, limit))
        return self.hits[:limit]

    def remember(self, event):
        self._call(("remember", event))
        if self.fail_keys.get(event.key, 0) > 0:
            self.fail_keys[event.key] -= 1
            return RememberOutcome((), True, "timeout")
        self.stored += 1
        return RememberOutcome((f"mem-{self.stored}",), False, None)

    def embed_pending(self):
        self._call(("embed_pending",))
        return 0

    def digest(self):
        self._call(("digest",))
        return f"{self.stored:064x}"

    def close(self):
        self.closed += 1


class LoggingBackend:
    def __init__(self, log):
        self.log = log

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        self.log.append(("backend", list(guidance)))
        return ("回复", None)


def _ported(port, backend=None, **kw):
    kw.setdefault("episode_id", "ep")
    return instantiate(backend or EchoGuidance(), skills=[], memory=port, library_version=LV, profile=None, **kw)


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
    hit = MemoryHit("mem-1", "pb: 担心预算")
    assert render_guidance(_calm_skill(), act, [hit]) == [
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
    cid = content_id("pb", "对方容易生气")
    echo = EchoGuidance()
    inst = instantiate(echo, skills=skills, memories=[mem], library_version={"skill": 7, "memory": 2}, profile={"name": "x"})
    _, _, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")
    assert trace["candidates"] == ["skill-firm", "skill-calm"]
    assert trace["selected"] == "skill-firm"
    assert trace["conflict"] == {"skills": ["skill-firm", "skill-calm"], "rule": TIE_BREAK_RULE}
    assert trace["memory_hits"] == [cid]
    assert trace["library_version"] == {"skill": "7", "memory": "2"}
    assert trace["schema"] == TRACE_SCHEMA_V2
    assert trace["memory"] == {"digest_before": TupleMemory((mem,)).digest(), "remembered": "0", "extraction_failed": "0"}
    assert echo.calls == [["[skill:skill-firm] 坚持立场", f"[memory:{cid}] pb: 对方容易生气"]]
    validate_trace(trace)


def test_renaming_an_entry_changes_neither_the_prompt_nor_the_trace():
    def run(mid):
        echo = EchoGuidance()
        mem = MemoryEntry(mid, 1, "pb", "对方容易生气", "2026-01-01T00:00:00Z", 0.9)
        inst = instantiate(echo, skills=[], memories=[mem], library_version=LV, profile=None)
        return echo.calls, inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "我很生气")], "a")[2]
    assert run("mem-angry") == run("mem-renamed")


def test_digest_then_recall_then_the_reply():
    log = []
    port = RecordingPort(log, hits=[MemoryHit("mem-a", "pb: 预算"), MemoryHit("mem-b", "pb: 人手")])
    inst = _ported(port, LoggingBackend(log), replay_read_only=True, memory_limit=1)
    history = [TurnRecord(0, "b", "钱不够"), TurnRecord(1, "a", "嗯"), TurnRecord(2, "b", "预算呢")]
    _, _, trace = inst.respond(_persona(), _scenario(), history, "a")
    assert log == [("digest",), ("recall", "预算呢", 1), ("backend", ["[memory:mem-a] pb: 预算"])]
    assert trace["memory_hits"] == ["mem-a"]
    assert trace["memory"]["digest_before"] == "0" * 64


def test_no_recall_before_the_opponent_has_spoken():
    log = []
    inst = _ported(RecordingPort(log), LoggingBackend(log), replay_read_only=True)
    _, _, trace = inst.respond(_persona(), _scenario(), [], "a")
    assert log == [("digest",), ("backend", [])] and trace["memory_hits"] == []


def test_decide_recalls_but_never_writes():
    port = RecordingPort(hits=[MemoryHit("mem-a", "pb: 预算")])
    routed, exp, hits = _ported(port).decide([TurnRecord(0, "b", "预算")], "a")
    assert port.log == [("recall", "预算", 3)] and hits == [MemoryHit("mem-a", "pb: 预算")]


@pytest.mark.parametrize("method", ["digest", "recall"])
def test_read_failures_become_memory_failures_before_the_backend_is_called(method):
    log = []
    port = RecordingPort(log, raise_on={method: RuntimeError("https://endpoint.example/v1 sk-secret")})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port, LoggingBackend(log)).respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算")], "a")
    assert info.value.transient is False and str(info.value) == "RuntimeError"  # 只报类名
    assert not any(entry[0] == "backend" for entry in log)
    original = MemoryFailureError("RecallDegradedError", transient=True)
    port = RecordingPort(raise_on={method: original})
    with pytest.raises(MemoryFailureError) as info:
        _ported(port).respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算")], "a")
    assert info.value is original  # 已是 MemoryFailureError 的原样上抛,transient 不被改写


def test_instantiate_memory_arguments():
    base = dict(skills=[], library_version=LV, profile=None)
    with pytest.raises(ActioDataError, match="exactly one of"):
        instantiate(EchoGuidance(), **base)
    with pytest.raises(ActioDataError, match="exactly one of"):
        instantiate(EchoGuidance(), **base, memories=[], memory=TupleMemory())
    with pytest.raises(ActioDataError, match="episode_id"):
        instantiate(EchoGuidance(), **base, memory=TupleMemory())
    with pytest.raises(ActioDataError, match="episode_id"):
        instantiate(EchoGuidance(), **base, memories=[], replay_read_only=False)
    with pytest.raises(ActioDataError, match="memory"):
        instantiate(EchoGuidance(), **base, memory=object(), episode_id="ep")
    for bad in ({"episode_id": ""}, {"episode_id": 3}, {"interlocutors": {"b": ""}},
                {"interlocutors": [("b", "pb")]}, {"replay_read_only": "yes"}):
        with pytest.raises(ActioDataError):
            instantiate(EchoGuidance(), **base, memory=TupleMemory(), **{"episode_id": "ep", **bad})
    short = instantiate(EchoGuidance(), **base, memories=[])
    assert short.replay_read_only is True and short.episode_id is None and short.interlocutors == {}
    ported = instantiate(EchoGuidance(), **base, memory=TupleMemory(), episode_id="ep", interlocutors={"b": "pb"})
    assert ported.replay_read_only is False and ported.interlocutors == {"b": "pb"}
    assert instantiate(EchoGuidance(), **base, memory=TupleMemory(), replay_read_only=True).episode_id is None


def test_close_forwards_to_the_port():
    port = RecordingPort()
    inst = _ported(port)
    inst.close()
    assert port.closed == 1


def test_an_instance_over_a_file_port_reads_the_same_store_the_management_plane_writes(tmp_path):
    store = FileTupleStore(tmp_path)
    store.remember_event("pa", "ep0:1", "对方担心预算", interlocutor="pb")
    inst = _ported(store.open("pa"), EchoGuidance())
    _, _, trace = inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算呢")], "a")
    assert len(trace["memory_hits"]) == 1 and trace["memory_hits"][0].startswith("mem-")
    assert trace["memory"]["digest_before"] == store.digest("pa")
    validate_trace(trace)
    inst.close()
    inst.close()  # 再关一次是空操作
    with pytest.raises(MemoryClosedError) as info:
        inst.respond(_persona(), _scenario(), [TurnRecord(0, "b", "预算呢")], "a")
    assert info.value.transient is False  # MemoryFailureError 原样上抛,不被 _guard 改写类型


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
