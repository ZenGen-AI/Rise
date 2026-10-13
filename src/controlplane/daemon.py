from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping

import yaml

from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.lock import acquire_repo_lock
from controlplane.migration import UnknownSchemaVersionError
from controlplane.models import max_model_version
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    agent_round_file,
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    nomination_file,
    planner_decision_file,
    proposal_file,
    recipe_track_note_file,
    run_dir,
)
from controlplane.reconcile import (
    CLOSED_LOOP_ACTIONS,
    DirectiveView,
    CandidateView,
    GateView,
    ModelView,
    RunView,
    TreeState,
    Action,
    reconcile,
)
from controlplane.states import assert_transition
from controlplane.validate import EnvelopeValidationError
from simulator.stubs import diagnostic_evaluator_stub, execute_dispatch

_NOW = "2026-09-26T00:00:00Z"

# 派发器:拿到 pending 的 directive,把它做成产物(候选等)。返回即视为派发成功,调和循环随后
# 把 directive 置 executed。状态迁移归调和循环,派发器不碰 directive 的状态。
Dispatcher = Callable[[GitStore, Envelope], None]

# 闭环集成设计 §3.3(R2):闭环动作的处理器。处理器自己负责写终态记录;确定性失败写自己产物的
# failed 与失败记录,瞬态异常原样上抛(与派发器同一约定,守护循环会退出)。
Handler = Callable[[GitStore, Action], None]
# 诊断评估器:(store, run_id, candidate_id),把诊断报告写到 diagnostic_report_file(run_id)。
Evaluator = Callable[[GitStore, str, str], None]

_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")


class DirectiveFailedError(Exception):
    """派发器用它说"这条指令确定性地做不成,重试无益"。调和循环把 directive 置 failed
    (pending -> failed,非审核类状态机允许),原因写进 payload["failure"],之后不再派发。
    其余异常原样上抛、directive 保持 pending:下一轮重新派发,续训与次数上限由派发器自己兜底。"""

    def __init__(self, reason: str, *, category: str = "T2") -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category


def stub_dispatcher(store: GitStore, directive: Envelope) -> None:
    """默认派发器:骨架里的桩,行为与引入派发缝之前逐字节相同。"""
    execute_dispatch(store, directive.id, "tom")


def _iter_run_ids(store: GitStore) -> list[str]:
    runs_dir = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if p.is_dir())


def read_tree_state(
    store: GitStore,
    *,
    enabled: frozenset[str] = frozenset(),
    accept_mock_evidence: bool = False,
) -> TreeState:
    """扫 git 树，投影成 reconcile() 需要的纯视图。

    enabled 为空时只读既有的三项,返回值与引入闭环视图之前逐字节相同(accept_mock_evidence
    此时不起作用:没有闭环动作,它无处可用)。非空时才去填 runs / models / gates。"""
    directives: list[DirectiveView] = []
    candidates: list[CandidateView] = []
    for run_id in _iter_run_ids(store):
        d_path = f"{run_dir(run_id)}/directive.yaml"
        if store.exists(d_path):
            d = store.read_product(d_path)
            directives.append(
                DirectiveView(
                    id=d.id,
                    status=d.status,
                    target_dimension=d.payload.get("target_dimension", ""),
                    recipe_ref=d.payload.get("recipe_ref", ""),
                )
            )
        c_path = f"{run_dir(run_id)}/candidate_checkpoint.yaml"
        if store.exists(c_path):
            c = store.read_product(c_path)
            candidates.append(
                CandidateView(
                    id=c.id, status=c.status, run_id=run_id,
                    has_diagnostic=store.exists(diagnostic_report_file(run_id)),
                )
            )
    model_count = len(store.list_products(f"{CONTROL_PLANE_ROOT}/models"))
    top_version = max_model_version(store)
    if not enabled:
        return TreeState(
            directives=directives, candidates=candidates, model_count=model_count,
            max_model_version=top_version,
        )
    return TreeState(
        directives=directives, candidates=candidates, model_count=model_count,
        max_model_version=top_version,
        runs=_read_run_views(store), models=_read_model_views(store), gates=_read_gate_views(store),
        enabled=frozenset(enabled), accept_mock_evidence=accept_mock_evidence,
    )


