import grpc
import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleBinding, RoleAssignment
from simulator.realtime.codec import persona_to_proto, role_assignment_to_proto
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import write_episode, read_episode


def _opponent_factory(scenario, assignment, personas, client_driven_slot):
    other = assignment.slot_ids() - {client_driven_slot}
    return {slot: ScriptedBackend({slot: ["opp1", "opp2"]}) for slot in other}


def test_on_session_closed_hook_persists_episode_to_controlplane(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    run_id = "run-realtime-1"

    def persist(episode):
        write_episode(store, run_id, episode)

    server = create_server(_opponent_factory, port=0, turn_timeout_s=2.0, on_session_closed=persist)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    stub = pb2_grpc.EnvironmentServiceStub(channel)

    import queue
    q: "queue.Queue" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    personas = {"pa": persona_to_proto(Persona("pa", ["t"], "m", "s", ["v"])),
                "pb": persona_to_proto(Persona("pb", ["t"], "m", "s", ["v"]))}
    q.put(pb2.ClientMessage(create_session=pb2.CreateSession(
        scenario_id="s1", role_assignment=role_assignment_to_proto(assignment),
        personas=personas, purpose="training", client_driven_slot="a",
    )))
    ready = next(resp_iter)
    session_id = ready.session_ready.session_id
    episode_id = f"ep-{session_id}"

    end_msg = None
    for i in range(2):
        q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"cand-{i}")))
        while True:
            msg = next(resp_iter)
            if msg.WhichOneof("payload") == "turn_update":
                if msg.turn_update.turn.speaker_slot == "a":
                    break
            elif msg.WhichOneof("payload") == "end_session":
                end_msg = msg
                break
        if end_msg:
            break
    while end_msg is None:
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "end_session":
            end_msg = msg

    q.put(None)
    channel.close()
    server.stop(0)

    assert store.exists(episode_file(run_id, episode_id))
    persisted = read_episode(store, run_id, episode_id)
    assert persisted.id == episode_id
    assert persisted.terminated_reason == "max_turns"
    assert [t.speaker_slot for t in persisted.turns] == ["a", "b", "a", "b"]
