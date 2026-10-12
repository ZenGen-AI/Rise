"""RecipeStore 测试共用的构造函数。不是测试文件(不以 test_ 开头),pytest 不收集它;
测试文件靠 pytest 默认的 prepend 导入模式把 tests/ 放进 sys.path 后直接导入。"""
from __future__ import annotations

import copy

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    planner_decision_file,
    recipe_candidate_file,
    run_dir,
)

NOW = "2026-10-09T00:00:00Z"
SHA = "a" * 64


def make_store(tmp_path) -> GitStore:
    store = GitStore(str(tmp_path))
    store.init()
    return store


def _ref(name: str) -> dict:
    return {"uri": f"file:///compute/{name}.safetensors", "sha256": SHA, "size_bytes": 1, "fetch_hint": None}


def training_config(mode: str = "batch") -> dict:
    cfg = {
        "mode": mode,
        "trained_slot": "a",
        "base_checkpoint_ref": _ref("base"),
        "teacher_model_ref": _ref("teacher"),
        "reward": {"components": ["outcome"], "attribution": "outcome_split", "combination": "sum"},
        "sft_steps": 1,
        "grpo_steps": 1,
        "sft_batch_size": 1,
        "groups_per_step": 1,
        "group_size": 2,
        "grpo_config": {"lr": 1e-5},
        "opd_coefficient": 0.1,
        "checkpoint_every_steps": 1,
        "allow_resume": True,
    }
    if mode == "batch":
        cfg["stage_manifest_refs"] = {"sft": "dsm-run-003-sft", "grpo_opd": "dsm-run-003-grpo_opd"}
    return cfg


def eval_anchor() -> dict:
    return {
        "taxonomy_version": "tax-v1",
        "item_pool_version": "pool-v1",
        "benchmark_version": "bench-v1",
        "judge_prompt_version": None,
        "scoring_config_version": None,
    }


def _common(recipe_type: str) -> dict:
    return {
        "recipe_type": recipe_type,
        "dimension": "tom",
        "source_run_id": "run-003",
        "eval_anchor": eval_anchor(),
        "result": {"gain": 0.05, "baseline_ref": "zengen-v1", "candidate_ref": "cand-run-003", "promoted": True},
        "repro": {"errors": [], "warnings": []},
        "nondeterministic": False,
    }


def batch_payload() -> dict:
    p = _common("batch")
    p["training_config"] = training_config("batch")
    p["stage_manifests"] = {
        "sft": {"id": "dsm-run-003-sft", "manifest_hash": "b" * 64},
        "grpo_opd": {"id": "dsm-run-003-grpo_opd", "manifest_hash": "c" * 64},
    }
    p["code_version"] = {"repo_commit": "0385004"}
    return p


def interact_payload() -> dict:
    p = _common("interact")
    p["nondeterministic"] = True
    p["training_config"] = training_config("interact")
    p["scenario_ids"] = ["scen-1"]
    p["bindings"] = [
        {"slot_id": "a", "persona_id": "p-1", "model_backend": "self_play"},
        {"slot_id": "b", "persona_id": "p-2", "model_backend": "frozen_reference", "model_ref": "zengen-v1"},
    ]
    p["code_version"] = {"repo_commit": "0385004"}
    return p


def agent_rsi_payload() -> dict:
    p = _common("agent_rsi")
    p["bound_model_id"] = "zengen-v2"
    p["scenario_ids"] = ["scen-1"]
    p["persona_ids"] = ["p-1"]
    p["analyzer_config"] = {"min_support": 3, "min_ratio": 0.5}
    p["verifier_config"] = {"method": "replay_behavior"}
    p["generator_version"] = "rule-v1"
    p["uses_bridge"] = False
    return p


def envelope(id_: str, type_: str, status: str, payload: dict, links: list[str] | None = None) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=1, links=list(links or []), payload=copy.deepcopy(payload),
    )


def l1(dimension: str, score: float, *, level: str = "l1") -> dict:
    return {
        "dimension": dimension, "level": level, "score": score, "ci_low": max(0.0, score - 0.05),
        "ci_high": min(1.0, score + 0.05), "sample_size": 40, "track_provenance": "static",
    }


