from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.realtime.driver import SessionDriver, DriverEvent
from simulator.realtime.session import SessionState


def _fixture(max_turns=4):
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "候选"), RoleSlot("b", "对手")],
        conflict="c", norms=["n"], purpose="training",
    )
    personas = {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
                "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    return scenario, assignment, personas


def test_driver_streams_turn_events_then_closed():
    scenario, assignment, personas = _fixture(max_turns=2)
    opponent_backend = ScriptedBackend({"b": ["opp1"]})
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": opponent_backend},
        client_driven_slot="a", episode_id="ep-1", turn_timeout_s=2.0,
    )
    assert driver.state == SessionState.CREATED
    driver.start()
    assert driver.state == SessionState.ACTIVE

    driver.submit_turn("candidate turn 1", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"
    assert ev1.payload.speaker_slot == "a"
    assert ev1.payload.utterance == "candidate turn 1"

    ev2 = driver.poll_event(timeout_s=2.0)
    assert ev2.kind == "turn"
    assert ev2.payload.speaker_slot == "b"
    assert ev2.payload.utterance == "opp1"

    ev3 = driver.poll_event(timeout_s=2.0)
    assert ev3.kind == "closed"
    assert ev3.payload.terminated_reason == "max_turns"
    assert len(ev3.payload.turns) == 2
    assert driver.state == SessionState.CLOSED


def test_driver_timeout_transitions_to_failed():
    scenario, assignment, personas = _fixture(max_turns=2)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": ScriptedBackend({"b": ["opp1"]})},
        client_driven_slot="a", episode_id="ep-2", turn_timeout_s=0.05,
    )
    driver.start()
    # 不调用 submit_turn，等待后台线程因超时失败
    ev = driver.poll_event(timeout_s=2.0)
    assert ev.kind == "failed"
    assert ev.payload == "timeout"
    assert driver.state == SessionState.FAILED


def test_driver_role_backend_failure_transitions_to_failed():
    class BoomBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("backend exploded")

    scenario, assignment, personas = _fixture(max_turns=2)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": BoomBackend()},
        client_driven_slot="a", episode_id="ep-3", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("candidate turn 1", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"  # 候选先手成功
    ev2 = driver.poll_event(timeout_s=2.0)
    assert ev2.kind == "failed"
    assert ev2.payload == "role_backend_failure"
    assert driver.state == SessionState.FAILED


def test_driver_request_end_stops_driver_promptly():
    scenario, assignment, personas = _fixture(max_turns=100)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": ScriptedBackend({"b": ["opp"] * 100})},
        client_driven_slot="a", episode_id="ep-4", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("only turn", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"
    driver.request_end("client_terminated")
    # 对手在途轮 + 哨兵触发的空轮可能各出一条 turn 事件（数量取决于 GIL 调度），
    # 随后必须是 closed。用有界循环收敛到 closed，而非硬编码穿插事件数。
    closed = None
    for _ in range(4):
        ev = driver.poll_event(timeout_s=2.0)
        assert ev is not None, "driver produced no event within timeout"
        if ev.kind == "closed":
            closed = ev
            break
        assert ev.kind == "turn"
    assert closed is not None, "driver did not close within 4 events after request_end"
    assert closed.payload.terminated_reason == "client_terminated"
    assert driver.state == SessionState.CLOSED
