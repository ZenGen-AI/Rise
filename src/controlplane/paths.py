from __future__ import annotations

CONTROL_PLANE_ROOT = "control-plane"
SUBDIRS = ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios", "personas", "diagnostics"]


def run_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/runs/{run_id}"


def model_file(n: int) -> str:
    return f"{CONTROL_PLANE_ROOT}/models/zengen-v{n}.yaml"


def backends_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/backends.yaml"


def scenario_file(scenario_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/scenarios/{scenario_id}.yaml"


def persona_file(persona_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/personas/{persona_id}.yaml"


def episodes_dir(run_id: str) -> str:
    return f"{run_dir(run_id)}/episodes"


def episode_file(run_id: str, episode_id: str) -> str:
    return f"{episodes_dir(run_id)}/{episode_id}.yaml"


def diagnostic_report_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/diagnostic_report.yaml"


def taxonomy_file(version: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/diagnostics/taxonomy_{version}.yaml"


def item_pool_file(version: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/diagnostics/item_pool_{version}.yaml"


def dataset_manifest_file(run_id: str, name: str) -> str:
    """数据集清单。按阶段（name，如 "sft" / "grpo_opd"）各一份，放 run 目录顶层——
    GitStore.list_products 不递归，顶层文件才能被 rismforge trace 扫到。"""
    return f"{run_dir(run_id)}/dataset_manifest_{name}.yaml"
