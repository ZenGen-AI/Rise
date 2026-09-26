from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DirectiveView:
    id: str
    status: str
    target_dimension: str
    recipe_ref: str


@dataclass(frozen=True)
class CandidateView:
    id: str
    status: str
    run_id: str


@dataclass
class TreeState:
    directives: list[DirectiveView] = field(default_factory=list)
    candidates: list[CandidateView] = field(default_factory=list)
    model_count: int = 0


@dataclass(frozen=True)
class Action:
    kind: str
    target_id: str
    detail: dict


def reconcile(state: TreeState) -> list[Action]:
    """纯函数：给定 git 树当前状态，返回下一步动作列表。不做任何 IO、不修改入参。

    - directive=pending → dispatch_directive（交执行器经 SSH 下发，本骨架交 stub）
    - candidate=pending_review → await_review（等待人工审核门①，无动作）
    - candidate=approved → promote_candidate（登记新 ZenGen 版本 + 写 outcome）
    """
    actions: list[Action] = []
    for d in state.directives:
        if d.status == "pending":
            actions.append(Action(kind="dispatch_directive", target_id=d.id, detail={}))
    for c in state.candidates:
        if c.status == "pending_review":
            actions.append(Action(kind="await_review", target_id=c.id, detail={}))
        elif c.status == "approved":
            actions.append(
                Action(
                    kind="promote_candidate",
                    target_id=c.id,
                    detail={"run_id": c.run_id, "model_version": state.model_count + 1},
                )
            )
    return actions
