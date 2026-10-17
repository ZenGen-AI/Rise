"""守护进程的延后通道(真实后端设计 §7.3):DeferredError 不写树、状态不动、带退避、有重试上限。"""
from __future__ import annotations

import pytest

from controlplane.daemon import (
    DeferralBook, DeferredExhaustedError, reconcile_once, run_daemon,
)
from controlplane.deferred import DeferredError
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir


def _put(store, run_id):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _status(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml").status


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _busy(calls, *, retry_after=None):
    def dispatcher(s, d):
        calls.append(d.id)
        raise DeferredError("gpu busy", retry_after_seconds=retry_after)
    return dispatcher


def test_a_deferred_dispatch_writes_nothing_and_keeps_the_directive_pending(store):
    _put(store, "run-1")
    before, calls = _head(store), []
    reconcile_once(store, dispatcher=_busy(calls), deferrals=DeferralBook(clock=_Clock()))
    assert calls == ["run-1"]
    assert _status(store, "run-1") == "pending" and _head(store) == before


def test_without_a_book_the_error_is_caught_but_not_backed_off(store):
    _put(store, "run-1")
    calls = []
    reconcile_once(store, dispatcher=_busy(calls))
    reconcile_once(store, dispatcher=_busy(calls))
    assert calls == ["run-1", "run-1"] and _status(store, "run-1") == "pending"


def test_the_action_is_skipped_during_backoff_and_retried_after(store):
    _put(store, "run-1")
    clock, calls = _Clock(), []
    book = DeferralBook(clock=clock, default_backoff_s=30)
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    clock.now += 29
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    assert calls == ["run-1"]
    clock.now += 2
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    assert calls == ["run-1", "run-1"]


def test_retry_after_raises_the_wait_and_the_cap_lowers_it(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=30, max_backoff_s=900)
    reconcile_once(store, dispatcher=_busy([], retry_after=120), deferrals=book)
    assert book.next_try(("dispatch_directive", "run-1")) == 1000.0 + 120
    clock.now += 121
    reconcile_once(store, dispatcher=_busy([], retry_after=99999), deferrals=book)
    assert book.next_try(("dispatch_directive", "run-1")) == clock.now + 900


def test_other_directives_are_handled_while_one_is_backed_off(store):
    _put(store, "run-1")
    _put(store, "run-2")
    seen = []

    def dispatcher(s, d):
        seen.append(d.id)
        if d.id == "run-1":
            raise DeferredError("busy")
        # run-2 正常返回,由调和循环置 executed

    reconcile_once(store, dispatcher=dispatcher, deferrals=DeferralBook(clock=_Clock()))
    assert sorted(seen) == ["run-1", "run-2"]
    assert _status(store, "run-1") == "pending" and _status(store, "run-2") == "executed"


def test_a_success_clears_the_streak(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=1, max_consecutive=2)
    for _ in range(2):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
        clock.now += 5
    book.clear(("dispatch_directive", "run-1"))
    reconcile_once(store, dispatcher=_busy([]), deferrals=book)  # 不抛:连续计数已清零


def test_the_streak_cap_turns_into_a_human_problem(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=1, max_consecutive=3)
    for _ in range(3):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
        clock.now += 5
    with pytest.raises(DeferredExhaustedError, match=r"dispatch_directive run-1.*3"):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
    assert _status(store, "run-1") == "pending"


def test_the_exhausted_error_is_not_a_deferred_error():
    assert not issubclass(DeferredExhaustedError, DeferredError)


def test_run_daemon_owns_the_book_and_backs_off_across_ticks(store):
    _put(store, "run-1")
    calls = []
    run_daemon(store, 0.0, max_ticks=3, dispatcher=_busy(calls))
    assert calls == ["run-1"]  # 默认退避 30 秒:后两个 tick 都在退避期内
    assert _status(store, "run-1") == "pending"


def test_run_daemon_with_a_success_after_a_deferral_resets_the_streak(store):
    _put(store, "run-1")
    attempts = {"n": 0}

    def dispatcher(s, d):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DeferredError("busy")

    book = DeferralBook(clock=_Clock(), default_backoff_s=0)
    reconcile_once(store, dispatcher=dispatcher, deferrals=book)
    reconcile_once(store, dispatcher=dispatcher, deferrals=book)
    assert _status(store, "run-1") == "executed"
    assert book.streak(("dispatch_directive", "run-1")) == 0
