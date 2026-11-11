from __future__ import annotations

import re
from dataclasses import dataclass, field

from controlplane.evidence import evidence_acceptable


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


@dataclass(frozen=True)
class RunView:
    """闭环集成设计 §3.1:一个 run 目录在闭环动作眼里的样子。只在有处理器被注入时才填。"""

    run_id: str
    directive_status: str | None
    recipe_ref: str
    candidate_status: str | None
    has_diagnostic: bool
    diagnostic_grade: str | None  # "stub" | "mock" | "real" | None(没有报告)
    planner_decision_status: str | None
    has_nomination_note: bool
    has_track_note: bool


@dataclass(frozen=True)
class ModelView:
    model_id: str
    version: int
    run_id: str
    status: str
    # 轮次产物(agent_round_file(model_id))的信封状态;None 表示没有可用的轮次产物
    agent_round_status: str | None

    @property
    def has_agent_round(self) -> bool:
        """派生量,恒等于 agent_round_status is not None;保留给只关心"有没有轮次"的读者。"""
        return self.agent_round_status is not None


@dataclass(frozen=True)
class GateView:
    id: str
    kind: str  # "recipe_candidate" | "skill_memory_proposal"
    status: str


# 控制面 CP-17:读不出的已提交产物在视图里的状态。任何触发条件都不匹配它,所以它本身不会被
# 派发、诊断、晋级或收录;它在 TreeState.unreadable 里的路径另外触发两条暂停规则。
UNREADABLE_STATUS = "unreadable"


@dataclass
class TreeState:
    directives: list[DirectiveView] = field(default_factory=list)
    candidates: list[CandidateView] = field(default_factory=list)
    model_count: int = 0
    # 闭环集成设计 §3.1:以下字段全部带默认值,既有构造不受影响
    runs: list[RunView] = field(default_factory=list)
    models: list[ModelView] = field(default_factory=list)
    gates: list[GateView] = field(default_factory=list)
    enabled: frozenset[str] = frozenset()
    accept_mock_evidence: bool = False
    # 控制面 CP-7:models/ 下最大的 zengen 版本号。None 表示调用方没填,版本号起点退回 model_count
    max_model_version: int | None = None
    # 控制面 CP-17:读不出的已提交产物的树内路径(排好序)。非空时闭环暂停,见 reconcile
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    kind: str
    target_id: str
    detail: dict


# 闭环集成设计 §3.2 的六个新动作。只有动作名在 state.enabled 里才会产生(R2)。
CLOSED_LOOP_ACTIONS: frozenset[str] = frozenset({
    "plan_next_round", "nominate_recipe", "record_track",
    "start_agent_round", "admit_recipe", "admit_proposal",
})

# 在途轮(R4):这些状态说明某一轮还没走完
_IN_FLIGHT_DIRECTIVE = frozenset({"pending", "awaiting_confirmation"})
_IN_FLIGHT_CANDIDATE = frozenset({"pending_review", "approved"})
_TERMINAL_CANDIDATE = frozenset({"effective", "rejected"})


def has_round_in_flight(state: TreeState) -> bool:
    """任何 run 的指令是 pending/awaiting_confirmation,或候选是 pending_review/approved。"""
    return any(
        r.directive_status in _IN_FLIGHT_DIRECTIVE or r.candidate_status in _IN_FLIGHT_CANDIDATE
        for r in state.runs
    )


# R5 / 设计 §3.2:"最大 run"按 run-<数字> 的数值序比较,不按字符串序(run-9 与 run-011、
# 超过 999 轮都会让字符串序选错)。不匹配的名字(scratch 之类)不参与比较。
# 与计划 5 分配 run id 的规则同一口径;controlplane 不导入 trainscheduler,所以这里自己写。
_RUN_ID = re.compile(r"^run-([0-9]+)\Z")   # [0-9] 而不是 \d:与计划 5 的 run_number 同一口径,避免 Unicode 数字被一边认、一边不认


def _run_order_key(run_id: str) -> tuple:
    """同种动作内的输出次序:run-<数字> 按数值在前,其余名字按字典序排在后面。
    同编号(run-7 与 run-007)再按 run id 字典序,结果与输入顺序无关。"""
    m = _RUN_ID.match(run_id)
    return (0, int(m.group(1)), run_id) if m else (1, 0, run_id)


def _latest_numbered_run(runs: list[RunView]) -> RunView | None:
    """run-<数字> 里数值最大的那个 run;一个都没有时为 None(刻意失败关闭:不规划)。
    同编号(只可能是手工命名,例如 run-7 与 run-007)时按 run id 字典序取最大,结果与输入顺序无关。"""
    numbered = [(int(m.group(1)), r) for r in runs if (m := _RUN_ID.match(r.run_id))]
    return max(numbered, key=lambda pair: (pair[0], pair[1].run_id))[1] if numbered else None


def _acceptable(state: TreeState, run: RunView) -> bool:
    return run.diagnostic_grade is not None and evidence_acceptable(
        run.diagnostic_grade, state.accept_mock_evidence
    )


