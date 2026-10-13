import pytest
import yaml

from actio.errors import ActioDataError
from actio.experience import ReasoningExperience, activate, select_tier
from actio.memory import MemoryEntry, overlap, retrieve, snapshot
from actio.signals import default_registry
from simulator.datamodel import TurnRecord

MEM_DICT = {
    "id": "mem-budget", "version": 2, "subject": "pb", "content": "对方担心预算超支",
    "keywords": ["预算", "成本"], "timestamp": "2026-10-01T00:00:00Z",
    "source_episode": "ep-sess-1", "confidence": 0.8,
}


def _m(mid, version, ts, *, subject="pb", content="预算不够", conf=0.5, keywords=()):
    return MemoryEntry(mid, version, subject, content, ts, conf, None, tuple(keywords))


# ---------------- MemoryEntry ----------------

def test_memory_round_trip_and_exact_shape():
    m = MemoryEntry.from_dict(MEM_DICT)
    assert m.to_dict() == MEM_DICT
    assert list(m.to_dict()) == ["id", "version", "subject", "content", "keywords",
                                 "timestamp", "source_episode", "confidence"]
    assert MemoryEntry.from_dict(yaml.safe_load(yaml.safe_dump(m.to_dict()))) == m


def test_memory_optional_keys_default_and_integer_confidence_becomes_float():
    d = {k: v for k, v in MEM_DICT.items() if k not in ("keywords", "source_episode")}
    m = MemoryEntry.from_dict({**d, "confidence": 1})
    assert (m.keywords, m.source_episode, m.confidence) == ((), None, 1.0)
    assert type(m.confidence) is float


@pytest.mark.parametrize("patch", [
    {"confidence": 1.5}, {"confidence": -0.1}, {"confidence": True}, {"confidence": "0.5"},
    {"timestamp": "2026-10-01"}, {"timestamp": "2026-10-01T00:00:00+08:00"},
    {"version": 0}, {"subject": ""}, {"keywords": "预算"}, {"source_episode": ""}, {"x": 1},
])
def test_memory_from_dict_rejects_bad_fields(patch):
    with pytest.raises(ActioDataError):
        MemoryEntry.from_dict({**MEM_DICT, **patch})


@pytest.mark.parametrize("bad_id", ["budget", "mem-", "MEM-budget", "skill-budget", "memory-budget"])
def test_memory_id_must_start_with_the_mem_prefix(bad_id):
    with pytest.raises(ActioDataError, match=r"memory\.id: must start with 'mem-'"):
        MemoryEntry.from_dict({**MEM_DICT, "id": bad_id})
    with pytest.raises(ActioDataError, match=r"memory\.id: must start with 'mem-'"):
        _m(bad_id, 1, "2026-01-01T00:00:00Z")


def test_a_mem_prefixed_id_is_accepted():
    assert MemoryEntry.from_dict({**MEM_DICT, "id": "mem-x"}).id == "mem-x"
    assert _m("mem-x", 1, "2026-01-01T00:00:00Z").id == "mem-x"


@pytest.mark.parametrize("patch, field", [
    ({"timestamp": "2026-10-01"}, "timestamp"), ({"timestamp": "2026-10-01T00:00:00+08:00"}, "timestamp"),
    ({"confidence": 1.5}, "confidence"), ({"confidence": "0.5"}, "confidence"), ({"confidence": True}, "confidence"),
    ({"version": "1"}, "version"), ({"subject": ""}, "subject"), ({"content": 3}, "content"),
    ({"source_episode": ""}, "source_episode"), ({"keywords": "预算"}, "keywords"), ({"keywords": ["预算"]}, "keywords"),
])
def test_memory_direct_construction_validates_every_field(patch, field):
    base = dict(id="mem-ok", version=1, subject="pb", content="预算", timestamp="2026-01-01T00:00:00Z",
                confidence=0.5)
    with pytest.raises(ActioDataError, match=field):
        MemoryEntry(**{**base, **patch})


