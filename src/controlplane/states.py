from __future__ import annotations

REVIEW = "review"
NONREVIEW = "nonreview"

# spec §1.3 需审核类：draft → pending_review → approved → effective →（deprecated|superseded）
#                       pending_review → rejected
REVIEW_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_review"},
    "pending_review": {"approved", "rejected"},
    "approved": {"effective"},
    "effective": {"deprecated", "superseded"},
    "rejected": set(),
    "superseded": set(),
    "deprecated": set(),
}

# spec §1.3 非审核类：pending → executed → consumed；pending → failed
NONREVIEW_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"executed", "failed"},
    "executed": {"consumed"},
    "consumed": set(),
    "failed": set(),
}


class InvalidTransitionError(Exception):
    """产物状态迁移不合法。"""


def _table(kind: str) -> dict[str, set[str]]:
    if kind == REVIEW:
        return REVIEW_TRANSITIONS
    if kind == NONREVIEW:
        return NONREVIEW_TRANSITIONS
    raise ValueError(f"unknown product kind: {kind}")


def next_states(kind: str, frm: str) -> set[str]:
    return set(_table(kind).get(frm, set()))


def can_transition(kind: str, frm: str, to: str) -> bool:
    return to in next_states(kind, frm)


def assert_transition(kind: str, frm: str, to: str) -> None:
    if not can_transition(kind, frm, to):
        raise InvalidTransitionError(f"{kind}: {frm} -> {to} not allowed")
