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
    # 对手（非阻塞 ScriptedBackend）在后台线程被唤醒之前很可能已经抢先完成
    # 自己那一轮——这与 Critical 修复无关，是独立的、良性的调度提前量。
    # 用有界轮询收集所有 turn 事件直到 closed，而不是硬编码轮次数；
    # 真正的回归防护点是：任何中间 turn 事件都不应是修复前那种空话语的
    # 哨兵伪造轮次，且最终 episode 的轮次必须恰好是两句真实提交。
    turns_seen = [ev1.payload]
    closed = None
    for _ in range(5):
        ev = driver.poll_event(timeout_s=2.0)
        assert ev is not None, "driver produced no event within timeout"
        if ev.kind == "closed":
            closed = ev
            break
        assert ev.kind == "turn"
        turns_seen.append(ev.payload)
    assert closed is not None, "driver did not close within bound after request_end"
    assert all(t.utterance != "" for t in turns_seen)  # 无空话语哨兵伪造轮次
    assert closed.payload.terminated_reason == "client_terminated"
    assert [t.utterance for t in closed.payload.turns] == ["only turn", "opp"]
    assert driver.state == SessionState.CLOSED


def test_driver_request_abort_marks_failed_not_closed():
    # §2.6.5：服务端检测到的客户端异常（断连）必须落 FAILED，
    # 不能和"客户端主动优雅结束"(request_end→closed) 混同。
    scenario, assignment, personas = _fixture(max_turns=100)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": ScriptedBackend({"b": ["opp"] * 100})},
        client_driven_slot="a", episode_id="ep-5", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("only turn", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"
    driver.request_abort("client_disconnected")
    failed = None
    for _ in range(5):
        ev = driver.poll_event(timeout_s=2.0)
        assert ev is not None, "driver produced no event within timeout"
        if ev.kind == "failed":
            failed = ev
            break
        assert ev.kind == "turn"
        assert ev.payload.utterance != ""  # 无空话语哨兵伪造轮次
    assert failed is not None, "driver did not fail within bound after request_abort"
    assert failed.payload == "client_disconnected"
    assert driver.state == SessionState.FAILED