def _status_or_none(store: GitStore, rel: str) -> str | None:
    return store.read_product(rel).status if store.exists(rel) else None


def _read_run_views(store: GitStore) -> list[RunView]:
    # directive.yaml 的路径 ↔ cli/src/commands.ts 的 DIRECTIVE_PATTERN(confirm 与 status 读它)
    views: list[RunView] = []
    for run_id in _iter_run_ids(store):
        d_path = f"{run_dir(run_id)}/directive.yaml"
        directive = store.read_product(d_path) if store.exists(d_path) else None
        recipe_ref = directive.payload.get("recipe_ref") if directive is not None else None
        report_path = diagnostic_report_file(run_id)
        has_diagnostic = store.exists(report_path)
        views.append(RunView(
            run_id=run_id,
            directive_status=directive.status if directive is not None else None,
            # 人写的指令里 recipe_ref 恒为 "";None 或非字符串一律当作"没有配方"
            recipe_ref=recipe_ref if isinstance(recipe_ref, str) else "",
            candidate_status=_status_or_none(store, candidate_checkpoint_file(run_id)),
            has_diagnostic=has_diagnostic,
            diagnostic_grade=_report_grade(store, report_path) if has_diagnostic else None,
            planner_decision_status=_status_or_none(store, planner_decision_file(run_id)),
            has_nomination_note=store.exists(nomination_file(run_id)),
            has_track_note=store.exists(recipe_track_note_file(run_id)),
        ))
    return views


def _report_grade(store: GitStore, rel: str) -> str:
    """诊断报告的证据等级。等级只有一处实现(controlplane.evidence),这里不另写判断;
    但"判不出来按最弱算":载荷不是 dict、或报告文件读不出(与 _UNREADABLE_ROUND 同一组异常),
    都记为 stub(永不可接受),不让一份坏报告在读视图时把守护进程带走。"""
    try:
        payload = store.read_product(rel).payload
    except _UNREADABLE_ROUND:
        return "stub"
    return diagnostic_grade(payload) if isinstance(payload, dict) else "stub"


def _read_model_views(store: GitStore) -> list[ModelView]:
    views: list[ModelView] = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = _MODEL_FILE.search(rel)
        if match is None:
            continue
        env = store.read_product(rel)
        # promote_candidate 写的 links 是 [候选 id, run id]:第二项就是产生该模型的 run。
        # 计划 3 的 recipestore.gain.baseline_run_of 对同一问题取"cand-<x> 也在 links 里的那一项"
        # (以该函数为准,不按文档行号引用);两种判法目前一致,
        # 改晋级写法时两处要一起改,计划 9 的一致性核对会加测试钉住。
        run_id = env.links[1] if len(env.links) >= 2 else ""
        views.append(ModelView(
            model_id=env.id, version=int(match.group(1)), run_id=run_id, status=env.status,
            agent_round_status=_agent_round_status(store, env.id),
        ))
    return views


# 轮次产物"文件在、内容读不出"时的异常:YAML 解析失败(含合并冲突标记)、缺键、类型不对、
# 信封校验失败、schema_version 无法迁移。OSError 不在其中,原样上抛(瞬态,守护进程退出)。
_UNREADABLE_ROUND = (KeyError, TypeError, AttributeError, yaml.YAMLError,
                     EnvelopeValidationError, UnknownSchemaVersionError)


def _agent_round_status(store: GitStore, model_id: str) -> str | None:
    """轮次产物的信封状态;没有产物或产物读不出时为 None(R19:按"没有可用轮次"处理,让
    start_agent_round 再次触发,由轮次处理器重入时判定损坏并自己写 failed)。"""
    rel = agent_round_file(model_id)
    if not store.exists(rel):
        return None
    try:
        return store.read_product(rel).status
    except _UNREADABLE_ROUND:
        return None


