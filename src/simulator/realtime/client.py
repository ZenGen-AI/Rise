from __future__ import annotations

import math
import queue
import threading
from copy import deepcopy

from simulator.datamodel import Episode, Persona, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, role_assignment_to_proto, turn_record_from_proto,
)
from simulator.realtime.proto import realtime_env_pb2 as pb2

# 接收线程放进 _recv_q 的"响应流正常结束"哨兵;异常则直接放异常对象本身。
_STREAM_END = object()


class SessionRejectedError(Exception):
    """服务端在 create_session 阶段就以 EndSession 拒绝了会话。reason/detail 原样
    来自 EndSession.reason/failure_detail：reason 为 "invalid_session" 表示
    请求本身有问题（重试无益）；为 "role_backend_failure" 表示服务端依赖故障
    （工厂/解析器出错），可能是瞬态的，可按 T1 重试。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class EnvironmentServiceClient:
    """EnvironmentService 的同步客户端包装器（未来 MindTrainer InteractTrain /
    MindAgent 经验采集直接复用的接口）。默认用直接 next() 拉取响应，不起消费者
    线程；给了 message_timeout_s 才起一个 daemon 接收线程（见 __init__）。
    逐轮累积 TurnRecord，在 EndSession 到达时于本地重建完整 Episode
    （§2.6.2："最终Episode摘要引用"，协议不重复内联整个 Episode）。

    轮次感知：服务端按 bindings[turn_index % n] 轮转发言，所以客户端知道
    "下一轮轮到谁"。核心原语 _await_client_turn 持续读取，直到下一轮属于
    客户端角色位（或会话结束）。因此支持任意数量的角色位、客户端处于任意
    位置——包括"客户端后说"（MindAgent 场景）：create_session 返回时，
    客户端之前的对方开场轮已收进 opening_turns。

    典型用法::

        client.create_session(...)
        for t in client.opening_turns: ...      # 客户端后说时非空
        while not client.closed:
            opponent_turns = client.submit_turn(my_utterance)
        episode = client.drain_until_end()

    注意 closed 在 create_session 返回时就可能已为 True（例如客户端后说而
    max_turns=1），先检查再提交。传输层出错（服务端宕机、连接被取消）时
    方法抛出 grpc.RpcError / RuntimeError，此时客户端已置为 closed 并释放
    请求流，不会泄漏后台线程。

    重建出的 Episode.terminated_reason 就是服务端 EndSession.reason。正常结束：max_turns、goal_reached、
    client_terminated。失败的几种里，调用方要分 T1（整条重跑有望成功）与 T2（重跑无益）：
    T1 是 timeout（含客户端相邻消息等待超时，见 message_timeout_s）、role_backend_timeout、
    role_backend_unavailable（对手 API 限流、5xx、连不上）、role_memory_unavailable（对手位记忆暂时不可用）、persistence_failed；
    T2 是 invalid_session、role_backend_rejected（对手 API 明确拒绝，4xx 且非 429）、role_memory_failure（对手位记忆确定性失败）；
    role_backend_failure 分不出 T1 与 T2（后端抛了说不清的异常，或工厂/解析器出错），调用方保守地不重跑；
    client_disconnected 是客户端自己断开。"""

    def __init__(self, stub, *, message_timeout_s: float | None = None) -> None:
        """message_timeout_s:相邻两条服务端消息的最长间隔(秒),必须是有限正数;None 表示
        不设限,此时直接 next() 拉取,不起接收线程,行为与加这个参数之前完全一致。

        给了它就起一个 daemon 接收线程把响应流搬进队列,读取端带超时地取。间隔超限时客户端
        **不抛异常**,而是在本地合成一条 EndSession(reason="timeout"),走既有的收尾路径:
        closed 为真、请求流被释放(服务端按断连判 FAILED,不落盘),drain_until_end() 的
        terminated_reason 是 "timeout";等 session_ready 时超时则抛 SessionRejectedError。
        这样调用方把它当成服务端报的 T1 失败重跑;若抛 RuntimeError,轨迹源会把它判成
        不可重跑的 client_protocol_error。"""
        if message_timeout_s is not None and not (
            math.isfinite(message_timeout_s) and message_timeout_s > 0
        ):
            raise ValueError(
                f"message_timeout_s must be a finite positive number or None, "
                f"got {message_timeout_s!r}"
            )
        self._stub = stub
        self._message_timeout_s = message_timeout_s
        self._send_q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()
        self._resp_iter = stub.Interact(self._request_gen())
        self._recv_q: "queue.Queue[object]" = queue.Queue()
        self._recv_thread: threading.Thread | None = None
        self._session_id = ""
        self._scenario_id = ""
        self._role_assignment: RoleAssignment | None = None
        self._client_slot = ""
        self._episode_id = ""
        self._turns: list[TurnRecord] = []
        self._opening_turns: list[TurnRecord] = []
        self._closed = False
        self._last_reason = ""
        if message_timeout_s is not None:
            self._recv_thread = threading.Thread(
                target=self._receive_loop, name="realtime-client-recv", daemon=True,
            )
            self._recv_thread.start()

    @property
    def closed(self) -> bool:
        """会话是否已终止（收到服务端 EndSession，或传输层出错后被放弃）。"""
        return self._closed

    @property
    def opening_turns(self) -> list[TurnRecord]:
        """create_session 返回前、客户端首次发言之前收到的对方轮次。"""
        return list(self._opening_turns)

    @property
    def turns(self) -> list[TurnRecord]:
        """目前已收到的完整历史(含客户端自己轮次的回显)的深拷贝。

        策略据此决定下一句。必须是深拷贝:TurnRecord 是可变数据类,策略若改了
        某条记录,会直接改坏客户端自己重建的 Episode。"""
        return deepcopy(self._turns)

    def _request_gen(self):
        while True:
            item = self._send_q.get()
            if item is None:
                return
            yield item

    def create_session(
        self,
        *,
        scenario_id: str,
        role_assignment: RoleAssignment,
        personas: dict[str, Persona],
        purpose: str,
        client_driven_slot: str,
        episode_id: str | None = None,
    ) -> str:
        """建立会话，返回 session_id。服务端拒绝时抛 SessionRejectedError；
        传输层出错时抛 grpc.RpcError。

        episode_id 是逻辑 episode id:给了就随 CreateSession 发给服务端,本地重建的
        Episode.id 也用它;不给则服务端用 ep-<session_id>,本地同样如此。格式由服务端
        判(invalid_session),客户端只拒空串:proto3 分不出空串与"没给",服务端会悄悄改用
        ep-<session_id>,本地 id 就与服务端分叉了。空串抛 ValueError,什么都不发。"""
        if episode_id == "":
            raise ValueError("episode_id must be a non-empty string or None, got ''")
        cs = pb2.CreateSession(
            scenario_id=scenario_id,
            role_assignment=role_assignment_to_proto(role_assignment),
            personas={pid: persona_to_proto(p) for pid, p in personas.items()},
            purpose=purpose,
            client_driven_slot=client_driven_slot,
        )
        if episode_id is not None:
            cs.episode_id = episode_id
        self._send_q.put(pb2.ClientMessage(create_session=cs))
        first = self._next_msg()
        kind = first.WhichOneof("payload")
        if kind == "end_session":
            end = first.end_session
            self._finish(end)
            raise SessionRejectedError(
                end.reason, end.failure_detail if end.HasField("failure_detail") else "",
            )
        if kind != "session_ready":
            self._abandon("protocol_error")
            raise RuntimeError(f"expected session_ready after create_session, got {kind!r}")
        self._session_id = first.session_ready.session_id
        self._scenario_id = scenario_id
        self._role_assignment = role_assignment
        self._client_slot = client_driven_slot
        self._episode_id = episode_id if episode_id is not None else f"ep-{self._session_id}"
        self._opening_turns = self._await_client_turn()
        return self._session_id

    def submit_turn(
        self,
        utterance: str,
        inner_thought: str | None = None,
        *,
        activation_trace: dict | None = None,
    ) -> list[TurnRecord]:
        """提交客户端这一轮，返回随后直到"再次轮到客户端"之前收到的全部对方
        轮次（可能为空）。返回值**不含**客户端自己那一轮的回显。会话在此期间
        结束则返回结束前收到的轮次，此后 closed 为 True。会话已结束时再调用
        抛 RuntimeError。不假定角色数量或客户端位置。

        activation_trace:客户端角色由完整 Actio 实例扮演时的激活轨迹,服务端写进
        该轮的 TurnRecord 并在回显里带回。None 表示不带(裸模型);{} 是"轨迹为空",
        两者在协议上可区分。经 protobuf Struct 传输,数字一律变成浮点数。"""
        if self._closed:
            raise RuntimeError("session already ended; check client.closed before submitting")
        submit = pb2.SubmitTurn(utterance=utterance)
        if inner_thought is not None:
            submit.inner_thought = inner_thought
        if activation_trace is not None:
            # SetInParent():update({}) 不会置位 presence,显式空轨迹会被误当成"未设置"
            submit.activation_trace.SetInParent()
            submit.activation_trace.update(activation_trace)
        self._send_q.put(pb2.ClientMessage(submit_turn=submit))
        # 服务端会把客户端自己的这一轮也作为 TurnUpdate 回显——先吞掉它，
        # 否则轮次计数会错位，后面的"轮到谁"判断就不成立。回显必须正是
        # 客户端这一轮，否则说明客户端与服务端对轮转顺序的理解出现分歧，
        # 与其静默错位不如立刻报错。
        own_index = len(self._turns)
        while not self._closed and len(self._turns) <= own_index:
            turn = self._pump_one()
            if turn is not None and (
                turn.turn_index != own_index or turn.speaker_slot != self._client_slot
            ):
                self._abandon("protocol_error")
                raise RuntimeError(
                    f"expected echo of own turn #{own_index} from slot "
                    f"{self._client_slot!r}, got #{turn.turn_index} from {turn.speaker_slot!r}"
                )
        return self._await_client_turn()

    def drain_until_end(self) -> Episode:
        """不主动提交，持续拉取直到会话自然终止（超时/后端失败/对手拒绝或不可用/max_turns）。"""
        while not self._closed:
            self._pump_one()
        return self._build_episode(self._last_reason)

    def end_session(self, reason: str = "client_terminated") -> Episode:
        self._send_q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason=reason)))
        while not self._closed:
            self._pump_one()
        return self._build_episode(self._last_reason)

    def abort(self) -> None:
        """放弃会话:**不发 EndSession**,置 closed 并释放请求流。

        服务端看到请求流结束而没有 EndSession,按客户端断连判 FAILED,
        不会经 on_session_closed 落盘。策略抛异常时必须走它——若改发
        end_session(),服务端会把这条半截 episode 当作正常结束并写进
        ControlPlane,污染训练数据。可重复调用;会话已结束时是空操作,
        不改写已有的终止原因。放弃之后不要使用 drain_until_end() 的结果。"""
        self._abandon("client_aborted")

    def _receive_loop(self) -> None:
        """接收线程:把响应流的每条消息放进 _recv_q;正常结束放 _STREAM_END,抛异常就把
        异常对象本身放进去。响应流被取消或服务端关流后 next() 返回/抛出,线程随之退出。"""
        try:
            for msg in self._resp_iter:
                self._recv_q.put(msg)
        except BaseException as exc:
            self._recv_q.put(exc)
            return
        self._recv_q.put(_STREAM_END)

    def _next_msg(self):
        """读一条服务端消息；传输层出错或流意外结束时放弃会话再抛出，避免
        请求生成器永远阻塞在 send_q.get()（gRPC 的请求消费线程会泄漏）。

        设了 message_timeout_s 时从接收线程的队列带超时地取,超时合成 timeout 的 EndSession。"""
        if self._message_timeout_s is None:
            try:
                return next(self._resp_iter)
            except StopIteration:
                self._abandon("stream_ended_without_end_session")
                raise RuntimeError("server closed the stream without sending EndSession") from None
            except BaseException:
                self._abandon("client_transport_error")
                raise
        try:
            item = self._recv_q.get(timeout=self._message_timeout_s)
        except queue.Empty:
            # 真 gRPC 流有 cancel():取消它让接收线程尽快退出;裸迭代器没有,就只合成收尾。
            cancel = getattr(self._resp_iter, "cancel", None)
            if callable(cancel):
                cancel()
            return pb2.ServerMessage(end_session=pb2.EndSession(
                reason="timeout",
                failure_detail=f"no server message within {self._message_timeout_s}s",
            ))
        if item is _STREAM_END:
            self._abandon("stream_ended_without_end_session")
            raise RuntimeError("server closed the stream without sending EndSession")
        if isinstance(item, BaseException):
            self._abandon("client_transport_error")
            raise item
        return item

    def _pump_one(self) -> TurnRecord | None:
        """读一条服务端消息：turn_update → 累积并返回该轮；end_session →
        收尾并返回 None。"""
        msg = self._next_msg()
        kind = msg.WhichOneof("payload")
        if kind == "turn_update":
            turn = turn_record_from_proto(msg.turn_update.turn)
            self._turns.append(turn)
            return turn
        if kind == "end_session":
            self._finish(msg.end_session)
            return None
        self._abandon("protocol_error")
        raise RuntimeError(f"unexpected server message {kind!r} mid-session")

    def _await_client_turn(self) -> list[TurnRecord]:
        """持续读取，直到下一轮属于客户端角色位或会话结束；返回期间收到的
        轮次。已达 max_turns 时继续读到 EndSession，让 closed 及时生效。"""
        assert self._role_assignment is not None
        bindings = self._role_assignment.bindings
        max_turns = self._role_assignment.max_turns
        received: list[TurnRecord] = []
        while not self._closed:
            next_index = len(self._turns)
            if (
                next_index < max_turns
                and bindings[next_index % len(bindings)].slot_id == self._client_slot
            ):
                break
            turn = self._pump_one()
            if turn is not None:
                received.append(turn)
        return received

    def _finish(self, end_session_proto) -> None:
        self._closed = True
        self._last_reason = end_session_proto.reason
        self._send_q.put(None)

    def _abandon(self, reason: str) -> None:
        """会话无法继续（传输层出错/协议错乱）：置为终止态并释放请求流。"""
        if not self._closed:
            self._closed = True
            self._last_reason = reason
            self._send_q.put(None)

    def _build_episode(self, reason: str) -> Episode:
        return Episode(
            id=self._episode_id,
            scenario_id=self._scenario_id,
            role_assignment=self._role_assignment,
            turns=list(self._turns),
            terminated_reason=reason,
        )
