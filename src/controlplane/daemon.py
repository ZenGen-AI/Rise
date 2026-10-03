from __future__ import annotations

import time

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


def apply_action(store: GitStore, action: Action) -> None:
    if action.kind == "dispatch_directive":
        run_id = action.target_id
        execute_dispatch(store, run_id, "tom")
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


def reconcile_once(store: GitStore) -> list[Action]:
    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。"""
    state = read_tree_state(store)
    actions = reconcile(state)
    for action in actions:
        apply_action(store, action)
    return actions


def run_daemon(store: GitStore, interval_s: float, max_ticks: int | None = None) -> None:
    """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。"""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        reconcile_once(store)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(interval_s)
