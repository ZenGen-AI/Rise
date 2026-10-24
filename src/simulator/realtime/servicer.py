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

from simulator.datamodel import Persona, RoleAssignment, Scenario, RoleSlot
from simulator.backends import ModelBackend
from simulator.realtime.backend_timeout import TimeoutBackend
from simulator.realtime.codec import (
    persona_from_proto, role_assignment_from_proto, turn_record_to_proto,
)
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.validation import validate_create_session

logger = logging.getLogger(__name__)

BackendsFactory = Callable[
    [Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]
]
ScenarioResolver = Callable[[str], Scenario]

_CLIENT_CLOSED = object()


class EnvironmentServicer(pb2_grpc.EnvironmentServiceServicer):
    """gRPC 双向流服务实现（§2.6.2）。每个 Interact 调用对应一个会话：
    先收 CreateSession 起 SessionDriver，随后把 SubmitTurn 转发给驱动器、
    把驱动器 outbox 里的事件转成 ServerMessage yield 出去。会话间除各自
    的 SessionDriver 实例外不共享任何可变状态（§2.6.4）。

    调用方约定：
    - on_session_closed 若提供，须自行保证线程安全——多个并发会话可能几乎
      同时调用它（例如都写同一个 ControlPlane GitStore）。
    - backends_factory 每次调用必须返回**全新**的后端实例。若多个会话共享
      同一个有状态后端（如带游标的 ScriptedBackend），就会出现 §2.6.4
      明确禁止的跨会话串话。
    - scenario_resolver 若提供，按 CreateSession.scenario_id 返回完整
      Scenario（此时其 purpose 以解析结果为准）；不提供则退回空 Scenario。
      未知 id 应抛 LookupError（含 KeyError）；返回的 Scenario 须是每次新建
      或实际不可变的对象——并发会话会共享同一个返回值，缓存可变对象会
      造成跨会话串话（§2.6.4）。

    资源边界：max_turns_cap 限制单会话轮次；create_timeout_s 限制连接建立
    后等待 CreateSession 的时长；backend_timeout_s 限制单次服务端后端调用。"""

    def __init__(
        self,
        backends_factory: BackendsFactory,
        *,
        turn_timeout_s: float = 30.0,
        on_session_closed=None,
        scenario_resolver: ScenarioResolver | None = None,
        max_turns_cap: int = 200,
        create_timeout_s: float = 10.0,
        backend_timeout_s: float = 60.0,
    ) -> None:
        self._backends_factory = backends_factory
        self._turn_timeout_s = turn_timeout_s
        self._on_session_closed = on_session_closed
        self._scenario_resolver = scenario_resolver
        self._max_turns_cap = max_turns_cap
        self._create_timeout_s = create_timeout_s
        self._backend_timeout_s = backend_timeout_s

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
                        driver, out = self._handle_create(msg.create_session, session_id)
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
                    for out in self._drain_ready_events(driver):
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
            if driver is not None and driver.state not in ("CLOSED", "FAILED"):
                driver.request_abort("client_disconnected")

    def _handle_create(self, create_session, session_id: str):
        """校验并启动会话。返回 (driver, 要 yield 的消息)：成功为
        (driver, SessionReady)，任何校验/解析/factory 失败为
        (None, EndSession(invalid_session))，且此时绝不启动 driver——
        校验必须先于 ACTIVE（§2.6.1"校验通过即直接进入 ACTIVE"）。"""
        role_assignment = role_assignment_from_proto(create_session.role_assignment)
        personas = {
            pid: persona_from_proto(p) for pid, p in create_session.personas.items()
        }
        client_slot = create_session.client_driven_slot

        reason = validate_create_session(
            role_assignment, personas, client_slot, max_turns_cap=self._max_turns_cap,
        )
        if reason is not None:
            return None, self._invalid_session(reason)

        if self._scenario_resolver is not None:
            try:
                scenario = self._scenario_resolver(create_session.scenario_id)
            except LookupError as exc:
                # 未知 scenario_id 是客户端输入错误（T2）
                return None, self._invalid_session(
                    f"unknown scenario {create_session.scenario_id!r}: {exc!r}"
                )
            except Exception as exc:
                # 其余异常（IO/网络等）是服务端依赖故障，可能瞬态（T1）
                logger.exception(
                    "scenario_resolver raised for scenario %r", create_session.scenario_id
                )
                return None, self._server_fault(
                    f"scenario_resolver failed for {create_session.scenario_id!r}: {exc!r}"
                )
            missing = role_assignment.slot_ids() - {s.slot_id for s in scenario.role_slots}
            if missing:
                return None, self._invalid_session(
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

        try:
            raw_backends = self._backends_factory(
                scenario, role_assignment, personas, client_slot,
            )
        except Exception as exc:
            logger.exception(
                "backends_factory raised for scenario %r", create_session.scenario_id
            )
            return None, self._server_fault(f"backends_factory failed: {exc!r}")

        # 工厂契约：必须为每个非客户端角色位返回后端。漏返回会在会话中途
        # （已发 SessionReady、甚至已有客户端轮次之后）才在 run_episode 里
        # KeyError，所以在启动 driver 之前就拦下。
        missing_backends = (role_assignment.slot_ids() - {client_slot}) - set(raw_backends)
        if missing_backends:
            return None, self._server_fault(
                f"backends_factory returned no backend for slots {sorted(missing_backends)}"
            )

        server_backends = {
            slot: TimeoutBackend(backend, self._backend_timeout_s)
            for slot, backend in raw_backends.items()
        }
        driver = SessionDriver(
            scenario, role_assignment, personas,
            server_backends=server_backends,
            client_driven_slot=client_slot,
            episode_id=f"ep-{session_id}",
            turn_timeout_s=self._turn_timeout_s,
        )
        driver.start()
        ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id=session_id))
        return driver, ready

    def _drain_ready_events(self, driver: SessionDriver):
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
                        self._on_session_closed(episode)
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
                                failure_detail=f"{type(exc).__name__}: {exc}"[:300],
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
