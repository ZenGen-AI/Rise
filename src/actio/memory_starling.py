"""Starling 记忆门面到记忆端口的适配器(场景二记忆设计二 4.1、4.4、10 节)。

全 src/ 只有本文件导入 starling。适配器做四件事:把门面的命中渲染成 `MemoryHit`(命中 id 是内容 id,
库间稳定);把 `remember_event` 的结果换成 `RememberOutcome`;把 Starling 抛的一切异常按第 10 节的
表映射成 `MemoryFailureError` 及其子类;`embed_pending` 按事件时钟给出嵌入时刻。

异常信息只放异常类名:原异常的文字可能含端点地址,所以一律 `from None` 改抛。

`StarlingDeploymentStore` 是同一个后端的部署管理面(满足 `DeploymentMemoryStore`):每个人格一个库
`<root>/<persona_id>.db`,管理面的调用各开一条短命连接。"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import starling

from actio.errors import ActioDataError
from actio.memory_port import (
    PERSONA_ID_PATTERN,
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    RememberOutcome,
)

# 事件时钟的起点(设计源 4.3 节)。每次 `remember_event` 都显式传它,不靠门面的默认值恰好同值:
# 门面哪天改了默认值,事件时刻和摘要会静默漂移。
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


@contextlib.contextmanager
def _translated() -> Iterator[None]:
    """块内抛的一切 Exception 按第 10 节的表改抛,原异常的文字不带出去。"""
    try:
        yield
    except Exception as exc:
        raise translate_failure(exc) from None


def _hit_id(content_id: str) -> str:
    return "mem-" + content_id[:_ID_HEX_CHARS]


def _outcome(memory, res) -> RememberOutcome:
    """`remember_event` 的返回译成 `RememberOutcome`,端口与管理面共用。"""
    if res.extraction_failed:
        return RememberOutcome((), True, res.failure_category or "unknown")
    content_ids = memory.content_ids(list(res.statement_ids))
    if _MISSING_CONTENT_ID in content_ids.values():
        # 门面对本库不认得的语句 id 返回 "missing"。刚写完的语句不该认不得;放行的话
        # 假 id mem-missing 会进 stored、准入载荷与轨迹,比一个报错难查得多。
        raise MemoryFailureError("remember: statement not found in library", transient=False)
    return RememberOutcome(tuple(_hit_id(content_ids[s]) for s in res.statement_ids), False, None)


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
             close_timeout_s: float = 60.0, on_close: Callable[[], None] | None = None) -> StarlingMemory:
        try:
            memory = starling.Memory.open(
                str(path), agent=persona_id, llm=endpoints.make_llm(), embedder=endpoints.make_embedder())
        except Exception as exc:
            raise translate_failure(exc) from None
        return cls(memory, close_timeout_s=close_timeout_s, on_close=on_close)

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
            # "[key] " 前缀由 Starling 加,这里只传原文。
            res = memory.remember_event(event.key, event.text, interlocutor=event.interlocutor, epoch=EPOCH)
            return _outcome(memory, res)
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


_WAL_SUFFIXES = ("-wal", "-shm")


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _copy_library(src: Path, dst: Path) -> None:
    """把库文件连同同名 -wal(有的话)拷到 dst。端口开着、WAL 没合并时,写入还在 -wal 里,只拷主文件
    算出的摘要会漏掉它们。-shm 不拷:它是 WAL 的索引,打开时由 -wal 重建。"""
    shutil.copyfile(src, dst)
    wal = _sidecar(src, "-wal")
    if wal.exists():
        shutil.copyfile(wal, _sidecar(dst, "-wal"))


class StarlingDeploymentStore:
    """部署记忆的管理面,满足 `DeploymentMemoryStore`:每个人格一个 Starling 库 `<root>/<persona_id>.db`
    (组合根传入 `<记忆根>/deploy`,设计源 5.3 节)。

    **调用方必须持该人格的人格锁。** 门面只在单个 `Memory` 内计在途调用,不串行化同一库上的多条连接;
    管理面每次调用另开一条短命连接,与端口、与别的管理面调用并发时没有任何保护。rise 今天每个调用点都在
    人格锁里(`mindagent.rounds._PersonaLocks`、`mindagent.admit` 的 `ExitStack`),所以本类不加自己的锁。

    端口数只在实例内计(与 `FileTupleStore` 一样),所以组合根只造一个 store。

    库不存在时:`pending_events` 返回 `[]`,`digest` 给空库的摘要,`abandon_event` 当未知 key,都不建文件;
    `open`、`remember_event`、`backup_to` 要建库,会建。"""

    def __init__(self, root: Path, *, endpoints: StarlingEndpoints, close_timeout_s: float = 60.0) -> None:
        # 构造不碰磁盘:根目录由第一次需要建库的调用建。
        self._root = Path(root)
        self._endpoints = endpoints
        self._close_timeout_s = close_timeout_s
        self._open: dict[str, int] = {}      # 每个人格打开着的端口数

    # ---- 路径与连接 ----

    def _path(self, persona_id: str) -> Path:
        """persona_id 直接拼进文件名:用 fullmatch(模式以 $ 结尾,match 会放过 "p1\\n"),
        '/' 与开头的 '.' 都不许。"""
        if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(persona_id):
            raise ActioDataError(f"persona_id: expected 1 to 128 characters of [A-Za-z0-9._-] "
                                 f"starting with a letter or digit, got {persona_id!r}")
        return self._root / f"{persona_id}.db"

    def _ensure_root(self) -> None:
        with _translated():
            self._root.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _short(self, path: Path, *, agent: str, extract: bool = False) -> Iterator[starling.Memory]:
        """一条短命连接:打开、交给块、关闭。`extract` 为真才配抽取与嵌入端点(只有 remember_event 要),
        其余调用不读 OPENAI_API_KEY。块里抛的一切 Exception 按第 10 节的表改抛;块已经失败时,关闭的
        失败被吞掉,让块的异常说话。`Memory.open` 对不存在的路径会建新库,要不要建由调用方先查。"""
        with _translated():
            memory = starling.Memory.open(
                str(path), agent=agent,
                llm=self._endpoints.make_llm() if extract else None,
                embedder=self._endpoints.make_embedder() if extract else None)
        try:
            yield memory
        except BaseException as exc:
            with contextlib.suppress(Exception):
                memory.close(timeout=self._close_timeout_s)
            if isinstance(exc, Exception):
                raise translate_failure(exc) from None
            raise
        with _translated():
            memory.close(timeout=self._close_timeout_s)

    def _released(self, persona_id: str) -> None:
        """端口的 on_close:只做字典减一,不做任何可能抛的事(它抛了,计数减一就丢了)。"""
        left = self._open.get(persona_id, 0) - 1
        if left > 0:
            self._open[persona_id] = left
        else:
            self._open.pop(persona_id, None)

    # ---- DeploymentMemoryStore ----

    def open(self, persona_id: str) -> StarlingMemory:
        path = self._path(persona_id)
        self._ensure_root()
        port = StarlingMemory.open(
            path, persona_id=persona_id, endpoints=self._endpoints, close_timeout_s=self._close_timeout_s,
            on_close=lambda: self._released(persona_id))
        self._open[persona_id] = self._open.get(persona_id, 0) + 1      # 打开成功后才计数
        return port

    def remember_event(self, persona_id: str, key: str, text: str, *, interlocutor: str) -> RememberOutcome:
        path = self._path(persona_id)
        self._ensure_root()
        with self._short(path, agent=persona_id, extract=True) as memory:
            res = memory.remember_event(key, text, interlocutor=interlocutor, epoch=EPOCH)
            return _outcome(memory, res)

    def abandon_event(self, persona_id: str, key: str) -> None:
        path = self._path(persona_id)
        if not path.exists():
            raise translate_failure(KeyError(key)) from None            # 没有库就没有这个 key
        with self._short(path, agent=persona_id) as memory:
            memory.abandon_event(key)

    def pending_events(self, persona_id: str) -> list[str]:
        path = self._path(persona_id)
        if not path.exists():
            return []
        with self._short(path, agent=persona_id) as memory:
            return [p.key for p in memory.pending_events()]

    def digest(self, persona_id: str) -> str:
        path = self._path(persona_id)
        if not path.exists():
            return self._empty_digest()
        with self._short(path, agent=persona_id) as memory:
            return memory.digest()

    def backup_to(self, persona_id: str, path: Path) -> str:
        """备份到 path 并返回副本的摘要。人格还没有库时先建空库,所以起点副本总是存在。
        path 已存在就覆盖(起点副本由 MindAgent 管,覆盖是它要的语义;Starling 拒绝已存在的目标,
        所以先删),旁边残留的 -wal、-shm 一并删:它们若属于被覆盖的旧文件,会被新文件当成自己的。"""
        library = self._path(persona_id)
        self._ensure_root()
        path = Path(path)
        with self._short(library, agent=persona_id) as memory:
            path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (path, *(_sidecar(path, s) for s in _WAL_SUFFIXES)):
                stale.unlink(missing_ok=True)
            memory.backup_to(path)
        return self.digest_of(path)

    def restore_from(self, persona_id: str, path: Path) -> str:
        """用 path 的内容覆盖该人格的库,返回恢复后的逻辑摘要。该人格还有打开的端口时拒绝;
        坏备份在碰库之前就被拒,库原样不动。

        不在原地打开 path:`Memory.open` 会跑迁移、写 WAL,而起点副本是提交点,字节不能变。
        所以拷到临时目录,在副本上开,`backup_to` 成库旁的 `<库>.restore-partial`(自洽的单文件),
        先删掉库旁的 -wal、-shm,再 `os.replace` 到库路径。

        **必须先删日志、后换文件。** `backup_to` 用 `VACUUM INTO`,产出的是回滚日志模式的单文件,header
        里不记 WAL 的 salt,SQLite 分不出库旁的 -wal 是不是它的。先换后删时,在两步之间崩溃会把旧库的
        -wal 留在新库旁,之后任何一次 `Memory.open` 都把它重放进新库,摘要回到还原前的值,回滚静默
        失效,close 时还会把重放的内容检查点写死进主文件。先删后换则两个崩溃点都安全:换之前崩溃,
        旧库仍是完整可开的(没有打开的端口,上一次 close 已合并 WAL,库旁本来就没有日志);换之后崩溃,
        库旁已经没有日志。"""
        library = self._path(persona_id)
        if self._open.get(persona_id, 0) > 0:
            raise MemoryFailureError("restore_from: persona has an open port", transient=False)
        path = Path(path)
        if not path.exists():
            raise MemoryFailureError("restore_from: no such backup", transient=False)
        with _translated(), tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "source.db"
            _copy_library(path, copy)
            partial = _sidecar(library, ".restore-partial")
            with self._short(copy, agent=persona_id) as memory:
                digest = memory.digest()             # 坏备份在这里抛:根目录与库都还没动
                self._ensure_root()
                partial.unlink(missing_ok=True)      # Starling 的 backup_to 遇到已存在的目标抛 FileExistsError
                memory.backup_to(partial)
            for suffix in _WAL_SUFFIXES:
                _sidecar(library, suffix).unlink(missing_ok=True)
            os.replace(partial, library)
        return digest

    # ---- 摘要 ----

    def digest_of(self, path: Path) -> str:
        """只读地算一个库文件的摘要(解析器与种子都用)。不在原地打开:`Memory.open` 会跑迁移、写 WAL,
        而种子库只读(5.3 节)、起点副本是提交点,算摘要不能改它们的字节。所以连同 -wal 拷进临时目录,
        在副本上算。摘要与打开时用的 agent 名无关。"""
        path = Path(path)
        if not path.exists():
            raise MemoryFailureError("digest_of: no such library", transient=False)
        with _translated(), tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "library.db"
            _copy_library(path, copy)
            with self._short(copy, agent="digest") as memory:
                return memory.digest()

    def _empty_digest(self) -> str:
        """空库的摘要,在临时目录里开一个空库算。摘要的格式是 Starling 的,不在 rise 里写死它的值。"""
        with _translated(), tempfile.TemporaryDirectory() as tmp:
            with self._short(Path(tmp) / "empty.db", agent="digest") as memory:
                return memory.digest()
