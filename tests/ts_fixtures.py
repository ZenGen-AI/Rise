"""TrainScheduler 测试共用的构造函数。不以 test_ 开头,pytest 不收集;测试文件靠默认的 prepend
导入模式直接 `from ts_fixtures import ...`。"""
from __future__ import annotations

import copy
import json

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    planner_decision_file,
    recipe_file,
    recipe_track_file,
    run_dir,
    run_trace_file,
)
from memory_fixtures import unresolved_memory
from trainscheduler.config import DEFAULT_TRAINING_CONFIG

NOW = "2026-10-09T00:00:00Z"
SHA = "a" * 64


def make_store(tmp_path, name: str = "repo") -> GitStore:
    store = GitStore(str(tmp_path / name))
    store.init()
    return store


def env(id_: str, type_: str, status: str, payload: dict, links: list[str] | None = None) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(), status=status,
        schema_version=1, links=list(links or []), payload=copy.deepcopy(payload),
    )


def ref(name: str) -> dict:
    return {"uri": f"file:///compute/{name}.safetensors", "sha256": SHA, "size_bytes": 1, "fetch_hint": None}


def batch_training_config(run_id: str = "run-001") -> dict:
    cfg = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    cfg["stage_manifest_refs"] = {"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"}
    return cfg


def write_directive(store: GitStore, run_id: str, *, dimension: str = "tom", status: str = "executed",
                    training_config: dict | None = None, recipe_ref: str = "", extra: dict | None = None) -> None:
    payload = {"target_dimension": dimension, "recipe_ref": recipe_ref,
               "training_config": training_config if training_config is not None else batch_training_config(run_id)}
    payload.update(extra or {})
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env(run_id, "directive", status, payload),
                        f"test: directive {run_id}")


def write_candidate(store: GitStore, run_id: str, status: str = "effective", *, reject_reason: str | None = None) -> None:
    payload = {"checkpoint_ref": ref(f"cand-{run_id}")}
    if reject_reason is not None:
        payload["reject_reason"] = reject_reason
    store.write_product(candidate_checkpoint_file(run_id),
                        env(f"cand-{run_id}", "candidate_checkpoint", status, payload, [run_id]),
                        f"test: candidate {run_id}")


def l1(dimension: str, score: float) -> dict:
    return {"dimension": dimension, "level": "l1", "score": score, "ci_low": max(0.0, score - 0.05),
            "ci_high": min(1.0, score + 0.05), "sample_size": 40, "track_provenance": "static"}


def write_report(store: GitStore, run_id: str, scores: dict[str, float] | None = None, *,
                 baseline_ref: str = "", flags: list[dict] | None = None, backends: str | None = "mock",
                 stub: bool = False, error_profiles: list[dict] | None = None,
                 benchmark_version: str = "bench-v1") -> None:
    """按 DiagnosticReport.to_dict() 的形状写报告;stub=True 时写成 diagnostic_evaluator_stub 的形状。
    backends=None 不写标记,controlplane.evidence 按失败关闭判 stub;要一份 real 报告传 backends="real"。"""
    if stub:
        payload = {"candidate_ref": f"cand-{run_id}", "benchmark_version": benchmark_version,
                   "dimension_count": 0, "stub": True}
    else:
        payload = {
            "id": run_id, "candidate_ref": f"cand-{run_id}", "baseline_ref": baseline_ref,
            "benchmark_version": benchmark_version, "run_tier": "quick", "sampling_k": 1,
            "dimension_scores": [l1(d, s) for d, s in (scores or {"tom": 0.5}).items()]
            + [{"dimension": "tom.false_belief", "level": "l2", "score": 0.4, "ci_low": 0.3,
                "ci_high": 0.5, "sample_size": 20, "track_provenance": "static"}],
            "error_profiles": list(error_profiles or []),
            "regression_flags": {"flags": list(flags or [])},
            "consistency_signals": {"rule_vs_judge_divergence": {}, "diagnostic_vs_reward_divergence": 0.0},
            "links": [run_id],
        }
        if backends is not None:
            payload["backends"] = backends
    store.write_product(diagnostic_report_file(run_id),
                        env(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]),
                        f"test: report {run_id}")


def write_model(store: GitStore, n: int, run_id: str, *, status: str = "effective") -> None:
    """与 controlplane.daemon 的 promote_candidate 一致:links=[候选 id, run id],带 checkpoint_ref。"""
    store.write_product(model_file(n),
                        env(f"zengen-v{n}", "zengen_model", status,
                            {"promoted_from": f"cand-{run_id}", "checkpoint_ref": ref(f"cand-{run_id}")},
                            [f"cand-{run_id}", run_id]),
                        f"test: model v{n}")


