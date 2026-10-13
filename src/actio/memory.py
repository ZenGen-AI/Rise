"""显式记忆:条目、按时间点取快照、按主体与关键词的词面重叠检索(闭环集成设计 §8.1)。"""
from __future__ import annotations

from dataclasses import dataclass

from actio import _check
from actio.errors import ActioDataError
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
