"""显式记忆:条目、按时间点取快照、按主体与关键词的词面重叠检索(闭环集成设计 §8.1),
以及记忆端口的两个替身与它们共用的账本 _Ledger:内存里的 TupleMemory,落盘的 FileTupleStore
(场景二记忆设计 §4.1)。账本是一个世界一份,条目与事件行都记着是哪个 holder(主体)写的
(设计三 §3.2):召回按 holder 过滤,摘要覆盖全部 holder。"""
from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from actio import _check
from actio.errors import ActioDataError
from actio.memory_port import (
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    PERSONA_ID_PATTERN,
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
_EVENT_ROW_OPTIONAL_KEYS = {"holder"}  # 读旧账本时行里没有 holder,按 "" 补
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
    row = _check.require_keys(row, "memory event row", set(_EVENT_ROW_KEYS), set(_EVENT_ROW_OPTIONAL_KEYS))
    what = f"memory event {row['key']!r}"
    _check.text(row["key"], f"{what}.key")
    _check.text(row["text"], f"{what}.text")
    _check.text(row["interlocutor"], f"{what}.interlocutor")
    _check.text(row.get("holder", ""), f"{what}.holder", allow_empty=True)
    if _check.integer(row["seq"], f"{what}.seq") < 0:
        raise ActioDataError(f"{what}.seq: expected an int >= 0, got {row['seq']!r}")
    if row["state"] not in _EVENT_STATES:
        raise ActioDataError(f"{what}.state: expected one of {list(_EVENT_STATES)}, got {row['state']!r}")
    ids = row["statement_ids"]
    if not isinstance(ids, list):
        raise ActioDataError(f"{what}.statement_ids: expected a list, got {ids!r}")
    for i, v in enumerate(ids):
        _check.text(v, f"{what}.statement_ids[{i}]")
    if bool(ids) != (row["state"] == "committed"):  # commit 才写语句 id;pending 与 abandoned 行里没有
        raise ActioDataError(f"{what}.statement_ids: a {row['state']} row must "
                             f"{'have' if row['state'] == 'committed' else 'not have'} statement ids, got {ids!r}")
    _check.optional_text(row["failure_category"], f"{what}.failure_category")
    return {"holder": "", **row}


class _Ledger:
    """两个替身共用的唯一一份逻辑:条目、事件表与事件序号,一个世界一份。TupleMemory 把它放内存,
    FileTupleStore 把它存成 JSON,所以同样内容在两者上的命中与摘要逐值相等。

    条目的 schema 不带 holder,账本另存 holders(条目 id → holder);不在表里的条目(构造时传入的、
    读旧账本得到的)属于 holder ""。事件行带 holder,commit 落条目时把它记进 holders。"""

    def __init__(
        self,
        entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = (),
        events: dict[str, dict] | None = None,
        next_seq: int = 0,
        holders: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(entries, (tuple, list)):
            raise ActioDataError(f"entries: expected a tuple or list, got {type(entries).__name__}")
        snapshot(entries)  # 逐条查类型,并让同 id 同版本的冲突在构造时就抛
        self.entries: list[MemoryEntry] = list(entries)
        self.events: dict[str, dict] = {}  # 不与调用方共享行;没有 holder 的行补 ""
        for key, row in ({} if events is None else events).items():
            self.events[key] = {"holder": "", **deepcopy(row)}
        self.next_seq: int = next_seq
        self.holders: dict[str, str] = {} if holders is None else dict(holders)

    # ---- 召回 ----

    def recall(self, query: str, *, limit: int, holder: str | None = None) -> list[MemoryHit]:
        """holder 不是 None 时只看该主体名下的条目("" 也是一个具体的主体:构造时传入的条目都在它名下),
        None 表示不过滤。

        不直接调 retrieve:它的平手裁决用条目自带 id,同一内容换个 id 进来顺序就会变,违背
        "prompt 与轨迹不变";它也不去重,两条同内容条目会让轨迹的 memory_hits 出现重复 id。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
        scored = []
        for e in snapshot(self.entries):
            if holder is not None and self.holders.get(e.id, "") != holder:
                continue
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

    def begin(self, event: MemoryEvent, *, holder: str) -> tuple[str, ...] | None:
        """登记事件,记在 holder 名下。去重键仍是 key(key 已带主体)。同 key 已 committed 且文本相同返回
        存下的语句 id(幂等);pending 且文本相同返回 None,交给调用方再抽一次;已放弃或文本不同抛
        MemoryEventConflict。新 key 写一行 pending。"""
        _check.text(event.key, "memory event key")
        _check.text(event.text, "memory event text")
        _check.text(event.interlocutor, "memory event interlocutor")
        _check.text(holder, "memory event holder", allow_empty=True)
        row = self.events.get(event.key)
        if row is not None:
            if row["state"] == "abandoned":
                raise MemoryEventConflict(f"memory event {event.key!r} was abandoned")
            if row["text"] != event.text:
                raise MemoryEventConflict(f"memory event {event.key!r} already exists with a different text")
            return tuple(row["statement_ids"]) if row["state"] == "committed" else None
        self.events[event.key] = {
            "key": event.key, "text": event.text, "interlocutor": event.interlocutor, "holder": holder,
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
            entry = MemoryEntry(
                id=_entry_id(key), version=1, subject=row["interlocutor"], content=row["text"],
                timestamp=_entry_time(row["seq"]), confidence=1.0, source_episode=key,
            )
            self.entries.append(entry)
            self.holders[entry.id] = row["holder"]
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
        多重集相同、按 id 分组的版本归属不同时,摘要相同而召回不同。每条带上自己的 holder,所以主体不同的
        同内容条目摘要不同;摘要覆盖全部 holder。abandoned 行只计 key 与 state。"""
        entries = sorted(
            ({"holder": self.holders.get(e.id, ""), **{k: v for k, v in e.to_dict().items() if k != "id"}}
             for e in snapshot(self.entries)),
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
            "events": [deepcopy(r) for r in sorted(self.events.values(), key=lambda r: r["seq"])],
            "holders": dict(self.holders),
            "next_seq": self.next_seq,
        }

    @classmethod
    def from_json(cls, obj: object) -> "_Ledger":
        obj = _check.require_keys(obj, "memory ledger", {"entries", "events", "next_seq"}, {"holders"})
        if not isinstance(obj["entries"], list) or not isinstance(obj["events"], list):
            raise ActioDataError("memory ledger: entries and events must be lists")
        entries = [MemoryEntry.from_dict(d) for d in obj["entries"]]
        holders = obj.get("holders", {})  # 旧账本没有这张表:所有条目属于 ""
        if not isinstance(holders, dict):
            raise ActioDataError(f"memory ledger.holders: expected a dict, got {type(holders).__name__}")
        entry_ids = {e.id for e in entries}
        for entry_id, who in holders.items():
            if entry_id not in entry_ids:
                raise ActioDataError(f"memory ledger.holders: {entry_id!r} is not an entry id")
            _check.text(who, f"memory ledger.holders[{entry_id!r}]", allow_empty=True)
        events: dict[str, dict] = {}
        for raw in obj["events"]:
            row = _check_event_row(raw)
            if row["key"] in events:
                raise ActioDataError(f"memory ledger: duplicate event key {row['key']!r}")
            events[row["key"]] = row
        if len({r["seq"] for r in events.values()}) != len(events):
            raise ActioDataError("memory ledger: duplicate event seq")
        next_seq = _check.integer(obj["next_seq"], "memory ledger.next_seq")
        if next_seq < 0 or any(r["seq"] >= next_seq for r in events.values()):
            raise ActioDataError(f"memory ledger.next_seq: {next_seq!r} does not exceed every event seq")
        return cls(entries, events, next_seq, holders)


class TupleMemory:
    """MemoryBackend 的内存替身:包一份 _Ledger,端口绑定一个 holder(主体)。命中 id 一律是内容 id,
    不用条目自带的 id,所以同一内容换个 id 进来,prompt 与轨迹不变。替身的抽取从不失败,也没有待嵌入的东西。

    不带 holder 构造时 holder 是 "",构造时传入的 entries 都在它名下。world(holder) 在同一份账本上
    取另一个主体的端口,训练替身的 rollout 世界用:一个世界一份账本,各角色位各开一个视图。"""

    def __init__(self, entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = ()) -> None:
        self._ledger = _Ledger(entries)
        self._holder = ""
        self._closed = False

    def world(self, holder: str) -> "TupleMemory":
        """共享本端口账本、绑定 holder 的新端口。各视图的关闭状态互相独立。"""
        _check.text(holder, "holder", allow_empty=True)
        view = TupleMemory()
        view._ledger = self._ledger
        view._holder = holder
        return view

    def _live(self) -> _Ledger:
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._ledger

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        return self._live().recall(query, limit=limit, holder=self._holder)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        ledger = self._live()
        ids = ledger.begin(event, holder=self._holder)
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


# ---------------- 记忆端口的落盘替身与部署管理面 ----------------

def _read_ledger(path: Path) -> _Ledger:
    """读并校验一份账本 JSON。读不了、不是 JSON、结构不合法一律是永久失败,异常信息只报类名
    (校验信息里带着记忆正文,不外传)。"""
    try:
        return _Ledger.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # JSONDecodeError、UnicodeDecodeError、ActioDataError 都是 ValueError
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _write_ledger(path: Path, ledger: _Ledger) -> None:
    """先写 <path>.tmp 再 replace 到正式文件,中途崩溃不会留下半个库。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(ledger.to_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _check_name(value: object, what: str) -> str:
    """世界 id 与主体都拼进文件名,过同一份白名单。"""
    if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
        raise ActioDataError(f"{what}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             f"starting with a letter or digit, got {value!r}")
    return value


class FileTupleStore:
    """DeploymentMemoryStore 的落盘替身:<root>/<world_id>.json 存一份 _Ledger.to_json(),世界还没有文件时
    等同空库。一个世界一个文件,各 holder(主体)的条目与事件行都在里面。每个操作都是"读整份、改、写整份"。

    账本的读改写不加锁:同一世界的写入由调用方串行(每个部署 episode 一条线程)。只有打开着的端口数加锁:
    端口会在服务端线程关,而部署驱动器在另一条线程上 wait_closed 或 restore_from。
    extraction_failure 是故障注入口:对一条事件返回非 None 的字符串,就当这次抽取以该
    failure_category 失败;缺省从不失败。"""

    def __init__(
        self,
        root: Path,
        *,
        extraction_failure: Callable[[MemoryEvent], str | None] | None = None,
    ) -> None:
        # 构造不碰磁盘(终审 M-1):装配在预检之前就造替身,预检失败时不能在 .git 里留下空目录;
        # 根目录由第一次写(_write_ledger)建
        self._root = Path(root)
        self._extraction_failure = extraction_failure
        self._lock = threading.Lock()
        self._all_closed = threading.Condition(self._lock)  # 某个世界的端口数归零时 notify_all
        self._open: dict[str, int] = {}  # 每个世界打开着的端口数(不分主体),读写都要持 _lock

    # ---- 读写 ----

    def _path(self, world_id: str) -> Path:
        """world_id 直接拼进文件名:用 fullmatch(模式以 $ 结尾,match 会放过 "p1\n"),
        '/' 与开头的 '.' 都不许。"""
        _check_name(world_id, "world_id")
        return self._root / f"{world_id}.json"

    def _load(self, world_id: str) -> _Ledger:
        path = self._path(world_id)
        return _read_ledger(path) if path.exists() else _Ledger()

    def _save(self, world_id: str, ledger: _Ledger) -> None:
        _write_ledger(self._path(world_id), ledger)

    # ---- 端口计数 ----

    def _port_opened(self, world_id: str) -> None:
        with self._lock:
            self._open[world_id] = self._open.get(world_id, 0) + 1

    def _port_closed(self, port: "_FilePort") -> None:
        """关一个端口。"已关"的判断与计数在同一把锁里:两条线程同时关同一个端口不会减两次。"""
        with self._lock:
            if port._closed:
                return
            port._closed = True
            left = self._open[port._world_id] - 1
            if left > 0:
                self._open[port._world_id] = left
            else:
                del self._open[port._world_id]
                self._all_closed.notify_all()

    # ---- DeploymentMemoryStore ----

    def open(self, world_id: str, holder: str) -> "_FilePort":
        self._path(world_id)
        _check_name(holder, "holder")
        return _FilePort(self, world_id, holder)

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """三段写入:① 登记 pending 行并落盘(行与 seq 在抽取之前就在库里);② 抽取;
        ③ 成功就重新读库、commit、落盘,失败就记下类别、落盘,返回 failed 而不抛。
        已 committed 的同文本事件直接返回存下的语句 id,不调抽取。pending 行重调一律重新抽取:
        替身的语句在 ③ 之前不进库,没有"补收据"的分支。事件与条目记在 holder 名下。"""
        _check_name(holder, "holder")
        event = MemoryEvent(key, text, interlocutor)
        ledger = self._load(world_id)
        ids = ledger.begin(event, holder=holder)
        if ids is not None:
            return RememberOutcome(ids, False, None)
        self._save(world_id, ledger)
        category = None if self._extraction_failure is None else self._extraction_failure(event)
        ledger = self._load(world_id)
        if category is None:
            ids = ledger.commit(key)
            self._save(world_id, ledger)
            return RememberOutcome(ids, False, None)
        ledger.fail(key, category)
        self._save(world_id, ledger)
        return RememberOutcome((), True, category)

    def abandon_event(self, world_id: str, key: str) -> None:
        ledger = self._load(world_id)
        ledger.abandon(key)
        self._save(world_id, ledger)

    def pending_events(self, world_id: str) -> list[str]:
        rows = [r for r in self._load(world_id).events.values() if r["state"] == "pending"]
        return [r["key"] for r in sorted(rows, key=lambda r: r["seq"])]

    def digest(self, world_id: str) -> str:
        return self._load(world_id).digest()

    def backup_to(self, world_id: str, path: Path) -> str:
        """把账本规范化写到 path(世界还没有文件时写空账本,所以起点副本总是存在),
        返回读回的副本的逻辑摘要。"""
        path = Path(path)
        _write_ledger(path, self._load(world_id))
        return _read_ledger(path).digest()

    def restore_from(self, world_id: str, path: Path) -> str:
        """用 path 的内容覆盖该世界的库,返回恢复后的逻辑摘要。该世界还有任何主体的端口开着时拒绝
        (别的世界开着端口不挡);坏备份在覆盖之前就被拒,库原样不动。检查与覆盖在同一把锁里,
        检查之后不会再有端口插进来。"""
        self._path(world_id)
        with self._lock:
            if self._open.get(world_id, 0) > 0:
                raise MemoryFailureError("restore_from: world has an open port", transient=False)
            ledger = _read_ledger(Path(path))
            self._save(world_id, ledger)
        return ledger.digest()

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 秒返回 False。端口可以在别的线程上关。"""
        self._path(world_id)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s >= 0:
            raise ActioDataError(f"wait_closed.timeout_s: expected a non-negative number, got {timeout_s!r}")
        with self._all_closed:
            return self._all_closed.wait_for(lambda: self._open.get(world_id, 0) == 0, timeout_s)


class _FilePort:
    """FileTupleStore 发出的端口,绑定 (世界, 主体):每次调用都经 store 读写磁盘上的库,所以端口与管理面看到
    同一份;recall 只看本主体名下的条目,remember 写在本主体名下。"""

    def __init__(self, store: FileTupleStore, world_id: str, holder: str) -> None:
        self._store = store
        self._world_id = world_id
        self._holder = holder
        self._closed = False  # 只在持 store._lock 时改(_port_closed);_live 的无锁读只会早一拍或晚一拍看到
        store._port_opened(world_id)

    def _live(self) -> FileTupleStore:
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._store

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        return self._live()._load(self._world_id).recall(query, limit=limit, holder=self._holder)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        return self._live().remember_event(
            self._world_id, self._holder, event.key, event.text, interlocutor=event.interlocutor)

    def embed_pending(self) -> int:
        self._live()
        return 0

    def digest(self) -> str:
        return self._live().digest(self._world_id)

    def close(self) -> None:
        self._store._port_closed(self)
