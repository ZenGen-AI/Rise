"""Starling 记忆服务到记忆端口的适配器(场景二记忆设计二 4.1、4.4、10 节;设计四 40 篇第 8 节)。

全 src/ 只有本文件与 `factory/starling_session.py` 导入 `starling_client`,没有文件导入 `starling`。适配器做四件事:
把服务的命中渲染成 `MemoryHit`(命中 id 是内容 id,世界间稳定);把 `remember_event` 的结果换成 `RememberOutcome`;
把客户端异常按码表映射成 `MemoryFailureError` 及其子类;`embed_pending` 按事件时钟给出嵌入时刻。

异常信息只放码与异常类名:服务端的 detail 可能含端点地址,所以一律 `from None` 改抛。

`StarlingDeploymentStore` 是同一个后端的部署管理面(满足 `DeploymentMemoryStore`):每个情境一个部署世界
`<前缀>.deploy.<scenario_id>`,世界里的各主体各开一个句柄;快照是服务的 held 快照,验证重放从快照分叉可回收的
重放世界。rise 不打开任何库文件。世界名与标签的规则在组合根(`factory.starling_names`),由组合根经 `names` 传进来:
本包不导入 `factory`。"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from starling_client import errors as _client_errors

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
    SnapshotRef,
)

# 事件时钟的起点(设计源 4.3 节)。每次 `remember_event` 都显式传它,不靠门面的默认值恰好同值:
# 门面哪天改了默认值,事件时刻和摘要会静默漂移。
# 嵌入时刻是 EPOCH 加 next_seq 秒,所以恒不早于任何已写事件的 created_at。
EPOCH = "2026-01-01T00:00:00Z"
_EPOCH_AT = datetime.fromisoformat(EPOCH)
_ID_HEX_CHARS = 16
# 服务的 `content_ids` 对本世界没有的语句 id 给的占位值。
_MISSING_CONTENT_ID = "missing"
# 写入结果里算"写成了"的两种 outcome(40 篇 AC-32):其余(no_store、rejected、source_missing、空串等)
# 都没按事件写下语句,当作成功会让门② 与对话事件静默丢记忆。
_STORED_OUTCOMES = ("accepted", "idempotent")


@dataclass(frozen=True)
class StarlingEndpoints:
    """抽取与嵌入两个 OpenAI 兼容端点的配置,只装端点字段(40 篇模块表)。密钥不在这里:服务进程从它自己的
    OPENAI_API_KEY 读。组合根把它们写进 rise 设置(`factory.starling_names.rise_settings`)。

    max_retries 对两个端点都生效,0 表示一次都不重试。Starling 的 C++ 默认是 3 次,
    一次失败的调用会拖到约 21 秒,打破 6.4 节"每次网络调用至多 4 秒"。"""

    extraction_base_url: str
    extraction_model: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    timeout_ms: int
    max_retries: int


def translate_client_failure(exc: BaseException, *, port_closed_by_rise: bool,
                             in_drain_window: bool) -> MemoryFailureError:
    """客户端 `starling_client` 异常的映射(40 篇第 8 节的码表、AC-33)。返回新异常,调用方 `raise ... from None`。

    消息一律 `starling <code> (<类名>)`,不带服务端的 detail:它可能含端点地址。瞬态按 Starling 给的 `transient`,
    只有三处由 rise 改判:
    - `handle_closed`:rise 自己关过的端口是 `MemoryClosedError`(永久);rise 没关的端口收到它,是外部服务重启,判瞬态;
    - `session_expired`:判瞬态,下一次开句柄换新会话;
    - `world_held`:排空窗口内判瞬态(服务端在等过期会话的在途调用结束),其余永久。
    `Unauthorized`、`ProtocolMismatch` 走"其余"(非瞬态);它们在装配期与重连时由会话模块先接住。
    `TimeoutError`(`RemoteMemory.close` 超时)与其他异常都是永久失败,只写类名。

    返回的异常带 `code` 属性(客户端异常的码):管理面据此认出 `world_exists`、`world_not_found`、`snapshot_not_found`,
    不解析消息文字。"""
    if isinstance(exc, MemoryFailureError):
        return exc
    out = _map_client(exc, port_closed_by_rise=port_closed_by_rise, in_drain_window=in_drain_window)
    out.code = getattr(exc, "code", None) or None
    return out


def _map_client(exc: BaseException, *, port_closed_by_rise: bool, in_drain_window: bool) -> MemoryFailureError:
    errors = _client_errors
    name = type(exc).__name__
    if not isinstance(exc, errors.StarlingClientError):
        return MemoryFailureError(f"starling client failed: {name}", transient=False)
    message = f"starling {exc.code} ({name})"
    if isinstance(exc, errors.EventConflict):
        return MemoryEventConflict(message)
    if isinstance(exc, errors.MemoryClosed):
        if port_closed_by_rise:
            return MemoryClosedError(message)
        return MemoryFailureError(message, transient=True)
    if isinstance(exc, errors.SessionExpired):
        return MemoryFailureError(message, transient=True)
    if exc.code == "world_held":
        return MemoryFailureError(message, transient=in_drain_window)
    return MemoryFailureError(message, transient=bool(exc.transient))


def _hit_id(content_id: str) -> str:
    return "mem-" + content_id[:_ID_HEX_CHARS]


def _outcome(memory, res) -> RememberOutcome:
    """`remember_event` 的返回译成 `RememberOutcome`,端口与管理面共用,所以写入判据两处一起严。

    抽取失败先判:cff319d 上抽取失败时 outcome 仍是 accepted,只看 extraction_failed。否则 outcome 不是
    accepted 或 idempotent 就是永久失败(AC-32),消息只带 outcome。`memory` 是客户端的 `RemoteMemory`。"""
    if res.extraction_failed:
        return RememberOutcome((), True, res.failure_category or "unknown")
    if res.outcome not in _STORED_OUTCOMES:
        raise MemoryFailureError(f"remember: outcome {res.outcome}", transient=False)
    content_ids = memory.content_ids(list(res.statement_ids))
    if _MISSING_CONTENT_ID in content_ids.values():
        # 服务对本世界不认得的语句 id 返回 "missing"。刚写完的语句不该认不得;放行的话
        # 假 id mem-missing 会进 stored、准入载荷与轨迹,比一个报错难查得多。
        raise MemoryFailureError("remember: statement not found in library", transient=False)
    return RememberOutcome(tuple(_hit_id(content_ids[s]) for s in res.statement_ids), False, None)


def _embed(memory) -> int:
    """嵌入所有还没有向量的语句,时刻取事件时钟。返回嵌入条数。
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
    """世界 id、主体、快照 id 与重放世界名都拼进世界名、标签或请求,过同一份白名单。用 fullmatch:
    模式以 $ 结尾,match 会放过 "p1\\n"。'/' 与开头的 '.' 都不许。"""
    if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
        raise SimpleActioDataError(f"{what}: expected 1 to 128 characters of [A-Za-z0-9._-] "
                             f"starting with a letter or digit, got {value!r}")
    return value


