import queue

import grpc
import pytest

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
    server = create_server(_opponent_backends_factory, port=0, turn_timeout_s=2.0)
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
    server = create_server(_opponent_backends_factory, port=0, turn_timeout_s=0.1)
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

    server = create_server(boom_factory, port=0, turn_timeout_s=2.0)
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


def test_abrupt_disconnect_does_not_hang_server(server_address):
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
    q.put(None)  # 立即关闭客户端流，不发 end_session（模拟进程崩溃）
    # 半关闭在本环境表现为优雅结束（StopIteration），个别 grpc 版本可能抛 RpcError；
    # 两者都可接受——协议层要求的是服务端行为（检测断连、收尾、不挂起），非客户端异常类型。
    try:
        for _ in resp_iter:
            pass
    except grpc.RpcError:
        pass
    channel.close()
    # 服务端未挂起、进程仍可继续接受新连接，用一次全新的正常会话验证
    channel2 = grpc.insecure_channel(server_address)
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
