from __future__ import annotations

import time
from collections.abc import Callable

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, run_dir, model_file, diagnostic_report_file
from controlplane.reconcile import (
    DirectiveView,
    CandidateView,
    TreeState,
    Action,
    reconcile,
)
from controlplane.states import assert_transition
from simulator.stubs import diagnostic_evaluator_stub, execute_dispatch

_NOW = "2026-09-26T00:00:00Z"

# 派发器:拿到 pending 的 directive,把它做成产物(候选等)。返回即视为派发成功,调和循环随后
# 把 directive 置 executed。状态迁移归调和循环,派发器不碰 directive 的状态。
Dispatcher = Callable[[GitStore, Envelope], None]


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


def read_tree_state(store: GitStore) -> TreeState:
    """扫 git 树，投影成 reconcile() 需要的纯视图。"""
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
    return TreeState(directives=directives, candidates=candidates, model_count=model_count)


def apply_action(
    store: GitStore, action: Action, *, dispatcher: Dispatcher | None = None
) -> None:
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
        version = action.detail["model_version"]
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
        # 骨架里计算层由 stub 顶替（同 execute_dispatch）。stub 报告带 stub 标记，
        # 审核门 review 会醒目标出，不会被误当成真诊断。真实诊断见 diagnostic.orchestrator。
        report = diagnostic_evaluator_stub(run_id, action.target_id)
        store.write_product(
            diagnostic_report_file(run_id), report,
            f"reconcile: diagnostic for {action.target_id}",
        )
    # await_review / noop：无自动动作，等待人工


def reconcile_once(store: GitStore, *, dispatcher: Dispatcher | None = None) -> list[Action]:
    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。
    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。"""
    state = read_tree_state(store)
    actions = reconcile(state)
    for action in actions:
        apply_action(store, action, dispatcher=dispatcher)
    return actions


def run_daemon(
    store: GitStore,
    interval_s: float,
    max_ticks: int | None = None,
    *,
    dispatcher: Dispatcher | None = None,
) -> None:
    """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。"""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        reconcile_once(store, dispatcher=dispatcher)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval_s)