class SessionView(Protocol):
    """端口对服务会话的最小依赖,按结构声明(`simple_actio` 不导入 `factory`)。组合根的
    `factory.starling_session.ServiceSession` 满足它。"""

    def owns_generation(self, generation: int) -> bool: ...

    def mark_lost(self, generation: int, *, writable_worlds: Iterable[str]) -> None: ...


# 收到这几类异常说明这一代会话已经不能用了:SessionExpired(租约过期)、rise 没关的端口收到 handle_closed(外部服务
# 重启后不认旧句柄),以及 ServiceUnavailable(服务进程不在了)。最后一种也报会话丢失:否则拉起模式下服务退出后
# 没有人触发重新拉起(会话只在代号丢失后的下一次调用里 restart),每次调用都只会再得到一次 ServiceUnavailable。
_LOST = (_client_errors.SessionExpired, _client_errors.ServiceUnavailable)


class StarlingMemory:
    """一个主体(holder)在一个 Starling 世界里的记忆端口,满足 `MemoryBackend`。包客户端的 `RemoteMemory`
    (一个世界上以一个主体开的句柄):召回只看本主体名下的语句,写入记在本主体名下。

    `generation` 是句柄所属会话的代号,`session` 是那个会话(`SessionView`)。`release` 在端口关闭或"当作已关"时
    调一次,给会话与管理面减端口计数用;**它必须不抛**,抛了照样算关上,异常改抛成非瞬态。
    `read_only` 只给管理面的 `open_replay` 用:`remember` 抛永久 MemoryFailureError("world_read_only"),
    `embed_pending` 返回 0 且不发请求。

    调用收到 SessionExpired、ServiceUnavailable,或 rise 没关的端口收到 handle_closed:先报这一代会话丢失
    (可写端口报出自己的世界,排空窗口按它算),再抛瞬态的 MemoryFailureError。"""

    def __init__(self, remote, *, generation: int, session: SessionView, release: Callable[[], None],
                 read_only: bool = False, close_timeout_s: float = 60.0) -> None:
        self._remote = remote
        self._generation = generation
        self._session = session
        self._release = release
        self._read_only = read_only
        self._close_timeout_s = close_timeout_s
        self._closing = False       # rise 已开始关它:之后收到的 handle_closed 是 rise 关的
        self._closed = False

    def _live(self):
        if self._closed:
            raise MemoryClosedError("memory port is closed")
        return self._remote

    def _lost(self) -> None:
        self._session.mark_lost(self._generation, writable_worlds=[] if self._read_only else [self._remote.world])

    def _failure(self, exc: Exception) -> MemoryFailureError:
        """客户端异常译成 MemoryFailureError;会话已不能用时先报丢失。"""
        by_rise = self._closing or self._closed
        if isinstance(exc, _LOST) or (isinstance(exc, _client_errors.MemoryClosed) and not by_rise):
            self._lost()
        return translate_client_failure(exc, port_closed_by_rise=by_rise, in_drain_window=False)

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        remote = self._live()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise SimpleActioDataError(f"recall.limit: expected a non-negative int, got {limit!r}")
        if limit == 0:
            return []
        try:
            # 三个开关显式传,不靠服务端的默认值(40 篇"召回显式传参"):
            # strict:查询词嵌不出就抛 RecallDegradedError,不退回只有词面的结果。
            # include_volatile:刚嵌入、还没被 tick 晋级的语句也要能召回(实例每轮 remember 后 embed_pending)。
            found = remote.recall(query, k=limit, mode="semantic", strict=True, include_volatile=True)
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
            raise self._failure(exc) from None

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        remote = self._live()
        if self._read_only:
            raise MemoryFailureError("world_read_only", transient=False)
        try:
            # "[key] " 前缀由 Starling 加,这里只传原文。
            res = remote.remember_event(event.key, event.text, interlocutor=event.interlocutor, epoch=EPOCH)
            return _outcome(remote, res)
        except Exception as exc:
            raise self._failure(exc) from None

    def embed_pending(self) -> int:
        remote = self._live()
        if self._read_only:
            return 0          # 重放世界只读:不写向量
        try:
            return _embed(remote)
        except Exception as exc:
            raise self._failure(exc) from None

    def digest(self) -> str:
        remote = self._live()
        try:
            return remote.digest()
        except Exception as exc:
            raise self._failure(exc) from None

    def _run(self, call: Callable[[object], object]) -> object:
        """管理面在端口上做端口协议之外的事(放弃事件):`call(remote)`,失败照端口的口径改抛。"""
        remote = self._live()
        try:
            return call(remote)
        except Exception as exc:
            raise self._failure(exc) from None

    def close(self) -> None:
        """关句柄,再调 `release`。三种情形不发请求或不看回答,直接当作已关:端口的代号已不是会话的当前代号
        (旧会话丢失后服务认不得它的句柄);关的时候收到 SessionExpired;收到 handle_closed。后两种同时报会话丢失。
        `TimeoutError`(在途调用在期限内没返回)抛非瞬态 MemoryFailureError,端口保持打开,返回后可以再关一次:
        训练路 rollout 世界的收尾靠这一点决定世界进不进保留表。"""
        if self._closed:
            return
        if self._session.owns_generation(self._generation):
            self._closing = True
            try:
                self._remote.close(timeout=self._close_timeout_s)
            except (_client_errors.SessionExpired, _client_errors.MemoryClosed):
                self._lost()
            except Exception as exc:
                self._closing = False
                raise translate_client_failure(exc, port_closed_by_rise=True, in_drain_window=False) from None
        self._closed = True       # 句柄确实不在了:先置位,release 抛了端口也不能再用
        try:
            self._release()
        except Exception as exc:
            raise translate_client_failure(exc, port_closed_by_rise=True, in_drain_window=False) from None


