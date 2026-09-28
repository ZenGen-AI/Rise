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
