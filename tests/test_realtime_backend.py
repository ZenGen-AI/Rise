import threading
import time

import pytest

from simulator.datamodel import Persona, Scenario, RoleSlot
from simulator.realtime.backend import ClientDrivenBackend, SessionEndRequested, TurnTimeoutError


def _persona_scenario():
    persona = Persona("pa", ["t"], "m", "s", ["v"])
    scenario = Scenario("s1", "bg", [RoleSlot("a", "role a")], "c", ["n"], "training")
    return persona, scenario


def test_submit_then_respond_returns_submitted_utterance():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("hello from client", None)
    utterance, inner_thought = backend.respond(persona, scenario, [], "a")
    assert utterance == "hello from client"
    assert inner_thought is None


def test_submit_with_inner_thought_passthrough():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("hi", "thinking hard")
    utterance, inner_thought = backend.respond(persona, scenario, [], "a")
    assert inner_thought == "thinking hard"


def test_respond_blocks_until_submit_from_another_thread():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=2.0)
    persona, scenario = _persona_scenario()
    result = {}

    def responder():
        result["value"] = backend.respond(persona, scenario, [], "a")

    t = threading.Thread(target=responder)
    t.start()
    time.sleep(0.1)  # respond() 应仍在阻塞
    assert "value" not in result
    backend.submit("late arrival", None)
    t.join(timeout=2.0)
    assert result["value"] == ("late arrival", None)


def test_respond_raises_on_timeout():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=0.05)
    persona, scenario = _persona_scenario()
    with pytest.raises(TurnTimeoutError):
        backend.respond(persona, scenario, [], "a")


def test_multiple_submits_are_consumed_in_order():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("first", None)
    backend.submit("second", None)
    u1, _ = backend.respond(persona, scenario, [], "a")
    u2, _ = backend.respond(persona, scenario, [], "a")
    assert (u1, u2) == ("first", "second")


def test_request_end_raises_session_end_requested_not_a_fake_utterance():
    # 回归防护（Critical 修复）：唤醒哨兵必须让 respond() 抛异常，
    # 绝不能被当成一句真实话语("", None) 返回。
    backend = ClientDrivenBackend(slot_id="a", timeout_s=2.0)
    persona, scenario = _persona_scenario()
    backend.request_end()
    with pytest.raises(SessionEndRequested):
        backend.respond(persona, scenario, [], "a")


def test_request_end_wakes_a_blocked_respond_call():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=2.0)
    persona, scenario = _persona_scenario()
    result = {}

    def responder():
        try:
            backend.respond(persona, scenario, [], "a")
        except SessionEndRequested:
            result["ended"] = True

    t = threading.Thread(target=responder)
    t.start()
    time.sleep(0.1)  # respond() 应仍在阻塞
    assert "ended" not in result
    backend.request_end()
    t.join(timeout=2.0)
    assert result.get("ended") is True


def test_pending_real_submit_is_consumed_before_a_later_request_end():
    # FIFO 顺序防护：先提交的真实话语必须先被消费，哨兵排在其后才生效。
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("real turn", None)
    backend.request_end()
    utterance, _ = backend.respond(persona, scenario, [], "a")
    assert utterance == "real turn"
    with pytest.raises(SessionEndRequested):
        backend.respond(persona, scenario, [], "a")
