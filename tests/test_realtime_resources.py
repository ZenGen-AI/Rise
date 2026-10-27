import dataclasses
import shutil
import threading
import time

import pytest

from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime import backend_timeout
from simulator.realtime.backend_timeout import (
    RoleBackendRejectedError, RoleBackendTimeoutError, RoleBackendUnavailableError,
    RoleMemoryFailureError, RoleMemoryUnavailableError, TimeoutBackend,
)
from simulator.realtime.resources import SessionClosingError, SessionInfo, SessionResources


def _args():
    scenario = Scenario("s1", "bg", [RoleSlot("a", ""), RoleSlot("b", "")], "c", [], "training")
    return Persona("pb", ["t"], "m", "s", ["v"]), scenario, [], "b"


class _Blocking:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def respond(self, persona, scenario, history, speaker_slot):
        self.entered.set()
        self.release.wait(5)
        return "late", None


def test_session_info_is_frozen_and_carries_both_ids():
    persona, scenario, _, _ = _args()
    assignment = RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")], 4)
    info = SessionInfo(scenario, assignment, {"pb": persona}, "a", "sess-0123456789ab", "ep-sess-0123456789ab")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.episode_id = "other"


def test_after_quiesce_a_wrapped_respond_raises_without_reaching_the_backend():
    calls = []

    class Recording:
        def respond(self, *args):
            calls.append(args)
            return "x", None

    resources = SessionResources(backends={"b": Recording()})
    wrapped = TimeoutBackend(resources.backends["b"], 1.0, resources)
    assert wrapped.respond(*_args()) == ("x", None)
    resources.quiesce()
    with pytest.raises(SessionClosingError):
        wrapped.respond(*_args())
    with pytest.raises(SessionClosingError):
        resources.enter_call()
    assert len(calls) == 1


def test_a_timed_out_call_stays_in_flight_until_its_thread_exits():
    inner = _Blocking()
    resources = SessionResources(backends={"b": inner})
    wrapped = TimeoutBackend(inner, 0.05, resources)
    with pytest.raises(RoleBackendTimeoutError):
        wrapped.respond(*_args())
    resources.quiesce()
    assert resources.join(0.1) is False            # 调用方不等了,线程还在集合里
    inner.release.set()
    assert resources.join(2.0) is True


def test_join_with_nothing_in_flight_returns_true_at_once():
    assert SessionResources(backends={}).join(0) is True


def test_close_runs_every_closer_once_even_when_one_raises(tmp_path):
    copy_dir = tmp_path / "sess-0123456789ab"
    copy_dir.mkdir()
    order = []

    def boom():
        order.append("boom")
        raise OSError("busy")

    def remove():
        order.append("rm")
        shutil.rmtree(copy_dir)

    resources = SessionResources(backends={}, closers=(boom, remove))
    resources.close()
    resources.close()                              # 第二次是空操作
    assert order == ["boom", "rm"]
    assert not copy_dir.exists()


def test_the_memory_error_bases_are_separate_from_the_backend_errors():
    backend_errors = (RoleBackendTimeoutError, RoleBackendRejectedError, RoleBackendUnavailableError)
    for memory_error in (RoleMemoryUnavailableError, RoleMemoryFailureError):
        assert not issubclass(memory_error, backend_errors)
    assert not issubclass(RoleMemoryUnavailableError, RoleMemoryFailureError)
    assert not issubclass(RoleMemoryFailureError, RoleMemoryUnavailableError)


# ---- 以下为补充用例:登记的时机、各条退出路径、close 的边界 ----


def test_a_normal_call_leaves_nothing_in_flight():
    inner = _Blocking()
    inner.release.set()
    resources = SessionResources(backends={"b": inner})
    wrapped = TimeoutBackend(inner, 1.0, resources)
    assert wrapped.respond(*_args()) == ("late", None)
    assert resources.join(0) is True


def test_an_inner_exception_still_releases_the_registration():
    class Boom:
        def respond(self, *args):
            raise RuntimeError("boom")

    resources = SessionResources(backends={"b": Boom()})
    wrapped = TimeoutBackend(resources.backends["b"], 1.0, resources)
    with pytest.raises(RuntimeError):
        wrapped.respond(*_args())
    assert resources.join(0) is True


def test_the_call_is_registered_before_its_thread_starts(monkeypatch):
    # 窗口:线程已起但还没登记,quiesce 之后的 join 会漏掉它。登记必须在 start() 之前。
    inner = _Blocking()
    resources = SessionResources(backends={"b": inner})
    seen = {}
    real_thread = threading.Thread

    class Spy(real_thread):
        def start(self):
            seen["settled_at_start"] = resources.join(0)
            super().start()

    monkeypatch.setattr(backend_timeout.threading, "Thread", Spy)
    wrapped = TimeoutBackend(inner, 0.05, resources)
    with pytest.raises(RoleBackendTimeoutError):
        wrapped.respond(*_args())
    assert seen["settled_at_start"] is False
    inner.release.set()
    assert resources.join(2.0) is True


def test_a_thread_that_cannot_start_does_not_leave_a_phantom_in_flight_call(monkeypatch):
    class NoStart(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(backend_timeout.threading, "Thread", NoStart)
    resources = SessionResources(backends={})
    wrapped = TimeoutBackend(_Blocking(), 1.0, resources)
    with pytest.raises(RuntimeError, match="can't start"):
        wrapped.respond(*_args())
    assert resources.join(0) is True


def test_quiesce_is_repeatable_and_lets_registered_calls_finish():
    inner = _Blocking()
    resources = SessionResources(backends={"b": inner})
    wrapped = TimeoutBackend(inner, 5.0, resources)
    result = {}

    def call():
        result["value"] = wrapped.respond(*_args())

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    assert inner.entered.wait(2.0)
    resources.quiesce()
    resources.quiesce()
    assert resources.join(0) is False              # quiesce 不打断已登记的调用
    inner.release.set()
    assert resources.join(2.0) is True
    caller.join(2.0)
    assert result["value"] == ("late", None)


def test_join_waits_without_holding_the_lock():
    # join 在等的时候,别的线程仍能 exit_call(否则永远等不到)。
    resources = SessionResources(backends={})
    resources.enter_call()
    threading.Timer(0.05, resources.exit_call).start()
    started = time.monotonic()
    assert resources.join(2.0) is True
    assert time.monotonic() - started < 1.5


def test_close_leaves_backends_alone_and_does_not_quiesce():
    backends = {"b": object()}
    resources = SessionResources(backends=backends)
    resources.close()
    assert resources.backends is backends
    assert list(backends) == ["b"]
    resources.enter_call()                          # 没 quiesce,所以还能登记
    resources.exit_call()


def test_resources_without_closers_close_cleanly():
    resources = SessionResources(backends={})
    resources.close()
    resources.close()
