"""plan():渲染一次提示,向后端要输出,严格解析,可选做领域校验;不合格就带着原因重新采样。

解析失败与校验违规共用一个重试上限:总尝试次数 = 1 + max_parse_retries。耗尽抛
PlannerFailedError。后端抛出的任何异常(包括 BridgeError)原样上抛,由调用方决定。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trainplanner.backends import DecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.decision import DecisionParseError, PlannerDecision, parse_decision
from trainplanner.prompt import render_prompt

Validator = Callable[[PlannerDecision], str | None]


class PlannerFailedError(Exception):
    """重试耗尽仍没有合格决策。reason 是最后一次被拒的原因,attempts 是总尝试次数,
    reasons 按顺序记下每一次被拒的原因。调用方按 T3 处理(设计 §7.2)。"""

    def __init__(self, reason: str, attempts: int, *, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(f"planner gave up after {attempts} attempts: {reason}")
        self.reason = reason
        self.attempts = attempts
        self.reasons = tuple(reasons) if reasons else (reason,)


@dataclass(frozen=True)
class PlannerResult:
    decision: PlannerDecision
    rationale: str
    template_version: str
    attempts: int
    backend: str
    rejected: tuple[str, ...] = ()

    def record_fields(self) -> dict:
        """planner_decision 产物 payload 里由规划器负责的四个键(设计 §4.2)。"""
        return {
            "template_version": self.template_version,
            "backend": self.backend,
            "decision": self.decision.decision_payload(),
            "rationale": self.rationale,
        }


def attempt_task_id(attempt: int) -> str:
    """第 attempt 次(从 1 起)后端调用的 task_id。每次尝试各一个,记账时不会被去重合并。"""
    return f"planner-attempt-{attempt}"


def plan(
    ctx: PlanningContext,
    template_text: str,
    template_version: str,
    backend: DecisionBackend,
    *,
    run_id: str,
    max_parse_retries: int = 2,
    validator: Validator | None = None,
) -> PlannerResult:
    if isinstance(max_parse_retries, bool) or not isinstance(max_parse_retries, int) or max_parse_retries < 0:
        raise ValueError(f"max_parse_retries must be a non-negative integer, got {max_parse_retries!r}")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    base = render_prompt(ctx, template_text, template_version)
    rejected: list[str] = []
    total = 1 + max_parse_retries
    for attempt in range(1, total + 1):
        packed = base.with_feedback(tuple(rejected))
        text = backend.decide(packed, run_id=run_id, task_id=attempt_task_id(attempt))
        try:
            decision = parse_decision(text)
        except DecisionParseError as exc:
            rejected.append(f"attempt {attempt}: parse error: {exc.reason}")
            continue
        if validator is not None:
            violation = validator(decision)
            if violation is not None:
                if not isinstance(violation, str) or not violation.strip():
                    raise TypeError("validator must return None or a non-empty violation string")
                rejected.append(f"attempt {attempt}: invalid decision: {violation}")
                continue
        return PlannerResult(
            decision=decision,
            rationale=decision.rationale,
            template_version=template_version,
            attempts=attempt,
            backend=backend.name,
            rejected=tuple(rejected),
        )
    raise PlannerFailedError(rejected[-1], total, reasons=tuple(rejected))
