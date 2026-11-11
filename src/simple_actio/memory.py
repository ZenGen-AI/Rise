"""显式记忆:条目、按时间点取快照、按主体与关键词的词面重叠检索(闭环集成设计 §8.1),
以及记忆端口的两个替身与它们共用的账本 _Ledger:内存里的 TupleMemory,落盘的 FileTupleStore
(场景二记忆设计 §4.1)。账本是一个世界一份,条目与事件行都记着是哪个 holder(主体)写的
(设计三 §3.2):召回按 holder 过滤,摘要覆盖全部 holder。"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from simple_actio import _check
from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    PERSONA_ID_PATTERN,
    PendingKey,
    RememberOutcome,
    SnapshotRef,
)
from simple_actio.text import tokens

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
            raise SimpleActioDataError(f"memory {mid}.keywords: expected a list, got {keywords!r}")
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
            raise SimpleActioDataError(f"expected MemoryEntry, got {type(e).__name__}")
        key = (e.id, e.version)
        cur = by_version.get(key)
        if cur is not None and cur != e:
            raise SimpleActioDataError(f"memory {e.id} has two different entries for version {e.version}")
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
        raise SimpleActioDataError(f"retrieve.limit: expected a non-negative int, got {limit!r}")
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
# 一般事实(事件的 interlocutor 为空串)落成条目时用的所述对象。条目的 subject 必须非空(MemoryEntry 校验,
# 落盘账本读回时也过它),所以空串只留在事件行里,条目取这个哨兵。选 "*":既不是合法人格 id(人格 id 以字母
# 或数字开头),也不产生任何词面,所以不会因为查询里碰巧有某个词就让一般事实多得一个命中。
_GENERAL_FACT_SUBJECT = "*"


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
    _check.text(row["interlocutor"], f"{what}.interlocutor", allow_empty=True)  # 空串是一般事实
    _check.text(row.get("holder", ""), f"{what}.holder", allow_empty=True)
    if _check.integer(row["seq"], f"{what}.seq") < 0:
        raise SimpleActioDataError(f"{what}.seq: expected an int >= 0, got {row['seq']!r}")
    if row["state"] not in _EVENT_STATES:
        raise SimpleActioDataError(f"{what}.state: expected one of {list(_EVENT_STATES)}, got {row['state']!r}")
    ids = row["statement_ids"]
    if not isinstance(ids, list):
        raise SimpleActioDataError(f"{what}.statement_ids: expected a list, got {ids!r}")
    for i, v in enumerate(ids):
        _check.text(v, f"{what}.statement_ids[{i}]")
    if bool(ids) != (row["state"] == "committed"):  # commit 才写语句 id;pending 与 abandoned 行里没有
        raise SimpleActioDataError(f"{what}.statement_ids: a {row['state']} row must "
                             f"{'have' if row['state'] == 'committed' else 'not have'} statement ids, got {ids!r}")
    _check.optional_text(row["failure_category"], f"{what}.failure_category")
    return {"holder": "", **row}


class _Ledger:
    """两个替身共用的唯一一份逻辑:条目、事件表与事件序号,一个世界一份。TupleMemory 把它放内存,
    FileTupleStore 把它存成 JSON,所以同样内容在两者上的命中与摘要逐值相等。

    条目的 schema 不带 holder,账本另存 holders(条目 id → holder);不在表里的条目(构造时传入的、
    读旧账本得到的)属于 holder ""。事件行带 holder,事件表按 (holder, key) 存:同一 key 在两个主体名下是
    两行(与 Starling 第 2 期起的事件表同语义);commit 落条目时把行的 holder 记进 holders。

    账本自己不加锁:begin 读 next_seq 再加一、digest 遍历事件表,两条线程交错就会出错。多线程共用一份账本时,
    由持有者在每次操作外面加锁(TupleMemory 的各视图共用一把锁,FileTupleStore 用 store 的锁)。"""

    def __init__(
        self,
        entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = (),
        events: dict[tuple[str, str], dict] | None = None,
        next_seq: int = 0,
        holders: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(entries, (tuple, list)):
            raise SimpleActioDataError(f"entries: expected a tuple or list, got {type(entries).__name__}")
        snapshot(entries)  # 逐条查类型,并让同 id 同版本的冲突在构造时就抛
        self.entries: list[MemoryEntry] = list(entries)
        self.events: dict[tuple[str, str], dict] = {}  # 键是 (holder, key);不与调用方共享行;没有 holder 的行补 ""
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
            raise SimpleActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
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
        """登记事件,记在 holder 名下。去重键是 (holder, key):同 key 换个主体是另一行。同一行已 committed
        且文本相同返回存下的语句 id(幂等);pending 且文本相同返回 None,交给调用方再抽一次;已放弃或文本
        不同抛 MemoryEventConflict。新行写成 pending。"""
        _check.text(event.key, "memory event key")
        _check.text(event.text, "memory event text")
        _check.text(event.interlocutor, "memory event interlocutor", allow_empty=True)  # 空串是一般事实,与 Starling 一致
        _check.text(holder, "memory event holder", allow_empty=True)
        row = self.events.get((holder, event.key))
        if row is not None:
            if row["state"] == "abandoned":
                raise MemoryEventConflict(f"memory event {event.key!r} was abandoned")
            if row["text"] != event.text:
                raise MemoryEventConflict(f"memory event {event.key!r} already exists with a different text")
            return tuple(row["statement_ids"]) if row["state"] == "committed" else None
        self.events[(holder, event.key)] = {
            "key": event.key, "text": event.text, "interlocutor": event.interlocutor, "holder": holder,
            "seq": self.next_seq, "state": "pending", "statement_ids": [], "failure_category": None,
        }
        self.next_seq += 1
        return None

    def _row(self, key: str, holder: str) -> dict:
        """只找 holder 名下的这一行:别的主体名下同 key 的行不算(与 Starling 的 event_not_found 同类)。"""
        row = self.events.get((holder, key))
        if row is None:
            raise MemoryFailureError(f"memory event {key!r} of holder {holder!r} is unknown", transient=False)
        return row

    def commit(self, key: str, *, holder: str) -> tuple[str, ...]:
        """按行里的原文追加一条条目,把行改成 committed。已 committed 的行原样返回,不再追加。"""
        row = self._row(key, holder)
        if row["state"] == "abandoned":
            raise MemoryEventConflict(f"memory event {key!r} was abandoned")
        if row["state"] == "pending":
            subject = row["interlocutor"] or _GENERAL_FACT_SUBJECT  # 一般事实:事件行留空串,条目取哨兵
            entry = MemoryEntry(
                id=_entry_id(key), version=1, subject=subject, content=row["text"],
                timestamp=_entry_time(row["seq"]), confidence=1.0, source_episode=key,
            )
            self.entries.append(entry)
            self.holders[entry.id] = row["holder"]
            row["state"] = "committed"
            row["statement_ids"] = [content_id(subject, row["text"])]
            row["failure_category"] = None
        return tuple(row["statement_ids"])

    def fail(self, key: str, category: str, *, holder: str) -> None:
        """抽取失败:行保持 pending,记下失败类别。"""
        _check.text(category, "memory event failure category")
        row = self._row(key, holder)
        if row["state"] != "pending":
            raise MemoryFailureError(f"memory event {key!r} is not pending", transient=False)
        row["failure_category"] = category

    def abandon(self, key: str, *, holder: str) -> None:
        """pending 改 abandoned;committed 抛 MemoryEventConflict;已 abandoned 是空操作。只动 holder 名下的行。"""
        row = self._row(key, holder)
        if row["state"] == "committed":
            raise MemoryEventConflict(f"memory event {key!r} is committed and cannot be abandoned")
        row["state"] = "abandoned"

    # ---- 摘要与序列化 ----

    def digest(self) -> str:
        """条目取 snapshot 的结果(召回实际看到的那一份)而不是原始列表:否则两个库的条目去掉 id 后
        多重集相同、按 id 分组的版本归属不同时,摘要相同而召回不同。每条带上自己的 holder,所以主体不同的
        同内容条目摘要不同;摘要覆盖全部 holder。abandoned 行只计 key 与 state。事件行按 (key, holder) 排序:
        一个 key 只有一行时与只按 key 排序的结果相同,所以已有账本的摘要不变。"""
        entries = sorted(
            ({"holder": self.holders.get(e.id, ""), **{k: v for k, v in e.to_dict().items() if k != "id"}}
             for e in snapshot(self.entries)),
            key=_canon,
        )
        events = [
            {"key": r["key"], "state": r["state"]} if r["state"] == "abandoned" else dict(r)
            for r in sorted(self.events.values(), key=lambda r: (r["key"], r["holder"]))
        ]
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
            raise SimpleActioDataError("memory ledger: entries and events must be lists")
        entries = [MemoryEntry.from_dict(d) for d in obj["entries"]]
        holders = obj.get("holders", {})  # 旧账本没有这张表:所有条目属于 ""
        if not isinstance(holders, dict):
            raise SimpleActioDataError(f"memory ledger.holders: expected a dict, got {type(holders).__name__}")
        entry_ids = {e.id for e in entries}
        for entry_id, who in holders.items():
            if entry_id not in entry_ids:
                raise SimpleActioDataError(f"memory ledger.holders: {entry_id!r} is not an entry id")
            _check.text(who, f"memory ledger.holders[{entry_id!r}]", allow_empty=True)
        events: dict[tuple[str, str], dict] = {}
        for raw in obj["events"]:
            row = _check_event_row(raw)  # 没有 holder 的行补 "":同 key 不同主体是两行,同 (holder, key) 才重复
            if (row["holder"], row["key"]) in events:
                raise SimpleActioDataError(
                    f"memory ledger: duplicate event key {row['key']!r} of holder {row['holder']!r}")
            events[(row["holder"], row["key"])] = row
        if len({r["seq"] for r in events.values()}) != len(events):
            raise SimpleActioDataError("memory ledger: duplicate event seq")
        next_seq = _check.integer(obj["next_seq"], "memory ledger.next_seq")
        if next_seq < 0 or any(r["seq"] >= next_seq for r in events.values()):
            raise SimpleActioDataError(f"memory ledger.next_seq: {next_seq!r} does not exceed every event seq")
        return cls(entries, events, next_seq, holders)


class TupleMemory:
    """MemoryBackend 的内存替身:包一份 _Ledger,端口绑定一个 holder(主体)。命中 id 一律是内容 id,
    不用条目自带的 id,所以同一内容换个 id 进来,prompt 与轨迹不变。替身的抽取从不失败,也没有待嵌入的东西。

    不带 holder 构造时 holder 是 "",构造时传入的 entries 都在它名下。world(holder) 在同一份账本上
    取另一个主体的端口,训练替身的 rollout 世界用:一个世界一份账本,各角色位各开一个视图。

    各视图会在不同线程上用(客户端线程与服务端线程),所以同一份账本配一把锁,所有视图共用它,
    每次账本操作都在锁里做。"""

    def __init__(self, entries: tuple[MemoryEntry, ...] | list[MemoryEntry] = ()) -> None:
        self._ledger = _Ledger(entries)
        self._lock = threading.Lock()  # 守 _ledger;world() 出来的视图共用这一把
        self._holder = ""
        self._closed = False

    def world(self, holder: str) -> "TupleMemory":
        """共享本端口账本与账本锁、绑定 holder 的新端口。各视图的关闭状态互相独立。"""
        _check.text(holder, "holder", allow_empty=True)
        view = TupleMemory()
        view._ledger = self._ledger
        view._lock = self._lock
        view._holder = holder
        return view

    def _live(self) -> _Ledger:
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._ledger

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        ledger = self._live()
        with self._lock:
            return ledger.recall(query, limit=limit, holder=self._holder)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        ledger = self._live()
        with self._lock:  # begin 与 commit 一起做:中间不让别的视图插进来占序号
            ids = ledger.begin(event, holder=self._holder)
            if ids is None:
                ids = ledger.commit(event.key, holder=self._holder)
        return RememberOutcome(ids, False, None)

    def embed_pending(self) -> int:
        self._live()
        return 0

    def digest(self) -> str:
        ledger = self._live()
        with self._lock:
            return ledger.digest()

    def close(self) -> None:
        self._closed = True


# ---------------- 记忆端口的落盘替身与部署管理面 ----------------

def _read_ledger(path: Path) -> _Ledger:
    """读并校验一份账本 JSON。读不了、不是 JSON、结构不合法一律是永久失败,异常信息只报类名
    (校验信息里带着记忆正文,不外传)。"""
    try:
        return _Ledger.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # JSONDecodeError、UnicodeDecodeError、SimpleActioDataError 都是 ValueError
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _exists(path: Path) -> bool:
    """`Path.exists()` 的 OSError(如 PermissionError)与读写同口径:永久失败,只报类名,不带路径。"""
    try:
        return path.exists()
    except OSError as exc:
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _write_json(path: Path, doc: dict) -> None:
    """先写 <path>.tmp 再 replace 到正式文件,中途崩溃不会留下半个库。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _write_ledger(path: Path, ledger: _Ledger) -> None:
    _write_json(path, ledger.to_json())


