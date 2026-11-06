import hashlib
import json
import sys
import threading

import pytest
import yaml

from simple_actio.errors import SimpleActioDataError
from simple_actio.experience import ReasoningExperience, activate, select_tier
from simple_actio.memory import MemoryEntry, TupleMemory, _Ledger, content_id, overlap, retrieve, snapshot
from simple_actio.memory_port import (MemoryClosedError, MemoryEvent, MemoryEventConflict, MemoryFailureError,
                               MemoryHit, RememberOutcome)
from simple_actio.signals import default_registry
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
    with pytest.raises(SimpleActioDataError):
        MemoryEntry.from_dict({**MEM_DICT, **patch})


@pytest.mark.parametrize("bad_id", ["budget", "mem-", "MEM-budget", "skill-budget", "memory-budget"])
def test_memory_id_must_start_with_the_mem_prefix(bad_id):
    with pytest.raises(SimpleActioDataError, match=r"memory\.id: must start with 'mem-'"):
        MemoryEntry.from_dict({**MEM_DICT, "id": bad_id})
    with pytest.raises(SimpleActioDataError, match=r"memory\.id: must start with 'mem-'"):
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
    with pytest.raises(SimpleActioDataError, match=field):
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
    with pytest.raises(SimpleActioDataError, match="two different entries for version 1"):
        snapshot([items[i] for i in order])


def test_snapshot_detects_conflicts_among_entries_the_time_filter_drops():
    late_a = _m("mem-1", 2, "2026-05-01T00:00:00Z")
    late_b = _m("mem-1", 2, "2026-05-01T00:00:00Z", content="别的")
    early = _m("mem-1", 1, "2026-01-01T00:00:00Z")
    with pytest.raises(SimpleActioDataError, match="two different entries for version 2"):
        snapshot([early, late_a, late_b], "2026-02-01T00:00:00Z")


def test_snapshot_rejects_conflicting_duplicates_and_bad_times():
    same = _m("mem-1", 1, "2026-01-01T00:00:00Z")
    assert snapshot([same, same]) == [same]
    with pytest.raises(SimpleActioDataError, match="two different entries"):
        snapshot([same, _m("mem-1", 1, "2026-01-01T00:00:00Z", content="别的")])
    with pytest.raises(SimpleActioDataError):
        snapshot([same], "yesterday")
    with pytest.raises(SimpleActioDataError):
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
    with pytest.raises(SimpleActioDataError):
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
    with pytest.raises(SimpleActioDataError):
        ReasoningExperience.from_dict(d)


def test_experience_direct_construction_validates_fields():
    from simple_actio.experience import Tier
    good = ReasoningExperience.from_dict(EXP_DICT)
    with pytest.raises(SimpleActioDataError, match="version"):
        ReasoningExperience(good.id, "1", good.pattern, good.applies_when, good.primary, good.secondary, good.fallback)
    with pytest.raises(SimpleActioDataError, match="fallback.when must be null"):
        ReasoningExperience(good.id, 1, good.pattern, good.applies_when, good.primary, good.secondary, good.primary)
    with pytest.raises(SimpleActioDataError, match="instructions"):
        Tier(instructions="先共情")
    with pytest.raises(SimpleActioDataError, match="when"):
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


# ---------------- TupleMemory ----------------

T0 = "2026-01-01T00:00:00Z"


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_of(entry_dicts, event_rows):
    """4.5 节最后一句:条目 to_dict() 去掉 id、带上所属 holder(缺省 "")的排序列表加事件表,整体 sha256。"""
    entries = sorted(({"holder": "", **{k: v for k, v in d.items() if k != "id"}} for d in entry_dicts),
                     key=_canon)
    events = sorted(event_rows, key=lambda r: r["key"])
    return hashlib.sha256(_canon({"entries": entries, "events": events}).encode("utf-8")).hexdigest()


def test_content_id_hashes_only_subject_and_content():
    cid = content_id("pb", "对方担心预算超支")
    raw = json.dumps(["pb", "对方担心预算超支"], ensure_ascii=False, separators=(",", ":"))
    assert cid == "mem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert content_id("pc", "对方担心预算超支") != cid


def test_tuple_memory_ignores_the_entry_ids_in_hits_and_digest():
    a_entries = [_m("mem-1", 1, T0, content="预算不够"), _m("mem-2", 1, T0, content="人手不够")]
    a = TupleMemory(tuple(a_entries))
    b = TupleMemory((_m("mem-x", 1, T0, content="预算不够"), _m("mem-y", 1, T0, content="人手不够")))
    assert a.recall("预算不够", limit=3) == b.recall("预算不够", limit=3)
    assert a.recall("预算不够", limit=3)[0] == MemoryHit(content_id("pb", "预算不够"), "pb: 预算不够")
    assert a.digest() == b.digest() == _digest_of([e.to_dict() for e in a_entries], [])


