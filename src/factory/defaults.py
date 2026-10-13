"""闭环模式的默认资产与配置:诊断分类表、题库、评估器配置、规划提示模板。

全部经 GitStore / write_config 写入,重复安装是无操作(内容相同不提交)。
分类表与题库是最小的 V1 夹具,与计划 8 任务 95 的测试资产同形;真实题库替换它们即可。
"""
from __future__ import annotations

from controlplane.config import write_config
from controlplane.gitstore import GitStore
from controlplane.paths import scheduler_config_file, training_defaults_file
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.taxonomy_loader import ItemPool
from factory.agent import AGENT_CONFIG
from factory.evaluation import EVALUATOR_CONFIG
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
from simulator.store_adapter import write_persona, write_scenario
from trainplanner.templates import install_default_template
from trainscheduler.config import install_default_configs

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

# MindAgent 部署模拟的默认素材:三个 deployment_simulation 情境、两个人格、对手罐头台词。
# 对手第一句含 emotion_high 的触发词,让经验分析器在空库上也有可分析的信号(计划 7 任务 88 同一手法)。
DEFAULT_AGENT_SCENARIOS = tuple(
    Scenario(sid, "a customer escalates a complaint", [RoleSlot("agent", "support"), RoleSlot("user", "customer")],
             "the order arrived broken", ["stay polite"], "deployment_simulation")
    for sid in ("factory-deploy-s1", "factory-deploy-s2", "factory-deploy-s3")
)
DEFAULT_AGENT_PERSONAS = (
    Persona("factory-agent-persona", ["calm"], "resolve the issue", "plain", ["honesty"]),
    Persona("factory-user-persona", ["impatient"], "get a refund", "blunt", ["fairness"]),
)
DEFAULT_AGENT_CONFIG = {
    "scenario_ids": [s.id for s in DEFAULT_AGENT_SCENARIOS],
    "bindings": [
        RoleBinding("user", "factory-user-persona", "frozen_reference", model_ref="zengen-v1").to_dict(),
        RoleBinding("agent", "factory-agent-persona", "self_play").to_dict(),
    ],
    "opponent_lines": ["我真的很生气!!", "好吧,你说说看", "那就这样吧"],
    "max_turns": 4,
    "min_support": 3,
    "min_ratio": 0.5,
}


def _write_config_once(store: GitStore, rel: str, config_id: str, payload: dict) -> None:
    if not store.exists(rel):
        write_config(store, rel, config_id, payload, f"factory: install default {rel.rsplit('/', 1)[-1]}")


def install_defaults(store: GitStore) -> list[str]:
    """写入默认资产与配置,返回涉及的相对路径(已存在的也列出)。
    已存在的配置文件一律不覆盖:人改过的配置优先。"""
    paths = [write_taxonomy(store, DEFAULT_TAXONOMY), write_item_pool(store, DEFAULT_ITEM_POOL)]
    _write_config_once(store, EVALUATOR_CONFIG, "evaluator", dict(DEFAULT_EVALUATOR_CONFIG))
    paths.append(EVALUATOR_CONFIG)
    paths.append(install_default_template(store))
    install_default_configs(store)
    paths += [scheduler_config_file(), training_defaults_file()]
    for scenario in DEFAULT_AGENT_SCENARIOS:
        write_scenario(store, scenario)
    for persona in DEFAULT_AGENT_PERSONAS:
        write_persona(store, persona)
    _write_config_once(store, AGENT_CONFIG, "agent", DEFAULT_AGENT_CONFIG)
    paths.append(AGENT_CONFIG)
    return paths
