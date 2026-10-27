import queue

import grpc
import pytest

from realtime_fixtures import plain_factory, plain_hook
from simulator.backends import ScriptedBackend
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.codec import (
    role_assignment_to_proto, persona_to_proto, turn_record_from_proto,
)
from simulator.realtime.server import create_server
from simulator.datamodel import Persona, RoleBinding, RoleAssignment


def _opponent_backends_factory(scenario, assignment, personas, client_driven_slot):
    other_slots = assignment.slot_ids() - {client_driven_slot}
    return {slot: ScriptedBackend({slot: ["opp1", "opp2", "opp3", "opp4"]}) for slot in other_slots}


@pytest.fixture
def server_address():
    server = create_server(plain_factory(_opponent_backends_factory), port=0, turn_timeout_s=2.0)
    server.start()
    yield f"127.0.0.1:{server.bound_port}"
    server.stop(0)


def _create_session_msg(max_turns=4):
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    personas = {
        "pa": persona_to_proto(Persona("pa", ["t"], "m", "s", ["v"])),
        "pb": persona_to_proto(Persona("pb", ["t"], "m", "s", ["v"])),
    }
    cs = pb2.CreateSession(
        scenario_id="s1",
        role_assignment=role_assignment_to_proto(assignment),
        personas=personas,
        purpose="training",
        client_driven_slot="a",
    )
    return pb2.ClientMessage(create_session=cs)


def test_full_session_happy_path(server_address):
    channel = grpc.insecure_channel(server_address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)

    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    ready = next(resp_iter)
    assert ready.WhichOneof("payload") == "session_ready"
    session_id = ready.session_ready.session_id
    assert session_id

    turns = []
    for i in range(2):  # 候选驱动 2 轮（a 在 turn 0/2），对手自动应答 b 在 turn 1/3
        q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"cand-{i}")))
        while True:
            msg = next(resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                turns.append(turn_record_from_proto(msg.turn_update.turn))
                if turns[-1].speaker_slot == "a":
                    break  # 候选轮已确认，进入下一次候选提交
            elif kind == "end_session":
                turns_final = msg
                break

    # 排空剩余事件直到 end_session
    end_msg = None
    while end_msg is None:
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "turn_update":
            turns.append(turn_record_from_proto(msg.turn_update.turn))
        elif msg.WhichOneof("payload") == "end_session":
            end_msg = msg

    q.put(None)
    channel.close()

    assert end_msg.end_session.reason == "max_turns"
    assert [t.speaker_slot for t in turns] == ["a", "b", "a", "b"]
    assert [t.utterance for t in turns] == ["cand-0", "opp1", "cand-1", "opp2"]


def test_timeout_ends_session_with_explicit_reason():
    server = create_server(plain_factory(_opponent_backends_factory), port=0, turn_timeout_s=0.1)
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    ready = next(resp_iter)
    assert ready.WhichOneof("payload") == "session_ready"
    # 不提交任何 SubmitTurn，等待服务端因超时主动结束
    end_msg = next(resp_iter)
    assert end_msg.WhichOneof("payload") == "end_session"
    assert end_msg.end_session.reason == "timeout"
    q.put(None)
    channel.close()
    server.stop(0)


def test_role_backend_failure_ends_session_with_explicit_reason():
    class BoomBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("boom")

    def boom_factory(scenario, assignment, personas, client_driven_slot):
        other = assignment.slot_ids() - {client_driven_slot}
        return {slot: BoomBackend() for slot in other}

    server = create_server(plain_factory(boom_factory), port=0, turn_timeout_s=2.0)
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    next(resp_iter)  # session_ready
    q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    next(resp_iter)  # turn_update for slot a
    end_msg = next(resp_iter)  # 对手 backend 抛异常 -> role_backend_failure
    assert end_msg.WhichOneof("payload") == "end_session"
    assert end_msg.end_session.reason == "role_backend_failure"
    q.put(None)
    channel.close()
    server.stop(0)


def test_client_initiated_end_terminates_promptly(server_address):
    channel = grpc.insecure_channel(server_address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=100))
    next(resp_iter)  # session_ready
    q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="only-turn")))
    next(resp_iter)  # turn_update for slot a
    q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason="client_terminated")))

    end_msg = None
    for _ in range(5):  # 对手轮可能先到，之后才是 end_session
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "end_session":
            end_msg = msg
            break
    assert end_msg is not None
    assert end_msg.end_session.reason == "client_terminated"
    q.put(None)
    channel.close()


