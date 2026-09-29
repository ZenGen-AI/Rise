import time

import pytest

from simulator.datamodel import Persona, Scenario, RoleSlot
from simulator.backends import ScriptedBackend
from simulator.realtime.backend_timeout import TimeoutBackend, RoleBackendTimeoutError


def _persona_scenario():
    persona = Persona("pa", ["t"], "m", "s", ["v"])
    scenario = Scenario("s1", "bg", [RoleSlot("a", "role a")], "c", ["n"], "training")
    return persona, scenario


def test_fast_backend_passes_through_unchanged():
    inner = ScriptedBackend({"a": ["hello"]})
    wrapped = TimeoutBackend(inner, timeout_s=1.0)
    persona, scenario = _persona_scenario()
    assert wrapped.respond(persona, scenario, [], "a") == ("hello", None)


def test_slow_backend_raises_role_backend_timeout():
    class Slow:
        def respond(self, persona, scenario, history, speaker_slot):
            time.sleep(0.3)
            return ("late", None)

    wrapped = TimeoutBackend(Slow(), timeout_s=0.05)
    persona, scenario = _persona_scenario()
    with pytest.raises(RoleBackendTimeoutError):
        wrapped.respond(persona, scenario, [], "a")


def test_inner_exception_propagates_as_is_not_as_timeout():
    class Boom:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("boom")

    wrapped = TimeoutBackend(Boom(), timeout_s=1.0)
    persona, scenario = _persona_scenario()
    with pytest.raises(RuntimeError):
        wrapped.respond(persona, scenario, [], "a")


def test_timeout_error_message_names_slot_and_deadline():
    class Slow:
        def respond(self, persona, scenario, history, speaker_slot):
            time.sleep(0.3)
            return ("late", None)

    wrapped = TimeoutBackend(Slow(), timeout_s=0.05)
    persona, scenario = _persona_scenario()
    with pytest.raises(RoleBackendTimeoutError) as exc:
        wrapped.respond(persona, scenario, [], "a")
    assert "'a'" in str(exc.value)
    assert "0.05" in str(exc.value)
