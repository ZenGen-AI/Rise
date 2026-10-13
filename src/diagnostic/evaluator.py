"""真实诊断评估器(闭环集成设计 §10.2):调和循环 request_diagnostic 这一步的可注入替身。

形状与 controlplane.daemon.Evaluator 相同:(store, run_id, candidate_id) -> None,
把报告写到 diagnostic_report_file(run_id)。diagnostic 只依赖 controlplane 与 simulator,
不导入 mindtrainer。V1 只跑静态轨(dynamic_specs 恒为空)。
"""
from __future__ import annotations

import re
from collections.abc import Callable

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from diagnostic.assets import read_item_pool, read_taxonomy
from diagnostic.datamodel import DimensionScore
from diagnostic.orchestrator import DiagnosticConfig, run_diagnostic
from diagnostic.paths import diagnostic_report_file
from diagnostic.scorer import JudgeBackend
from diagnostic.someval_adapter import SoMEvalRunner
from diagnostic.store_adapter import BACKENDS_VALUES

NO_BASELINE = "none"

_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")


def _latest_effective_model(store: GitStore):
    """版本号(按数字)最大、状态 effective 的 zengen 模型信封;没有则 None。"""
    found = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = _MODEL_FILE.search(rel)
        if match:
            found.append((int(match.group(1)), rel))
    for _, rel in sorted(found, reverse=True):
        env = store.read_product(rel)
        if env.status == "effective":
            return env
    return None


def resolve_baseline(
    store: GitStore, run_id: str, benchmark_version: str, *, backends: str
) -> tuple[str, list[DimensionScore]]:
    """基线 = 最新已晋级模型所在 run 的诊断报告。返回 (baseline_ref, baseline_scores)。

    以下情况都没有可比基线,返回 ("none", []):没有已晋级模型;模型的 links 里没有 run id
    (promote_candidate 写的是 [候选 id, run id]);那个 run 就是本次被评的 run;那个 run 没有报告;
    报告等级是 stub(没有分数);报告的 dimension_scores 不是非空列表(mock 报告可能只带 backends
    标记而没有分数);报告的 benchmark_version 与本次不同(跨版本分数不可比);报告的证据等级与本次 backends 不同
    (real 对 mock,真分数与假分数不可比)。
    可比性只看 benchmark_version:报告载荷里没有题库版本,换题库必须同时换 benchmark_version。"""
    model = _latest_effective_model(store)
    if model is None or len(model.links) < 2:
        return NO_BASELINE, []
    # links = [候选 id, run id](promote_candidate);取第二项。计划 3 的 recipestore.gain.baseline_run_of
    # 取"cand-<x> 也在 links 里的那一项"(以该函数为准,不按文档行号引用),两种判法目前一致,
    # 改晋级写法时两处要一起改,计划 9 的一致性核对会加测试钉住。
    base_run = model.links[1]
    rel = diagnostic_report_file(base_run)
    if base_run == run_id or not store.exists(rel):
        return NO_BASELINE, []
    payload = store.read_product(rel).payload
    grade = diagnostic_grade(payload)
    if grade == "stub" or payload.get("benchmark_version") != benchmark_version:
        return NO_BASELINE, []
    if grade != backends:
        return NO_BASELINE, []
    # diagnostic_grade 对 backends:"mock" 不看分数就返回 mock,所以这里不能假定键在
    scores = payload.get("dimension_scores")
    if not isinstance(scores, list) or not scores:
        return NO_BASELINE, []
    return model.id, [DimensionScore.from_dict(d) for d in scores]


def make_diagnostic_evaluator(
    *,
    taxonomy_version: str,
    item_pool_version: str,
    someval_runner: SoMEvalRunner,
    judge_backend: JudgeBackend,
    benchmark_version: str,
    tier: str = "full",
    sampling_k: int = 4,
    backends: str = "mock",
) -> Callable[[GitStore, str, str], None]:
    """返回 Evaluator。分类表与题库每次调用时从树里读(diagnostic.assets),缺了抛
    DiagnosticAssetError(瞬态语义:原样上抛,守护循环退出,补齐资产后重启即可)。
    backends 由调用方声明("mock" 或 "real"),写进报告载荷,controlplane.evidence 据此定等级。"""
    if backends not in BACKENDS_VALUES:
        raise ValueError(f"backends must be one of {BACKENDS_VALUES}, got {backends!r}")

    def evaluate(store: GitStore, run_id: str, candidate_id: str) -> None:
        taxonomy = read_taxonomy(store, taxonomy_version)
        pool = read_item_pool(store, item_pool_version)
        baseline_ref, baseline_scores = resolve_baseline(store, run_id, benchmark_version, backends=backends)
        cfg = DiagnosticConfig(
            run_id=run_id,
            candidate_ref=candidate_id,
            candidate_model_id=candidate_id,
            baseline_ref=baseline_ref,
            tier=tier,
            sampling_k=sampling_k,
            benchmark_version=benchmark_version,
        )
        run_diagnostic(
            cfg, taxonomy, pool, someval_runner, judge_backend, store,
            baseline_scores=baseline_scores, dynamic_specs=[], backends=backends,
        )

    return evaluate
