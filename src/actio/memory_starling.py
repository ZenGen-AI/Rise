"""Starling 记忆门面到记忆端口的适配器(场景二记忆设计二 4.1、4.4、10 节)。

全 src/ 只有本文件导入 starling。适配器做四件事:把门面的命中渲染成 `MemoryHit`(命中 id 是内容 id,
库间稳定);把 `remember_event` 的结果换成 `RememberOutcome`;把 Starling 抛的一切异常按第 10 节的
表映射成 `MemoryFailureError` 及其子类;`embed_pending` 按事件时钟给出嵌入时刻。

异常信息只放异常类名:原异常的文字可能含端点地址,所以一律 `from None` 改抛。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import starling

from actio.errors import ActioDataError
from actio.memory_port import (
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    RememberOutcome,
)

# 事件时钟的起点:与 `Memory.remember_event` 的默认 epoch 同值(设计源 4.3 节)。
# 嵌入时刻是 EPOCH 加 next_seq 秒,所以恒不早于任何已写事件的 created_at。
EPOCH = "2026-01-01T00:00:00Z"
_EPOCH_AT = datetime.fromisoformat(EPOCH)
_ID_HEX_CHARS = 16
# Starling 门面 `content_ids` 对本库没有的语句 id 给的占位值(与摘要里解不出引用时同一个标记)。
_MISSING_CONTENT_ID = "missing"


@dataclass(frozen=True)
class StarlingEndpoints:
    """抽取与嵌入两个 OpenAI 兼容端点的配置。密钥不在这里:C++ 适配器只从 OPENAI_API_KEY 读。

    max_retries 对两个端点都生效,0 表示一次都不重试。Starling 的 C++ 默认是 3 次,
    一次失败的调用会拖到约 21 秒,打破 6.4 节"每次网络调用至多 4 秒"。"""

    extraction_base_url: str
    extraction_model: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    timeout_ms: int
    max_retries: int

    def make_llm(self):
        return starling.make_openai_llm(
            model=self.extraction_model, base_url=self.extraction_base_url,
            timeout_ms=self.timeout_ms, max_retries=self.max_retries)

    def make_embedder(self):
        return starling.make_openai_embedder(
            model=self.embedding_model, base_url=self.embedding_base_url, dim=self.embedding_dim,
            timeout_ms=self.timeout_ms, max_retries=self.max_retries)


def translate_failure(exc: BaseException) -> MemoryFailureError:
    """第 10 节的穷举映射。消息只含异常类名,不含原文。

    瞬态只有一种:查询词嵌不出(`RecallDegradedError`,端点暂时不可用最常见)。冲突与已关是各自的
    子类,transient 恒为假。其余一切(`KeyError`、`sqlite3.Error`、`OSError`、`TimeoutError`、
    C++ 抛出的 `RuntimeError`)都是永久失败。"""
    if isinstance(exc, MemoryFailureError):
        return exc
    name = type(exc).__name__
    if isinstance(exc, starling.RecallDegradedError):
        return MemoryFailureError(name, transient=True)
    if isinstance(exc, starling.EventConflict):
        return MemoryEventConflict(name)
    if isinstance(exc, starling.MemoryClosed):
        return MemoryClosedError(name)
    return MemoryFailureError(f"starling memory failed: {name}", transient=False)


def _hit_id(content_id: str) -> str:
    return "mem-" + content_id[:_ID_HEX_CHARS]


def _render(row) -> str:
    """设计源 4.4 节。落库后的 modality 与 polarity 是小写(抽取 JSON 里的大写在落库时转了小写);
    括号与逗号是全角。`StatementRow` 没有 nesting_depth,不渲染嵌套层次。"""
    negation = "不" if row.polarity == "neg" else ""
    return (f"{row.subject_id} {negation}{row.predicate} {row.object_value}"
            f"（{row.modality}，置信度 {row.confidence:.2f}）")


class StarlingMemory:
    """一个人格的一个 Starling 记忆库,满足 `MemoryBackend`。

    `memory` 是 `starling.Memory`(单测里是只有用到的几个方法的替身)。`on_close` 在 close 成功后
    调一次,给部署管理面登记端口已关用。**`on_close` 必须不抛。** 它抛时,门面已经关了、端口已置为
    关闭,`close()` 把它的异常经 `translate_failure` 改抛(非瞬态),之后再调 `close()` 是空操作,
    不会再调 `on_close`:登记的副作用(例如计数减一)就此丢了。"""

    def __init__(self, memory, *, close_timeout_s: float = 60.0,
                 on_close: Callable[[], None] | None = None) -> None:
        self._memory = memory
        self._close_timeout_s = close_timeout_s
        self._on_close = on_close
        self._closed = False

    @classmethod
    def open(cls, path: Path, *, persona_id: str, endpoints: StarlingEndpoints,
             close_timeout_s: float = 60.0) -> StarlingMemory:
        try:
            memory = starling.Memory.open(
                str(path), agent=persona_id, llm=endpoints.make_llm(), embedder=endpoints.make_embedder())
        except Exception as exc:
            raise translate_failure(exc) from None
        return cls(memory, close_timeout_s=close_timeout_s)

    def _live(self):
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._memory

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        memory = self._live()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
        if limit == 0:
            return []
        try:
            # strict:查询词嵌不出就抛 RecallDegradedError,不退回只有词面的结果。
            # include_volatile:刚嵌入、还没被 tick 晋级的语句也要能召回(实例每轮 remember 后 embed_pending)。
            found = memory.recall(query, k=limit, mode="semantic", strict=True, include_volatile=True)
            hits: list[MemoryHit] = []
            seen: set[str] = set()
            for item in found:
                # 余弦不为正就是不相似。Starling 的 top-k 会用零分行补满 k 条,不丢就会把无关的语句召回。
                if item["score"] <= 0.0:
                    continue
                hit_id = _hit_id(item["content_id"])
                if hit_id in seen:
                    continue
                seen.add(hit_id)
                hits.append(MemoryHit(hit_id, _render(item["row"])))
            return hits
        except Exception as exc:
            raise translate_failure(exc) from None

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        memory = self._live()
        try:
            # epoch 用门面的默认值;"[key] " 前缀由 Starling 加,这里只传原文。
            res = memory.remember_event(event.key, event.text, interlocutor=event.interlocutor)
            if res.extraction_failed:
                return RememberOutcome((), True, res.failure_category or "unknown")
            content_ids = memory.content_ids(list(res.statement_ids))
            if _MISSING_CONTENT_ID in content_ids.values():
                # 门面对本库不认得的语句 id 返回 "missing"。刚写完的语句不该认不得;放行的话
                # 假 id mem-missing 会进 stored、准入载荷与轨迹,比一个报错难查得多。
                raise MemoryFailureError("remember: statement not found in library", transient=False)
            return RememberOutcome(tuple(_hit_id(content_ids[s]) for s in res.statement_ids), False, None)
        except Exception as exc:
            raise translate_failure(exc) from None

    def embed_pending(self) -> int:
        memory = self._live()
        try:
            now = (_EPOCH_AT + timedelta(seconds=memory.next_seq())).strftime("%Y-%m-%dT%H:%M:%SZ")
            stats = memory.embed_pending(now)
        except Exception as exc:
            raise translate_failure(exc) from None
        if stats.failed > 0:
            # Starling 只计数不抛;失败的语句下一轮召回就少一条,不能静默。
            raise MemoryFailureError(f"embed_pending: {stats.failed} statement(s) failed to embed", transient=True)
        return stats.embedded

    def digest(self) -> str:
        memory = self._live()
        try:
            return memory.digest()
        except Exception as exc:
            raise translate_failure(exc) from None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._memory.close(timeout=self._close_timeout_s)
        except Exception as exc:
            # 不置 _closed:超时意味着在途调用还没返回,门面仍开着,返回后可以再关一次。
            # 训练副本的收尾靠这一点决定删不删目录。
            raise translate_failure(exc) from None
        self._closed = True       # 门面确实已经关了:先置位,on_close 抛了端口也不能再用
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception as exc:
                raise translate_failure(exc) from None
