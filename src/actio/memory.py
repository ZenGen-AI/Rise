"""显式记忆:条目、按时间点取快照、按主体与关键词的词面重叠检索(闭环集成设计 §8.1),
以及记忆端口的内存替身 TupleMemory 与两个替身共用的账本 _Ledger(场景二记忆设计 §4.1)。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from actio import _check
from actio.errors import ActioDataError
from actio.memory_port import (
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    RememberOutcome,
)
from actio.text import tokens

MEMORY_ID_PREFIX = "mem-"


@dataclass(frozen=True)
class MemoryEntry:
    """一条记忆。同一 id 可以有多个版本;timestamp 是该版本成立的时刻(UTC,秒精度)。
    confidence 在 [0, 1];source_episode 是来源 episode 的 id(人工录入时为 None)。"""

    id: str
    version: int
    subject: str
    content: str
    timestamp: str
    confidence: float
    source_episode: str | None = None
    keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """直接构造与 from_dict 走同一套校验。整数 confidence 规范成 float。"""
        mid = _check.prefixed_id(self.id, "memory.id", MEMORY_ID_PREFIX)
        _check.positive_int(self.version, f"memory {mid}.version")
        _check.text(self.subject, f"memory {mid}.subject")
        _check.text(self.content, f"memory {mid}.content")
        _check.timestamp(self.timestamp, f"memory {mid}.timestamp")
        object.__setattr__(self, "confidence", _check.unit_interval(self.confidence, f"memory {mid}.confidence"))
        _check.optional_text(self.source_episode, f"memory {mid}.source_episode")
        _check.text_tuple(self.keywords, f"memory {mid}.keywords", non_empty=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "subject": self.subject,
            "content": self.content,
            "keywords": list(self.keywords),
            "timestamp": self.timestamp,
            "source_episode": self.source_episode,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: object) -> "MemoryEntry":
        d = _check.require_keys(
            d, "memory",
            {"id", "version", "subject", "content", "timestamp", "confidence"},
            {"keywords", "source_episode"},
        )
        mid = _check.prefixed_id(d["id"], "memory.id", MEMORY_ID_PREFIX)
        keywords = d.get("keywords", [])
        if not isinstance(keywords, list):
            raise ActioDataError(f"memory {mid}.keywords: expected a list, got {keywords!r}")
        # 字段值的校验全在 __post_init__ 里;这里只查键、把 YAML 的 list 换成 tuple
        return cls(
            id=mid,
            version=d["version"],
            subject=d["subject"],
            content=d["content"],
            timestamp=d["timestamp"],
            confidence=d["confidence"],
            source_episode=d.get("source_episode"),
            keywords=tuple(keywords),
        )


def snapshot(entries: list[MemoryEntry] | tuple[MemoryEntry, ...], at: str | None = None) -> list[MemoryEntry]:
    """"某时刻的信念状态":每个 id 取 timestamp <= at 的最高版本(at 为 None 时不限时刻),
    结果按 id 排序。

    先按 (id, version) 分组检测数据损坏(同一 id 的同一版本出现两次且内容不同),
    再做 at 过滤:检测与输入顺序无关,被 at 过滤掉的条目也照样检测。"""
    if at is not None:
        _check.timestamp(at, "snapshot.at")
    by_version: dict[tuple[str, int], MemoryEntry] = {}
    for e in entries:
        if not isinstance(e, MemoryEntry):
            raise ActioDataError(f"expected MemoryEntry, got {type(e).__name__}")
        key = (e.id, e.version)
        cur = by_version.get(key)
        if cur is not None and cur != e:
            raise ActioDataError(f"memory {e.id} has two different entries for version {e.version}")
        by_version[key] = e
    best: dict[str, MemoryEntry] = {}
    for e in by_version.values():
        if at is not None and e.timestamp > at:  # 固定格式的 UTC 时间戳,字符串序即时间序
            continue
        cur = best.get(e.id)
        if cur is None or e.version > cur.version:
            best[e.id] = e
    return [best[k] for k in sorted(best)]


def overlap(entry: MemoryEntry, query: str) -> int:
    """查询与条目(主体、内容、关键词)共有的词面个数。"""
    words = tokens(entry.subject) | tokens(entry.content)
    for k in entry.keywords:
        words |= tokens(k)
    return len(tokens(query) & words)


def retrieve(
    entries: list[MemoryEntry] | tuple[MemoryEntry, ...],
    query: str,
    *,
    subjects: list[str] | tuple[str, ...] | None = None,
    at: str | None = None,
    limit: int = 3,
) -> list[MemoryEntry]:
    """先取 at 时刻的快照,再按 subjects 过滤(None 表示不限主体),
    保留词面重叠 >= 1 的条目,按 (重叠数降序, 置信度降序, id) 排序后取前 limit 条。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ActioDataError(f"retrieve.limit: expected a non-negative int, got {limit!r}")
    pool = snapshot(entries, at)
    if subjects is not None:
        allowed = set(subjects)
        pool = [e for e in pool if e.subject in allowed]
    scored = [(overlap(e, query), e) for e in pool]
    hits = sorted((p for p in scored if p[0] >= 1), key=lambda p: (-p[0], -p[1].confidence, p[1].id))
    return [e for _, e in hits[:limit]]