def _read_gate_views(store: GitStore) -> list[GateView]:
    views: list[GateView] = []
    # 配方候选:recipe_candidates/<id>.yaml,顶层文件,list_products 够用
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/recipe_candidates"):
        env = store.read_product(rel)
        views.append(GateView(id=env.id, kind="recipe_candidate", status=env.status))
    # 技能提案在子目录里(skill_memory_proposals/<id>/proposal.yaml)。list_products 不递归,
    # 所以像 _iter_run_ids 那样自己遍历目录;路径仍由 proposal_file 拼(文件夹名就是产物 id)。
    # 两类路径模式 ↔ cli/src/commands.ts 的 GATE_PATTERNS。
    proposals_dir = store.repo_root / CONTROL_PLANE_ROOT / "skill_memory_proposals"
    if proposals_dir.is_dir():
        for sub in sorted(p for p in proposals_dir.iterdir() if p.is_dir()):
            rel = proposal_file(sub.name)
            if store.exists(rel):
                env = store.read_product(rel)
                views.append(GateView(id=env.id, kind="skill_memory_proposal", status=env.status))
    return views


def _check_handlers(handlers: Mapping[str, Handler] | None) -> frozenset[str]:
    if not handlers:
        return frozenset()
    unknown = set(handlers) - CLOSED_LOOP_ACTIONS
    if unknown:
        raise ValueError(
            f"handlers only cover closed-loop actions {sorted(CLOSED_LOOP_ACTIONS)}; "
            f"got unknown kinds {sorted(unknown)}"
        )
    return frozenset(handlers)


def _recorded_model_version(store: GitStore, candidate_id: str) -> int | None:
    """models/ 下已经登记过这个候选的模型的版本号;没有返回 None(控制面 CP-7)。
    晋级在"模型已写、候选还没置 effective"时崩溃,重放就靠它认出上一次写下的那个模型。"""
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = _MODEL_FILE.search(rel)
        if match and store.read_product(rel).payload.get("promoted_from") == candidate_id:
            return int(match.group(1))
    return None


