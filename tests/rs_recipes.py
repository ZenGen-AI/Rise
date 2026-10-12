"""直接往 recipes/ 里放配方与战绩,供检索、战绩与战绩处理器的测试用(绕开收录处理器)。"""
from __future__ import annotations

from controlplane.gitstore import GitStore
from controlplane.paths import recipe_file, recipe_track_file
from rs_fixtures import batch_payload, envelope


def put_recipe(
    store: GitStore,
    run_id: str,
    *,
    status: str = "effective",
    dimension: str = "tom",
    payload: dict | None = None,
    entries: list[dict] | None = None,
) -> str:
    body = dict(batch_payload() if payload is None else payload)
    body["dimension"] = dimension
    body["source_run_id"] = run_id
    recipe_id = f"recipe-{run_id}"
    store.write_product(recipe_file(run_id), envelope(recipe_id, "recipe", status, body, [recipe_id, run_id]), "t")
    if entries is not None:
        store.write_product(
            recipe_track_file(run_id),
            envelope(f"track-{recipe_id}", "recipe_track_record", "executed",
                     {"recipe_id": recipe_id, "entries": entries}, [recipe_id]),
            "t",
        )
    return recipe_id


def entry(run_id: str, gain, *, regressed: bool = False, promoted: bool = True, used_as: str = "clone") -> dict:
    return {"run_id": run_id, "gain": gain, "promoted": promoted, "regressed": regressed, "used_as": used_as}