def test_tuple_memory_digest_follows_what_recall_sees_not_the_raw_entries():
    # 去掉 id 后多重集相同,但 a 里两条是同一 id 的两个版本(召回只看 v2),b 里是两个 id(都召回)
    a = TupleMemory((_m("mem-1", 1, T0, content="预算不够"), _m("mem-1", 2, T0, content="人手不够")))
    b = TupleMemory((_m("mem-1", 1, T0, content="预算不够"), _m("mem-2", 2, T0, content="人手不够")))
    assert a.recall("预算不够", limit=3) != b.recall("预算不够", limit=3)
    assert a.digest() != b.digest()


def test_tuple_memory_ties_break_by_content_id_and_same_content_merges():
    entries = (_m("mem-z", 1, T0, content="预算甲"), _m("mem-a", 1, T0, content="预算乙"),
               _m("mem-dup", 1, T0, content="预算甲"))
    hits = TupleMemory(entries).recall("预算", limit=3)
    assert [h.id for h in hits] == sorted({content_id("pb", "预算甲"), content_id("pb", "预算乙")})


def test_tuple_memory_validates_entries_at_construction():
    with pytest.raises(SimpleActioDataError):
        TupleMemory(({"id": "mem-1"},))
    with pytest.raises(SimpleActioDataError, match="two different entries"):
        TupleMemory((_m("mem-1", 1, T0), _m("mem-1", 1, T0, content="别的")))


def test_tuple_memory_remember_appends_an_entry_and_keeps_an_event_table():
    m = TupleMemory()
    out = m.remember(MemoryEvent("ep#1", "pb：预算不够", "pb"))
    cid = content_id("pb", "pb：预算不够")
    assert out == RememberOutcome((cid,), False, None)
    assert m.recall("预算", limit=3) == [MemoryHit(cid, "pb: pb：预算不够")]
    assert m.remember(MemoryEvent("ep#1", "pb：预算不够", "pb")) == out  # 幂等,不再追加
    assert TupleMemory().remember(MemoryEvent("other#9", "pb：预算不够", "pb")).stored == (cid,)  # 与 key 无关
    with pytest.raises(MemoryEventConflict):
        m.remember(MemoryEvent("ep#1", "pb：别的", "pb"))
    m.remember(MemoryEvent("ep#3", "pc：好吧", "pc"))
    entry = lambda key, subject, text, ts: {"id": "-", "version": 1, "subject": subject, "content": text,
                                            "keywords": [], "timestamp": ts, "source_episode": key,
                                            "confidence": 1.0}
    row = lambda key, text, who, seq: {"key": key, "text": text, "interlocutor": who, "holder": "", "seq": seq,
                                       "state": "committed", "statement_ids": [content_id(who, text)],
                                       "failure_category": None}
    assert m.digest() == _digest_of(
        [entry("ep#1", "pb", "pb：预算不够", T0), entry("ep#3", "pc", "pc：好吧", "2026-01-01T00:00:01Z")],
        [row("ep#1", "pb：预算不够", "pb", 0), row("ep#3", "pc：好吧", "pc", 1)],
    )
    assert m.embed_pending() == 0


def test_world_views_on_two_threads_keep_every_write_and_survive_concurrent_reads():
    # 训练替身的 rollout 世界:客户端线程与服务端线程各用一个视图读写同一份账本。每次写之后紧跟 digest 与
    # recall:digest 遍历事件表,另一条线程这时插入新行就是 "dictionary changed size during iteration"
    world = TupleMemory()
    views = {who: world.world(who) for who in ("pa", "pb")}
    start, errors = threading.Barrier(len(views)), []

    def work(who):
        try:
            start.wait()
            for i in range(100):
                views[who].remember(MemoryEvent(f"t#{i}@{who}", f"pc：第{i}句", "pc"))
                views[who].digest()
                views[who].recall("第", limit=3)
        except BaseException as exc:  # 线程里的异常不会让测试失败,收起来在主线程断言
            errors.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 线程切换拉到最密,让没有锁时的竞争能在这些调用里露出来
    try:
        threads = [threading.Thread(target=work, args=(who,)) for who in views]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert errors == []
    ledger = world._live()
    assert len(ledger.events) == 200 and len(ledger.entries) == 200 and ledger.next_seq == 200
    assert sorted(r["seq"] for r in ledger.events.values()) == list(range(200))
    assert sorted(ledger.holders.values()) == ["pa"] * 100 + ["pb"] * 100
    _Ledger.from_json(json.loads(json.dumps(ledger.to_json())))  # 序号重复的账本读不回来