class WorldNames(Protocol):
    """世界名与标签的规则(00 篇世界表与世界名规则)。组合根传 `factory.starling_names` 模块本身,它满足这个协议;
    本包不导入 `factory`,规则也只有那一处。"""

    def deploy_world(self, prefix: str, scenario_id: str) -> str: ...

    def replay_world(self, prefix: str, round_id: str, scenario_id: str, suffix: str) -> str: ...

    def empty_seed_world(self, prefix: str) -> str: ...

    def new_suffix(self) -> str: ...

    def labels(self, prefix: str, root: str, kind: str, **extra: str) -> dict[str, str]: ...


class AdminSession(SessionView, Protocol):
    """管理面对服务会话的依赖:端口那两项,加开句柄与管理调用。`factory.starling_session.ServiceSession` 满足它。
    两者抛的都已是译好的 MemoryFailureError,带客户端异常的 `code`。"""

    def open_port(self, world: str, holder: str, *, read_only: bool, settings: dict,
                  wrap: Callable[[object, Callable[[], None], int], object]) -> object: ...

    def admin(self, op: str, body: dict) -> dict: ...


# list_worlds 每页的上限(S:python/starling/dashboard/routes/client.py 的 _ListWorldsArgs;C++ 判越界)
_LIST_PAGE = 500


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def list_all_worlds(session: AdminSession, labels: dict[str, str], *, with_digest: bool = False) -> list[dict]:
    """`list_worlds` 按标签查,翻完所有页。"""
    rows: list[dict] = []
    page = 1
    while True:
        out = session.admin("list_worlds", {"labels": labels, "page": page, "page_size": _LIST_PAGE,
                                            "with_digest": with_digest})
        batch = out.get("worlds") or []
        rows += batch
        if not batch or page * _LIST_PAGE >= int(out.get("total") or 0):
            return rows
        page += 1