def apply_action(
    store: GitStore,
    action: Action,
    *,
    dispatcher: Dispatcher | None = None,
    handlers: Mapping[str, Handler] | None = None,
    evaluator: Evaluator | None = None,
) -> None:
    _check_handlers(handlers)
    if handlers and action.kind in handlers:
        # 闭环动作:处理器异常原样上抛,不吞
        handlers[action.kind](store, action)
        return
    if action.kind == "dispatch_directive":
        run_id = action.target_id
        path = f"{run_dir(run_id)}/directive.yaml"
        try:
            (dispatcher or stub_dispatcher)(store, store.read_product(path))
        except DirectiveFailedError as exc:
            directive = store.read_product(path)
            assert_transition("nonreview", directive.status, "failed")
            directive.status = "failed"
            directive.payload["failure"] = {"category": exc.category, "reason": exc.reason}
            store.write_product(
                path, directive, f"reconcile: directive {run_id} failed ({exc.category})"
            )
            return
        directive = store.read_product(f"{run_dir(run_id)}/directive.yaml")
        assert_transition("nonreview", directive.status, "executed")
        directive.status = "executed"
        store.write_product(
            f"{run_dir(run_id)}/directive.yaml", directive, f"reconcile: dispatched {run_id}"
        )
    elif action.kind == "promote_candidate":
        run_id = action.detail["run_id"]
        # 崩溃重放(CP-7):这个候选已经登记过模型,就复用它的版本号(不再登记新版本);
        # 找不到才按 reconcile 给的版本号新登记。
        recorded = _recorded_model_version(store, action.target_id)
        version = action.detail["model_version"] if recorded is None else recorded
        cand = store.read_product(f"{run_dir(run_id)}/candidate_checkpoint.yaml")
        # 1) 登记 ZenGen 版本产物（仅引用）。把候选的 checkpoint_ref 带过来：
        # 下一轮训练的基座要从"最新已晋级模型"解析（controlplane.models），
        # 不能再靠人手填。候选没有该引用时不编造——解析侧会明确报错。
        model_payload: dict = {"promoted_from": action.target_id}
        if "checkpoint_ref" in cand.payload:
            model_payload["checkpoint_ref"] = cand.payload["checkpoint_ref"]
        model = Envelope(
            id=f"zengen-v{version}",
            type="zengen_model",
            version=1,
            created_at=_NOW,
            created_by=OwnerRef(),
            status="effective",
            schema_version=1,
            links=[action.target_id, run_id],
            payload=model_payload,
        )
        # 复用时这一次写也照发:内容与已提交的逐字节相同,write_product 什么都不做;
        # 若上次崩在 git add 与 git commit 之间,模型文件还停在暂存区,这一次把它提交掉
        # (提交只带本文件,不会有别的提交替它收尾)。
        store.write_product(model_file(version), model, f"reconcile: promote zengen-v{version}")
        # 2) 候选 approved → effective
        assert_transition("review", cand.status, "effective")
        cand.status = "effective"
        store.write_product(
            f"{run_dir(run_id)}/candidate_checkpoint.yaml", cand, f"reconcile: {run_id} effective"
        )
        # 3) 写本轮结果摘要，供下一轮决策读取
        outcome = Envelope(
            id=f"outcome-{run_id}",
            type="outcome",
            version=1,
            created_at=_NOW,
            created_by=OwnerRef(),
            status="effective",
            schema_version=1,
            links=[action.target_id, f"zengen-v{version}"],
            payload={"promoted_model": f"zengen-v{version}"},
        )
        store.write_product(f"{run_dir(run_id)}/outcome.yaml", outcome, f"reconcile: outcome {run_id}")
    elif action.kind == "request_diagnostic":
        run_id = action.detail["run_id"]
        if evaluator is not None:
            # 注入的评估器只替换这一步:它自己把报告写到 diagnostic_report_file(run_id)
            evaluator(store, run_id, action.target_id)
            return
        # 骨架里计算层由 stub 顶替（同 execute_dispatch）。stub 报告带 stub 标记，
        # 审核门 review 会醒目标出，不会被误当成真诊断。真实诊断见 diagnostic.orchestrator。
        report = diagnostic_evaluator_stub(run_id, action.target_id)
        store.write_product(
            diagnostic_report_file(run_id), report,
            f"reconcile: diagnostic for {action.target_id}",
        )
    # await_review / noop：无自动动作，等待人工


def reconcile_once(
    store: GitStore,
    *,
    dispatcher: Dispatcher | None = None,
    handlers: Mapping[str, Handler] | None = None,
    evaluator: Evaluator | None = None,
    accept_mock_evidence: bool = False,
) -> list[Action]:
    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。
    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。
    handlers 的键就是启用的闭环动作(闭环集成设计 §3.3);没有处理器的动作种类不会出现在
    返回列表里。evaluator 缺省为诊断桩;注入后只替换 request_diagnostic 这一步。"""
    enabled = _check_handlers(handlers)
    state = read_tree_state(store, enabled=enabled, accept_mock_evidence=accept_mock_evidence)
    actions = reconcile(state)
    for action in actions:
        apply_action(store, action, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator)
    return actions


def run_daemon(
    store: GitStore,
    interval_s: float,
    max_ticks: int | None = None,
    *,
    dispatcher: Dispatcher | None = None,
    handlers: Mapping[str, Handler] | None = None,
    evaluator: Evaluator | None = None,
    accept_mock_evidence: bool = False,
) -> None:
    """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。
    同一棵树只许一个守护进程写(控制面 CP-12):第一个 tick 之前取仓库文件锁,
    已被占用就抛 RepoLockedError,一个产物都不写;退出(含异常)时释放。"""
    _check_handlers(handlers)
    with acquire_repo_lock(store.repo_root):
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            reconcile_once(
                store, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator,
                accept_mock_evidence=accept_mock_evidence,
            )
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(interval_s)
