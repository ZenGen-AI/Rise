"""Starling 记忆门面到记忆端口的适配器(场景二记忆设计二 4.1、4.4、10 节)。

全 src/ 只有本文件导入 starling。适配器做四件事:把门面的命中渲染成 `MemoryHit`(命中 id 是内容 id,
库间稳定);把 `remember_event` 的结果换成 `RememberOutcome`;把 Starling 抛的一切异常按第 10 节的
表映射成 `MemoryFailureError` 及其子类;`embed_pending` 按事件时钟给出嵌入时刻。

异常信息只放异常类名:原异常的文字可能含端点地址,所以一律 `from None` 改抛。

`StarlingDeploymentStore` 是同一个后端的部署管理面(满足 `DeploymentMemoryStore`):每个世界一个库
`<root>/<world_id>.db`,世界里的各主体以 `agent=<主体>` 打开端口,管理面的调用各开一条短命连接。"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import starling

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (
    PERSONA_ID_PATTERN,
    MemoryClosedError,
    MemoryEvent,
    MemoryEventConflict,
    MemoryFailureError,
    MemoryHit,
    PendingKey,
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
# 世界级操作(pending_events、digest、备份与还原)不属于任何主体,短连接用这个 agent 名开。
# 这些操作的结果与 agent 无关(`tests/test_simple_actio_starling_store.py` 里有一条用例钉住摘要这一点)。
# abandon_event 不用它:Starling 第 2 期起门面只放弃本主体名下的事件,要以 agent=<主体> 开。
_WORLD_AGENT = "world"
# 写入结果里算"写成了"的两种 outcome(40 篇 AC-32):其余(no_store、rejected、source_missing、空串等)
# 都没按事件写下语句,当作成功会让门② 与对话事件静默丢记忆。
_STORED_OUTCOMES = ("accepted", "idempotent")


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
    """`remember_event` 的返回译成 `RememberOutcome`,端口与管理面共用,所以写入判据两处一起严。

    抽取失败先判:cff319d 上抽取失败时 outcome 仍是 accepted,只看 extraction_failed。否则 outcome 不是
    accepted 或 idempotent 就是永久失败(AC-32),消息只带 outcome。"""
    if res.extraction_failed:
        return RememberOutcome((), True, res.failure_category or "unknown")
    if res.outcome not in _STORED_OUTCOMES:
        raise MemoryFailureError(f"remember: outcome {res.outcome}", transient=False)
    content_ids = memory.content_ids(list(res.statement_ids))
    if _MISSING_CONTENT_ID in content_ids.values():
        # 门面对本库不认得的语句 id 返回 "missing"。刚写完的语句不该认不得;放行的话
        # 假 id mem-missing 会进 stored、准入载荷与轨迹,比一个报错难查得多。
        raise MemoryFailureError("remember: statement not found in library", transient=False)
    return RememberOutcome(tuple(_hit_id(content_ids[s]) for s in res.statement_ids), False, None)


def _embed(memory) -> int:
    """嵌入所有还没有向量的语句,时刻取事件时钟,端口与管理面共用。返回嵌入条数。
    Starling 只计数不抛;有失败就抛 transient 的 MemoryFailureError,失败的语句召回会少一条,不能静默。"""
    now = (_EPOCH_AT + timedelta(seconds=memory.next_seq())).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = memory.embed_pending(now)
    if stats.failed > 0:
        raise MemoryFailureError(f"embed_pending: {stats.failed} statement(s) failed to embed", transient=True)
    return stats.embedded


def _render(row) -> str:
    """设计源 4.4 节。落库后的 modality 与 polarity 是小写(抽取 JSON 里的大写在落库时转了小写);
    括号与逗号是全角。`StatementRow` 没有 nesting_depth,不渲染嵌套层次。"""
    negation = "不" if row.polarity == "neg" else ""
    return (f"{row.subject_id} {negation}{row.predicate} {row.object_value}"
            f"（{row.modality}，置信度 {row.confidence:.2f}）")


def _check_name(value: object, what: str) -> str:
    """世界 id 与主体都拼进文件名或 Starling 的 agent 名,过同一份白名单。用 fullmatch:
    模式以 $ 结尾,match 会放过 "p1\\n"。'/' 与开头的 '.' 都不许。"""
    if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
        raise SimpleActioDataError(f"{what}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             f"starting with a letter or digit, got {value!r}")
    return value


class StarlingMemory:
    """一个主体(holder)在一个 Starling 世界库里的记忆端口,满足 `MemoryBackend`。holder 作 Starling 的
    `agent=`:同一个库里各主体的语句各记在自己名下,召回只看本主体的。

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
    def open(cls, path: Path, *, holder: str, endpoints: StarlingEndpoints,
             close_timeout_s: float = 60.0, on_close: Callable[[], None] | None = None) -> StarlingMemory:
        try:
            memory = starling.Memory.open(
                str(path), agent=holder, llm=endpoints.make_llm(), embedder=endpoints.make_embedder())
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
            raise SimpleActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
        if limit == 0:
            return []
        try:
            # 两个开关显式传,不靠门面的默认值(40 篇"召回显式传参",接服务后客户端照样传):
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
            return _embed(memory)
        except Exception as exc:
            raise translate_failure(exc) from None

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
            # 训练路 rollout 世界的收尾靠这一点决定删不删世界。
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
    0 字节的"备份"(磁盘满、写副本时被 SIGKILL、手工 touch)会让 `restore_from` 把世界库清成空库,
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

    公开名:`library_digest`、`restore_from` 用它,训练路的 rollout 世界(`factory.memory_runtime.StarlingTrainingWorlds`)
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
    它的值。库不存在的世界(`StarlingDeploymentStore.digest`)与"从空库起"的种子解析(`factory.memory_runtime`)
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
    `tests/test_simple_actio_memory_starling.py` 里有一条用例钉住"不传 extraction"。"""
    config = json.dumps(asdict(starling.ExtractionConfig()), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
    return hashlib.sha256(config.encode("utf-8")).hexdigest()


class StarlingDeploymentStore:
    """部署记忆的管理面,满足 `DeploymentMemoryStore`:每个世界一个 Starling 库 `<root>/<world_id>.db`
    (组合根传入 `<记忆根>/deploy`,设计三 4.1、6.2 节)。世界里的各主体共用这个库,端口以 `agent=<主体>` 打开,
    `remember_event` 与 `abandon_event` 也以 `agent=<holder>` 开短连接,所以语句与放弃都落在 holder 名下;
    pending_events、digest、备份与还原是世界级的,不分主体。

    **调用方必须持该世界的世界锁(设计三 4.2 节)。** 门面只在单个 `Memory` 内计在途调用,不串行化同一库上的
    多条连接;管理面每次调用另开一条短命连接。同一个库上的并发写由 SQLite 串行化(WAL 加 5 秒 busy_timeout),
    所以本类不在每次写外面加锁;"先读后判断再写"这类跨调用的顺序没有保护,靠调用方的世界锁。
    本类只有一把锁,守两样东西:每个世界打开着的端口数 `_open`(端口会在服务端线程关,驱动器线程在另一条
    线程上 `wait_closed` 或 `restore_from`),以及 `restore_from` 的"查端口数、换文件"。

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
        self._lock = threading.Lock()                         # 守 _open 与 restore_from 的"查数、换文件"
        self._all_closed = threading.Condition(self._lock)    # 某个世界的端口数归零时 notify_all
        self._open: dict[str, int] = {}                       # 每个世界打开着的端口数(不分主体),读写都要持 _lock

    # ---- 路径与连接 ----

    def _path(self, world_id: str) -> Path:
        _check_name(world_id, "world_id")
        return self._root / f"{world_id}.db"

    def _ensure_root(self) -> None:
        with _translated():
            self._root.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _short(self, path: Path, *, agent: str, extract: bool = False) -> Iterator[starling.Memory]:
        """一条短命连接,见 `_short_connection`。`extract` 为真才配本 store 的端点。"""
        with _short_connection(path, agent=agent, close_timeout_s=self._close_timeout_s,
                               endpoints=self._endpoints if extract else None) as memory:
            yield memory

    # ---- 端口计数 ----

    def _port_reserved(self, world_id: str) -> None:
        with self._lock:
            self._open[world_id] = self._open.get(world_id, 0) + 1

    def _release_once(self, world_id: str) -> Callable[[], None]:
        """给一个端口造它的 on_close:计数减一,归零时唤醒 `wait_closed`。只做字典减一与通知,不做任何可能抛的事
        (它抛了,计数减一就丢了)。

        **同一个端口只减一次。** 两条线程同时关同一个端口时,`StarlingMemory.close` 的"已关"判断没有锁,on_close
        会被调两次;多减的那次会把同世界别的端口的计数也扣掉,`restore_from` 就在有端口开着时放行。所以
        "是否已减过"的判断与减一在同一把锁里。"""
        done = False

        def release() -> None:
            nonlocal done
            with self._lock:
                if done:
                    return
                done = True
                left = self._open.get(world_id, 0) - 1
                if left > 0:
                    self._open[world_id] = left
                else:
                    self._open.pop(world_id, None)
                    self._all_closed.notify_all()

        return release

    def _refuse_if_open(self, world_id: str, *, leftover: Path | None = None) -> None:
        """该世界有任何主体的端口开着就拒绝(非瞬态)。`leftover` 是拒绝前要删掉的半成品。**调用时必须持 `_lock`**:
        查数要和调用方接着做的事(拒绝,或删日志、换文件)在同一次持锁里。"""
        if self._open.get(world_id, 0) > 0:
            if leftover is not None:
                leftover.unlink(missing_ok=True)
            raise MemoryFailureError("restore_from: world has an open port", transient=False)

    # ---- DeploymentMemoryStore ----

    def open(self, world_id: str, holder: str) -> StarlingMemory:
        path = self._path(world_id)
        _check_name(holder, "holder")
        self._ensure_root()
        release = self._release_once(world_id)
        # 先计数再开库:开库的这段时间里,并发的 restore_from 就已经看到这个世界有端口,不会把正在被打开的
        # 文件换掉。开库失败(坏库、打不开)时把这一次计数退掉,失败的端口不算开着。
        self._port_reserved(world_id)
        try:
            return StarlingMemory.open(
                path, holder=holder, endpoints=self._endpoints, close_timeout_s=self._close_timeout_s,
                on_close=release)
        except BaseException:
            release()
            raise

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """写完在同一条连接上 embed_pending,与端口每轮"写后嵌入"同口径。不嵌的话门② 收录的语句没有向量,
        召回看不到,要等下一个部署轮次首轮的 embed_pending 顺带补上(真机 2026-11-01)。

        连接以 `agent=holder` 开,语句记在 holder 名下。嵌入失败抛 transient 的 MemoryFailureError;此时语句与
        事件已提交,重入同 key 返回同一批语句 id(不二次抽取),这次的 embed_pending 再补。抽取失败不嵌:没有新语句。"""
        path = self._path(world_id)
        _check_name(holder, "holder")
        self._ensure_root()
        with self._short(path, agent=holder, extract=True) as memory:
            res = memory.remember_event(key, text, interlocutor=interlocutor, epoch=EPOCH)
            outcome = _outcome(memory, res)
            if not outcome.failed:
                _embed(memory)
            return outcome

    def abandon_event(self, world_id: str, holder: str, key: str) -> None:
        """以 `agent=holder` 开短连接放弃。门面只放弃本主体名下的行,别的主体名下的 key 抛 `KeyError`
        (`S:python/starling/memory.py:472-497`),照旧译成永久失败。"""
        path = self._path(world_id)
        _check_name(holder, "holder")
        if not path.exists():
            raise translate_failure(KeyError(key)) from None            # 没有库就没有这个 key
        with self._short(path, agent=holder) as memory:
            memory.abandon_event(key)

    def pending_events(self, world_id: str) -> list[PendingKey]:
        """全部主体的 pending 行,按 seq 升序(门面的顺序)。主体为空串的行只出自 0039 之前的旧库,
        rise 的世界里没有,见到就判永久失败(AC-30),不从 key 里解析主体。"""
        path = self._path(world_id)
        if not path.exists():
            return []
        with self._short(path, agent=_WORLD_AGENT) as memory:
            pending = memory.pending_events()
        out = []
        for p in pending:
            if p.holder == "":
                raise MemoryFailureError(f"pending event {p.key!r} has no holder", transient=False)
            out.append(PendingKey(p.key, p.holder))
        return out

    def digest(self, world_id: str) -> str:
        path = self._path(world_id)
        if not path.exists():
            return empty_digest()
        with self._short(path, agent=_WORLD_AGENT) as memory:
            return memory.digest()

    def backup_to(self, world_id: str, path: Path) -> str:
        """备份整个世界到 path 并返回副本的摘要。世界还没有库时先建空库,所以起点副本总是存在。
        path 已存在就覆盖(起点副本由 MindAgent 管,覆盖是它要的语义;Starling 拒绝已存在的目标,
        所以先删),旁边残留的 -wal、-shm 一并删:它们若属于被覆盖的旧文件,会被新文件当成自己的。"""
        library = self._path(world_id)
        self._ensure_root()
        path = Path(path)
        with self._short(library, agent=_WORLD_AGENT) as memory:
            path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (path, *(_sidecar(path, s) for s in _WAL_SUFFIXES)):
                stale.unlink(missing_ok=True)
            memory.backup_to(path)
        return self.digest_of(path)

    def restore_from(self, world_id: str, path: Path) -> str:
        """用 path 的内容覆盖该世界的库(覆盖全部主体),返回恢复后的逻辑摘要。该世界还有任何主体的端口开着时拒绝,
        别的世界开着端口不挡;坏备份(不存在、0 字节、文件头不对、打不开)在碰库与根目录之前就被拒,库原样不动。

        端口数查两次:进来先查一次,有端口就在碰任何东西之前拒绝;拷贝与生成 `.restore-partial` 不持锁(它们要开
        Starling 连接,关连接最长等 `close_timeout_s`,持锁会把别的世界的开关端口一起拖住);删日志与换文件
        前在锁里再查一次,查与换之间不会再有端口插进来(`open` 先计数再开库,计数拿同一把锁)。准备期间有端口
        开了,就删掉 `.restore-partial` 后拒绝,库原样不动。

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
        再 open",都在调用方持的锁里。所以不会有别的路径在重试之前先打开这个库,把未合并的帧读成数据。"""
        library = self._path(world_id)
        path = Path(path)
        with self._lock:
            self._refuse_if_open(world_id)
        if not path.exists():
            raise MemoryFailureError("restore_from: no such backup", transient=False)
        _require_library_file(path, "restore_from")
        with _translated(), tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "source.db"
            copy_library(path, copy)
            partial = _sidecar(library, ".restore-partial")
            with self._short(copy, agent=_WORLD_AGENT) as memory:
                digest = memory.digest()             # 坏备份在这里抛:根目录与库都还没动
                self._ensure_root()
                partial.unlink(missing_ok=True)      # Starling 的 backup_to 遇到已存在的目标抛 FileExistsError
                memory.backup_to(partial)
            with self._lock:
                self._refuse_if_open(world_id, leftover=partial)
                for suffix in _WAL_SUFFIXES:
                    _sidecar(library, suffix).unlink(missing_ok=True)
                os.replace(partial, library)
        return digest

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 秒返回 False。端口可以在别的线程上关。
        不碰磁盘:没有库的世界端口数也是 0,立刻返回 True。"""
        self._path(world_id)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s >= 0:
            raise SimpleActioDataError(f"wait_closed.timeout_s: expected a non-negative number, got {timeout_s!r}")
        with self._all_closed:
            return self._all_closed.wait_for(lambda: self._open.get(world_id, 0) == 0, timeout_s)

    # ---- 摘要 ----

    def digest_of(self, path: Path) -> str:
        """只读地算一个库文件的摘要,约束见 `library_digest`;连接按本 store 的关闭期限关。"""
        return library_digest(path, close_timeout_s=self._close_timeout_s)
