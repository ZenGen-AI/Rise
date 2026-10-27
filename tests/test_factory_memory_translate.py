from __future__ import annotations

import pytest

from actio.memory import TupleMemory
from actio.memory_port import MemoryClosedError, MemoryEventConflict, MemoryFailureError
from factory.memory_translate import (
    FactoryMemoryFailureError, FactoryMemoryUnavailableError, MemoryTranslatingBackend, memory_role_backend,
)
from factory.opponents import OpponentApiError
from simulator.backends import ScriptedBackend
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError


class _Instance:
    """假的 Actio 实例:respond 抛给定异常或返回三元组;close 记次数,可抛。"""
    def __init__(self, raises=None, close_raises=None):
        self.raises, self.close_raises, self.closed = raises, close_raises, 0

    def respond(self, persona, scenario, history, speaker_slot):
        if self.raises is not None:
            raise self.raises
        return "u", None, {"schema": "actio.trace.v2"}

    def close(self):
        self.closed += 1
        if self.close_raises is not None:
            raise self.close_raises


def test_a_transient_memory_failure_becomes_the_unavailable_error_and_names_only_the_class():
    cause = MemoryFailureError("https://embed.example/v1 timed out", transient=True)
    with pytest.raises(FactoryMemoryUnavailableError) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert isinstance(caught.value, RoleMemoryUnavailableError)
    assert caught.value.__cause__ is cause
    assert "MemoryFailureError" in str(caught.value) and "'b'" in str(caught.value)
    assert "embed.example" not in str(caught.value)


@pytest.mark.parametrize("cause", [
    MemoryFailureError("disk", transient=False), MemoryEventConflict("k#0"), MemoryClosedError("closed"),
])
def test_every_other_memory_failure_becomes_the_failure_error(cause):
    with pytest.raises(FactoryMemoryFailureError) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert isinstance(caught.value, RoleMemoryFailureError)
    assert type(cause).__name__ in str(caught.value)


@pytest.mark.parametrize("cause", [OpponentApiError("429"), ValueError("bad"), RoleMemoryUnavailableError("x")])
def test_non_memory_errors_pass_through_as_the_same_object(cause):
    with pytest.raises(type(cause)) as caught:
        MemoryTranslatingBackend(_Instance(raises=cause)).respond(None, None, [], "b")
    assert caught.value is cause


def test_the_reply_passes_through_and_close_goes_to_the_instance():
    inst = _Instance()
    backend = MemoryTranslatingBackend(inst)
    assert backend.respond(None, None, [], "b") == ("u", None, {"schema": "actio.trace.v2"})
    backend.close()
    assert inst.closed == 1 and backend.instance is inst


def test_a_memory_failure_while_closing_is_translated_too():
    inst = _Instance(close_raises=MemoryClosedError("closed"))
    with pytest.raises(FactoryMemoryFailureError):
        MemoryTranslatingBackend(inst).close()


def test_memory_role_backend_builds_an_actio_instance_that_writes_v2_traces():
    scripted = ScriptedBackend({"b": ["hi"]})
    backend = memory_role_backend(scripted, memory=TupleMemory(), episode_id="ep-x", interlocutors={"a": "pa"})
    utterance, _, trace = backend.respond(None, None, [], "b")
    assert utterance == "hi" and trace["schema"] == "actio.trace.v2"
    assert backend.instance.backend.inner is scripted       # 只有 respond 的底座被套了 IgnoreGuidance


def test_closing_twice_is_harmless_on_a_real_instance():
    backend = memory_role_backend(
        ScriptedBackend({"b": ["hi"]}), memory=TupleMemory(), episode_id="ep-x", interlocutors={},
    )
    backend.close()
    backend.close()


def test_a_closed_port_surfaces_as_the_failure_error_without_the_original_text():
    backend = memory_role_backend(
        ScriptedBackend({"b": ["hi"]}), memory=TupleMemory(), episode_id="ep-x", interlocutors={},
    )
    backend.close()
    with pytest.raises(FactoryMemoryFailureError) as caught:
        backend.respond(None, None, [], "b")
    assert "MemoryClosedError" in str(caught.value) and "'b'" in str(caught.value)
