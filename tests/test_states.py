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


def test_approved_can_be_rejected_only_by_the_admit_handler():
    # 设计二第 9 节"门② 失败"(F23):抽取失败的记忆提案 approved → rejected
    assert can_transition("review", "approved", "rejected")
    assert next_states("review", "approved") == {"effective", "rejected"}


def test_the_cli_table_deliberately_lacks_the_admit_only_edge():
    # CLI 的 cli/src/commands.ts 表是给人用的:人不能把已批准的提案改回拒绝,只有收录处理器能
    import re
    from pathlib import Path
    ts = (Path(__file__).resolve().parents[1] / "cli/src/commands.ts").read_text(encoding="utf-8")
    assert re.search(r'^\s*approved: \["effective"\],$', ts, re.MULTILINE)
