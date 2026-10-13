import time

import grpc
import pytest

from actio.instance import instantiate
from actio.memory import MemoryEntry
from actio.skills import Skill
from actio.trace import validate_trace
from controlplane.gitstore import GitStore
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_episode, read_scenario, write_episode, write_scenario

RUN = "run-actio"


class EchoGuidance:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)} turns")


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _assignment():
    # 对手 b 先说,Actio 扮演的 a 后说(MindAgent 场景)
    return RoleAssignment(
        "s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
               RoleBinding("a", "pa", "self_play")], 4,
    )


def _instance(skills):
    mem = MemoryEntry("mem-temper", 1, "pb", "对方一急就会生气", "2026-10-01T00:00:00Z", 0.9,
                      "ep-earlier", ("生气",))
    return instantiate(EchoGuidance(), skills=skills, memories=[mem],
                       library_version={"skill": 4, "memory": "mem-lib-1"}, profile={"role": "agent"})


CALM = Skill("skill-calm", 2, "micro", {"signal": "emotion_high"}, ("先回应对方的情绪",), priority=1)
FIRM = Skill("skill-firm", 1, "macro", {"signal": "emotion_high"}, ("坚持立场",), priority=1)


@pytest.fixture
def env(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    write_scenario(store, Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"],
                                   "deployment_simulation"))
    stored = []

    def on_closed(episode):
        write_episode(store, RUN, episode)
        stored.append(episode.id)

    def factory(scenario, assignment, personas, client_slot):
        return {s: ScriptedBackend({s: ["我真的很生气!!", "好吧"]}) for s in assignment.slot_ids() - {client_slot}}

    server = create_server(factory, port=0, turn_timeout_s=2.0, on_session_closed=on_closed,
                           scenario_resolver=lambda sid: read_scenario(store, sid))
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    yield store, stored, channel
    channel.close()
    server.stop(0)


def _play(store, channel, instance):
    """MindAgent 客户端循环的最小形态:respond → 带轨迹提交 → 直到会话结束。"""
    client = EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))
    client.create_session(scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
                          purpose="deployment_simulation", client_driven_slot="a")
    scenario = read_scenario(store, "s1")
    sent = []
    while not client.closed:
        utterance, thought, trace = instance.respond(_personas()["pa"], scenario, client.turns, "a")
        sent.append(trace)
        client.submit_turn(utterance, thought, activation_trace=trace)
    return client.drain_until_end(), sent


def _stored(store, stored):
    deadline = time.monotonic() + 3.0
    while not stored and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stored, "episode was not persisted"
    return read_episode(store, RUN, stored[0])


def test_client_turns_carry_traces_and_opponent_turns_do_not(env):
    store, stored, channel = env
    episode, sent = _play(store, channel, _instance([CALM, FIRM]))
    persisted = _stored(store, stored)

    assert [t.speaker_slot for t in persisted.turns] == ["b", "a", "b", "a"]
    assert [t.activation_trace for t in persisted.turns if t.speaker_slot == "b"] == [None, None]
    client_traces = [t.activation_trace for t in persisted.turns if t.speaker_slot == "a"]
    assert client_traces == sent  # 经真实 gRPC 往返再落盘,逐字段相等
    for got, want in zip(client_traces, sent):
        validate_trace(got)
        for key in want:
            assert type(got[key]) is type(want[key]), key

    first = client_traces[0]
    assert first["candidates"] == ["skill-calm", "skill-firm"]  # 同优先级,版本 2 先于版本 1
    assert first["selected"] == "skill-calm"
    assert first["conflict"]["skills"] == ["skill-calm", "skill-firm"]
    assert first["memory_hits"] == ["mem-temper"]
    assert first["library_version"] == {"skill": "4", "memory": "mem-lib-1"}
    assert client_traces[1]["selected"] is None  # "好吧" 不触发 emotion_high

    assert persisted.turns[1].utterance == "[skill:skill-calm] 先回应对方的情绪|[memory:mem-temper] pb: 对方一急就会生气"
    assert persisted.turns[1].inner_thought == "saw 1 turns"
    # 客户端本地重建的 episode 与服务端落盘的一致(客户端自己的轮次带轨迹回显)
    assert [t.to_dict() for t in episode.turns] == [t.to_dict() for t in persisted.turns]


def test_without_skills_the_wire_output_differs_and_traces_select_nothing(env):
    store, stored, channel = env
    _, sent = _play(store, channel, _instance([]))
    persisted = _stored(store, stored)
    assert persisted.turns[1].utterance == "[memory:mem-temper] pb: 对方一急就会生气"
    assert [t["selected"] for t in sent] == [None, None]
    assert [t.activation_trace for t in persisted.turns if t.speaker_slot == "a"] == sent