def _recipe_gate_open(state: TreeState, run: RunView) -> bool:
    """规划是否可以越过配方入库门(设计 CP-11)。没启用提名、或候选被拒绝(被拒绝的候选不提名)时恒为真;
    否则要求提名标记在场(nominated 与 skipped 都写它),且这个 run 的配方候选不在门前。"""
    if "nominate_recipe" not in state.enabled or run.candidate_status != "effective":
        return True
    if not run.has_nomination_note:
        return False
    recipe_id = f"recipe-{run.run_id}"
    return not any(
        g.id == recipe_id and g.kind == "recipe_candidate" and g.status in _IN_FLIGHT_CANDIDATE
        for g in state.gates
    )


def _closed_loop_actions(state: TreeState) -> list[Action]:
    """六个新动作。输出顺序固定:admit_recipe → admit_proposal → record_track →
    nominate_recipe → plan_next_round → start_agent_round;同一种动作内:门按 id 字典序,
    run 按编号的数值序(_run_order_key)。plan_next_round 至多一条,只针对编号最大的
    run-<数字>,见 _latest_numbered_run。
    detail 只是提示,处理器必须自己重读树(动作列表来自 tick 开头的快照)。"""
    on = state.enabled
    out: list[Action] = []
    gates = sorted(state.gates, key=lambda g: g.id)
    # 只决定 record_track / nominate_recipe 的输出顺序。按编号的数值序(字典序会把 run-10
    # 排在 run-2 前面);不用来选"最大 run",那个见 _latest_numbered_run。
    runs = sorted(state.runs, key=lambda r: _run_order_key(r.run_id))
    if "admit_recipe" in on:
        for g in gates:
            if g.kind == "recipe_candidate" and g.status == "approved":
                out.append(Action(kind="admit_recipe", target_id=g.id, detail={}))
    if "admit_proposal" in on:
        for g in gates:
            if g.kind == "skill_memory_proposal" and g.status == "approved":
                out.append(Action(kind="admit_proposal", target_id=g.id, detail={}))
    if "record_track" in on:
        for r in runs:
            if (r.recipe_ref and r.candidate_status in _TERMINAL_CANDIDATE
                    and _acceptable(state, r) and not r.has_track_note):
                out.append(Action(
                    kind="record_track", target_id=r.run_id,
                    detail={"run_id": r.run_id, "recipe_ref": r.recipe_ref},
                ))
    if "nominate_recipe" in on:
        for r in runs:
            if (r.candidate_status == "effective" and _acceptable(state, r)
                    and not r.has_nomination_note):
                out.append(Action(kind="nominate_recipe", target_id=r.run_id, detail={"run_id": r.run_id}))
    if "plan_next_round" in on and not has_round_in_flight(state):
        # R5:只看 run-<数字> 里数值最大的那个 run(在途轮仍看全部 run)。较早 run 的候选即使
        # 到了终态、证据可接受,也不会被(重复)规划;最大 run 的指令 failed、没有候选时,
        # 闭环停住等人写新指令;一个 run-<数字> 都没有时也不规划。
        latest = _latest_numbered_run(state.runs)
        if (latest is not None and latest.candidate_status in _TERMINAL_CANDIDATE
                and _acceptable(state, latest) and latest.planner_decision_status in (None, "pending")
                and _recipe_gate_open(state, latest)):
            out.append(Action(kind="plan_next_round", target_id=latest.run_id, detail={"run_id": latest.run_id}))
    if "start_agent_round" in on:
        effective = [m for m in state.models if m.status == "effective"]
        if effective:
            top = max(effective, key=lambda m: m.version)
            # R19:没有轮次产物,或轮次产物仍是 pending(上一次瞬态失败或进程被杀留下的),
            # 都要(再次)触发;只有终态 executed / failed 才算这一轮已结束。与 plan_next_round
            # 的"决策为空或 pending"同一模式,重入的幂等由轮次处理器负责。
            if top.agent_round_status in (None, "pending"):
                out.append(Action(
                    kind="start_agent_round", target_id=top.model_id,
                    detail={"version": top.version, "run_id": top.run_id},
                ))
    return out


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
    # CP-17:有模型读不出时,诊断(真实评估器要找基线模型)与晋级(重放判定要读每个模型的
    # promoted_from)都判不准,暂停;派发与等待审核不读模型,照常
    models_unreadable = any(_is_model_path(rel) for rel in state.unreadable)
    next_model_version = (
        state.max_model_version if state.max_model_version is not None else state.model_count
    )
    for d in state.directives:
        if d.status == "pending":
            actions.append(Action(kind="dispatch_directive", target_id=d.id, detail={}))
    for c in state.candidates:
        if models_unreadable and c.status in ("pending_review", "approved"):
            if c.status == "pending_review" and c.has_diagnostic:
                actions.append(Action(kind="await_review", target_id=c.id, detail={}))
            continue
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
    # CP-17:任何一份读不出就整体暂停闭环。闭环处理器读树范围很广(规划装配最近几轮与晋级基座,
    # 提名与战绩读基线模型),只挡住读不出的那个对象,处理器读到它时异常仍会漏出去
    if state.enabled and not state.unreadable:
        actions.extend(_closed_loop_actions(state))
    return actions


def _is_model_path(rel: str) -> bool:
    """models/ 下的模型产物。reconcile 是纯函数,只看路径形状,不导入 paths。"""
    parent, _, name = rel.rpartition("/")
    return parent.endswith("/models") or parent == "models"