def _find(session: AdminSession, *, name: str, kind: str, ids: dict[str, str], prefix: str) -> dict | None:
    """`list_worlds` 按 `repo`、`kind` 与这类世界的 id 标签找名字等于 `name` 的项;没有返回 None。"""
    for item in list_all_worlds(session, {"repo": prefix, "kind": kind, **ids}):
        if item.get("name") == name:
            return item
    return None


def _verify(item: dict | None, *, name: str, kind: str, prefix: str, root_label: str, seed: bool) -> None:
    """00 篇 `ensure_world` 的 ②③:找不到、`seed` 或 `read_only` 与这类世界不符、`kind` 或 `repo` 不对,判永久
    "不是 rise 建的";`root` 标签不等于本记忆根的摘要,判永久"属于另一个记忆根"。设置是否一致由随后开句柄比较。"""
    tags = (item or {}).get("labels") or {}
    if (item is None or bool(item.get("seed")) != seed or bool(item.get("read_only")) != seed
            or tags.get("kind") != kind or tags.get("repo") != prefix):
        raise MemoryFailureError(f"world {name} was not created by rise", transient=False)
    if tags.get("root") != root_label:
        raise MemoryFailureError(f"world {name} belongs to another memory root", transient=False)


def _ensure(session: AdminSession, *, name: str, kind: str, ids: dict[str, str], prefix: str, root_label: str,
            labels: dict[str, str], settings: dict, seed: bool) -> None:
    """00 篇 `ensure_world` 的三步,部署世界与空种子共用:① `create_world`,带世界名、标签与 rise 设置;
    收到 `world_exists` 时 ②③ 核对已在的那个(`_verify`)。"""
    try:
        session.admin("create_world", {"name": name, "labels": labels, "settings": settings, "seed": seed})
        return
    except MemoryFailureError as exc:
        if _code(exc) != "world_exists":
            raise
    _verify(_find(session, name=name, kind=kind, ids=ids, prefix=prefix),
            name=name, kind=kind, prefix=prefix, root_label=root_label, seed=seed)