def write_report(
    store: GitStore,
    run_id: str,
    *,
    scores: list[dict] | None = None,
    baseline_ref: str = "",
    flags: list[dict] | None = None,
    benchmark_version: str = "bench-v1",
    stub: bool = False,
    backends: str | None = "mock",
) -> None:
    """按 diagnostic.datamodel.DiagnosticReport.to_dict() 的形状手工写一份诊断报告;
    stub=True 时写成 simulator.stubs.diagnostic_evaluator_stub 的形状。
    backends=None 不写标记,controlplane.evidence 按失败关闭判 stub;要一份 real 报告传 backends="real"。"""
    if stub:
        payload = {"candidate_ref": f"cand-{run_id}", "benchmark_version": benchmark_version,
                   "dimension_count": 0, "stub": True}
    else:
        payload = {
            "id": run_id, "candidate_ref": f"cand-{run_id}", "baseline_ref": baseline_ref,
            "benchmark_version": benchmark_version, "run_tier": "quick", "sampling_k": 1,
            "dimension_scores": list(scores or []), "error_profiles": [],
            "regression_flags": {"flags": list(flags or [])},
            "consistency_signals": {"rule_vs_judge_divergence": {}, "diagnostic_vs_reward_divergence": 0.0},
            "links": [run_id],
        }
        if backends is not None:
            payload["backends"] = backends
    store.write_product(
        diagnostic_report_file(run_id),
        envelope(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]),
        f"test: report {run_id}",
    )


def write_model(store: GitStore, n: int, run_id: str, *, status: str = "effective") -> None:
    """与 controlplane.daemon.apply_action 的 promote_candidate 写法一致:links=[候选 id, run id]。"""
    store.write_product(
        model_file(n),
        envelope(f"zengen-v{n}", "zengen_model", status, {"promoted_from": f"cand-{run_id}"},
                 [f"cand-{run_id}", run_id]),
        f"test: model v{n}",
    )


def write_candidate_checkpoint(store: GitStore, run_id: str, status: str) -> None:
    store.write_product(
        candidate_checkpoint_file(run_id),
        envelope(f"cand-{run_id}", "candidate_checkpoint", status, {"checkpoint_ref": _ref(run_id)}, [run_id]),
        f"test: candidate {run_id}",
    )


def write_directive(store: GitStore, run_id: str, *, recipe_ref: str = "", status: str = "executed",
                    planner_decision_ref: str | None = None) -> None:
    """planner_decision_ref 为 None 时不写这个键(人工指令);计划 5 的指令写 planner-<源 run>。"""
    payload = {"target_dimension": "tom", "recipe_ref": recipe_ref}
    if planner_decision_ref is not None:
        payload["planner_decision_ref"] = planner_decision_ref
    store.write_product(
        f"{run_dir(run_id)}/directive.yaml",
        envelope(run_id, "directive", status, payload),
        f"test: directive {run_id}",
    )


def write_planner_decision(store: GitStore, source_run_id: str, next_run_id: str, action: str,
                           *, status: str = "executed") -> None:
    """规划决策放在触发规划的那个 run 下(设计 §4.1),next_run_id 指向它产出的指令。"""
    store.write_product(
        planner_decision_file(source_run_id),
        envelope(f"planner-{source_run_id}", "planner_decision", status, {
            "source_run_id": source_run_id, "next_run_id": next_run_id, "template_version": "v1",
            "backend": "heuristic",
            "decision": {"target_dimension": "tom", "action": action, "mode": None,
                         "source_recipe_id": "recipe-run-003", "perturbation": {},
                         "data_synthesis_hint": ""},
            "rationale": "test", "failure": None,
        }),
        f"test: decision {source_run_id}",
    )


def write_recipe_candidate(store: GitStore, run_id: str, status: str, payload: dict | None = None) -> None:
    body = batch_payload() if payload is None else payload
    body = copy.deepcopy(body)
    body["source_run_id"] = run_id
    store.write_product(
        recipe_candidate_file(run_id),
        envelope(f"recipe-{run_id}", "recipe_candidate", status, body, [run_id, f"cand-{run_id}"]),
        f"test: recipe candidate {run_id}",
    )
