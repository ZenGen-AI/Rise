import grpc
import pytest

from realtime_fixtures import plain_factory, plain_hook
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.backend import ClientDrivenBackend
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server

TRACE = {
    "candidates": ["skill-a", "skill-b"], "selected": "skill-a",
    "conflict": {"skills": ["skill-a", "skill-b"], "rule": "priority>version>id"},
    "memory_hits": [], "experience": None,
    "library_version": {"skill": "lib-3", "memory": "lib-1"},
}


def _scenario():
    return Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _assignment(max_turns=4):
    return RoleAssignment(
        "s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns,
    )


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


class TracingOpponent:
    """服务端的对手后端,返回三元组:它的轨迹会落盘,但不得下行给客户端。"""

    def __init__(self):
        self._k = 0

    def respond(self, persona, scenario, history, speaker_slot):
        self._k += 1
        return f"b{self._k}", f"secret{self._k}", {"selected": f"opp-{self._k}"}


# ---------------- ClientDrivenBackend ----------------

def test_backend_without_a_trace_still_returns_a_pair():
    backend = ClientDrivenBackend("a", 1.0)
    backend.submit("hi", None)
    assert backend.respond(_personas()["pa"], _scenario(), [], "a") == ("hi", None)


def test_backend_with_a_trace_returns_a_triple():
    backend = ClientDrivenBackend("a", 1.0)
    backend.submit("hi", "t", activation_trace={"selected": None})
    assert backend.respond(_personas()["pa"], _scenario(), [], "a") == ("hi", "t", {"selected": None})


def test_backend_trace_is_keyword_only():
    backend = ClientDrivenBackend("a", 1.0)
    with pytest.raises(TypeError):
        backend.submit("hi", None, {"x": "y"})


# ---------------- SessionDriver ----------------

def test_driver_trace_is_keyword_only():
    driver = SessionDriver(
        _scenario(), _assignment(2), _personas(),
        server_backends={"b": ScriptedBackend({"b": ["b1"]})},
        client_driven_slot="a", episode_id="ep-1", turn_timeout_s=2.0,
    )
    with pytest.raises(TypeError):
        driver.submit_turn("a1", None, {"x": "y"})  # 不启动驱动器:参数检查在入队之前


def test_driver_carries_the_trace_into_the_turn_event_and_the_episode():
    driver = SessionDriver(
        _scenario(), _assignment(2), _personas(),
        server_backends={"b": ScriptedBackend({"b": ["b1"]})},
        client_driven_slot="a", episode_id="ep-1", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("a1", None, activation_trace={"selected": "skill-a"})
    first = driver.poll_event(timeout_s=2.0)
    assert first.kind == "turn" and first.payload.activation_trace == {"selected": "skill-a"}
    second = driver.poll_event(timeout_s=2.0)
    closed = driver.poll_event(timeout_s=2.0)
    assert second.payload.activation_trace is None
    assert closed.kind == "closed"
    assert [t.activation_trace for t in closed.payload.turns] == [{"selected": "skill-a"}, None]


# ---------------- 真实 loopback gRPC ----------------

@pytest.fixture
def live():
    made = []

    def start(opponent="scripted"):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            if opponent == "tracing":
                return {s: TracingOpponent() for s in assignment.slot_ids() - {client_slot}}
            return {s: ScriptedBackend({s: ["b1", "b2", "b3"]}) for s in assignment.slot_ids() - {client_slot}}

        server = create_server(plain_factory(factory), port=0, turn_timeout_s=2.0,
                               on_session_closed=plain_hook(closed.append))
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        made.append((server, channel))
        client = EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        client.create_session(
            scenario_id="s1", role_assignment=_assignment(4), personas=_personas(),
            purpose="training", client_driven_slot="a",
        )
        return client, closed

    yield start
    for server, channel in made:
        channel.close()
        server.stop(0)


def _wait(closed):
    import time
    deadline = time.monotonic() + 3.0
    while not closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert closed, "on_session_closed was not called"
    return closed[0]


def test_trace_survives_a_real_round_trip_field_by_field(live):
    client, closed = live()
    client.submit_turn("a1", "think", activation_trace=TRACE)
    client.submit_turn("a2")
    client.drain_until_end()
    echoed = client.turns[0].activation_trace
    assert echoed == TRACE
    for key, value in TRACE.items():
        assert echoed[key] == value and type(echoed[key]) is type(value)
    stored = _wait(closed)
    assert stored.turns[0].activation_trace == TRACE
    assert stored.turns[2].activation_trace is None  # 第二轮没带轨迹 → None


def test_an_explicitly_empty_trace_stays_distinct_from_none(live):
    client, closed = live()
    client.submit_turn("a1", activation_trace={})
    client.submit_turn("a2")
    client.drain_until_end()
    assert client.turns[0].activation_trace == {}
    assert client.turns[2].activation_trace is None
    stored = _wait(closed)
    assert stored.turns[0].activation_trace == {}


def test_without_a_trace_nothing_changes(live):
    client, closed = live()
    client.submit_turn("a1")
    client.submit_turn("a2")
    episode = client.drain_until_end()
    assert all(t.activation_trace is None for t in episode.turns)
    assert all(t.activation_trace is None for t in _wait(closed).turns)


def test_opponent_traces_are_stored_but_never_sent_to_the_client(live):
    client, closed = live(opponent="tracing")
    client.submit_turn("a1", activation_trace={"selected": "mine"})
    client.submit_turn("a2", activation_trace={"selected": "mine-2"})
    client.drain_until_end()
    seen = client.turns
    assert [t.activation_trace for t in seen] == [
        {"selected": "mine"}, None, {"selected": "mine-2"}, None,
    ]
    assert seen[1].inner_thought is None
    stored = _wait(closed)
    assert [t.activation_trace for t in stored.turns] == [
        {"selected": "mine"}, {"selected": "opp-1"}, {"selected": "mine-2"}, {"selected": "opp-2"},
    ]


def test_integers_in_a_trace_come_back_as_floats(live):
    # 坑的回归钉:协议层不做数字规范化。激活轨迹的模式因此不含数字(见对外契约)。
    client, _ = live()
    client.submit_turn("a1", activation_trace={"n": 3})
    client.submit_turn("a2")
    client.drain_until_end()
    assert client.turns[0].activation_trace == {"n": 3.0}
    assert type(client.turns[0].activation_trace["n"]) is float


def test_client_trace_is_keyword_only(live):
    client, _ = live()
    with pytest.raises(TypeError):
        client.submit_turn("a1", None, {"x": "y"})
    client.abort()