def world_digest(session: AdminSession, *, name: str, labels: dict[str, str]) -> str:
    """按标签查到名字等于 `name` 的世界,返回它的摘要(`list_worlds(with_digest=True)`)。找不到判永久。
    不开句柄:解析器在进策略上下文之前调它,那时不该占任何世界。"""
    for item in list_all_worlds(session, labels, with_digest=True):
        if item.get("name") == name and item.get("digest"):
            return item["digest"]
    raise MemoryFailureError(f"world {name} has no digest", transient=False)


def ensure_empty_seed(session: AdminSession, *, names: WorldNames, prefix: str, root_label: str,
                      settings: dict) -> tuple[str, str]:
    """建或核对空种子 `<前缀>.seed-empty`(装配期调,00 篇世界表),返回 `(世界名, 摘要)`。没有种子的情境从它分叉;
    部署世界不存在时的摘要也取它(两者相等已实测)。标签 `kind=seed-empty`,不带 `seed_id`。"""
    name = names.empty_seed_world(prefix)
    kind = "seed-empty"
    _ensure(session, name=name, kind=kind, ids={}, prefix=prefix, root_label=root_label,
            labels=names.labels(prefix, root_label, kind), settings=settings, seed=True)
    return name, world_digest(session, name=name, labels={"repo": prefix, "kind": kind})


