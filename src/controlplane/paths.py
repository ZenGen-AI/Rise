from __future__ import annotations

CONTROL_PLANE_ROOT = "control-plane"
SUBDIRS = ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios", "personas"]


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
