"""战绩与生命周期(设计 §6.2,R8)。

- 战绩条目 {run_id, gain, promoted, regressed, used_as},按 run_id 幂等追加。
- verdict:最近一条战绩 regressed 为真 → "deprecate",否则 "keep"。
- deprecate:effective → deprecated(走 controlplane.states.assert_transition),
  先在 lineage 里记原因与触发的 run,再改配方状态,所以崩在两步之间重跑能收敛。
- superseded 不自动产生(R8)。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import recipe_file, recipe_lineage_file, recipe_track_file
from controlplane.states import REVIEW, assert_transition
from recipestore.ids import run_id_of
from recipestore.library import RecipeNotFoundError, get_recipe

NOW = "2026-10-09T00:00:00Z"

USED_AS = ("clone", "replay")
KEEP = "keep"
DEPRECATE = "deprecate"


def _env(id_: str, type_: str, status: str, links: list[str], payload: dict) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=CURRENT_SCHEMA_VERSION, links=links, payload=payload,
    )


def track_record_envelope(recipe_id: str, entries: list[dict]) -> Envelope:
    return _env(f"track-{recipe_id}", "recipe_track_record", "executed", [recipe_id],
                {"recipe_id": recipe_id, "entries": [dict(e) for e in entries]})


def lineage_envelope(recipe_id: str, payload: dict) -> Envelope:
    return _env(f"lineage-{recipe_id}", "recipe_lineage", "executed", [recipe_id], payload)


def validate_entry(entry: dict) -> None:
    if not isinstance(entry, dict):
        raise ValueError("track entry must be a mapping")
    expected = {"run_id", "gain", "promoted", "regressed", "used_as"}
    if set(entry) != expected:
        raise ValueError(f"track entry keys must be exactly {sorted(expected)}, got {sorted(entry)}")
    if not isinstance(entry["run_id"], str) or not entry["run_id"]:
        raise ValueError("track entry run_id must be a non-empty string")
    gain = entry["gain"]
    if gain is not None and (isinstance(gain, bool) or not isinstance(gain, (int, float))):
        raise ValueError("track entry gain must be a number or None")
    for key in ("promoted", "regressed"):
        if not isinstance(entry[key], bool):
            raise ValueError(f"track entry {key} must be a boolean")
    if entry["used_as"] not in USED_AS:
        raise ValueError(f"track entry used_as must be one of {list(USED_AS)}")


def append_track_entry(store: GitStore, recipe_id: str, entry: dict) -> None:
    """追加一条战绩。同一 run_id 已有条目时什么都不写(先写者为准)。"""
    validate_entry(entry)
    get_recipe(store, recipe_id)  # 配方不在抛 RecipeNotFoundError
    path = recipe_track_file(run_id_of(recipe_id))
    entries = list(store.read_product(path).payload.get("entries", [])) if store.exists(path) else []
    if any(e.get("run_id") == entry["run_id"] for e in entries):
        return
    entries.append(dict(entry))
    store.write_product(path, track_record_envelope(recipe_id, entries),
                        f"recipestore: track {recipe_id} <- {entry['run_id']}")


def verdict(entries: list[dict]) -> str:
    if entries and entries[-1].get("regressed") is True:
        return DEPRECATE
    return KEEP


def deprecate(store: GitStore, recipe_id: str, *, reason: str, run_id: str) -> bool:
    """effective → deprecated。已经 deprecated 返回 False 且不写任何东西;做了返回 True。"""
    if not reason:
        raise ValueError("deprecate needs a reason")
    recipe = get_recipe(store, recipe_id)
    if recipe.status == "deprecated":
        return False
    assert_transition(REVIEW, recipe.status, "deprecated")
    source_run = run_id_of(recipe_id)
    lpath = recipe_lineage_file(source_run)
    if store.exists(lpath):
        lineage = store.read_product(lpath).payload
    else:
        lineage = {"recipe_id": recipe_id, "source_run_id": source_run, "events": []}
    events = list(lineage.get("events", []))
    if not any(e.get("event") == "deprecated" for e in events):
        events.append({"event": "deprecated", "run_id": run_id, "reason": reason})
        lineage = dict(lineage, events=events)
        store.write_product(lpath, lineage_envelope(recipe_id, lineage),
                            f"recipestore: lineage {recipe_id} deprecated by {run_id}")
    recipe.status = "deprecated"
    store.write_product(recipe_file(source_run), recipe, f"recipestore: {recipe_id} deprecated")
    return True


def deprecated_by(store: GitStore, recipe_id: str) -> str | None:
    """lineage 里记下的、触发 deprecate 的 run id;没有这条事件返回 None。"""
    lpath = recipe_lineage_file(run_id_of(recipe_id))
    if not store.exists(lpath):
        return None
    for event in store.read_product(lpath).payload.get("events", []):
        if event.get("event") == "deprecated":
            return event.get("run_id")
    return None


__all__ = [
    "DEPRECATE", "KEEP", "USED_AS", "RecipeNotFoundError", "append_track_entry", "deprecate", "deprecated_by",
    "lineage_envelope", "track_record_envelope", "validate_entry", "verdict",
]