# ---------------- _Ledger(Task 4 的 FileTupleStore 复用) ----------------

def test_tuple_memory_is_a_memory_backend_and_rejects_a_bad_limit():
    from simple_actio.memory_port import MemoryBackend
    m = TupleMemory()
    assert isinstance(m, MemoryBackend)
    for bad in (-1, True, "3", None):
        with pytest.raises(SimpleActioDataError, match=r"recall\.limit"):
            m.recall("预算", limit=bad)
    m.close()
    with pytest.raises(MemoryClosedError):
        m.recall("预算", limit=3)


def test_ledger_event_state_machine_pending_failed_committed_abandoned():
    led = _Ledger()
    ev = MemoryEvent("ep#1", "pb：预算不够", "pb")
    assert led.begin(ev, holder="") is None and led.events["ep#1"]["state"] == "pending" and led.next_seq == 1
    led.fail("ep#1", "timeout")
    assert led.events["ep#1"]["failure_category"] == "timeout" and led.events["ep#1"]["state"] == "pending"
    assert led.begin(ev, holder="") is None and led.next_seq == 1  # pending 同文本:交给调用方再抽,不重复占序号
    ids = led.commit("ep#1")
    assert ids == (content_id("pb", "pb：预算不够"),) and led.events["ep#1"]["failure_category"] is None
    assert led.begin(ev, holder="") == ids and len(led.entries) == 1  # committed 幂等
    with pytest.raises(MemoryEventConflict):
        led.abandon("ep#1")  # committed 放弃不了
    assert led.begin(MemoryEvent("ep#2", "pc：好吧", "pc"), holder="") is None
    led.abandon("ep#2")
    led.abandon("ep#2")  # 已放弃:空操作
    with pytest.raises(MemoryEventConflict):
        led.begin(MemoryEvent("ep#2", "pc：好吧", "pc"), holder="")  # 放弃的 key 同文本也抛
    with pytest.raises(MemoryFailureError) as unknown:
        led.abandon("ep#nope")
    assert unknown.value.transient is False and not isinstance(unknown.value, MemoryEventConflict)


def test_ledger_digest_keeps_only_key_and_state_for_abandoned_rows_and_round_trips_through_json():
    led = _Ledger()
    led.begin(MemoryEvent("ep#1", "pb：预算不够", "pb"), holder="")
    led.commit("ep#1")
    led.begin(MemoryEvent("ep#2", "pc：好吧", "pc"), holder="")
    before = led.digest()
    led.abandon("ep#2")
    assert led.digest() != before
    led.events["ep#2"]["text"] = "任何文本"  # abandoned 行的正文不进摘要
    assert led.digest() == _digest_of(
        [{**e.to_dict()} for e in led.entries],
        [led.events["ep#1"], {"key": "ep#2", "state": "abandoned"}],
    )
    led.events["ep#2"]["text"] = "pc：好吧"
    clone = _Ledger.from_json(json.loads(json.dumps(led.to_json())))
    assert clone.digest() == led.digest() and clone.next_seq == 2
    assert clone.recall("预算", limit=3) == led.recall("预算", limit=3)
    assert [r["seq"] for r in clone.to_json()["events"]] == [0, 1]


@pytest.mark.parametrize("patch", [
    {"extra": 1}, {"next_seq": -1}, {"next_seq": True}, {"entries": "x"},
    {"events": [{"key": "ep#1"}]},
    {"events": [{"key": "ep#1", "text": "t", "interlocutor": "pb", "seq": 0, "state": "pending",
                 "statement_ids": [], "failure_category": None}]},  # seq 0 不小于 next_seq 0
    {"events": [{"key": "ep#1", "text": "t", "interlocutor": "pb", "seq": 0, "state": "done",
                 "statement_ids": [], "failure_category": None}]},
])
def test_ledger_from_json_rejects_bad_shapes(patch):
    good = {"entries": [], "events": [], "next_seq": 0}
    with pytest.raises(SimpleActioDataError):
        _Ledger.from_json({**good, **patch})
    with pytest.raises(SimpleActioDataError):
        _Ledger.from_json([])
