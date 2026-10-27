import shutil
import threading
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.resources import SessionInfo, SessionResources
from simulator.realtime.server import create_server

PERSONAS = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _assignment(max_turns=2):
    return RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"),
                                 RoleBinding("b", "pb", "frozen_reference")], max_turns)


@pytest.fixture
def serve():
    servers, channels = [], []

    def _serve(factory, **kwargs):
        kwargs.setdefault("turn_timeout_s", 2.0)
        server = create_server(factory, port=0, **kwargs)
        server.start()
        servers.append(server)

        def connect():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
        return server, connect

    yield _serve
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


def _open(client, *, episode_id=None, max_turns=2):
    return client.create_session(scenario_id="s1", role_assignment=_assignment(max_turns), personas=PERSONAS,
                                 purpose="training", client_driven_slot="a", episode_id=episode_id)


def _play(client):
    while not client.closed:
        client.submit_turn("嗯")
    return client.drain_until_end()


def _scripted(info):
    return SessionResources(backends={"b": ScriptedBackend({"b": ["一", "二"]})})


def _eventually(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_factory_gets_a_session_info_and_the_hook_gets_the_session_id(serve):
    infos, closed = [], []

    def factory(info):
        infos.append(info)
        return _scripted(info)

    _, connect = serve(factory, on_session_closed=lambda sid, ep: closed.append((sid, ep.id)))
    client = connect()
    session_id = _open(client)
    episode = _play(client)
    (info,) = infos
    assert isinstance(info, SessionInfo)
    assert (info.session_id, info.episode_id, info.client_slot) == (session_id, f"ep-{session_id}", "a")
    assert info.scenario.id == "s1" and set(info.personas) == {"pa", "pb"}
    assert episode.id == f"ep-{session_id}"
    assert closed == [(session_id, f"ep-{session_id}")]


def test_without_an_episode_id_the_server_keeps_ep_session_id(serve):
    infos = []
    _, connect = serve(lambda info: infos.append(info) or _scripted(info))
    client = connect()
    session_id = _open(client)
    assert _play(client).id == f"ep-{session_id}"
    assert infos[0].episode_id == f"ep-{session_id}"