class StarlingDeploymentStore:
    """部署记忆的管理面,满足 `DeploymentMemoryStore`,经服务会话做一切事(40 篇"接服务后管理面的签名"表)。

    - 部署世界 `<前缀>.deploy.<world_id>`。`open`、`remember_event`、`snapshot` 先 `ensure_world`(00 篇三步加 90 篇 FC-49
      的创建标记),同一进程内每个世界只做一次;`digest`、`pending_events` 不建世界,世界不在时分别给空种子的摘要与空列表;
      `abandon_event` 在世界不在时判永久失败。
    - 创建标记 `<marker_dir>/<world_id>.created`:rise 第一次建这个部署世界时写。标记在而世界不在,判永久"部署世界被删",
      不静默重建;要从空开始,人删标记。
    - held 快照按 id(`snapshot`、`restore`、`delete_snapshot`);删一个已删的快照服务回 `snapshot_not_found`,这里当作成功。
    - 重放世界:`fork_replay` 从快照分叉可回收世界,返回完整世界名;`open_replay` 原样开它,永远只读,不走 `ensure_world`;
      `release_replay` 只关不删(客户端没有删世界的接口,重放世界留给 Starling 第 6 期清理)。
    - `open` 只收部署世界 id,不看名字长什么样:名为 `<前缀>.verify.x` 的情境照旧是部署世界(AC-34)。

    端口计数按完整世界名,部署与重放两种端口共用一张表,只给 `wait_closed` 用。恢复时有没有句柄开着由服务判
    (`world_held`):旧代号会话的端口在服务那边已经不在了,本地计数挡不住也不该挡。

    **调用方必须持该世界的世界锁(设计三 4.2 节)。** 服务只挡两个会话同时写,同一会话里"先读后判断再写"的顺序靠世界锁。"""

    def __init__(self, session: AdminSession, *, names: WorldNames, prefix: str, root_label: str, settings: dict,
                 marker_dir: Path, empty_seed_digest: str, close_timeout_s: float = 60.0) -> None:
        # 构造不发请求、不碰磁盘:空种子由组合根先建好,把摘要传进来
        self._session = session
        self._names = names
        self.prefix = prefix
        self.root_label = root_label
        self._settings = settings
        self._marker_dir = Path(marker_dir)
        self.empty_seed_digest = empty_seed_digest
        self._close_timeout_s = close_timeout_s
        self._lock = threading.Lock()                         # 守 _open 与 _ensured
        self._all_closed = threading.Condition(self._lock)    # 某个世界的端口数归零时 notify_all
        self._open: dict[str, int] = {}                       # 每个完整世界名打开着的端口数(不分主体)
        self._ensured: set[str] = set()                       # 本进程已核对过的部署世界 id

    # ---- 名字与创建标记 ----

    def _deploy(self, world_id: str) -> str:
        return self._names.deploy_world(self.prefix, _check_name(world_id, "world_id"))

    def _marker(self, world_id: str) -> Path:
        return self._marker_dir / f"{world_id}.created"

    def _write_marker(self, world_id: str, name: str) -> None:
        """临时文件加改名:读者要么看不到,要么看到完整内容。内容是世界名,只给人看。"""
        path = self._marker(world_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            self._marker_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(name + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise MemoryFailureError(f"creation marker for {name}: {type(exc).__name__}", transient=False) from None

    @property
    def settings(self) -> dict:
        """rise 设置(建世界、分叉、开句柄都传它)。"""
        return self._settings

    def verify_world(self, world_id: str) -> str | None:
        """只读地核对部署世界,不建:世界在就核对 ②③ 并返回完整世界名;不在而创建标记在,判永久"部署世界被删";
        不在也没有标记,返回 None(第一次用时再建)。持仓库锁之后的装配核对用它(90 篇第 23 条)。"""
        name = self._deploy(world_id)
        item = _find(self._session, name=name, kind="deploy", ids={"scenario_id": world_id}, prefix=self.prefix)
        if item is None:
            if self._marker(world_id).exists():
                # 看板删世界的路由不经过 rise,静默重建会让部署记忆悄悄从空开始(FC-49)
                raise MemoryFailureError(
                    f"deploy world {name} was deleted; to start it from empty, delete deploy/{world_id}.created",
                    transient=False)
            return None
        _verify(item, name=name, kind="deploy", prefix=self.prefix, root_label=self.root_label, seed=False)
        return name

    def ensure_world(self, world_id: str) -> str:
        """部署世界的 `ensure_world`,返回完整世界名。有创建标记时世界必须还在(`verify_world`);没有标记时走三步,
        建成或核对过就写标记(标记丢了就补写)。同一进程内对每个世界只做一次。"""
        name = self._deploy(world_id)
        with self._lock:
            if world_id in self._ensured:
                return name
        if self._marker(world_id).exists():
            self.verify_world(world_id)
        else:
            _ensure(self._session, name=name, kind="deploy", ids={"scenario_id": world_id}, prefix=self.prefix,
                    root_label=self.root_label,
                    labels=self._names.labels(self.prefix, self.root_label, "deploy", scenario_id=world_id),
                    settings=self._settings, seed=False)
            self._write_marker(world_id, name)
        with self._lock:
            self._ensured.add(world_id)
        return name

    # ---- 端口与计数 ----

    def _release_once(self, key: str) -> Callable[[], None]:
        """给一个端口造它的 release:计数减一,归零时唤醒 `wait_closed`。同一个端口只减一次(两条线程同时关同一个端口时,
        多减的那次会把同世界别的端口的计数也扣掉)。只做字典减一与通知,不抛。"""
        done = False

        def release() -> None:
            nonlocal done
            with self._lock:
                if done:
                    return
                done = True
                left = self._open.get(key, 0) - 1
                if left > 0:
                    self._open[key] = left
                else:
                    self._open.pop(key, None)
                    self._all_closed.notify_all()

        return release

    def _open_port(self, name: str, holder: str, *, read_only: bool) -> StarlingMemory:
        """在世界 `name` 上开 holder 的句柄,造成端口。先计数再开:开的这段时间里 `wait_closed` 也等它;开不成就退掉计数。"""
        release = self._release_once(name)
        with self._lock:
            self._open[name] = self._open.get(name, 0) + 1
        session = self._session

        def wrap(remote, session_release, generation):
            def both() -> None:
                try:
                    session_release()
                finally:
                    release()

            return StarlingMemory(remote, generation=generation, session=session, release=both,
                                  read_only=read_only, close_timeout_s=self._close_timeout_s)

        try:
            return session.open_port(name, holder, read_only=read_only, settings=self._settings, wrap=wrap)
        except BaseException:
            release()
            raise

    @staticmethod
    def _close_after(port: StarlingMemory, failing: bool) -> None:
        """短用的端口用完就关;块已经失败时关闭的失败被吞掉,让块的异常说话。"""
        try:
            port.close()
        except MemoryFailureError:
            if not failing:
                raise

    # ---- DeploymentMemoryStore ----

    def open(self, world_id: str, holder: str) -> StarlingMemory:
        """只开部署世界:先 `ensure_world`,再开可写句柄。不看 `world_id` 长什么样(AC-34)。"""
        _check_name(holder, "holder")
        name = self.ensure_world(world_id)
        return self._open_port(name, holder, read_only=False)

    def open_replay(self, replay_world: str, holder: str) -> StarlingMemory:
        """在 `fork_replay` 返回的重放世界上开只读句柄,名字原样用,不走 `ensure_world`。世界不在时服务回
        `world_not_found`,判永久。"""
        _check_name(replay_world, "replay_world")
        _check_name(holder, "holder")
        return self._open_port(replay_world, holder, read_only=True)

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """在 holder 的可写句柄上写一件事,写成了就在同一个句柄上 embed_pending,再关(与端口每轮"写后嵌入"同口径;
        不嵌的话门② 收录的语句没有向量,召回看不到)。嵌入失败抛 transient 的 MemoryFailureError;此时语句与事件已提交,
        重入同 key 返回同一批语句 id(不二次抽取),这次的 embed_pending 再补。"""
        port = self.open(world_id, holder)
        failing = True
        try:
            outcome = port.remember(MemoryEvent(key, text, interlocutor))
            if not outcome.failed:
                port.embed_pending()
            failing = False
            return outcome
        finally:
            self._close_after(port, failing)

    def abandon_event(self, world_id: str, holder: str, key: str) -> None:
        """在 holder 的句柄上放弃:服务只放弃本主体名下的行,别的主体名下的 key 回 `event_not_found`(永久)。世界不在时
        开句柄就回 `world_not_found`(永久),不建世界。放弃一个已放弃的事件是空操作。"""
        _check_name(holder, "holder")
        port = self._open_port(self._deploy(world_id), holder, read_only=False)
        failing = True
        try:
            port._run(lambda remote: remote.abandon_event(key))
            failing = False
        finally:
            self._close_after(port, failing)

    def pending_events(self, world_id: str) -> list[PendingKey]:
        """全部主体的 pending 行,按 seq 升序。世界不在返回 `[]`,不建世界。主体为空串的行只出自 0039 之前的旧库,
        rise 的世界里没有,见到就判永久失败(AC-30),不从 key 里解析主体。"""
        name = self._deploy(world_id)
        try:
            events = self._session.admin("pending_events", {"world": name})["events"]
        except MemoryFailureError as exc:
            if _code(exc) == "world_not_found":
                return []
            raise
        out = []
        for event in sorted(events, key=lambda e: e["seq"]):
            if not event.get("holder"):
                raise MemoryFailureError(f"pending event {event['key']!r} has no holder", transient=False)
            out.append(PendingKey(event["key"], event["holder"]))
        return out

    def digest(self, world_id: str) -> str:
        """世界不在时给空种子的摘要,不建世界。"""
        name = self._deploy(world_id)
        try:
            return self._session.admin("digest", {"world": name})["digest"]
        except MemoryFailureError as exc:
            if _code(exc) == "world_not_found":
                return self.empty_seed_digest
            raise

    def snapshot(self, world_id: str, *, note: str) -> SnapshotRef:
        """先确保世界已建,再拍 held 快照;`note` 让人在看板的快照列表里认得出。"""
        if not isinstance(note, str):
            raise SimpleActioDataError(f"snapshot.note: expected a str, got {type(note).__name__}")
        name = self.ensure_world(world_id)
        out = self._session.admin("snapshot", {"world": name, "note": note})
        return SnapshotRef(out["snapshot_id"], out["digest"])

    def restore(self, world_id: str, snapshot_id: str) -> str:
        """服务恢复,返回恢复后的摘要。崩溃安全由服务负责(复制、校验、归档、一步改名)。有句柄开着回 `world_held`,
        快照不在回 `snapshot_not_found`,摘要不等回 `restore_digest_mismatch`,都是永久;`world_busy` 瞬态。"""
        name = self._deploy(world_id)
        out = self._session.admin("restore", {"world": name, "snapshot_id": _check_name(snapshot_id, "snapshot_id")})
        return out["digest"]

    def delete_snapshot(self, world_id: str, snapshot_id: str) -> None:
        """held 快照不参与修剪,由调用方显式删。快照已删时服务回 `snapshot_not_found`,这里当作成功(与替身的幂等一致)。"""
        name = self._deploy(world_id)
        try:
            self._session.admin("delete_snapshot", {"world": name, "snapshot_id": _check_name(snapshot_id, "snapshot_id")})
        except MemoryFailureError as exc:
            if _code(exc) != "snapshot_not_found":
                raise

    def fork_replay(self, world_id: str, snapshot_id: str, *, round_id: str) -> str:
        """从快照分叉一个可回收的重放世界,`retention_group` 是轮次 id;可回收世界从不跑后台任务。世界名
        `<前缀>.verify.<round_id>.<world_id>.<后缀>` 在调用之前定,返回完整世界名,调用方原样传给 `open_replay`。"""
        source = self._deploy(world_id)
        _check_name(snapshot_id, "snapshot_id")
        _check_name(round_id, "round_id")
        name = self._names.replay_world(self.prefix, round_id, world_id, self._names.new_suffix())
        self._session.admin("fork", {
            "source": source, "name": name, "snapshot_id": snapshot_id,
            "labels": self._names.labels(self.prefix, self.root_label, "verify", round_id=round_id,
                                         scenario_id=world_id),
            "retention_group": round_id, "reclaimable": True, "seed": False, "settings": self._settings,
        })
        return name

    def release_replay(self, replay_world_id: str) -> None:
        """关重放世界;世界已不在是空操作。客户端没有删世界的接口,重放世界留给 Starling 第 6 期清理。"""
        try:
            self._session.admin("close_world", {"world": _check_name(replay_world_id, "replay_world")})
        except MemoryFailureError as exc:
            if _code(exc) != "world_not_found":
                raise

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该部署世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 秒返回 False。端口可以在别的线程上关。
        不发请求:没有句柄的世界端口数是 0,立刻返回 True。重放端口记在重放世界名下,不算进来。"""
        key = self._deploy(world_id)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s >= 0:
            raise SimpleActioDataError(f"wait_closed.timeout_s: expected a non-negative number, got {timeout_s!r}")
        with self._all_closed:
            return self._all_closed.wait_for(lambda: self._open.get(key, 0) == 0, timeout_s)
