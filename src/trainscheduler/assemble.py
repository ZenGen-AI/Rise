"""装配规划上下文(设计 §7.2):从树里读出源 run 的候选、诊断、配方、近期决策与近 K 轮成本增益,
产出 trainplanner.context.PlanningContext。只读,不写树。"""
from __future__ import annotations

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, planner_decision_file, run_dir, run_trace_file
from trainplanner.context import (
    CANDIDATE_TERMINAL_STATES,
    RECIPE_TYPES,
    CandidateSummary,
    CostGainEntry,
    DecisionOutcome,
    DiagnosticSummary,
    ErrorProfileSummary,
    InvalidContextError,
    PlanningContext,
    RecipeSummary,
    RegressionMark,
)
from trainscheduler.config import SchedulerConfig, load_scheduler_config
from trainscheduler.errors import ContextAssemblyError
from trainscheduler.recipes_port import compute_gain, find_recipes, read_report_payload, regression_flags
from trainscheduler.run_ids import list_run_dirs, run_order_key


def directive_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/directive.yaml"


def _candidate(store: GitStore, run_id: str) -> CandidateSummary:
    path = candidate_checkpoint_file(run_id)
    if not store.exists(path):
        raise ContextAssemblyError(f"{run_id} has no candidate checkpoint")
    env = store.read_product(path)
    if env.status not in CANDIDATE_TERMINAL_STATES:
        raise ContextAssemblyError(
            f"candidate {env.id} is {env.status}; planning needs one of {list(CANDIDATE_TERMINAL_STATES)}"
        )
    reason = env.payload.get("reject_reason") if env.status == "rejected" else None
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    return CandidateSummary(id=env.id, status=env.status, reject_reason=reason)


def _number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def regression_mark(flag: dict) -> RegressionMark:
    """把诊断报告的一条退化标记归一化成 {kind, subject, magnitude}(计划 4 的契约)。"""
    kind = flag.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ContextAssemblyError(f"regression flag has no kind: {flag!r}")
    if kind == "social_regression":
        subject, magnitude = flag.get("dimension"), _number(flag.get("drop"))
    elif kind == "g_probe_failure":
        subject, magnitude = flag.get("probe"), _number(flag.get("score"))
    elif kind == "reward_hacking_suspected":
        reward, diag = _number(flag.get("reward_delta")), _number(flag.get("diag_delta"))
        subject = "reward"
        magnitude = reward - diag if reward is not None and diag is not None else None
    else:
        subject = flag.get("dimension") or flag.get("probe") or kind
        magnitude = next((m for m in (_number(flag.get(k)) for k in ("drop", "score")) if m is not None), 0.0)
    if not isinstance(subject, str) or not subject or magnitude is None:
        raise ContextAssemblyError(f"regression flag of kind {kind!r} is malformed: {flag!r}")
    return RegressionMark(kind=kind, subject=subject, magnitude=magnitude)


NO_ERROR_LABEL = ""


def drop_no_error_label(distribution: object) -> object:
    """设计 R21:diagnostic.error_classifier 对"基题与变体都答对"的变体族给空标签 "",
    aggregate_error_profile 把它照常数进分布。空标签的意思是"没有错误",不是一种错误类型;
    计划 4 的 PlanningContext 拒绝空键。这里把它丢掉,不重新归一化(剩下的和可以小于 1)。
    不是映射时原样返回,交给 ErrorProfileSummary 报形状错。"""
    if not isinstance(distribution, dict):
        return distribution
    return {k: v for k, v in distribution.items() if k != NO_ERROR_LABEL}


