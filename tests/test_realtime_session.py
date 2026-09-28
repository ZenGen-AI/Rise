import pytest

from simulator.realtime.session import (
    SessionState,
    can_transition,
    next_states,
    assert_transition,
    InvalidSessionTransitionError,
)


def test_created_to_active():
    assert can_transition(SessionState.CREATED, SessionState.ACTIVE)


def test_active_to_completing_to_closed():
    assert can_transition(SessionState.ACTIVE, SessionState.COMPLETING)
    assert can_transition(SessionState.COMPLETING, SessionState.CLOSED)


def test_failed_reachable_from_created_active_completing():
    assert can_transition(SessionState.CREATED, SessionState.FAILED)
    assert can_transition(SessionState.ACTIVE, SessionState.FAILED)
    assert can_transition(SessionState.COMPLETING, SessionState.FAILED)


def test_closed_and_failed_are_terminal():
    assert next_states(SessionState.CLOSED) == set()
    assert next_states(SessionState.FAILED) == set()


def test_illegal_transition_rejected():
    assert not can_transition(SessionState.CREATED, SessionState.COMPLETING)
    assert not can_transition(SessionState.CLOSED, SessionState.ACTIVE)


def test_assert_transition_raises_with_states_named():
    with pytest.raises(InvalidSessionTransitionError) as exc:
        assert_transition(SessionState.CLOSED, SessionState.ACTIVE)
    assert "CLOSED" in str(exc.value)
    assert "ACTIVE" in str(exc.value)


def test_assert_transition_allows_legal():
    assert_transition(SessionState.CREATED, SessionState.ACTIVE)  # 不抛异常
