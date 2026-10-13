"""闭环模式的默认资产与配置:诊断分类表、题库、评估器配置、规划提示模板。

全部经 GitStore / write_config 写入,重复安装是无操作(内容相同不提交)。
分类表与题库是最小的 V1 夹具,与计划 8 任务 95 的测试资产同形;真实题库替换它们即可。
"""
from __future__ import annotations

from controlplane.config import write_config
from controlplane.gitstore import GitStore
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.taxonomy_loader import ItemPool
from factory.evaluation import EVALUATOR_CONFIG
from trainplanner.templates import install_default_template

DEFAULT_TAXONOMY = DiagnosticTaxonomy(
    "factory-tax-v1",
    ["mental_representation"],
    {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]},
    ["format_compliance"],
)
DEFAULT_ITEM_POOL = ItemPool(
    "factory-pool-v1",
    [
        DiagnosticItem("fi1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "fvf1"),
        DiagnosticItem("fi1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "fvf1"),
    ],
    [VariantFamily("fvf1", "fi1", ["fi1v"])],
)
DEFAULT_EVALUATOR_CONFIG = {
    "taxonomy_version": DEFAULT_TAXONOMY.version,
    "item_pool_version": DEFAULT_ITEM_POOL.version,
    "benchmark_version": "factory-bench-v1",
    "tier": "full",
    "sampling_k": 4,
}


def install_defaults(store: GitStore) -> list[str]:
    """写入默认资产与配置,返回写到的相对路径(已存在且相同的也列出)。
    已存在的 evaluator.yaml 不覆盖:人改过的配置优先。"""
    paths = [write_taxonomy(store, DEFAULT_TAXONOMY), write_item_pool(store, DEFAULT_ITEM_POOL)]
    if not store.exists(EVALUATOR_CONFIG):
        write_config(store, EVALUATOR_CONFIG, "evaluator", dict(DEFAULT_EVALUATOR_CONFIG),
                     "factory: install default evaluator config")
    paths.append(EVALUATOR_CONFIG)
    paths.append(install_default_template(store))
    return paths
