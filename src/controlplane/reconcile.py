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
    # 该 run 下是否已有诊断报告。默认 False：未知即视为"没有证据"，纯函数调用方
    # 必须显式声明证据在场，审核门才会放行（spec：证据驱动的审核门）。
    has_diagnostic: bool = False


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
    - candidate=pending_review 且无诊断报告 → request_diagnostic（先产出证据）
    - candidate=pending_review 且有诊断报告 → await_review（等待人工审核门①，无动作）
    - candidate=approved 且有诊断报告 → promote_candidate（登记新 ZenGen 版本 + 写 outcome）
    - candidate=approved 但无诊断报告 → request_diagnostic，**不晋级**：CLI 不是唯一
      能改状态的写入方，晋级的底线是"树里已有诊断报告"，不依赖审批人走了哪条路。
      （这只保证证据在场，不证明有人读过。）
    """
    actions: list[Action] = []
    next_model_version = state.model_count
    for d in state.directives:
        if d.status == "pending":
            actions.append(Action(kind="dispatch_directive", target_id=d.id, detail={}))
    for c in state.candidates:
        if c.status == "pending_review":
            if c.has_diagnostic:
                actions.append(Action(kind="await_review", target_id=c.id, detail={}))
            else:
                actions.append(
                    Action(kind="request_diagnostic", target_id=c.id, detail={"run_id": c.run_id})
                )
        elif c.status == "approved":
            if not c.has_diagnostic:
                # 不占用模型版本号：没晋级就不能消耗 zengen-vN
                actions.append(
                    Action(kind="request_diagnostic", target_id=c.id, detail={"run_id": c.run_id})
                )
                continue
            next_model_version += 1
            actions.append(
                Action(
                    kind="promote_candidate",
                    target_id=c.id,
                    detail={"run_id": c.run_id, "model_version": next_model_version},
                )
            )
    return actions
