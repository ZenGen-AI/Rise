"""接记忆服务后的服务会话与运行时身份(设计四;90 篇运行时身份表、"服务会话 ServiceSession"一节、FC-42、FC-46、FC-47)。

一个进程只用一个服务会话(FC-46)。`ServiceSession` 持当前客户端与从 1 递增的代号;每个端口记下打开时的代号,
让那个代号的端口计数加一,关闭时减一。会话丢失(调用收到 SessionExpired,或 rise 没关的端口收到 handle_closed)
后,下一次需要服务时换新客户端、代号加一;旧客户端等它的端口计数归零才关,所以在途调用拿到的是 SessionExpired
(译成瞬态),不会撞上已关的传输。换会话后重新核对 `info()`,不符就进致命状态:之后一切记忆调用都抛瞬态
`MemoryFailureError`,在途的指令与轮次只延后,停守护进程由组合根做(90 篇"停守护进程分两半")。

管理调用走另一个不带会话的传输(请求体不带 `session_id`,服务端路由也不查会话),超时 300 秒,会话过期后照常可用。

第 3 步不接线:装配不导入本模块。`starling_client` 只由本模块与 `simple_actio/memory_starling.py` 导入
(`tests/test_factory_cross_plan.py` 守着),且一律在函数里惰性导入;`translate_client_failure` 也在用到时才从
`simple_actio.memory_starling` 取(第 3 步那个模块在模块层导入进程内的 `starling`):没装 extra `memory` 时本模块照样能导入。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from factory.errors import FactoryConfigError
from factory.memory_config import MemoryConfig
from factory.starling_names import canonical_digest, settings_digest
from factory.starling_service import ServiceRestartsExhausted
from simple_actio.memory_port import MemoryFailureError
from simple_actio.trace import TRACE_SCHEMA_V2

_log = logging.getLogger(__name__)
_ADMIN_PATH = "/api/v1/client/admin/"
_STOPPING = "starling session is stopping"

# 90 篇"客户端超时"表(FC-46):句柄调用用 connect 的缺省 30 秒;管理调用(建世界、分叉、快照、恢复)要拷库,给 300 秒
HANDLE_TIMEOUT_S = 30.0
ADMIN_TIMEOUT_S = 300.0
# 排空窗口:一个会话丢失后 60 秒内,在它丢失时有可写句柄的世界上开句柄收到 world_held 判瞬态
DRAIN_WINDOW_S = 60.0


def service_runtime(info: dict, settings: dict, config: MemoryConfig) -> dict:
    """接服务后的记忆运行时身份(90 篇运行时身份表):五个顶层键不变,所以替身的身份常量不用动。

    - `starling_version` 取服务 `info()` 的版本、提交与协议;安装来源不再进身份,只在装配期与服务报的提交比对。
    - `extraction.prompts` 改为 `info()["extraction_config"]` 规范化 JSON 的 sha256;另加 `world_settings`:rise 设置的摘要。
    - 两个端点地址用配置里已规范化的值;`config.root` 不进身份。
    每次返回一份新字典。"""
    return {
        "backend": "starling",
        "starling_version": f"{info['version']}+git:{info['commit']}+protocol:{info['protocol']}",
        "extraction": {
            "base_url": config.extraction_base_url, "model": config.extraction_model,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
            "prompts": canonical_digest(info["extraction_config"]),
            "world_settings": settings_digest(settings),
        },
        "embedding": {
            "base_url": config.embedding_base_url, "model": config.embedding_model, "dim": config.embedding_dim,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
        },
        "trace_schema": TRACE_SCHEMA_V2,
    }


@dataclass(frozen=True)
class ServiceIdentity:
    """第 1 代连上时记下的服务身份;之后每次换会话都与它比。"""

    commit: str
    protocol: int
    extraction_digest: str      # info()["extraction_config"] 规范化 JSON 的 sha256


class ServiceFatal(Exception):
    """会话已进致命状态;reason 只含码、提交号与 service.log 的相对名。"""


@dataclass
class _Generation:
    """一代会话:它的客户端与管理传输,开着的端口数与在途的管理调用数,开着的可写端口按世界计数,是否已丢失。
    丢失之后,两个计数都归零才关它的客户端与管理传输。"""

    number: int
    client: object
    transport: object
    ports: int = 0
    admin_calls: int = 0
    writers: dict[str, int] = field(default_factory=dict)
    lost: bool = False
    closed: bool = False

    def idle(self) -> bool:
        return self.ports == 0 and self.admin_calls == 0


def _identity(info: dict) -> ServiceIdentity:
    return ServiceIdentity(info["commit"], info["protocol"], canonical_digest(info["extraction_config"]))


def _mismatch(expected: ServiceIdentity, seen: ServiceIdentity) -> str | None:
    """两个身份哪里不同;只写码与提交号,不写抽取配置的内容。"""
    if seen.commit != expected.commit:
        return f"commit {expected.commit} != {seen.commit}"
    if seen.protocol != expected.protocol:
        return f"protocol {expected.protocol} != {seen.protocol}"
    if seen.extraction_digest != expected.extraction_digest:
        return "extraction_config changed"
    return None


def _code_of(exc: BaseException) -> str:
    return getattr(exc, "code", "") or type(exc).__name__


def _translate(exc: Exception, *, in_drain_window: bool = False) -> MemoryFailureError:
    """客户端异常照 `translate_client_failure` 映射;会话这一侧的端口与管理调用都不是 rise 关过的端口。"""
    from simple_actio.memory_starling import translate_client_failure

    return translate_client_failure(exc, port_closed_by_rise=False, in_drain_window=in_drain_window)


class ServiceSession:
    """组合根造一个,部署管理面、训练世界、解析器、实时服务端共用(90 篇"服务会话 ServiceSession"一节)。

    一把锁护代号、各代号的端口计数、丢失表与致命原因;网络调用(`connect`、`info`、`open`、管理 POST、`close`)都在锁外。
    `connect` 与 `transport` 缺省取 `starling_client.connect` 与 `starling_client.Transport`,测试注入假实现;
    `restart` 只在拉起模式下给(`SpawnedService.restart`)。"""

    def __init__(self, *, url_of: Callable[[], str], token_of: Callable[[], str],
                 pinned_commit: str, connect: Callable[..., object] | None = None,
                 transport: Callable[..., object] | None = None,
                 restart: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        import starling_client

        self._url_of, self._token_of, self._pinned = url_of, token_of, pinned_commit
        self._connect = connect if connect is not None else starling_client.connect
        self._transport_factory = transport if transport is not None else starling_client.Transport
        self._protocol = starling_client.PROTOCOL
        self._restart, self._clock = restart, clock
        self._lock = threading.Lock()
        self._switching = threading.Lock()                 # 同一时刻只有一条线程换会话
        self._fatal: str | None = None
        self._closed = False
        self._old: list[_Generation] = []                  # 已丢失、端口或管理调用还没走完的旧代号
        # 排空窗口的记录:(丢失时刻, 那时有可写句柄的完整世界名)。与代号分开记,旧代号的客户端关了窗口照样算满 60 秒
        self._drain: list[tuple[float, frozenset[str]]] = []
        client, info = self._first_connection()
        self.identity = _identity(info)
        self.info = info
        try:
            transport = self._transport_factory(url_of(), token_of(), ADMIN_TIMEOUT_S)
        except Exception as exc:
            self._close_quietly(client)
            raise FactoryConfigError(f"memory service admin transport failed: starling {_code_of(exc)}") from None
        self._current = _Generation(1, client, transport)

    @property
    def _admin_transport(self) -> object:
        """当前代号的管理传输(测试看它的超时与请求)。"""
        with self._lock:
            return self._current.transport

    # ---- 构造与换会话 ----

    def _first_connection(self) -> tuple[object, dict]:
        """装配期的第一次连接:连不上、token 错、协议不一致、提交不是钉住的那个,都是装配错误(退出码 2)。"""
        try:
            client = self._connect(self._url_of(), token=self._token_of(), timeout_s=HANDLE_TIMEOUT_S)
        except Exception as exc:
            raise FactoryConfigError(f"memory service connection refused: starling {_code_of(exc)}") from None
        try:
            info = client.info()
        except Exception as exc:
            self._close_quietly(client)
            raise FactoryConfigError(f"memory service info() failed: starling {_code_of(exc)}") from None
        if info.get("commit") != self._pinned:
            self._close_quietly(client)
            raise FactoryConfigError(
                f"memory service runs Starling {info.get('commit')} but rise pins {self._pinned}; "
                "sync the dashboard service's source tree or rise's pinned Starling version")
        if info.get("protocol") != self._protocol:
            self._close_quietly(client)
            raise FactoryConfigError(
                f"memory service speaks protocol {info.get('protocol')} but the client speaks {self._protocol}")
        return client, info

    def _live(self) -> _Generation:
        """当前代号;已丢失就先换会话。致命或已关时抛。"""
        with self._lock:
            self._check_usable()
            gen = self._current
            if not gen.lost:
                return gen
        with self._switching:
            with self._lock:
                self._check_usable()
                if not self._current.lost:                 # 别的线程刚换完
                    return self._current
                old = self._current
            client, transport = self._reconnect(old.number)
            with self._lock:
                if self._closed:
                    closable = [_Generation(0, client, transport)]
                else:
                    self._current = _Generation(old.number + 1, client, transport)
                    self._old.append(old)
                    closable = self._collect_closable()
                new = self._current
        self._close_generations(closable)
        if new is old:                                     # 换的途中会话被关了
            raise MemoryFailureError("starling session is closed", transient=False)
        _log.warning("memory service session %d lost; switched to %d", old.number, new.number)
        return new

    def _reconnect(self, old_number: int) -> tuple[object, object]:
        """拉起模式下先 restart(),再连、调 info() 与装配期的身份比。身份不符、Unauthorized、ProtocolMismatch、
        重启次数用完:记下致命原因,抛瞬态。连不上之类的瞬态失败:这一代仍标着丢失,下次再试。"""
        from starling_client import errors

        if self._restart is not None:
            try:
                self._restart()
            except ServiceRestartsExhausted:
                self._turn_fatal("memory service restarts exhausted; see service.log")
            except Exception as exc:
                raise MemoryFailureError(f"starling service restart failed: {type(exc).__name__}",
                                         transient=True) from None
        try:
            client = self._connect(self._url_of(), token=self._token_of(), timeout_s=HANDLE_TIMEOUT_S)
        except (errors.Unauthorized, errors.ProtocolMismatch) as exc:
            self._turn_fatal(f"reconnect refused: {exc.code}")
        except Exception as exc:
            raise _translate(exc) from None
        try:
            seen = _identity(client.info())
        except (errors.Unauthorized, errors.ProtocolMismatch) as exc:
            self._close_quietly(client)
            self._turn_fatal(f"reconnect refused: {exc.code}")
        except Exception as exc:
            self._close_quietly(client)
            raise _translate(exc) from None
        why = _mismatch(self.identity, seen)
        if why is not None:
            self._close_quietly(client)
            self._turn_fatal(f"memory service changed after session {old_number}: {why}")
        try:
            transport = self._transport_factory(self._url_of(), self._token_of(), ADMIN_TIMEOUT_S)
        except Exception as exc:
            self._close_quietly(client)
            raise _translate(exc) from None
        return client, transport

    def _turn_fatal(self, reason: str) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = reason
        _log.error("memory service session is fatal: %s", reason)
        raise MemoryFailureError(_STOPPING, transient=True)

    def _check_usable(self) -> None:
        """锁内调用。"""
        if self._fatal is not None:
            raise MemoryFailureError(_STOPPING, transient=True)
        if self._closed:
            raise MemoryFailureError("starling session is closed", transient=False)

    def _collect_closable(self) -> list[_Generation]:
        """锁内调用:端口与在途管理调用都走完的旧代号,摘出来交给锁外关。"""
        done = [g for g in self._old if g.idle() and not g.closed]
        for g in done:
            g.closed = True
        self._old = [g for g in self._old if not g.closed]
        return done

    def _close_generations(self, gens: Iterable[_Generation]) -> None:
        """锁外调用:关每一代的客户端与管理传输。"""
        for g in gens:
            self._close_quietly(g.client)
            self._close_quietly(g.transport)

    @staticmethod
    def _close_quietly(obj: object) -> None:
        if obj is None:
            return
        try:
            obj.close()
        except Exception as exc:                            # 过期的客户端 close() 不发 disconnect,客户端自己处理
            _log.warning("closing a memory service client failed: %s", type(exc).__name__)

    # ---- 端口 ----

    def open_port(self, world: str, holder: str, *, read_only: bool, settings: dict,
                  wrap: Callable[[object, Callable[[], None], int], object]) -> object:
        """在当前代号上开一个句柄,交给 `wrap(remote, release, generation)` 造端口。`release` 在端口关闭或
        "当作已关"时调一次(多调是空操作),减计数;计数归零且这一代已丢失时关它的客户端。"""
        from starling_client import errors

        gen = self._live()
        with self._lock:
            gen.ports += 1
            if not read_only:
                gen.writers[world] = gen.writers.get(world, 0) + 1
        try:
            remote = gen.client.open(world, holder=holder, read_only=read_only, settings=settings)
        except BaseException as exc:
            self._release(gen, world, read_only)
            if isinstance(exc, errors.SessionExpired):
                self.mark_lost(gen.number, writable_worlds=[])
            if not isinstance(exc, Exception):
                raise
            raise _translate(exc, in_drain_window=self.in_drain_window(world)) from None
        released = threading.Event()
        guard = threading.Lock()

        def release() -> None:
            with guard:
                if released.is_set():
                    return
                released.set()
            self._release(gen, world, read_only)

        try:
            return wrap(remote, release, gen.number)
        except BaseException:
            self._close_quietly(remote)                    # 没造成端口:句柄关掉,计数退回
            release()
            raise

    def _release(self, gen: _Generation, world: str, read_only: bool) -> None:
        with self._lock:
            gen.ports -= 1
            if not read_only:
                left = gen.writers.get(world, 0) - 1
                if left > 0:
                    gen.writers[world] = left
                else:
                    gen.writers.pop(world, None)
            closable = self._collect_closable() if gen.lost else []
        self._close_generations(closable)

    def mark_lost(self, generation: int, *, writable_worlds: Iterable[str]) -> None:
        """代号 `generation` 的会话丢失了(调用收到 SessionExpired,或 rise 没关的端口收到 handle_closed)。
        记下丢失时刻与那时有可写句柄的世界(报告里的加上这一代还开着的可写端口所在的世界)。只对当前代号生效:
        旧代号重复报告是空操作。"""
        reported = frozenset(writable_worlds)
        with self._lock:
            gen = self._current
            if gen.number != generation or gen.lost:
                return
            gen.lost = True
            self._drain.append((self._clock(), reported | frozenset(gen.writers)))

    def in_drain_window(self, world: str) -> bool:
        """有一个已丢失的代号在丢失时于 `world`(完整世界名)上有可写句柄,且距丢失不到 60 秒。"""
        now = self._clock()
        with self._lock:
            self._drain = [(at, worlds) for at, worlds in self._drain if now - at < DRAIN_WINDOW_S]
            return any(world in worlds for _, worlds in self._drain)

    def owns_generation(self, generation: int) -> bool:
        """端口记的代号还是当前代号;不是时适配器把端口当作已关。"""
        with self._lock:
            return generation == self._current.number

    @property
    def generation(self) -> int:
        with self._lock:
            return self._current.number

    @property
    def fatal(self) -> str | None:
        with self._lock:
            return self._fatal

    # ---- 管理传输 ----

    def admin(self, op: str, body: dict) -> dict:
        """经不带会话的管理传输 POST `/api/v1/client/admin/<op>`,超时 300 秒。会话丢失时先换会话(管理传输随之重建),
        但过期本身不影响管理调用。客户端异常照 `translate_client_failure`(两个改判开关都为假)。"""
        gen = self._live()
        with self._lock:
            gen.admin_calls += 1                           # 在途的管理调用也让这一代的传输留着
        try:
            return gen.transport.post(f"{_ADMIN_PATH}{op}", body, timeout_s=ADMIN_TIMEOUT_S)
        except Exception as exc:
            raise _translate(exc) from None
        finally:
            with self._lock:
                gen.admin_calls -= 1
                closable = self._collect_closable() if gen.lost else []
            self._close_generations(closable)

    # ---- 关闭 ----

    def close(self) -> None:
        """关当前与尚未关掉的旧代号的客户端与管理传输(过期的客户端 close() 不发 disconnect,客户端自己处理)。
        拉起服务的停止不在这里,由组合根另登记(Task 16)。幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            gens = [g for g in [*self._old, self._current] if not g.closed]
            for g in gens:
                g.closed = True
            self._old = []
        self._close_generations(gens)
