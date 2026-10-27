from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

import grpc
from google.protobuf.json_format import MessageToDict

from simulator.datamodel import Episode, Scenario, RoleSlot
from simulator.realtime.backend_timeout import TimeoutBackend
from simulator.realtime.codec import (
    persona_from_proto, role_assignment_from_proto, turn_record_to_proto,
)
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.resources import SessionInfo, SessionResources
from simulator.realtime.validation import validate_create_session, validate_episode_id

logger = logging.getLogger(__name__)

BackendsFactory = Callable[[SessionInfo], SessionResources]
SessionClosedHook = Callable[[str, Episode], None]
ScenarioResolver = Callable[[str], Scenario]

_CLIENT_CLOSED = object()


class EnvironmentServicer(pb2_grpc.EnvironmentServiceServicer):
    """gRPC 双向流服务实现（§2.6.2）。每个 Interact 调用对应一个会话：
    先收 CreateSession 起 SessionDriver，随后把 SubmitTurn 转发给驱动器、
    把驱动器 outbox 里的事件转成 ServerMessage yield 出去。会话间除各自
    的 SessionDriver 实例外不共享任何可变状态（§2.6.4）。

    调用方约定：
    - on_session_closed 若提供，以 (session_id, episode) 调用，须自行保证线程
      安全——多个并发会话可能几乎同时调用它（例如都写同一个 ControlPlane
      GitStore）。
    - backends_factory 收一个 SessionInfo（情境、角色分配、人格、客户端位、
      session_id、episode_id），返回这条会话自己的 SessionResources。每次调用
      必须返回**全新**的 SessionResources 与后端实例。若多个会话共享同一个
      有状态后端（如带游标的 ScriptedBackend），就会出现 §2.6.4 明确禁止的
      跨会话串话。
    - CreateSession.episode_id 非空时，格式过 validate_episode_id 就成为这条
      会话的 episode id（SessionInfo.episode_id 与落盘的 Episode.id 都用它），
      不过则以 invalid_session 拒绝且不调工厂；空串当作没给，用 ep-<session_id>。
    - scenario_resolver 若提供，按 CreateSession.scenario_id 返回完整
      Scenario（此时其 purpose 以解析结果为准）；不提供则退回空 Scenario。
      未知 id 应抛 LookupError（含 KeyError）；返回的 Scenario 须是每次新建
      或实际不可变的对象——并发会话会共享同一个返回值，缓存可变对象会
      造成跨会话串话（§2.6.4）。

    资源边界：max_turns_cap 限制单会话轮次；create_timeout_s 限制连接建立
    后等待 CreateSession 的时长；backend_timeout_s 限制单次服务端后端调用。

    会话资源的收尾：工厂返回的 SessionResources 由 Interact 的 finally 统一收尾——
    quiesce（拒绝新调用）、join(resource_join_timeout_s)（等在途后端线程退出）、
    close（跑 closers）。每份资源的 close 只有一个驱动者，不会有两个线程同时关它：
    join 成功就是 finally 自己关；join 等不到则不关、不删，资源移交滞留表，此后
    只由唯一的回收线程每 linger_retry_s 秒 join(0) 重试，成功才 close 并从表里删掉。
    滞留的资源仍占一个会话名额（设计源 6.2、6.3 节）。quiesce、join、close 抛异常只记日志（类名），
    不让 finally 抛出 gRPC 线程。

    准入（设计源 M19）：_handle_create 在解析情境之后、调工厂之前，在 self._lock 这一把锁里
    检查"活动数加滞留数小于 max_workers"且 episode_id 没有活动会话，并登记进 _active。
    两步在同一次持锁里完成，并发的两条 create 不会都通过。不过则以 role_backend_unavailable
    （T1，瞬态）拒绝，不调工厂。登记在 Interact 的 finally 里释放：收尾关完就删，
    join 等不到则在移交滞留表的同一次持锁里从 _active 删掉，名额计数不出现空档。
    max_workers 应与 gRPC 的 maximum_concurrent_rpcs 同值。"""

    def __init__(
        self,
        backends_factory: BackendsFactory,
        *,
        turn_timeout_s: float = 30.0,
        on_session_closed: SessionClosedHook | None = None,
        scenario_resolver: ScenarioResolver | None = None,
        max_turns_cap: int = 200,
        create_timeout_s: float = 10.0,
        backend_timeout_s: float = 60.0,
        resource_join_timeout_s: float = 60.0,
        linger_retry_s: float = 60.0,
        max_workers: int = 16,
    ) -> None:
        self._backends_factory = backends_factory
        self._turn_timeout_s = turn_timeout_s
        self._on_session_closed = on_session_closed
        self._scenario_resolver = scenario_resolver
        self._max_turns_cap = max_turns_cap
        self._create_timeout_s = create_timeout_s
        self._backend_timeout_s = backend_timeout_s
        self._resource_join_timeout_s = resource_join_timeout_s
        self._linger_retry_s = linger_retry_s
        self._max_workers = max_workers
        # 一把锁守护活动登记、滞留表与回收线程句柄；准入检查在这把锁里读写三者。
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}          # session_id -> episode_id，已准入且未收尾的会话
        self._lingering: dict[str, SessionResources] = {}
        self._reaper: threading.Thread | None = None

    @staticmethod
    def _invalid_session(detail: str) -> pb2.ServerMessage:
        """协议层显式拒绝：EndSession 是唯一的错误通道，且按约定是终止性的。"""
        return pb2.ServerMessage(
            end_session=pb2.EndSession(
                reason="invalid_session", episode_id="", failure_detail=detail,
            )
        )

    @staticmethod
    def _server_fault(detail: str) -> pb2.ServerMessage:
        """服务端自身依赖故障（工厂/解析器出错、工厂漏返回后端）。刻意不用
        invalid_session：那表示"请求本身有问题、重试无益"，而这类故障可能是
        瞬态的（T1），调用方应能区分（§2.6.5、补充 E：每个失败点显式标注 T1/T2）。"""
        return pb2.ServerMessage(
            end_session=pb2.EndSession(
                reason="role_backend_failure", episode_id="", failure_detail=detail,
            )
        )

    @staticmethod
    def _unavailable(detail: str) -> pb2.ServerMessage:
        """准入被拒（名额满、同 episode_id 已有活动会话）。与 _server_fault 同形，原因是
        role_backend_unavailable：瞬态（T1），名额或 id 过一会儿会放开，调用方可按条重跑。
        detail 只放固定文案、计数与已过白名单的 episode_id，不放异常正文。"""
        return pb2.ServerMessage(
            end_session=pb2.EndSession(
                reason="role_backend_unavailable", episode_id="", failure_detail=detail,
            )
        )

    def Interact(self, request_iterator, context):
        """双向流主循环（select 风格）。一个后台泄流线程把 request_iterator 的
        消息搬进 inbox，迭代器耗尽（客户端正常结束或异常断连）时投递
        _CLIENT_CLOSED 哨兵。主循环交替做两件事：非阻塞轮询 inbox 处理客户端
        消息；抽干驱动器 outbox 转发服务端事件。这样服务端主动产生的事件
        （超时、后端失败）无需等待下一条客户端消息即可推送（§2.6.5），
        TurnUpdate 也不再与客户端消息一一绑定（§2.6.2）。

        终止判定只信任"drain 到一条 end_session"这一件事——不读 driver.state
        做旁路判断（那条路径存在"状态先置位、事件后入队"的竞态，曾经导致
        整个会话在半关闭时无声消失）。client_closed 分两种收尾：客户端已
        显式发过 end_session（explicit_end_requested）则什么都不做，静候
        驱动器自然收尾；否则视为异常断连，主动调 request_abort 把会话判为
        FAILED（§2.6.5：客户端侧异常由服务端自行判定，不需要客户端配合），
        且这条路径不会触发 on_session_closed——半途断开的会话不是正常完成
        的 episode。

        协议违规一律以 EndSession(reason="invalid_session") 显式拒绝并结束
        流，而不是静默丢弃：CreateSession 校验不过、未知 scenario_id、
        同一条流上重复 create、create 之前就收到 submit_turn/end_session、
        迟迟不发 create（create_timeout_s）。

        服务端自身依赖故障不冒充客户端输入错误：backends_factory 抛异常或漏
        返回后端、scenario_resolver 抛非 LookupError 异常，一律以
        role_backend_failure 拒绝——它们可能是瞬态的（模型加载失败、外部
        API 不可达），调用方应按可重试处理；invalid_session 则表示请求本身
        有问题、重试无益。"""
        inbox: "queue.Queue" = queue.Queue()

        def _pump() -> None:
            try:
                for msg in request_iterator:
                    inbox.put(msg)
            except grpc.RpcError:
                # 流被取消/客户端断连：对泄流线程而言这与迭代器正常耗尽是同一
                # 终止条件，哨兵已足以通知主循环，无需向上传播（§2.6.5 客户端
                # 侧异常由服务端自行检测收尾，不需要客户端配合）。
                pass
            finally:
                inbox.put(_CLIENT_CLOSED)

        threading.Thread(target=_pump, daemon=True).start()

        started_at = time.monotonic()
        driver: SessionDriver | None = None
        session_id: str | None = None
        resources: SessionResources | None = None
        client_closed = False
        explicit_end_requested = False
        disconnect_handled = False
        try:
            while True:
                try:
                    msg = inbox.get(timeout=0.01)
                except queue.Empty:
                    msg = None

                if msg is _CLIENT_CLOSED:
                    client_closed = True
                elif msg is not None:
                    kind = msg.WhichOneof("payload")
                    if kind == "create_session":
                        if driver is not None:
                            # 同一条流只承载一次会话。EndSession 是唯一（且终止性
                            # 的）错误通道，所以重复 create 会连带结束整条流；
                            # 已启动的第一个会话判为 FAILED（不落盘）。
                            driver.request_abort("invalid_session")
                            yield self._invalid_session(
                                "create_session received twice on one stream"
                            )
                            return
                        session_id = f"sess-{uuid.uuid4().hex[:12]}"
                        # 工厂返回之后的失败也会带着 resources 回来（driver 为 None），
                        # 由下面同一个 finally 收尾；工厂自己抛异常时 resources 是 None。
                        driver, resources, out = self._handle_create(msg.create_session, session_id)
                        yield out
                        if driver is None:
                            return  # 校验/解析/factory 失败：未启动任何 driver
                    elif kind in ("submit_turn", "end_session") and driver is None:
                        yield self._invalid_session(f"{kind} received before create_session")
                        return
                    elif kind == "submit_turn":
                        submit = msg.submit_turn
                        driver.submit_turn(
                            submit.utterance,
                            submit.inner_thought if submit.HasField("inner_thought") else None,
                            # HasField 区分"没有轨迹"(None)与"轨迹为空"({}),§2.6.3。
                            # MessageToDict 把 Struct 转成纯 dict/list,落盘可 yaml 序列化。
                            activation_trace=MessageToDict(submit.activation_trace)
                            if submit.HasField("activation_trace") else None,
                        )
                    elif kind == "end_session":
                        driver.request_end("client_terminated")
                        explicit_end_requested = True

                if (
                    driver is None
                    and not client_closed
                    and time.monotonic() - started_at > self._create_timeout_s
                ):
                    yield self._invalid_session(
                        f"no create_session received within {self._create_timeout_s}s"
                    )
                    return

                if driver is not None:
                    terminal = False
                    for out in self._drain_ready_events(driver, session_id):
                        yield out
                        if out.WhichOneof("payload") == "end_session":
                            terminal = True
                    if terminal:
                        return

                if client_closed:
                    if driver is None:
                        return
                    if not explicit_end_requested and not disconnect_handled:
                        driver.request_abort("client_disconnected")
                        disconnect_handled = True
                    # 继续循环：无论走哪条收尾路径，都不在此处自行判定终止——
                    # 等驱动器真正产出 closed/failed 事件，由上面的 drain
                    # 转发并 return，避免旁路读 driver.state 的竞态。
        finally:
            try:
                if driver is not None and driver.state not in ("CLOSED", "FAILED"):
                    driver.request_abort("client_disconnected")
            finally:
                # 无论正常结束、客户端断连、流被取消还是工厂之后的启动失败，都走这一处。
                if resources is not None:
                    self._wind_down(session_id, resources)
                elif session_id is not None:
                    # 没有资源可收尾（被拒、工厂抛了，或 _handle_create 在登记之后意外抛出而没把
                    # resources 带回来）：只把可能残留的准入登记放掉。没登记过则是空操作。
                    self._release_admission(session_id)

    def _handle_create(self, create_session, session_id: str):
        """校验并启动会话。返回 (driver, resources, 要 yield 的消息)：成功为
        (driver, SessionResources, SessionReady)；工厂调用之前的失败（校验、解析、准入被拒、
        工厂自己抛异常）为 (None, None, EndSession(...))，此时没有资源可收尾（准入登记在
        工厂抛异常时由这里撤销）；工厂
        返回之后的失败（缺后端、SessionDriver 构造或 start 抛）为
        (None, resources, EndSession(...))，资源交给调用方的 finally 收尾，这里不自己
        关。任何失败都绝不让 driver 跑起来——校验必须先于 ACTIVE
        （§2.6.1"校验通过即直接进入 ACTIVE"）。"""
        role_assignment = role_assignment_from_proto(create_session.role_assignment)
        personas = {
            pid: persona_from_proto(p) for pid, p in create_session.personas.items()
        }
        client_slot = create_session.client_driven_slot

        reason = validate_create_session(
            role_assignment, personas, client_slot, max_turns_cap=self._max_turns_cap,
        )
        if reason is not None:
            return None, None, self._invalid_session(reason)

        # proto3 分不出空串与"没给"：空串一律当没给，服务端用 ep-<session_id> 命名。
        # 非空的 id 要过格式校验；它在工厂调用之前拒绝，所以坏 id 不会造出任何资源。
        requested = create_session.episode_id
        if requested:
            id_reason = validate_episode_id(requested)
            if id_reason is not None:
                return None, None, self._invalid_session(f"invalid episode_id: {id_reason}")
        episode_id = requested or f"ep-{session_id}"

        if self._scenario_resolver is not None:
            try:
                scenario = self._scenario_resolver(create_session.scenario_id)
            except LookupError as exc:
                # 未知 scenario_id 是客户端输入错误（T2）
                return None, None, self._invalid_session(
                    f"unknown scenario {create_session.scenario_id!r}: {exc!r}"
                )
            except Exception as exc:
                # 其余异常（IO/网络等）是服务端依赖故障，可能瞬态（T1）
                logger.exception(
                    "scenario_resolver raised for scenario %r", create_session.scenario_id
                )
                return None, None, self._server_fault(
                    f"scenario_resolver failed for {create_session.scenario_id!r}: {exc!r}"
                )
            missing = role_assignment.slot_ids() - {s.slot_id for s in scenario.role_slots}
            if missing:
                return None, None, self._invalid_session(
                    f"scenario {create_session.scenario_id!r} lacks role slots "
                    f"required by bindings: {sorted(missing)}"
                )
        else:
            scenario = Scenario(
                id=create_session.scenario_id,
                background="",
                role_slots=[
                    RoleSlot(slot_id=s, description="") for s in role_assignment.slot_ids()
                ],
                conflict="", norms=[], purpose=create_session.purpose,
            )

        # 准入：名额与同 episode_id 的检查和登记在同一次持锁里完成，工厂在锁外跑（它可能很慢）。
        # 先登记再调工厂，并发的第二条 create 才看得到第一条。拒绝的会话没有资源，不收尾。
        with self._lock:
            if self._lingering:
                # 回收线程起不来时条目留在表里、名额一直被占；准入路径顺手补一次启动。
                self._ensure_reaper_locked()
            if len(self._active) + len(self._lingering) >= self._max_workers:
                return None, None, self._unavailable(
                    f"{len(self._active)} active and {len(self._lingering)} lingering "
                    f"sessions reach the cap {self._max_workers}"
                )
            if episode_id in self._active.values():
                return None, None, self._unavailable(
                    f"episode {episode_id!r} already has an active session"
                )
            self._active[session_id] = episode_id

        try:
            resources = self._backends_factory(SessionInfo(
                scenario, role_assignment, personas, client_slot, session_id, episode_id,
            ))
        except Exception as exc:
            logger.exception(
                "backends_factory raised for scenario %r", create_session.scenario_id
            )
            self._release_admission(session_id)
            return None, None, self._server_fault(f"backends_factory failed: {exc!r}")

        # 工厂契约：必须为每个非客户端角色位返回后端。漏返回会在会话中途
        # （已发 SessionReady、甚至已有客户端轮次之后）才在 run_episode 里
        # KeyError，所以在启动 driver 之前就拦下。
        missing_backends = (role_assignment.slot_ids() - {client_slot}) - set(resources.backends)
        if missing_backends:
            return None, resources, self._server_fault(
                f"backends_factory returned no backend for slots {sorted(missing_backends)}"
            )

        try:
            server_backends = {
                slot: TimeoutBackend(backend, self._backend_timeout_s, resources)
                for slot, backend in resources.backends.items()
            }
            driver = SessionDriver(
                scenario, role_assignment, personas,
                server_backends=server_backends,
                client_driven_slot=client_slot,
                episode_id=episode_id,
                turn_timeout_s=self._turn_timeout_s,
            )
            driver.start()
        except Exception as exc:
            # 服务端自身故障（起不了线程等），可能瞬态（T1）。detail 只放类名，细节在服务端日志。
            logger.exception("session driver could not be started for session %s", session_id)
            return None, resources, self._server_fault(
                f"session driver failed to start: {type(exc).__name__}"
            )
        ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id=session_id))
        return driver, resources, ready

    def _wind_down(self, session_id: str, resources: SessionResources) -> None:
        """会话终态收尾：quiesce → join(有界) → close。只在 Interact 的 finally 里调用，
        所以每份资源的 close 只有一个驱动者；绝不向调用方（gRPC 线程）抛异常，日志只记类名。
        准入登记（_active）在 close 之后释放，或在移交滞留表的同一次持锁里释放。

        join 在 RPC 的 worker 线程里同步进行：客户端在这之前已收到 EndSession，不受影响；
        这段时间该会话仍占 gRPC 的并发名额（设计源 6.3 节）。join 等不到（或 join 自己
        抛了，无法确认在途调用已退出）就不 close：关库不能压在还在跑的调用下面，
        资源转入滞留表，由回收线程重试。"""
        try:
            resources.quiesce()
        except Exception as exc:
            logger.warning(
                "session %s resources quiesce failed: %s", session_id, type(exc).__name__,
            )
        try:
            quiesced = resources.join(self._resource_join_timeout_s)
        except Exception as exc:
            logger.warning(
                "session %s resources join failed: %s; lingering", session_id, type(exc).__name__,
            )
        else:
            if not quiesced:
                logger.warning(
                    "session %s resources did not quiesce within %ss; lingering",
                    session_id, self._resource_join_timeout_s,
                )
            else:
                self._close_resources(session_id, resources)
                self._release_admission(session_id)
                return
        self._linger(session_id, resources)

    @staticmethod
    def _close_resources(session_id: str, resources: SessionResources) -> None:
        try:
            resources.close()
        except Exception as exc:
            logger.warning(
                "session %s resources close failed: %s", session_id, type(exc).__name__,
            )

    def _release_admission(self, session_id: str) -> None:
        """放掉一条准入登记（持锁）。没登记过则是空操作。"""
        with self._lock:
            self._active.pop(session_id, None)

    def _linger(self, session_id: str, resources: SessionResources) -> None:
        """把等不到退出的资源放进滞留表，没有活着的回收线程就起一个。从 _active 删和放进
        _lingering 在同一次持锁里，名额计数不出现空档（准入检查同样持这把锁）。"""
        with self._lock:
            self._active.pop(session_id, None)
            self._lingering[session_id] = resources
            self._ensure_reaper_locked()

    def _ensure_reaper_locked(self) -> None:
        """没有活着的回收线程就起一个；调用方须持 self._lock。回收线程句柄由回收线程自己在
        持锁时清空，所以这里看到 None 就一定要起新的，不会出现"线程刚决定退出、新条目被漏掉"。
        起不来也不抛：只记类名，句柄不赋值，下一次有会话滞留或下一次准入时会再试。"""
        if self._reaper is not None:
            return
        reaper = threading.Thread(
            target=self._reap_lingering, name="session-reaper", daemon=True,
        )
        try:
            reaper.start()
        except Exception as exc:
            logger.warning("session reaper could not start: %s", type(exc).__name__)
            return
        self._reaper = reaper

    def lingering_count(self) -> int:
        """滞留表里的资源数（持锁读）。"""
        with self._lock:
            return len(self._lingering)

    def _reap_lingering(self) -> None:
        """回收线程:每 linger_retry_s 秒对滞留的资源各 join(0) 一次,成功的锁外 close,
        关完才从表里删(名额在目录删掉之后才释放)。仍有滞留就报一次数量(设计源 6.3 节
        "日志每分钟报一次滞留数",间隔就是 linger_retry_s)。表空了线程退出,下次有新滞留再起。"""
        try:
            while True:
                time.sleep(self._linger_retry_s)
                with self._lock:
                    snapshot = list(self._lingering.items())
                for session_id, resources in snapshot:
                    self._retry_lingering(session_id, resources)
                with self._lock:
                    remaining = len(self._lingering)
                    if remaining == 0:
                        self._reaper = None
                        return
                logger.warning("%d session resources still lingering", remaining)
        finally:
            with self._lock:
                if self._reaper is threading.current_thread():
                    self._reaper = None

    def _retry_lingering(self, session_id: str, resources: SessionResources) -> None:
        try:
            quiesced = resources.join(0)
        except Exception as exc:
            logger.warning(
                "session %s lingering resources join failed: %s", session_id, type(exc).__name__,
            )
            return
        if not quiesced:
            return
        self._close_resources(session_id, resources)
        with self._lock:
            self._lingering.pop(session_id, None)

    def _drain_ready_events(self, driver: SessionDriver, session_id: str):
        """把驱动器 outbox 里已就绪的事件全部转发出去（非阻塞轮询，
        一次 poll_event(0) 抽干当前积压，避免每条 ClientMessage 只对应
        恰好一条 ServerMessage 的强绑定——TurnUpdate 需要在轮次完成后
        立即推送，可能一次提交触发多条服务端消息）。"""
        while True:
            event = driver.poll_event(timeout_s=0.01)
            if event is None:
                return
            if event.kind == "turn":
                turn = event.payload
                if turn.speaker_slot != driver.client_driven_slot:
                    # 对手的内部推理/激活轨迹不下行给客户端（被训练模型）：和
                    # run_episode 里给后端的 history 同一条规则。服务端落盘的
                    # Episode 来自 driver 自己的完整记录，不受影响。
                    turn = replace(turn, inner_thought=None, activation_trace=None)
                yield pb2.ServerMessage(
                    turn_update=pb2.TurnUpdate(turn=turn_record_to_proto(turn))
                )
            elif event.kind == "closed":
                episode = event.payload
                end_msg = pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=episode.terminated_reason, episode_id=episode.id,
                    )
                )
                if self._on_session_closed is not None:
                    try:
                        self._on_session_closed(session_id, episode)
                    except Exception as exc:
                        # 持久化钩子失败不应把整条流也跟着弄挂——客户端仍需
                        # 拿到收尾消息;钩子异常记日志,不向上传播。但不能照报
                        # 正常的终止原因:客户端会把这条 episode 当成已落盘收下,
                        # 而树里没有它。改报 persistence_failed,不给 episode_id。
                        logger.exception(
                            "on_session_closed hook raised for episode %s", episode.id
                        )
                        end_msg = pb2.ServerMessage(
                            end_session=pb2.EndSession(
                                reason="persistence_failed", episode_id="",
                                # 只放类名:异常文字可能带远端地址或凭证,而 failure_detail 会被客户端转述进
                                # RolloutFailedError 与指令的失败原因。细节在上面 logger.exception 的服务端日志里。
                                failure_detail=type(exc).__name__[:300],
                            )
                        )
                yield end_msg
                return
            elif event.kind == "failed":
                yield pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=event.payload, episode_id="", failure_detail=event.payload,
                    )
                )
                return
