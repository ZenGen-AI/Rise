"""配方 id 与 run id 的互转。配方候选与收录后的配方共用同一个 id:recipe-<来源 run>。"""
from __future__ import annotations

RECIPE_PREFIX = "recipe-"


def recipe_id_for(run_id: str) -> str:
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    return f"{RECIPE_PREFIX}{run_id}"


def run_id_of(recipe_id: str) -> str:
    """recipe-run-003 -> run-003。形状不对抛 ValueError,不猜。"""
    if not isinstance(recipe_id, str) or not recipe_id.startswith(RECIPE_PREFIX):
        raise ValueError(f"not a recipe id: {recipe_id!r}")
    run_id = recipe_id[len(RECIPE_PREFIX):]
    if not run_id:
        raise ValueError(f"not a recipe id: {recipe_id!r}")
    return run_id
