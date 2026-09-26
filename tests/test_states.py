import pytest

from controlplane.states import (
    can_transition,
    next_states,
    assert_transition,
    InvalidTransitionError,
)


def test_review_happy_path():
    assert can_transition("review", "draft", "pending_review")
    assert can_transition("review", "pending_review", "approved")
    assert can_transition("review", "approved", "effective")
    assert can_transition("review", "effective", "deprecated")
    assert can_transition("review", "effective", "superseded")


def test_review_reject_path():
    assert can_transition("review", "pending_review", "rejected")


def test_review_illegal_transition():
    assert not can_transition("review", "draft", "effective")
    assert not can_transition("review", "rejected", "approved")


def test_nonreview_paths():
    assert can_transition("nonreview", "pending", "executed")
    assert can_transition("nonreview", "pending", "failed")
    assert can_transition("nonreview", "executed", "consumed")
    assert not can_transition("nonreview", "consumed", "executed")


def test_next_states():
    assert next_states("review", "pending_review") == {"approved", "rejected"}


def test_assert_transition_raises():
    with pytest.raises(InvalidTransitionError):
        assert_transition("review", "draft", "effective")
