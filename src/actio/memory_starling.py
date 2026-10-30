"""Starling 记忆门面到记忆端口的适配器(场景二记忆设计二 4.1、4.4、10 节)。

全 src/ 只有本文件导入 starling。适配器做四件事:把门面的命中渲染成 `MemoryHit`(命中 id 是内容 id,
库间稳定);把 `remember_event` 的结果换成 `RememberOutcome`;把 Starling 抛的一切异常按第 10 节的
表映射成 `MemoryFailureError` 及其子类;`embed_pending` 按事件时钟给出嵌入时刻。

异常信息只放异常类名:原异常的文字可能含端点地址,所以一律 `from None` 改抛。

`StarlingDeploymentStore` 是同一个后端的部署管理面(满足 `DeploymentMemoryStore`):每个人格一个库
`<root>/<persona_id>.db`,管理面的调用各开一条短命连接。"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
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


# SQLite 数据库文件的前 16 字节。
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _require_library_file(path: Path, method: str) -> None:
    """拷贝或打开之前先认文件头。SQLite 把已存在的 0 字节文件当成合法的空数据库,不校验的话,
    0 字节的"备份"(磁盘满、写副本时被 SIGKILL、手工 touch)会让 `restore_from` 把人格库清成空库,
    而回滚接着删掉副本,原记忆就找不回来。头不对的文件同理:不是库,就不该交给 `Memory.open`。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(_SQLITE_MAGIC))
    except OSError as exc:
        raise translate_failure(exc) from None
    if head != _SQLITE_MAGIC:
        raise MemoryFailureError(f"{method}: not a library file", transient=False)


def copy_library(src: Path, dst: Path) -> None:
    """把库文件连同同名 -wal(有的话)拷到 dst。端口开着、WAL 没合并时,写入还在 -wal 里,只拷主文件
    算出的摘要会漏掉它们。-shm 不拷:它是 WAL 的索引,打开时由 -wal 重建。

    公开名:`library_digest`、`restore_from` 用它,训练路的副本(`factory.memory_runtime.TrainingCopies`)
    也用它从种子取起点。取副本用文件复制而不用 `backup_to`:`backup_to` 要先打开源库,`Memory.open` 会建
    -wal、-shm 并跑迁移,种子目录与文件是只读时失败;文件复制只读源,两种权限下摘要都等于 `library_digest`
    给的起点摘要。**源必须静止**(没有连接在写它),约束同 `library_digest`。"""
    shutil.copyfile(src, dst)
    wal = _sidecar(src, "-wal")
    if wal.exists():
        shutil.copyfile(wal, _sidecar(dst, "-wal"))


@contextlib.contextmanager
def _short_connection(path: Path, *, agent: str, close_timeout_s: float,
                      endpoints: StarlingEndpoints | None = None) -> Iterator[starling.Memory]:
    """一条短命连接:打开、交给块、关闭。`endpoints` 给了才配抽取与嵌入端点(只有 remember_event 要),
    其余调用不读 OPENAI_API_KEY。块里抛的一切 Exception 按第 10 节的表改抛;块已经失败时,关闭的
    失败被吞掉,让块的异常说话。`Memory.open` 对不存在的路径会建新库,要不要建由调用方先查。

    模块级而不是 store 的方法:`library_digest` 与 `empty_digest` 不需要端点,解析器手里也没有。"""
    with _translated():
        memory = starling.Memory.open(
            str(path), agent=agent,
            llm=endpoints.make_llm() if endpoints is not None else None,
            embedder=endpoints.make_embedder() if endpoints is not None else None)
    try:
        yield memory
    except BaseException as exc:
        with contextlib.suppress(Exception):
            memory.close(timeout=close_timeout_s)
        if isinstance(exc, Exception):
            raise translate_failure(exc) from None
        raise
    with _translated():
        memory.close(timeout=close_timeout_s)


def library_digest(path: Path, *, close_timeout_s: float = 60.0) -> str:
    """只读地算一个库文件的摘要(解析器与种子都用)。不在原地打开:`Memory.open` 会跑迁移、写 WAL,
    而种子库只读(5.3 节)、起点副本是提交点,算摘要不能改它们的字节。所以连同 -wal 拷进临时目录,
    在副本上算。摘要与打开时用的 agent 名无关。

    **`path` 必须静止:调用期间没有任何连接在写它。** 主文件与 -wal 分两次拷,在写的库可能拷出
    不对应任何提交点的文件对。端口开着但没在写时,写入还在 -wal 里,连 -wal 一起拷才对;在写时
    不支持。今天的调用方都满足:`backup_to` 末尾算的是刚写完的副本,解析器与种子算的是只读的种子库。
    文件不存在、0 字节、文件头不对,都在拷贝之前抛非瞬态。悬空的符号链接算"不存在"。"""
    path = Path(path)
    if not path.exists():
        raise MemoryFailureError("digest_of: no such library", transient=False)
    _require_library_file(path, "digest_of")
    with _translated(), tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "library.db"
        copy_library(path, copy)
        with _short_connection(copy, agent="digest", close_timeout_s=close_timeout_s) as memory:
            return memory.digest()