# ---------------- 记忆端口的内存替身 ----------------

_EVENT_ROW_KEYS = {"key", "text", "interlocutor", "seq", "state", "statement_ids", "failure_category"}
_EVENT_STATES = ("pending", "committed", "abandoned")
_CLOCK_ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def content_id(subject: str, content: str) -> str:
    """内容 id:只由主体与内容决定,同一内容换个条目 id 进来也得到同一个值。"""
    raw = json.dumps([subject, content], ensure_ascii=False, separators=(",", ":"))
    return MEMORY_ID_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _canon(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entry_id(key: str) -> str:
    """事件落成条目时的条目 id:取事件 key 的哈希而不是内容 id。两个 episode 说同一句话时内容 id
    相同,同 id 同版本的两条不同条目会让 snapshot 抛 "two different entries";条目 id 不进命中也不进摘要。"""
    return MEMORY_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _entry_time(seq: int) -> str:
    return (_CLOCK_ORIGIN + timedelta(seconds=seq)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_event_row(row: object) -> dict:
    row = _check.require_keys(row, "memory event row", set(_EVENT_ROW_KEYS))
    what = f"memory event {row['key']!r}"
    _check.text(row["key"], f"{what}.key")
    _check.text(row["text"], f"{what}.text")
    _check.text(row["interlocutor"], f"{what}.interlocutor")
    if _check.integer(row["seq"], f"{what}.seq") < 0:
        raise ActioDataError(f"{what}.seq: expected an int >= 0, got {row['seq']!r}")
    if row["state"] not in _EVENT_STATES:
        raise ActioDataError(f"{what}.state: expected one of {list(_EVENT_STATES)}, got {row['state']!r}")
    ids = row["statement_ids"]
    if not isinstance(ids, list):
        raise ActioDataError(f"{what}.statement_ids: expected a list, got {ids!r}")
    for i, v in enumerate(ids):
        _check.text(v, f"{what}.statement_ids[{i}]")
    _check.optional_text(row["failure_category"], f"{what}.failure_category")
    return dict(row)


class _Ledger:
    """两个替身共用的唯一一份逻辑:条目、事件表与事件序号。TupleMemory 把它放内存,
    FileTupleStore 把它存成 JSON,所以同样内容在两者上的命中与摘要逐值相等。"""

    def __init__(
        self,
        entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = (),
        events: dict[str, dict] | None = None,
        next_seq: int = 0,
    ) -> None:
        if not isinstance(entries, (tuple, list)):
            raise ActioDataError(f"entries: expected a tuple or list, got {type(entries).__name__}")
        snapshot(entries)  # 逐条查类型,并让同 id 同版本的冲突在构造时就抛
        self.entries: list[MemoryEntry] = list(entries)
        self.events: dict[str, dict] = {} if events is None else events
        self.next_seq: int = next_seq

    # ---- 召回 ----

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        """不直接调 retrieve:它的平手裁决用条目自带 id,同一内容换个 id 进来顺序就会变,违背
        "prompt 与轨迹不变";它也不去重,两条同内容条目会让轨迹的 memory_hits 出现重复 id。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
        scored = []
        for e in snapshot(self.entries):
            n = overlap(e, query)
            if n >= 1:
                scored.append((-n, -e.confidence, content_id(e.subject, e.content), e))
        scored.sort(key=lambda t: t[:3])
        hits: list[MemoryHit] = []
        seen: set[str] = set()
        for _, _, cid, e in scored:
            if cid in seen:
                continue
            seen.add(cid)
            hits.append(MemoryHit(cid, f"{e.subject}: {e.content}"))
        return hits[:limit]

    # ---- 事件表 ----

    def begin(self, event: MemoryEvent) -> tuple[str, ...] | None:
        """登记事件。同 key 已 committed 且文本相同返回存下的语句 id(幂等);pending 且文本相同返回
        None,交给调用方再抽一次;已放弃或文本不同抛 MemoryEventConflict。新 key 写一行 pending。"""
        _check.text(event.key, "memory event key")
        _check.text(event.text, "memory event text")
        _check.text(event.interlocutor, "memory event interlocutor")
        row = self.events.get(event.key)
        if row is not None:
            if row["state"] == "abandoned":
                raise MemoryEventConflict(f"memory event {event.key!r} was abandoned")
            if row["text"] != event.text:
                raise MemoryEventConflict(f"memory event {event.key!r} already exists with a different text")
            return tuple(row["statement_ids"]) if row["state"] == "committed" else None
        self.events[event.key] = {
            "key": event.key, "text": event.text, "interlocutor": event.interlocutor,
            "seq": self.next_seq, "state": "pending", "statement_ids": [], "failure_category": None,
        }
        self.next_seq += 1
        return None

    def _row(self, key: str) -> dict:
        row = self.events.get(key)
        if row is None:
            raise MemoryFailureError(f"memory event {key!r} is unknown", transient=False)
        return row

    def commit(self, key: str) -> tuple[str, ...]:
        """按行里的原文追加一条条目,把行改成 committed。已 committed 的行原样返回,不再追加。"""
        row = self._row(key)
        if row["state"] == "abandoned":
            raise MemoryEventConflict(f"memory event {key!r} was abandoned")
        if row["state"] == "pending":
            self.entries.append(MemoryEntry(
                id=_entry_id(key), version=1, subject=row["interlocutor"], content=row["text"],
                timestamp=_entry_time(row["seq"]), confidence=1.0, source_episode=key,
            ))
            row["state"] = "committed"
            row["statement_ids"] = [content_id(row["interlocutor"], row["text"])]
            row["failure_category"] = None
        return tuple(row["statement_ids"])

    def fail(self, key: str, category: str) -> None:
        """抽取失败:行保持 pending,记下失败类别。"""
        _check.text(category, "memory event failure category")
        row = self._row(key)
        if row["state"] != "pending":
            raise MemoryFailureError(f"memory event {key!r} is not pending", transient=False)
        row["failure_category"] = category

    def abandon(self, key: str) -> None:
        """pending 改 abandoned;committed 抛 MemoryEventConflict;已 abandoned 是空操作。"""
        row = self._row(key)
        if row["state"] == "committed":
            raise MemoryEventConflict(f"memory event {key!r} is committed and cannot be abandoned")
        row["state"] = "abandoned"

    # ---- 摘要与序列化 ----

    def digest(self) -> str:
        """条目取 snapshot 的结果(召回实际看到的那一份)而不是原始列表:否则两个库的条目去掉 id 后
        多重集相同、按 id 分组的版本归属不同时,摘要相同而召回不同。abandoned 行只计 key 与 state。"""
        entries = sorted(
            ({k: v for k, v in e.to_dict().items() if k != "id"} for e in snapshot(self.entries)),
            key=_canon,
        )
        events = sorted(
            ({"key": r["key"], "state": r["state"]} if r["state"] == "abandoned" else dict(r)
             for r in self.events.values()),
            key=lambda r: r["key"],
        )
        return hashlib.sha256(_canon({"entries": entries, "events": events}).encode("utf-8")).hexdigest()

    def to_json(self) -> dict:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "events": [dict(r) for r in sorted(self.events.values(), key=lambda r: r["seq"])],
            "next_seq": self.next_seq,
        }

    @classmethod
    def from_json(cls, obj: object) -> "_Ledger":
        obj = _check.require_keys(obj, "memory ledger", {"entries", "events", "next_seq"})
        if not isinstance(obj["entries"], list) or not isinstance(obj["events"], list):
            raise ActioDataError("memory ledger: entries and events must be lists")
        entries = [MemoryEntry.from_dict(d) for d in obj["entries"]]
        events: dict[str, dict] = {}
        for raw in obj["events"]:
            row = _check_event_row(raw)
            if row["key"] in events:
                raise ActioDataError(f"memory ledger: duplicate event key {row['key']!r}")
            events[row["key"]] = row
        next_seq = _check.integer(obj["next_seq"], "memory ledger.next_seq")
        if next_seq < 0 or any(r["seq"] >= next_seq for r in events.values()):
            raise ActioDataError(f"memory ledger.next_seq: {next_seq!r} does not exceed every event seq")
        return cls(entries, events, next_seq)


class TupleMemory:
    """MemoryBackend 的内存替身:包一份 _Ledger。命中 id 一律是内容 id,不用条目自带的 id,
    所以同一内容换个 id 进来,prompt 与轨迹不变。替身的抽取从不失败,也没有待嵌入的东西。"""

    def __init__(self, entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = ()) -> None:
        self._ledger = _Ledger(entries)
        self._closed = False

    def _live(self) -> _Ledger:
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._ledger

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        return self._live().recall(query, limit=limit)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        ledger = self._live()
        ids = ledger.begin(event)
        if ids is None:
            ids = ledger.commit(event.key)
        return RememberOutcome(ids, False, None)

    def embed_pending(self) -> int:
        self._live()
        return 0

    def digest(self) -> str:
        return self._live().digest()

    def close(self) -> None:
        self._closed = True