def write_round(store: GitStore, run_id: str, scores: dict[str, float], *, model_n: int | None = None,
                baseline_ref: str = "", candidate_status: str = "effective", backends: str | None = "mock",
                flags: list[dict] | None = None, dimension: str = "tom") -> None:
    """一整轮:指令(executed)、候选、诊断;model_n 不为 None 时再登记晋级模型。"""
    write_directive(store, run_id, dimension=dimension)
    write_candidate(store, run_id, candidate_status)
    write_report(store, run_id, scores, baseline_ref=baseline_ref, backends=backends, flags=flags)
    if model_n is not None:
        write_model(store, model_n, run_id)


def recipe_payload(run_id: str = "run-001", *, dimension: str = "tom", recipe_type: str = "batch") -> dict:
    cfg = batch_training_config(run_id)
    if recipe_type == "interact":
        cfg.update(mode="interact", stage_manifest_refs={"sft": f"dsm-{run_id}-sft"}, scenario_ids=["scen-1", "scen-2"],
                   bindings=[{"slot_id": "zengen", "persona_id": "p-1", "model_backend": "self_play", "model_ref": None},
                             {"slot_id": "other", "persona_id": "p-2", "model_backend": "frozen_reference",
                              "model_ref": "zengen-v1"}],
                   max_turns_per_episode=4, memory=unresolved_memory())
    return {
        "recipe_type": recipe_type, "dimension": dimension, "source_run_id": run_id,
        "eval_anchor": {"taxonomy_version": "tax-v1", "item_pool_version": "pool-v1", "benchmark_version": "bench-v1",
                        "judge_prompt_version": None, "scoring_config_version": None},
        "result": {"gain": 0.05, "baseline_ref": "zengen-v1", "candidate_ref": f"cand-{run_id}", "promoted": True},
        "repro": {"errors": [], "warnings": []},
        "nondeterministic": recipe_type == "interact",
        "training_config": cfg,
        "stage_manifests": {s: {"id": i, "manifest_hash": "b" * 64} for s, i in cfg["stage_manifest_refs"].items()},
        "code_version": {"repo_commit": "0385004"},
    }


def write_recipe(store: GitStore, run_id: str, *, status: str = "effective", payload: dict | None = None,
                 entries: list[dict] | None = None) -> str:
    """按计划 3 的收录写法写一条配方(recipe.yaml + track_record.yaml),返回配方 id。"""
    rid = f"recipe-{run_id}"
    body = payload if payload is not None else recipe_payload(run_id)
    store.write_product(recipe_file(run_id), env(rid, "recipe", status, body, [run_id]), f"test: recipe {rid}")
    store.write_product(recipe_track_file(run_id),
                        env(f"track-{rid}", "recipe_track_record", "executed",
                            {"recipe_id": rid, "entries": list(entries or [])}, [rid]),
                        f"test: track {rid}")
    return rid


def write_trace(store: GitStore, run_id: str, records: list[dict]) -> None:
    store.write_product(run_trace_file(run_id),
                        env(f"trace-{run_id}", "run_trace", "executed", {"cost_records": records}, [run_id]),
                        f"test: trace {run_id}")


def write_decision(store: GitStore, source: str, next_run: str | None, *, status: str = "executed",
                   action: str = "new", dimension: str = "tom", source_recipe_id: str | None = None,
                   mode: str | None = "batch", perturbation: dict | None = None) -> None:
    payload = {
        "source_run_id": source, "next_run_id": next_run, "template_version": "v1", "backend": "scripted",
        "decision": {"target_dimension": dimension, "action": action, "mode": mode,
                     "source_recipe_id": source_recipe_id, "perturbation": dict(perturbation or {}),
                     "data_synthesis_hint": ""},
        "rationale": "test", "failure": None,
    }
    store.write_product(planner_decision_file(source),
                        env(f"planner-{source}", "planner_decision", status, payload, [source]),
                        f"test: decision {source}")


def decision_json(*, action: str = "new", dimension: str = "tom", mode: str | None = "batch",
                  source_recipe_id: str | None = None, perturbation: dict | None = None,
                  rationale: str = "because the evidence says so") -> str:
    if action != "new" and mode == "batch":
        mode = None
    return json.dumps({
        "target_dimension": dimension, "action": action, "mode": mode,
        "source_recipe_id": source_recipe_id, "perturbation": dict(perturbation or {}),
        "data_synthesis_hint": "", "rationale": rationale,
    })