@functools.cache
def empty_digest() -> str:
    """空库的摘要,在临时目录里开一个空库算(进程内只算一次)。摘要的格式是 Starling 的,不在 rise 里写死
    它的值。库不存在的人格(`StarlingDeploymentStore.digest`)与"从空库起"的种子解析(`factory.memory_runtime`)
    共用这一份。失败不进缓存:下一次调用重新算。"""
    with _translated(), tempfile.TemporaryDirectory() as tmp:
        with _short_connection(Path(tmp) / "empty.db", agent="digest", close_timeout_s=60.0) as memory:
            return memory.digest()


def starling_version() -> str:
    """已安装 Starling 的 `__version__`。它区分不了同版本号的两个提交,所以记忆运行时身份里
    另有安装来源(`factory.memory_config.starling_install_source`)。"""
    return starling.__version__


def extraction_prompts_digest() -> str:
    """Starling 抽取配置(三段提示词、两个阈值与其余开关)规范化 JSON 的 sha256。进记忆运行时身份:
    提示词或阈值随 Starling 升级变化,抽取出的记忆就变,身份跟着变,续训判 T3。

    **前提:摘的是 `starling.ExtractionConfig()` 的默认值,必须与运行时实际用的一致。** 现在一致,
    是因为本模块从不给 `Memory.open` 传 `extraction=`,而门面在 `extraction=None` 时用 `ExtractionConfig()`
    (Starling `_memory_core.py` 的 `self._extraction = extraction or ExtractionConfig()`)。
    将来若开始传自定义的 extraction,这里要一起改成摘那一份,否则身份描述的不是实际在用的配置。
    `tests/test_actio_memory_starling.py` 里有一条用例钉住"不传 extraction"。"""
    config = json.dumps(asdict(starling.ExtractionConfig()), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
    return hashlib.sha256(config.encode("utf-8")).hexdigest()


class StarlingDeploymentStore:
    """部署记忆的管理面,满足 `DeploymentMemoryStore`:每个人格一个 Starling 库 `<root>/<persona_id>.db`
    (组合根传入 `<记忆根>/deploy`,设计源 5.3 节)。

    **调用方必须持该人格的人格锁。** 门面只在单个 `Memory` 内计在途调用,不串行化同一库上的多条连接;
    管理面每次调用另开一条短命连接,与端口、与别的管理面调用并发时没有任何保护。rise 今天每个调用点都在
    人格锁里(`mindagent.rounds._PersonaLocks`、`mindagent.admit` 的 `ExitStack`),所以本类不加自己的锁。

    端口数只在实例内计(与 `FileTupleStore` 一样),所以**组合根只造一个 store,这个假设在这里承重**:
    另一个 store 实例或另一个进程开着的连接,`restore_from` 看不见,会把它库旁的 -wal、-shm 删掉,
    它之后的提交落进已经被替换掉的旧文件(unlink 的 inode),丢失,不报错。

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
        """一条短命连接,见 `_short_connection`。`extract` 为真才配本 store 的端点。"""
        with _short_connection(path, agent=agent, close_timeout_s=self._close_timeout_s,
                               endpoints=self._endpoints if extract else None) as memory:
            yield memory

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
            return empty_digest()
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
        坏备份(不存在、0 字节、文件头不对、打不开)在碰库与根目录之前就被拒,库原样不动。

        不在原地打开 path:`Memory.open` 会跑迁移、写 WAL,而起点副本是提交点,字节不能变。
        所以拷到临时目录,在副本上开,`backup_to` 成库旁的 `<库>.restore-partial`(自洽的单文件),
        先删掉库旁的 -wal、-shm,再 `os.replace` 到库路径。

        **必须先删日志、后换文件。** `backup_to` 用 `VACUUM INTO`,产出的是回滚日志模式的单文件,header
        里不记 WAL 的 salt,SQLite 分不出库旁的 -wal 是不是它的。先换后删时,在两步之间崩溃会把旧库的
        -wal 留在新库旁,之后任何一次 `Memory.open` 都把它重放进新库,摘要回到还原前的值,回滚静默
        失效,close 时还会把重放的内容检查点写死进主文件。先删后换则两个崩溃点都安全:换之前崩溃,
        旧库仍是完整可开的;换之后崩溃,库旁已经没有日志。

        "换之前崩溃,旧库完整"有一个前提:这里删掉的 -wal 里没有只存在于 WAL 的已提交写入。上一次正常
        关闭时它成立(close 合并 WAL 并删掉日志)。上一次若被 SIGKILL,库旁可能留着含已提交、未合并帧
        的 -wal,这里会把它删掉。之所以仍然安全,是调用方在任何打开之前都先重试 `restore_from`:
        `mindagent.rounds._close_round` 先回滚再写终态,`mindagent.deployment` 重入时"副本在就先覆盖
        再 open",都在人格锁里。所以不会有别的路径在重试之前先打开这个库,把未合并的帧读成数据。"""
        library = self._path(persona_id)
        if self._open.get(persona_id, 0) > 0:
            raise MemoryFailureError("restore_from: persona has an open port", transient=False)
        path = Path(path)
        if not path.exists():
            raise MemoryFailureError("restore_from: no such backup", transient=False)
        _require_library_file(path, "restore_from")
        with _translated(), tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "source.db"
            copy_library(path, copy)
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
        """只读地算一个库文件的摘要,约束见 `library_digest`;连接按本 store 的关闭期限关。"""
        return library_digest(path, close_timeout_s=self._close_timeout_s)