def l1_scores(payload: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    raw = payload.get("dimension_scores")
    if not isinstance(raw, list):
        raise ContextAssemblyError("diagnostic report has no dimension_scores list")
    for item in raw:
        if not isinstance(item, dict) or item.get("level") != "l1":
            continue
        dim, score = item.get("dimension"), _number(item.get("score"))
        if not isinstance(dim, str) or not dim or score is None:
            raise ContextAssemblyError(f"malformed l1 score in diagnostic report: {item!r}")
        if dim in scores:
            raise ContextAssemblyError(f"diagnostic report has more than one l1 score for {dim!r}")
        scores[dim] = score
    if not scores:
        raise ContextAssemblyError("diagnostic report has no l1 scores")
    return scores


def _diagnostic(store: GitStore, run_id: str) -> DiagnosticSummary:
    payload = read_report_payload(store, run_id)
    if payload is None:
        raise ContextAssemblyError(f"{run_id} has no diagnostic report")
    grade = diagnostic_grade(payload)
    if grade == "stub":
        raise ContextAssemblyError(f"diagnostic report for {run_id} is graded stub; stub evidence cannot drive planning")
    profiles = []
    raw_profiles = payload.get("error_profiles", [])
    if not isinstance(raw_profiles, list):
        raise ContextAssemblyError("diagnostic report error_profiles must be a list")
    for p in raw_profiles:
        if not isinstance(p, dict):
            raise ContextAssemblyError(f"malformed error profile: {p!r}")
        profiles.append(
            ErrorProfileSummary(
                l2_dimension=p.get("l2_dimension"),
                error_type_distribution=drop_no_error_label(p.get("error_type_distribution") or {}),
            )
        )
    return DiagnosticSummary(
        l1_scores=l1_scores(payload),
        regression_flags=[regression_mark(f) for f in regression_flags(payload)],
        error_profiles=profiles,
        benchmark_version=payload.get("benchmark_version"),
        grade=grade,
    )


def _recipes(store: GitStore) -> list[RecipeSummary]:
    out = []
    for view in find_recipes(store):
        if view.recipe_type not in RECIPE_TYPES or not view.dimension:
            continue  # 形状不对的配方 clone 不了,也不该让它挡住整轮规划
        out.append(
            RecipeSummary(
                id=view.id, recipe_type=view.recipe_type, dimension=view.dimension,
                status=view.status, mean_gain=view.mean_gain, n_reuse=view.n_reuse,
            )
        )
    return out


def decision_is_executed(store: GitStore, env) -> bool:
    """executed,或 pending 而它的 next_run_id 指令已在树里(崩在"指令已写、决策未标记"之间,设计 §4.1)。"""
    if env.status == "executed":
        return True
    next_run = env.payload.get("next_run_id")
    return (
        env.status == "pending"
        and isinstance(next_run, str)
        and bool(next_run)
        and store.exists(directive_file(next_run))
    )


def _ordered_runs_up_to(store: GitStore, run_id: str) -> list[str]:
    limit = run_order_key(run_id)
    return [r for r in sorted(list_run_dirs(store), key=run_order_key) if run_order_key(r) <= limit]


def _recent_decisions(store: GitStore, runs: list[str], window: int) -> list[DecisionOutcome]:
    out: list[DecisionOutcome] = []
    for source in runs:
        path = planner_decision_file(source)
        if not store.exists(path):
            continue
        env = store.read_product(path)
        if not decision_is_executed(store, env):
            continue
        decision = env.payload.get("decision") or {}
        next_run = env.payload["next_run_id"]
        cpath = candidate_checkpoint_file(next_run)
        candidate_status = store.read_product(cpath).status if store.exists(cpath) else None
        dimension = decision.get("target_dimension")
        gain = compute_gain(store, next_run, dimension).gain if candidate_status else None
        out.append(
            DecisionOutcome(
                decision_id=env.id, next_run_id=next_run, target_dimension=dimension,
                action=decision.get("action"), source_recipe_id=decision.get("source_recipe_id"),
                candidate_status=candidate_status, gain=gain,
            )
        )
    return out[-window:]


def _summed(records: list[dict], key: str, cast):
    """有记录才求和;一条都没有这个键就是 None(未记录),不是 0。"""
    values = [r[key] for r in records if key in r]
    return cast(sum(values)) if values else None


def _cost_gain(store: GitStore, runs: list[str], window: int) -> list[CostGainEntry]:
    out: list[CostGainEntry] = []
    for run_id in runs:
        dpath = directive_file(run_id)
        if not store.exists(dpath):
            continue
        dimension = store.read_product(dpath).payload.get("target_dimension")
        if not isinstance(dimension, str) or not dimension:
            continue
        records: list[dict] = []
        if store.exists(run_trace_file(run_id)):
            records = list(store.read_product(run_trace_file(run_id)).payload.get("cost_records", []))
        out.append(
            CostGainEntry(
                run_id=run_id, target_dimension=dimension,
                gpu_hours=_summed(records, "gpu_hours", float),
                tokens=_summed(records, "tokens", int),
                gain=compute_gain(store, run_id, dimension).gain,
            )
        )
    return out[-window:]


def assemble_context(
    store: GitStore, run_id: str, *, config: SchedulerConfig | None = None
) -> PlanningContext:
    """config 缺省时从树里读 scheduler.yaml(窗口大小 K 来自它)。"""
    config = config if config is not None else load_scheduler_config(store)
    try:
        runs = _ordered_runs_up_to(store, run_id)
        return PlanningContext(
            run_id=run_id,
            candidate=_candidate(store, run_id),
            diagnostic=_diagnostic(store, run_id),
            recipes=_recipes(store),
            recent_decisions=_recent_decisions(store, runs, config.recent_decision_window),
            cost_gain=_cost_gain(store, runs, config.cost_gain_window),
        )
    except InvalidContextError as exc:
        raise ContextAssemblyError(f"cannot assemble planning context for {run_id}: {exc}") from exc