def test_memory_direct_construction_normalizes_an_int_confidence():
    m = MemoryEntry("mem-ok", 1, "pb", "预算", "2026-01-01T00:00:00Z", 1)
    assert m.confidence == 1.0 and type(m.confidence) is float


# ---------------- 快照 ----------------

def test_snapshot_takes_the_latest_version_at_or_before_the_time():
    entries = [_m("mem-1", 1, "2026-01-01T00:00:00Z"), _m("mem-1", 2, "2026-03-01T00:00:00Z"),
               _m("mem-2", 1, "2026-02-01T00:00:00Z")]
    assert [(e.id, e.version) for e in snapshot(entries)] == [("mem-1", 2), ("mem-2", 1)]
    assert [(e.id, e.version) for e in snapshot(entries, "2026-02-15T00:00:00Z")] == [("mem-1", 1), ("mem-2", 1)]
    assert [(e.id, e.version) for e in snapshot(entries, "2026-03-01T00:00:00Z")] == [("mem-1", 2), ("mem-2", 1)]
    assert snapshot(entries, "2025-12-31T23:59:59Z") == []


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (2, 0, 1), (1, 2, 0)])
def test_snapshot_conflict_detection_does_not_depend_on_input_order(order):
    v1a = _m("mem-1", 1, "2026-01-01T00:00:00Z")
    v1b = _m("mem-1", 1, "2026-01-01T00:00:00Z", content="别的")
    v2 = _m("mem-1", 2, "2026-03-01T00:00:00Z")
    items = [v1a, v1b, v2]
    with pytest.raises(ActioDataError, match="two different entries for version 1"):
        snapshot([items[i] for i in order])


def test_snapshot_detects_conflicts_among_entries_the_time_filter_drops():
    late_a = _m("mem-1", 2, "2026-05-01T00:00:00Z")
    late_b = _m("mem-1", 2, "2026-05-01T00:00:00Z", content="别的")
    early = _m("mem-1", 1, "2026-01-01T00:00:00Z")
    with pytest.raises(ActioDataError, match="two different entries for version 2"):
        snapshot([early, late_a, late_b], "2026-02-01T00:00:00Z")


def test_snapshot_rejects_conflicting_duplicates_and_bad_times():
    same = _m("mem-1", 1, "2026-01-01T00:00:00Z")
    assert snapshot([same, same]) == [same]
    with pytest.raises(ActioDataError, match="two different entries"):
        snapshot([same, _m("mem-1", 1, "2026-01-01T00:00:00Z", content="别的")])
    with pytest.raises(ActioDataError):
        snapshot([same], "yesterday")
    with pytest.raises(ActioDataError):
        snapshot([MEM_DICT])


# ---------------- 检索 ----------------

def test_overlap_counts_shared_tokens_across_subject_content_and_keywords():
    m = _m("mem-1", 1, "2026-01-01T00:00:00Z", content="担心预算", keywords=["成本"])
    assert overlap(m, "预算和成本") == 2   # "预算"、"成本"
    assert overlap(m, "天气") == 0


def test_retrieve_ranks_by_overlap_then_confidence_then_id():
    entries = [
        _m("mem-a", 1, "2026-01-01T00:00:00Z", content="预算不够", conf=0.2),
        _m("mem-b", 1, "2026-01-01T00:00:00Z", content="预算不够人手不够", conf=0.1),
        _m("mem-c", 1, "2026-01-01T00:00:00Z", content="预算不够", conf=0.9),
        _m("mem-d", 1, "2026-01-01T00:00:00Z", content="天气很好", conf=1.0),
    ]
    assert [e.id for e in retrieve(entries, "预算不够人手不够")] == ["mem-b", "mem-c", "mem-a"]
    assert [e.id for e in retrieve(entries, "预算不够人手不够", limit=1)] == ["mem-b"]
    assert retrieve(entries, "预算", limit=0) == []


