"""增益的唯一实现(闭环集成设计 §11):候选 run 的一级维度分数减去基线 run 的同维度分数。

提名(TrainScheduler)与战绩回写(本包)都调 compute_gain,不得各写一份。
本包不导入 diagnostic:报告按 DiagnosticReport.to_dict() 的键名直接读 payload 字典。
任何一环缺失都返回 gain=None 并在 reason 写明缺什么,绝不编造数字。
证据等级一律用 controlplane.evidence.diagnostic_grade(设计 §11 唯一实现),本包不另判。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file, model_file

_MODEL_ID = re.compile(r"^zengen-v(\d+)$")

OK = "ok"


@dataclass(frozen=True)
class GainResult:
    gain: float | None
    baseline_run_id: str | None
    reason: str


def read_report_payload(store: GitStore, run_id: str) -> dict | None:
    """该 run 的诊断报告 payload;文件不在返回 None。"""
    path = diagnostic_report_file(run_id)
    if not store.exists(path):
        return None
    return store.read_product(path).payload


def l1_score(payload: dict, dimension: str) -> tuple[float | None, str]:
    """取 level 为 l1 且 dimension 匹配的 DimensionScore.score。返回 (分数, 原因)。"""
    scores = payload.get("dimension_scores")
    if not isinstance(scores, list):
        return None, "report has no dimension_scores list"
    hits = [
        s for s in scores
        if isinstance(s, dict) and s.get("level") == "l1" and s.get("dimension") == dimension
    ]
    if not hits:
        return None, f"report has no l1 score for dimension {dimension!r}"
    if len(hits) > 1:
        return None, f"report has {len(hits)} l1 scores for dimension {dimension!r}; refusing to pick one"
    score = hits[0].get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None, f"l1 score for dimension {dimension!r} is not a number"
    if not math.isfinite(score):
        # NaN 与任何数比较都为假:gain < min_gain 会把它当成达标
        return None, f"l1 score for dimension {dimension!r} is not a finite number"
    return float(score), OK


def regression_flags(payload: dict) -> list[dict]:
    """报告的退化标记列表(diagnostic.regression 写的 social_regression 与 g_probe_failure)。
    没有该字段(stub 报告)返回空列表。"""
    raw = payload.get("regression_flags")
    if not isinstance(raw, dict):
        return []
    flags = raw.get("flags")
    if not isinstance(flags, list):
        return []
    return [f for f in flags if isinstance(f, dict)]


def baseline_run_of(store: GitStore, baseline_ref: str) -> tuple[str | None, str]:
    """zengen-vN → 产出它的 run id。

    controlplane.daemon 晋级时写 links=[候选 id, run id],候选 id 恒为 cand-<run>;
    所以取 links 里满足"cand-<它> 也在 links 里"的那一项,不靠位置猜。
    注意:计划 8 的 read_tree_state 与诊断评估器对同一问题取 links[1]
    (docs/superpowers/plans/2026-10-09-reconcile-extension.md 第 43、1258、2313 行)。
    两种判法在 promote_candidate 的产物上结果相同;改晋级写法时两处要一起改。"""
    match = _MODEL_ID.match(baseline_ref or "")
    if match is None:
        return None, f"baseline_ref {baseline_ref!r} is not a zengen-vN model id"
    path = model_file(int(match.group(1)))
    if not store.exists(path):
        return None, f"baseline model {baseline_ref} not found"
    links = store.read_product(path).links
    runs = [link for link in links if f"cand-{link}" in links]
    if len(runs) != 1:
        return None, f"baseline model {baseline_ref} links do not name exactly one producing run"
    return runs[0], OK


def compute_gain(store: GitStore, run_id: str, dimension: str) -> GainResult:
    cand = read_report_payload(store, run_id)
    if cand is None:
        return GainResult(None, None, f"no diagnostic report for {run_id}")
    cand_grade = diagnostic_grade(cand)
    if cand_grade == "stub":
        return GainResult(None, None, f"diagnostic report for {run_id} is a stub")
    cand_score, why = l1_score(cand, dimension)
    if cand_score is None:
        return GainResult(None, None, f"{run_id}: {why}")
    baseline_ref = cand.get("baseline_ref")
    if not isinstance(baseline_ref, str) or not baseline_ref:
        return GainResult(None, None, f"diagnostic report for {run_id} has no baseline_ref")
    base_run, why = baseline_run_of(store, baseline_ref)
    if base_run is None:
        return GainResult(None, None, why)
    if base_run == run_id:
        return GainResult(None, base_run, f"baseline {baseline_ref} was produced by {run_id} itself")
    base = read_report_payload(store, base_run)
    if base is None:
        return GainResult(None, base_run, f"no diagnostic report for baseline run {base_run}")
    base_grade = diagnostic_grade(base)
    if base_grade == "stub":
        return GainResult(None, base_run, f"diagnostic report for baseline run {base_run} is a stub")
    if cand_grade == "real" and base_grade != "real":
        # real 候选减 mock 基线:增益一半来自 mock 分数,不能让它按 real 证据驱动提名(R17)
        return GainResult(
            None, base_run,
            f"baseline run {base_run} evidence is {base_grade} while {run_id} is real; scores are not comparable",
        )
    if cand.get("benchmark_version") != base.get("benchmark_version"):
        return GainResult(
            None, base_run,
            f"benchmark versions differ ({cand.get('benchmark_version')!r} vs "
            f"{base.get('benchmark_version')!r}); scores are not comparable",
        )
    base_score, why = l1_score(base, dimension)
    if base_score is None:
        return GainResult(None, base_run, f"baseline run {base_run}: {why}")
    return GainResult(cand_score - base_score, base_run, OK)
