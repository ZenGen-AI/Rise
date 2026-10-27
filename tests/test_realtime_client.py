import grpc
import pytest

from realtime_fixtures import plain_factory
from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.server import create_server


def _scenario():
    return Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "候选"), RoleSlot("b", "对手")],
        conflict="c", norms=["n"], purpose="training",
    )


def _assignment(max_turns):
    return RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )


def _personas():
    return {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
            "pb": Persona("pb", ["t"], "m", "s", ["v"])}


def _opponent_factory(opponent_lines):
    def factory(scenario, assignment, personas, client_driven_slot):
        other = assignment.slot_ids() - {client_driven_slot}
        return {slot: ScriptedBackend({slot: list(opponent_lines)}) for slot in other}
    return factory


@pytest.fixture
def client_and_server():
    opponent_lines = ["opp1", "opp2"]
    server = create_server(plain_factory(_opponent_factory(opponent_lines)), port=0, turn_timeout_s=2.0)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    client = EnvironmentServiceClient(stub)
    yield client, opponent_lines
    channel.close()
    server.stop(0)


def test_client_reconstructs_episode_matching_direct_run_episode(client_and_server):
    client, opponent_lines = client_and_server
    candidate_lines = ["cand1", "cand2"]

    session_id = client.create_session(
        scenario_id="s1", role_assignment=_assignment(4), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    assert session_id

    for line in candidate_lines:
        client.submit_turn(line)
    episode = client.drain_until_end()

    # 直接调 run_episode 产出的"参照答案"：用同样的两条剧本（候选走
    # ScriptedBackend，与客户端逐轮提交语义一致）
    reference = run_episode(
        _scenario(), _assignment(4), _personas(),
        backends={
            "a": ScriptedBackend({"a": candidate_lines}),
            "b": ScriptedBackend({"b": opponent_lines}),
        },
        episode_id=episode.id,  # episode_id 由服务端生成，对齐后比较其余字段
    )

    assert [t.to_dict() for t in episode.turns] == [t.to_dict() for t in reference.turns]
    assert episode.terminated_reason == reference.terminated_reason == "max_turns"


def test_client_end_session_returns_episode_with_client_terminated_reason(client_and_server):
    client, _ = client_and_server
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(100), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    client.submit_turn("only-turn")
    episode = client.end_session()
    assert episode.terminated_reason == "client_terminated"
    assert len(episode.turns) >= 1