def test_retrieve_filters_by_subject_and_time():
    entries = [_m("mem-1", 1, "2026-01-01T00:00:00Z", subject="pb"),
               _m("mem-2", 1, "2026-01-01T00:00:00Z", subject="pc"),
               _m("mem-1", 2, "2026-05-01T00:00:00Z", subject="pb", content="天气")]
    assert [e.id for e in retrieve(entries, "预算", subjects=["pb"])] == []   # mem-1 最新版本说的是天气
    assert [(e.id, e.version) for e in retrieve(entries, "预算", subjects=["pb"], at="2026-02-01T00:00:00Z")] == [("mem-1", 1)]
    assert [e.id for e in retrieve(entries, "预算")] == ["mem-2"]
    with pytest.raises(ActioDataError):
        retrieve(entries, "预算", limit=-1)


# ---------------- 推理经验 ----------------

EXP_DICT = {
    "id": "exp-deescalate", "version": 1, "pattern": "情绪高时先降温",
    "applies_when": {"turn_at_least": 1},
    "tiers": {
        "primary": {"when": {"signal": "emotion_high"}, "instructions": ["先共情"]},
        "secondary": {"when": {"signal": "no_new_information"}, "instructions": ["换个角度提问"]},
        "fallback": {"when": None, "instructions": ["复述对方的要点"]},
    },
}


def test_experience_round_trip_and_exact_shape():
    e = ReasoningExperience.from_dict(EXP_DICT)
    assert e.to_dict() == EXP_DICT
    assert ReasoningExperience.from_dict(yaml.safe_load(yaml.safe_dump(e.to_dict()))) == e


@pytest.mark.parametrize("path, value", [
    (("tiers", "fallback", "when"), {"signal": "first_turn"}),
    (("tiers", "primary", "when"), None),
    (("tiers", "secondary", "instructions"), []),
    (("applies_when",), {"bad": 1}),
    (("version",), 0),
])
def test_experience_from_dict_rejects_bad_fields(path, value):
    import copy
    d = copy.deepcopy(EXP_DICT)
    node = d
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ActioDataError):
        ReasoningExperience.from_dict(d)


def test_experience_direct_construction_validates_fields():
    from actio.experience import Tier
    good = ReasoningExperience.from_dict(EXP_DICT)
    with pytest.raises(ActioDataError, match="version"):
        ReasoningExperience(good.id, "1", good.pattern, good.applies_when, good.primary, good.secondary, good.fallback)
    with pytest.raises(ActioDataError, match="fallback.when must be null"):
        ReasoningExperience(good.id, 1, good.pattern, good.applies_when, good.primary, good.secondary, good.primary)
    with pytest.raises(ActioDataError, match="instructions"):
        Tier(instructions="先共情")
    with pytest.raises(ActioDataError, match="when"):
        Tier(instructions=("x",), when={"bad": 1})


def test_three_tier_fallback():
    reg = default_registry()
    e = ReasoningExperience.from_dict(EXP_DICT)
    angry = [TurnRecord(0, "b", "我很生气")]
    stuck = [TurnRecord(0, "b", "预算不够"), TurnRecord(1, "a", "嗯"), TurnRecord(2, "b", "预算不够")]
    calm = [TurnRecord(0, "b", "你好")]
    assert (select_tier(e, angry, "a", reg).tier, select_tier(e, angry, "a", reg).instructions) == ("primary", ("先共情",))
    assert select_tier(e, stuck, "a", reg).tier == "secondary"
    assert select_tier(e, calm, "a", reg).tier == "fallback"


def test_activate_picks_the_newest_applicable_experience_or_none():
    reg = default_registry()
    v1 = ReasoningExperience.from_dict(EXP_DICT)
    v2 = ReasoningExperience.from_dict({**EXP_DICT, "id": "exp-other", "version": 3})
    calm = [TurnRecord(0, "b", "你好")]
    assert activate([v1, v2], calm, "a", reg).experience_id == "exp-other"
    assert activate([v1, v2], [], "a", reg) is None  # applies_when: turn_at_least 1 不成立
    assert activate([], calm, "a", reg) is None
