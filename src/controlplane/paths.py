from __future__ import annotations

CONTROL_PLANE_ROOT = "control-plane"
SUBDIRS = [
    "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
    "personas", "diagnostics",
    # 闭环集成设计 §4.1:追加在末尾,顺序固定
    "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
]


def run_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/runs/{run_id}"


def model_file(n: int) -> str:
    return f"{CONTROL_PLANE_ROOT}/models/zengen-v{n}.yaml"


def backends_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/backends.yaml"


def compute_endpoints_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/compute/endpoints.yaml"


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


def training_run_file(run_id: str) -> str:
    """训练运行记录。放 run 目录顶层——GitStore.list_products 不递归,
    rismforge trace 靠它扫描,放进子目录就看不到了。"""
    return f"{run_dir(run_id)}/training_run.yaml"


def candidate_checkpoint_file(run_id: str) -> str:
    """候选 checkpoint 产物。与 controlplane.daemon 读取的路径一致。"""
    return f"{run_dir(run_id)}/candidate_checkpoint.yaml"


# ---- 闭环集成设计 §4.1 / §11 的产物路径。名字是各计划之间的契约,不得改名。----
# 约定:形参叫 run_id / model_id 的函数自己加前缀(recipe-<run>、agent-round-<model>);
# 形参叫 proposal_id / skill_id / mem_id 的,传入的就是产物 id 本身(已带 proposal- / skill- / mem- 前缀),
# 文件名等于产物 id。


def recipe_candidate_file(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/recipe_candidates/recipe-{run_id}.yaml"


def recipe_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/recipes/recipe-{run_id}"


def recipe_file(run_id: str) -> str:
    return f"{recipe_dir(run_id)}/recipe.yaml"


def recipe_lineage_file(run_id: str) -> str:
    return f"{recipe_dir(run_id)}/lineage.yaml"


def recipe_track_file(run_id: str) -> str:
    """配方的战绩记录(RecipeStore 维护)。run 目录里的战绩标记见 recipe_track_note_file。"""
    return f"{recipe_dir(run_id)}/track_record.yaml"


def proposal_file(proposal_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/skill_memory_proposals/{proposal_id}/proposal.yaml"


def skill_file(skill_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/skill_library/{skill_id}.yaml"


def memory_file(mem_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/memory_library/{mem_id}.yaml"


def library_state_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/library_state.yaml"


def agent_round_file(model_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/agent_rounds/agent-round-{model_id}.yaml"


def planner_decision_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/planner_decision.yaml"


def nomination_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/nomination.yaml"


def recipe_track_note_file(run_id: str) -> str:
    """run 目录里的战绩回写标记(record_track 的幂等键)。"""
    return f"{run_dir(run_id)}/recipe_track.yaml"


def run_trace_file(run_id: str) -> str:
    """与 controlplane.observe.cost 读取的路径一致。"""
    return f"{run_dir(run_id)}/run_trace.yaml"


def failure_record_file(run_id: str, task_id: str) -> str:
    return f"{run_dir(run_id)}/failure-{task_id}.yaml"


def scheduler_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/scheduler.yaml"


def training_defaults_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/training_defaults.yaml"


def planner_prompt_file(version: str) -> str:
    """planner 提示模板信封(type: prompt_template)。prompts/ 不在 SUBDIRS 里:
    GitStore.write_product 会自己建父目录。"""
    return f"{CONTROL_PLANE_ROOT}/config/prompts/planner-{version}.yaml"