def _read_snapshot(path: Path) -> _Ledger:
    """读并校验一份 held 快照 `{"note": ..., "ledger": ...}`。与 `_read_ledger` 同口径:读不出一律永久失败,只报类名。"""
    try:
        doc = _check.require_keys(json.loads(path.read_text(encoding="utf-8")), "held snapshot", {"note", "ledger"})
        _check.text(doc["note"], "held snapshot.note", allow_empty=True)
        return _Ledger.from_json(doc["ledger"])
    except (OSError, ValueError) as exc:
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


def _check_name(value: object, what: str) -> str:
    """世界 id 与主体都拼进文件名,过同一份白名单。"""
    if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
        raise SimpleActioDataError(f"{what}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             f"starting with a letter or digit, got {value!r}")
    return value


# 替身的世界名前缀:`world_prefix` 的缺省值。替身不读本机记忆配置,所以写死(40 篇 5.1 节方法表)。
STAND_IN_WORLD_PREFIX = "rise"


class FileTupleStore:
    """DeploymentMemoryStore 的落盘替身:<root>/<world_id>.json 存一份 _Ledger.to_json(),世界还没有文件时
    等同空库。一个世界一个文件,各 holder(主体)的条目与事件行都在里面。每个操作都是"读整份、改、写整份"。
    held 快照是 <held_root>/<world_id>/<snapshot_id>.json(内容 `{"note", "ledger"}`),重放世界是
    <replay_root>/<重放世界名>.json(组合根传 `<记忆根>/held` 与 `<记忆根>/replay`,90 篇第 4 章布局表)。
    `open` 与其余按世界 id 的方法只读写 root,`open_replay` 只读 replay_root,不看名字长什么样(40 篇 AC-34)。

    一个世界的所有主体共用这一个文件,端口又会在两条线程上用(部署驱动器线程写智能体位,服务端线程写对手位),
    所以每个碰世界文件的操作(端口与管理面一样)都在 store 的锁里完成"读整份、改、写整份":两次读改写
    不会互相覆盖,也不会同时写同一个 .tmp。同一把锁还守打开着的端口数:端口会在服务端线程关,
    而驱动器在另一条线程上 wait_closed 或 restore。端口数按完整世界名计,部署端口记在
    `rise.deploy.<world_id>` 下,重放端口记在重放世界名下,两种共用一张表。锁只在一个 store 实例内有效,
    所以组合根只造一个 store(与 StarlingDeploymentStore 的假设一致)。
    extraction_failure 是故障注入口:对一条事件返回非 None 的字符串,就当这次抽取以该
    failure_category 失败;缺省从不失败。它在锁外调用,所以可以慢,也可以回头调这个 store。"""

    def __init__(
        self,
        root: Path,
        *,
        held_root: Path,
        replay_root: Path,
        extraction_failure: Callable[[MemoryEvent], str | None] | None = None,
    ) -> None:
        # 构造不碰磁盘(终审 M-1):装配在预检之前就造替身,预检失败时不能在 .git 里留下空目录;
        # 三个根目录各由第一次写(_write_json)建
        self._root = Path(root)
        self._held_root = Path(held_root)
        self._replay_root = Path(replay_root)
        self._extraction_failure = extraction_failure
        self._lock = threading.Lock()  # 守世界文件的读改写与 _open;公开操作各自持有,内部的 _load/_save 不拿
        self._all_closed = threading.Condition(self._lock)  # 某个世界的端口数归零时 notify_all
        self._open: dict[str, int] = {}  # 每个完整世界名打开着的端口数(不分主体),读写都要持 _lock

    # ---- 读写 ----

    def _path(self, world_id: str) -> Path:
        """world_id 直接拼进文件名:用 fullmatch(模式以 $ 结尾,match 会放过 "p1\n"),
        '/' 与开头的 '.' 都不许。"""
        _check_name(world_id, "world_id")
        return self._root / f"{world_id}.json"

    def _held_path(self, world_id: str, snapshot_id: str) -> Path:
        _check_name(world_id, "world_id")
        _check_name(snapshot_id, "snapshot_id")
        return self._held_root / world_id / f"{snapshot_id}.json"

    def _replay_path(self, replay_world: str) -> Path:
        _check_name(replay_world, "replay_world")
        return self._replay_root / f"{replay_world}.json"

    @staticmethod
    def _deploy_key(world_id: str) -> str:
        """部署世界在端口计数表里的键:完整世界名,与 Starling 的 `<前缀>.deploy.<id>` 同形。"""
        return f"{STAND_IN_WORLD_PREFIX}.deploy.{world_id}"

    @staticmethod
    def _load_at(path: Path) -> _Ledger:
        return _read_ledger(path) if _exists(path) else _Ledger()

    def _load(self, world_id: str) -> _Ledger:
        return self._load_at(self._path(world_id))

    def _save(self, world_id: str, ledger: _Ledger) -> None:
        _write_ledger(self._path(world_id), ledger)

    # ---- 端口计数 ----

    def _port_opened(self, key: str) -> None:
        with self._lock:
            self._open[key] = self._open.get(key, 0) + 1

    def _port_closed(self, port: "_FilePort") -> None:
        """关一个端口。"已关"的判断与计数在同一把锁里:两条线程同时关同一个端口不会减两次。"""
        with self._lock:
            if port._closed:
                return
            port._closed = True
            left = self._open[port._key] - 1
            if left > 0:
                self._open[port._key] = left
            else:
                del self._open[port._key]
                self._all_closed.notify_all()

    # ---- DeploymentMemoryStore ----

    def open(self, world_id: str, holder: str) -> "_FilePort":
        path = self._path(world_id)
        _check_name(holder, "holder")
        return _FilePort(self, world_id, holder, path=path, key=self._deploy_key(world_id), read_only=False)

    def open_replay(self, replay_world: str, holder: str) -> "_FilePort":
        """在重放世界上开只读端口。文件不在是永久失败(与 Starling 的 world_not_found 同类),不建。"""
        path = self._replay_path(replay_world)
        _check_name(holder, "holder")
        if not _exists(path):
            raise MemoryFailureError("open_replay: world_not_found", transient=False)
        return _FilePort(self, replay_world, holder, path=path, key=replay_world, read_only=True)

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """三段写入:① 登记 pending 行并落盘(行与 seq 在抽取之前就在库里);② 抽取;
        ③ 成功就重新读库、commit、落盘,失败就记下类别、落盘,返回 failed 而不抛。
        已 committed 的同文本事件直接返回存下的语句 id,不调抽取。pending 行重调一律重新抽取:
        替身的语句在 ③ 之前不进库,没有"补收据"的分支。事件与条目记在 holder 名下。"""
        _check_name(holder, "holder")
        event = MemoryEvent(key, text, interlocutor)
        with self._lock:  # ① 登记
            ledger = self._load(world_id)
            ids = ledger.begin(event, holder=holder)
            if ids is not None:
                return RememberOutcome(ids, False, None)
            self._save(world_id, ledger)
        category = None if self._extraction_failure is None else self._extraction_failure(event)  # ② 不持锁
        with self._lock:  # ③ 重新读库再改:②期间别的线程可能已经写过这个世界
            ledger = self._load(world_id)
            if category is None:
                ids = ledger.commit(key, holder=holder)
                self._save(world_id, ledger)
                return RememberOutcome(ids, False, None)
            ledger.fail(key, category, holder=holder)
            self._save(world_id, ledger)
        return RememberOutcome((), True, category)

    def abandon_event(self, world_id: str, holder: str, key: str) -> None:
        """只放弃 holder 名下的这一行;别的主体名下同 key 的行不碰,找不到是永久失败。"""
        _check_name(holder, "holder")
        with self._lock:
            ledger = self._load(world_id)
            ledger.abandon(key, holder=holder)
            self._save(world_id, ledger)

    def pending_events(self, world_id: str) -> list[PendingKey]:
        """全部主体的 pending 行,按 seq 升序。空主体的行只会出自旧账本,rise 的世界里没有,见到就是永久失败
        (不从 key 里解析主体)。"""
        with self._lock:
            ledger = self._load(world_id)
        rows = [r for r in ledger.events.values() if r["state"] == "pending"]  # 读回的是私有副本,锁外算
        out = []
        for r in sorted(rows, key=lambda r: r["seq"]):
            if r["holder"] == "":
                raise MemoryFailureError(f"pending event {r['key']!r} has no holder", transient=False)
            out.append(PendingKey(r["key"], r["holder"]))
        return out

    def digest(self, world_id: str) -> str:
        return self._digest_at(self._path(world_id))

    def _digest_at(self, path: Path) -> str:
        with self._lock:
            ledger = self._load_at(path)
        return ledger.digest()

    def _recall_at(self, path: Path, holder: str, query: str, limit: int) -> list[MemoryHit]:
        with self._lock:
            ledger = self._load_at(path)
        return ledger.recall(query, limit=limit, holder=holder)

    def snapshot(self, world_id: str, *, note: str) -> SnapshotRef:
        """把账本连同 note 写成 held 快照,返回快照 id 与读回的快照的逻辑摘要。id 在写之前就定;世界还没有文件时
        快照是空账本,摘要等于空世界摘要,也不建部署世界文件。"""
        _check.text(note, "snapshot.note", allow_empty=True)
        snapshot_id = "snap-" + secrets.token_hex(8)
        path = self._held_path(world_id, snapshot_id)
        with self._lock:
            ledger = self._load(world_id)
            _write_json(path, {"note": note, "ledger": ledger.to_json()})
            return SnapshotRef(snapshot_id, _read_snapshot(path).digest())

    def restore(self, world_id: str, snapshot_id: str) -> str:
        """用这份快照覆盖该世界的库,返回恢复后的逻辑摘要。该世界还有任何主体的部署端口开着时拒绝
        (与 Starling 服务的 world_held 同类;别的世界与重放世界开着端口不挡);快照不在或读不出时在覆盖之前就拒,
        库原样不动。查端口数、读快照、写世界在同一次持锁里,检查之后不会再有端口插进来。"""
        path = self._held_path(world_id, snapshot_id)
        with self._lock:
            if self._open.get(self._deploy_key(world_id), 0) > 0:
                raise MemoryFailureError("restore: world has an open port", transient=False)
            if not _exists(path):
                raise MemoryFailureError("restore: snapshot_not_found", transient=False)
            ledger = _read_snapshot(path)
            self._save(world_id, ledger)
        return ledger.digest()

    def delete_snapshot(self, world_id: str, snapshot_id: str) -> None:
        """删快照文件;不在是空操作(Starling 回 snapshot_not_found,rise 也当作成功,两边都幂等)。"""
        path = self._held_path(world_id, snapshot_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise MemoryFailureError(type(exc).__name__, transient=False) from None

    def fork_replay(self, world_id: str, snapshot_id: str, *, round_id: str) -> str:
        """从快照分叉一个重放世界:写 replay_root/<名字>.json,返回名字 `rise.verify.<round_id>.<world_id>.<8 位十六进制>`。
        快照不在是永久失败。崩溃留下的重放世界文件不清,只占磁盘(90 篇失败表)。"""
        path = self._held_path(world_id, snapshot_id)
        _check_name(round_id, "round_id")
        name = f"{STAND_IN_WORLD_PREFIX}.verify.{round_id}.{world_id}.{secrets.token_hex(4)}"
        target = self._replay_path(name)            # 拼出来超过 128 字符时在这里拒绝,不写任何文件
        with self._lock:
            if not _exists(path):
                raise MemoryFailureError("fork_replay: snapshot_not_found", transient=False)
            _write_ledger(target, _read_snapshot(path))
        return name

    def release_replay(self, replay_world_id: str) -> None:
        """删重放世界的文件;不在是空操作。"""
        path = self._replay_path(replay_world_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise MemoryFailureError(type(exc).__name__, transient=False) from None

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该部署世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 秒返回 False。端口可以在别的线程上关。
        重放端口记在重放世界名下,不算进来。"""
        self._path(world_id)
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s < 0):
            raise SimpleActioDataError(f"wait_closed.timeout_s: expected a finite non-negative number, got {timeout_s!r}")
        key = self._deploy_key(world_id)
        with self._all_closed:
            return self._all_closed.wait_for(lambda: self._open.get(key, 0) == 0, timeout_s)


class _FilePort:
    """FileTupleStore 发出的端口,绑定 (世界, 主体):每次调用都经 store 读写磁盘上的库,所以端口与管理面看到
    同一份;recall 只看本主体名下的条目,remember 写在本主体名下。`open_replay` 开的端口只读:remember 抛永久
    MemoryFailureError("world_read_only")(与 Starling 的同名码同类),embed_pending 返回 0。"""

    def __init__(self, store: FileTupleStore, world_id: str, holder: str, *,
                 path: Path, key: str, read_only: bool) -> None:
        self._store = store
        self._world_id = world_id
        self._holder = holder
        self._path = path          # 部署世界在 root 下,重放世界在 replay_root 下,开端口时就定
        self._key = key            # 端口计数表里的完整世界名
        self._read_only = read_only
        self._closed = False  # 只在持 store._lock 时改(_port_closed);_live 的无锁读只会早一拍或晚一拍看到
        store._port_opened(key)

    def _live(self) -> FileTupleStore:
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._store

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        return self._live()._recall_at(self._path, self._holder, query, limit)

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        store = self._live()
        if self._read_only:
            raise MemoryFailureError("world_read_only", transient=False)
        return store.remember_event(
            self._world_id, self._holder, event.key, event.text, interlocutor=event.interlocutor)

    def embed_pending(self) -> int:
        self._live()
        return 0

    def digest(self) -> str:
        return self._live()._digest_at(self._path)

    def close(self) -> None:
        self._store._port_closed(self)
