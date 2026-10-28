from __future__ import annotations

REVIEW = "review"
NONREVIEW = "nonreview"

# spec §1.3 需审核类：draft → pending_review → approved → effective →（deprecated|superseded）
#                       pending_review → rejected
# approved → rejected 只许收录处理器在记忆抽取失败时走(设计二第 9 节,F23);
# cli/src/commands.ts 的 REVIEW_TRANSITIONS 有意不加,人不能把已批准的提案改回拒绝。
REVIEW_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_review"},
    "pending_review": {"approved", "rejected"},
    "approved": {"effective", "rejected"},
    "effective": {"deprecated", "superseded"},
    "rejected": set(),
    "superseded": set(),
    "deprecated": set(),
}

# spec §1.3 非审核类：pending → executed → consumed；pending → failed
# 闭环集成设计 §3.4(R3):调度器构造的指令先写 awaiting_confirmation,人确认后变 pending;
# 放弃则 failed。reconcile 不为这个状态产生任何动作。
# 迁移表按产物种类(非审核类)定义,所以它对所有非审核产物都合法;目前只有指令使用它。
NONREVIEW_TRANSITIONS: dict[str, set[str]] = {
    "awaiting_confirmation": {"pending", "failed"},
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
