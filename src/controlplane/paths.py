from __future__ import annotations

CONTROL_PLANE_ROOT = "control-plane"
SUBDIRS = ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config"]


def run_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/runs/{run_id}"


def model_file(n: int) -> str:
    return f"{CONTROL_PLANE_ROOT}/models/zengen-v{n}.yaml"


def backends_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/backends.yaml"
