from __future__ import annotations


class SessionState:
    """会话生命周期状态常量(§2.6.1)。"""

    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETING = "COMPLETING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


# §2.6.1:CREATED→ACTIVE 无额外确认步骤；ACTIVE→COMPLETING→CLOSED 正常收尾；
# CREATED/ACTIVE/COMPLETING 任一阶段均可因异常转入 FAILED（§2.6.5）。
SESSION_TRANSITIONS: dict[str, set[str]] = {
    SessionState.CREATED: {SessionState.ACTIVE, SessionState.FAILED},
    SessionState.ACTIVE: {SessionState.COMPLETING, SessionState.FAILED},
    SessionState.COMPLETING: {SessionState.CLOSED, SessionState.FAILED},
    SessionState.CLOSED: set(),
    SessionState.FAILED: set(),
}


class InvalidSessionTransitionError(Exception):
    """会话状态迁移不合法。"""


def next_states(frm: str) -> set[str]:
    return set(SESSION_TRANSITIONS.get(frm, set()))


def can_transition(frm: str, to: str) -> bool:
    return to in next_states(frm)


def assert_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise InvalidSessionTransitionError(f"session: {frm} -> {to} not allowed")