def test_abrupt_disconnect_does_not_hang_server():
    # 断连必须判定为 FAILED（§2.6.5），不能被误记为 client_terminated 正常
    # 结束，也不应触发 on_session_closed 持久化——半途断开的会话不是正常
    # 完成的 episode（修复回归防护）。不用共享 fixture，因为需要注入自己的
    # on_session_closed 探针。
    closed_episodes = []
    server = create_server(
        plain_factory(_opponent_backends_factory), port=0, turn_timeout_s=2.0,
        on_session_closed=plain_hook(lambda ep: closed_episodes.append(ep.id)),
    )
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=100))
    ready = next(resp_iter)
    assert ready.WhichOneof("payload") == "session_ready"
    q.put(None)  # 立即关闭客户端流，不发 end_session（模拟进程崩溃）
    # 半关闭在本环境表现为优雅结束（StopIteration），个别 grpc 版本可能抛
    # RpcError；两者都可接受——协议层要求的是服务端行为，非客户端异常类型。
    end_msg = None
    try:
        for msg in resp_iter:
            if msg.WhichOneof("payload") == "end_session":
                end_msg = msg
    except grpc.RpcError:
        pass
    channel.close()
    assert end_msg is not None, "服务端应主动判定断连并发出 EndSession，而非静默挂起"
    assert end_msg.end_session.reason == "client_disconnected"
    assert closed_episodes == []  # 断连不应触发持久化钩子
    # 服务端未挂起、进程仍可继续接受新连接，用一次全新的正常会话验证
    channel2 = grpc.insecure_channel(address)
    stub2 = pb2_grpc.EnvironmentServiceStub(channel2)
    q2: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen2():
        while True:
            item = q2.get()
            if item is None:
                return
            yield item

    resp_iter2 = stub2.Interact(gen2())
    q2.put(_create_session_msg(max_turns=2))
    ready2 = next(resp_iter2)
    assert ready2.WhichOneof("payload") == "session_ready"
    q2.put(None)
    channel2.close()
    server.stop(0)


def test_persistence_hook_exception_does_not_break_stream():
    # 修复回归防护：on_session_closed 抛异常不应吞掉/打断 EndSession 的
    # 发送——客户端必须仍能拿到收尾消息。
    def boom_hook(episode):
        raise RuntimeError("simulated persistence failure")

    server = create_server(
        plain_factory(_opponent_backends_factory), port=0, turn_timeout_s=2.0,
        on_session_closed=plain_hook(boom_hook),
    )
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=2))
    next(resp_iter)  # session_ready
    q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    end_msg = None
    for _ in range(5):
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "end_session":
            end_msg = msg
            break
    assert end_msg is not None, "持久化钩子异常不应阻止 EndSession 送达客户端"
    # 落盘失败时不能报 max_turns:客户端会把这条 episode 当成已落盘的正常结束收下,
    # 而树里没有它。改报 persistence_failed,episode_id 置空,原因写进 failure_detail。
    assert end_msg.end_session.reason == "persistence_failed"
    assert end_msg.end_session.episode_id == ""
    # failure_detail 只放异常类名:异常文字(这里是 "simulated persistence failure")可能带远端地址或凭证
    assert "RuntimeError" in end_msg.end_session.failure_detail
    assert "simulated persistence failure" not in end_msg.end_session.failure_detail
    q.put(None)
    channel.close()
    server.stop(0)


def test_concurrent_sessions_do_not_cross_talk(server_address):
    import threading

    def run_one_session(tag, out):
        channel = grpc.insecure_channel(server_address)
        stub = pb2_grpc.EnvironmentServiceStub(channel)
        q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

        def gen():
            while True:
                item = q.get()
                if item is None:
                    return
                yield item

        resp_iter = stub.Interact(gen())
        q.put(_create_session_msg(max_turns=4))
        ready = next(resp_iter)
        session_id = ready.session_ready.session_id
        seen = [f"session:{session_id}"]
        for i in range(2):
            q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"{tag}-{i}")))
            while True:
                msg = next(resp_iter)
                if msg.WhichOneof("payload") == "turn_update":
                    seen.append(msg.turn_update.turn.utterance)
                    if msg.turn_update.turn.speaker_slot == "a":
                        break
        end_msg = None
        while end_msg is None:
            msg = next(resp_iter)
            if msg.WhichOneof("payload") == "turn_update":
                seen.append(msg.turn_update.turn.utterance)
            elif msg.WhichOneof("payload") == "end_session":
                end_msg = msg
        q.put(None)
        channel.close()
        out[tag] = (session_id, seen)

    results: dict[str, tuple] = {}
    tags = [f"client-{i}" for i in range(5)]
    threads = [threading.Thread(target=run_one_session, args=(tag, results)) for tag in tags]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 5
    session_ids = [sid for sid, _ in results.values()]
    assert len(set(session_ids)) == 5  # 每会话唯一 session_id
    for tag in tags:
        session_id, seen = results[tag]
        cand_utterances = [u for u in seen if u.startswith(tag)]
        assert cand_utterances == [f"{tag}-0", f"{tag}-1"]  # 只看到自己提交的候选话语
        other_tags = [t for t in tags if t != tag]
        assert not any(u.startswith(other) for u in seen for other in other_tags)
