"""Top-level diagnostic orchestrator (§2.5 合流点).

Wires together the modules from Tasks 1-13 into a single end-to-end
diagnostic run: load item pool -> static track (SoMEval adapter + rule
scoring) + dynamic track (run_episode + judge scoring) -> error
classification -> L3->L2->L1 aggregation (track provenance + fusion) ->
regression detection -> report assembly -> write to ControlPlane.

This module is a THIN coordinator: it contains no statistical or
decision logic of its own, only calls into the pure functions defined
in the other diagnostic.* modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from controlplane.gitstore import GitStore
from diagnostic.aggregator import aggregate_l1, aggregate_l2, aggregate_l3, fuse_tracks
from diagnostic.datamodel import (
    ConsistencySignals,
    DiagnosticItem,
    DiagnosticReport,
    DiagnosticTaxonomy,
    DimensionScore,
    ItemResult,
)
from diagnostic.error_classifier import (
    aggregate_error_profile,
    classify_dynamic_consistency,
    classify_static_family,
)
from diagnostic.dynamic_track import run_dynamic_item
from diagnostic.regression import detect_regressions
from diagnostic.report import build_report
from diagnostic.scorer import JudgeBackend, LLMJudgeScorer, RuleScorer
from diagnostic.someval_adapter import SoMEvalRunner, run_static_items, run_static_items_scored
from diagnostic.store_adapter import write_report
from diagnostic.taxonomy_loader import ItemPool, select_items, variant_families_in


@dataclass
class DiagnosticConfig:
    run_id: str
    candidate_ref: str
    candidate_model_id: str
    baseline_ref: str
    tier: str
    sampling_k: int
    benchmark_version: str


def run_diagnostic(
    cfg: DiagnosticConfig,
    taxonomy: DiagnosticTaxonomy,
    pool: ItemPool,
    someval_runner: SoMEvalRunner,
    judge_backend: JudgeBackend,
    store: GitStore,
    *,
    baseline_scores: list[DimensionScore],
    dynamic_specs: list[dict],
    backends: str,
    someval: dict | None = None,
) -> DiagnosticReport:
    """Run one full diagnostic pass and persist the resulting report.

    backends ("mock" | "real") is written into the persisted payload by
    write_report in the same write (R17); required, no default (DX-12).
    someval, when given, is written into the payload verbatim as a record-only
    block (protocol, sample counts, error tallies); diagnostic_grade ignores it."""

    # 1. Select items for this run's tier.
    selected_items = select_items(pool, cfg.tier)
    static_items = [item for item in selected_items if item.track == "static"]

    # 2. Static track: SoMEval adapter, then re-score rule-mode items with
    # RuleScorer to guarantee consistency (run_static_items' internal rule
    # hit is an exact-match shortcut; judge-mode static routing is
    # deferred -- out of scope for V1 fixtures, which are all rule-mode).
    static_results: dict[str, ItemResult] = {}
    rule_items = [item for item in static_items if item.scoring_mode != "someval"]
    scored_items = [item for item in static_items if item.scoring_mode == "someval"]
    if rule_items:
        raw_results = run_static_items(
            rule_items, someval_runner, cfg.candidate_ref, cfg.sampling_k
        )
        rule_scorer = RuleScorer()
        for item, result in zip(rule_items, raw_results):
            if item.scoring_mode == "rule":
                result = rule_scorer.score_samples(item, result.raw_outputs)
            static_results[item.id] = result
    if scored_items:
        # SoMEval 已经判好:选项被打乱、答案以文本存放,规则打分器对它不成立(DX-16)。
        if not hasattr(someval_runner, "evaluate_scored"):
            raise ValueError(
                "the item pool has someval items but the runner has no evaluate_scored(); "
                "refusing before running anything"
            )
        for item, result in zip(
            scored_items,
            run_static_items_scored(scored_items, someval_runner, cfg.candidate_ref, cfg.sampling_k),
        ):
            static_results[item.id] = result
        # 运行器跑完才有统计(协议、样本数、错误计数);调用方显式给了就以调用方为准。
        if someval is None and hasattr(someval_runner, "last_stats"):
            someval = someval_runner.last_stats()

    # 3. Dynamic track: run_episode + judge scoring of candidate utterances.
    # Each spec carries item + scenario/assignment/personas/backends; the
    # caller guarantees the assignment has a self_play binding. Empty
    # dynamic_specs -> pure static run, this whole block is skipped.
    dynamic_items: dict[str, DiagnosticItem] = {}
    dynamic_results: dict[str, ItemResult] = {}
    dynamic_utterances: dict[str, list[str]] = {}
    if dynamic_specs:
        llm_judge = LLMJudgeScorer(judge_backend, cfg.candidate_model_id)
        for spec in dynamic_specs:
            item: DiagnosticItem = spec["item"]
            outcome = run_dynamic_item(
                item,
                spec["scenario"],
                spec["assignment"],
                spec["personas"],
                spec["backends"],
                spec.get("episode_id", f"{cfg.run_id}-{item.id}"),
            )
            utterances = [
                outcome.episode.turns[i].utterance
                for i in outcome.candidate_turn_indices
            ]
            dynamic_items[item.id] = item
            dynamic_results[item.id] = llm_judge.score_samples(item, utterances)
            dynamic_utterances[item.id] = utterances

    static_items_by_id = {item.id: item for item in static_items}

    # 4. Error classification: static via variant families, dynamic via
    # utterance-consistency; both grouped into per-L2 ErrorProfiles.
    error_profiles = []
    static_families = variant_families_in(pool, list(static_items_by_id))
    static_labels_by_l2: dict[str, list[str]] = {}
    for family in static_families:
        label = classify_static_family(family, static_results)
        base_item = static_items_by_id[family.base_item_id]
        l2 = _l2_for_l3_task(taxonomy, base_item.l3_task)
        if l2 is not None:
            static_labels_by_l2.setdefault(l2, []).append(label)
    for l2, labels in static_labels_by_l2.items():
        error_profiles.append(aggregate_error_profile(l2, labels, "static"))

    dynamic_labels_by_l2: dict[str, list[str]] = {}
    for item_id, utterances in dynamic_utterances.items():
        label = classify_dynamic_consistency(utterances, lambda u: True)
        if label is not None:
            item = dynamic_items[item_id]
            l2 = _l2_for_l3_task(taxonomy, item.l3_task)
            if l2 is not None:
                dynamic_labels_by_l2.setdefault(l2, []).append(label)
    for l2, labels in dynamic_labels_by_l2.items():
        error_profiles.append(aggregate_error_profile(l2, labels, "dynamic"))

    # 5. Aggregation L3 -> L2 -> L1, per track, fusing dimensions present
    # in both tracks. Track provenance is recorded truthfully throughout.
    dimension_scores: list[DimensionScore] = []

    static_l3 = _aggregate_l3(taxonomy, static_items_by_id, static_results, "static")
    dynamic_l3 = _aggregate_l3(taxonomy, dynamic_items, dynamic_results, "dynamic")
    dimension_scores.extend(static_l3.values())
    dimension_scores.extend(dynamic_l3.values())

    static_l2 = _aggregate_l2(taxonomy, static_l3, "static")
    dynamic_l2 = _aggregate_l2(taxonomy, dynamic_l3, "dynamic")

    l2_final: dict[str, DimensionScore] = {}
    all_l2s = {l2 for l2s in taxonomy.l2_dimensions.values() for l2 in l2s}
    for l2 in all_l2s:
        s, d = static_l2.get(l2), dynamic_l2.get(l2)
        if s is not None and d is not None:
            fused = fuse_tracks(s, d)
            dimension_scores.append(fused)
            l2_final[l2] = fused
        elif s is not None:
            dimension_scores.append(s)
            l2_final[l2] = s
        elif d is not None:
            dimension_scores.append(d)
            l2_final[l2] = d

    for l1 in taxonomy.l1_dimensions:
        members = [l2_final[l2] for l2 in taxonomy.l2_dimensions[l1] if l2 in l2_final]
        if not members:
            continue
        tracks = {m.track_provenance for m in members}
        track = tracks.pop() if len(tracks) == 1 else "fused"
        dimension_scores.append(aggregate_l1(l1, members, taxonomy, track))

    # 6. Regression detection against baseline (empty baseline -> no flags).
    regression_flags = detect_regressions(
        dimension_scores, baseline_scores, baseline_version=cfg.baseline_ref
    )

    # 7. Consistency signals: no rule-vs-judge divergence or reward data
    # available in this V1 run -> empty/zero, filled in truthfully when
    # such data exists.
    consistency_signals = ConsistencySignals({}, 0.0)

    # 8. Assemble and persist the report.
    report = build_report(
        report_id=cfg.run_id,
        candidate_ref=cfg.candidate_ref,
        baseline_ref=cfg.baseline_ref,
        benchmark_version=cfg.benchmark_version,
        run_tier=cfg.tier,
        sampling_k=cfg.sampling_k,
        dimension_scores=dimension_scores,
        error_profiles=error_profiles,
        regression_flags=regression_flags,
        consistency_signals=consistency_signals,
        run_id=cfg.run_id,
    )
    write_report(store, cfg.run_id, report, backends=backends, extra={"someval": someval} if someval else None)
    return report


def _l2_for_l3_task(taxonomy: DiagnosticTaxonomy, l3_task: str) -> str | None:
    """Find the L2 dimension that owns a given L3 task, per taxonomy.l3_tasks."""
    for l2, l3_tasks in taxonomy.l3_tasks.items():
        if l3_task in l3_tasks:
            return l2
    return None


def _aggregate_l3(
    taxonomy: DiagnosticTaxonomy,
    items_by_id: dict[str, DiagnosticItem],
    results_by_id: dict[str, ItemResult],
    track: str,
) -> dict[str, DimensionScore]:
    """Group ItemResults by their item's l3_task and aggregate_l3 per task."""
    results_by_l3_task: dict[str, list[ItemResult]] = {}
    for item_id, result in results_by_id.items():
        l3_task = items_by_id[item_id].l3_task
        results_by_l3_task.setdefault(l3_task, []).append(result)
    return {
        l3_task: aggregate_l3(l3_task, results, track)
        for l3_task, results in results_by_l3_task.items()
    }


def _aggregate_l2(
    taxonomy: DiagnosticTaxonomy,
    l3_scores_by_task: dict[str, DimensionScore],
    track: str,
) -> dict[str, DimensionScore]:
    """Roll up per-track L3 scores into per-track L2 scores."""
    l3_scores = list(l3_scores_by_task.values())
    result: dict[str, DimensionScore] = {}
    all_l2s = {l2 for l2s in taxonomy.l2_dimensions.values() for l2 in l2s}
    for l2 in all_l2s:
        members = [ds for ds in l3_scores if ds.dimension in set(taxonomy.l3_tasks[l2])]
        if members:
            result[l2] = aggregate_l2(l2, l3_scores, taxonomy, track)
    return result
