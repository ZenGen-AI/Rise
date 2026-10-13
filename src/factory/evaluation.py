"""评估器装配(闭环集成设计 §10.2、R17):V1 注入 make_diagnostic_evaluator + mock 后端。

评估器配置是信封 control-plane/config/evaluator.yaml,载荷恰好五个键:
taxonomy_version、item_pool_version、benchmark_version、tier、sampling_k。
backends 标记不进配置,恒为 "mock":V1 只有 MockSoMEvalRunner 与 MockJudgeBackend,
标成 real 就是谎报证据等级。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from diagnostic.assets import DiagnosticAssetError, read_item_pool, read_taxonomy
from diagnostic.evaluator import make_diagnostic_evaluator
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from factory.configio import read_tree_config
from factory.errors import FactoryConfigError

EVALUATOR_CONFIG = f"{CONTROL_PLANE_ROOT}/config/evaluator.yaml"
EVALUATOR_KEYS = ("taxonomy_version", "item_pool_version", "benchmark_version", "tier", "sampling_k")
MOCK_JUDGE_ID = "factory-mock-judge"
MOCK_WRONG = "factory-mock-wrong-answer"

Evaluator = Callable[[GitStore, str, str], None]  # 形状同 controlplane.daemon.Evaluator


def read_evaluator_config(store: GitStore) -> dict:
    try:
        payload = read_tree_config(store, EVALUATOR_CONFIG)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{EVALUATOR_CONFIG} is missing; closed-loop mode needs it to build the diagnostic evaluator"
        ) from None
    keys = set(payload)
    if keys != set(EVALUATOR_KEYS):
        raise FactoryConfigError(
            f"{EVALUATOR_CONFIG} must have exactly the keys {list(EVALUATOR_KEYS)}; "
            f"missing {sorted(set(EVALUATOR_KEYS) - keys)}, unknown {sorted(keys - set(EVALUATOR_KEYS))}"
        )
    for key in ("taxonomy_version", "item_pool_version", "benchmark_version"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise FactoryConfigError(f"{EVALUATOR_CONFIG}: {key} must be a non-empty string")
    if payload["tier"] not in ("full", "quick"):
        raise FactoryConfigError(f"{EVALUATOR_CONFIG}: tier must be 'full' or 'quick', got {payload['tier']!r}")
    k = payload["sampling_k"]
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise FactoryConfigError(f"{EVALUATOR_CONFIG}: sampling_k must be an int >= 1, got {k!r}")
    return payload


def build_mock_evaluator(store: GitStore) -> Evaluator:
    """启动时读配置与资产(缺了立刻 FactoryConfigError,不等到第一个候选);返回的评估器每次调用
    仍按计划 8 的约定从树里重读资产。

    mock SoMEval 的答案(Ruling 5):变体族的基题恒答参考答案,变体题恒答错,其余题恒答对,
    各 sampling_k 次。所以每个变体族都被归为 surface_match,错误画像里没有空标签。"""
    cfg = read_evaluator_config(store)
    try:
        read_taxonomy(store, cfg["taxonomy_version"])
        pool = read_item_pool(store, cfg["item_pool_version"])
    except DiagnosticAssetError as exc:
        raise FactoryConfigError(f"diagnostic assets: {exc}") from None
    variants = {v for fam in pool.variant_families for v in fam.variant_item_ids}
    fixed = {
        item.id: [MOCK_WRONG if item.id in variants else (item.reference_answer or "")] * cfg["sampling_k"]
        for item in pool.items
    }
    return make_diagnostic_evaluator(
        taxonomy_version=cfg["taxonomy_version"],
        item_pool_version=cfg["item_pool_version"],
        someval_runner=MockSoMEvalRunner(fixed=fixed),
        judge_backend=MockJudgeBackend(model_id=MOCK_JUDGE_ID, fixed={}),
        benchmark_version=cfg["benchmark_version"],
        tier=cfg["tier"],
        sampling_k=cfg["sampling_k"],
        backends="mock",
    )
