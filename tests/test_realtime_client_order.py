import grpc
import pytest

from realtime_fixtures import plain_factory
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.runner import run_episode


def _scenario(*slot_ids):
    return Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot(s, s) for s in slot_ids],
        conflict="c", norms=["n"], purpose="training",
    )


def _personas(*slot_ids):
    return {f"p{s}": Persona(f"p{s}", ["t"], "m", "s", ["v"]) for s in slot_ids}


def _assignment(slot_order, max_turns):
    return RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding(s, f"p{s}", "frozen_reference") for s in slot_order],
        max_turns=max_turns,
    )


def _opp_factory(lines_by_slot):
    def factory(scenario, assignment, personas, client_driven_slot):
        other = assignment.slot_ids() - {client_driven_slot}
        return {slot: ScriptedBackend({slot: list(lines_by_slot[slot])}) for slot in other}
    return factory


@pytest.fixture
def make_client():
    resources = []

    def _make(factory):
        server = create_server(plain_factory(factory), port=0, turn_timeout_s=2.0)
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        client = EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        resources.append((server, channel))
        return client

    yield _make
    for server, channel in resources:
        channel.close()
        server.stop(0)


def _utterances(turns):
    return [t.utterance for t in turns]


def test_client_speaking_second_receives_opponent_opening_turn(make_client):
    # MindAgent 场景：客户端角色 "a" 在 bindings 里排第二，对方 "b" 先说
    lines = {"b": ["opp1", "opp2"]}
    client = make_client(_opp_factory(lines))
    assignment = _assignment(["b", "a"], max_turns=4)
    client.create_session(
        scenario_id="s1", role_assignment=assignment, personas=_personas("a", "b"),
        purpose="training", client_driven_slot="a",
    )
    assert _utterances(client.opening_turns) == ["opp1"]  # 客户端发言前就已收到开场轮
    assert _utterances(client.submit_turn("cand1")) == ["opp2"]
    assert client.submit_turn("cand2") == []
    assert client.closed
    episode = client.drain_until_end()

    reference = run_episode(
        _scenario("a", "b"), assignment, _personas("a", "b"),
        backends={
            "a": ScriptedBackend({"a": ["cand1", "cand2"]}),
            "b": ScriptedBackend({"b": lines["b"]}),
        },
        episode_id=episode.id,
    )
    assert [t.to_dict() for t in episode.turns] == [t.to_dict() for t in reference.turns]
    assert episode.terminated_reason == "max_turns"


def test_three_roles_client_first_returns_all_opponent_turns(make_client):
    lines = {"b": ["b1", "b2"], "c": ["c1", "c2"]}
    client = make_client(_opp_factory(lines))
    assignment = _assignment(["a", "b", "c"], max_turns=6)
    client.create_session(
        scenario_id="s1", role_assignment=assignment, personas=_personas("a", "b", "c"),
        purpose="training", client_driven_slot="a",
    )
    assert client.opening_turns == []
    assert _utterances(client.submit_turn("cand1")) == ["b1", "c1"]
    assert _utterances(client.submit_turn("cand2")) == ["b2", "c2"]
    assert client.closed
    episode = client.drain_until_end()
    assert _utterances(episode.turns) == ["cand1", "b1", "c1", "cand2", "b2", "c2"]


def test_three_roles_client_in_the_middle(make_client):
    lines = {"b": ["b1", "b2"], "c": ["c1", "c2"]}
    client = make_client(_opp_factory(lines))
    assignment = _assignment(["b", "a", "c"], max_turns=6)
    client.create_session(
        scenario_id="s1", role_assignment=assignment, personas=_personas("a", "b", "c"),
        purpose="training", client_driven_slot="a",
    )
    assert _utterances(client.opening_turns) == ["b1"]
    assert _utterances(client.submit_turn("cand1")) == ["c1", "b2"]
    assert _utterances(client.submit_turn("cand2")) == ["c2"]
    assert client.closed
    episode = client.drain_until_end()
    assert _utterances(episode.turns) == ["b1", "cand1", "c1", "b2", "cand2", "c2"]


def test_rejected_create_raises_session_rejected_error(make_client):
    client = make_client(_opp_factory({"b": ["x"]}))
    with pytest.raises(SessionRejectedError) as exc:
        client.create_session(
            scenario_id="s1", role_assignment=_assignment(["a", "b"], max_turns=4),
            personas=_personas("a", "b"), purpose="training", client_driven_slot="ghost",
        )
    assert exc.value.reason == "invalid_session"
    assert "ghost" in exc.value.detail
    assert client.closed


def test_submit_turn_after_session_closed_raises(make_client):
    client = make_client(_opp_factory({"b": ["opp1"]}))
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(["a", "b"], max_turns=2),
        personas=_personas("a", "b"), purpose="training", client_driven_slot="a",
    )
    client.submit_turn("cand1")
    assert client.closed
    with pytest.raises(RuntimeError):
        client.submit_turn("cand2")
