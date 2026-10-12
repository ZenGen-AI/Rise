"""配方检索(设计 §6.2)。不建索引(R9):每次调用直接扫描 recipes/ 目录。"""
from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, recipe_file, recipe_track_file
from recipestore.ids import RECIPE_PREFIX, run_id_of

ORDERS = ("track", "id")

# compare() 只比"配置":这三个键是结果、校验输出与来源,不是配置,不算配置差异(Ruling 22)。
# stage_manifests / stage_manifest_refs 是训练数据的锚,属于配置,照常比较。
NON_CONFIG_KEYS = frozenset({"result", "repro", "source_run_id"})


class RecipeNotFoundError(LookupError):
    """点名的配方不在 recipes/ 里。"""


@dataclass(frozen=True)
class RecipeView:
    id: str
    recipe_type: str
    dimension: str
    status: str
    payload: dict
    mean_gain: float | None
    n_reuse: int


def get_recipe(store: GitStore, recipe_id: str) -> Envelope:
    try:
        run_id = run_id_of(recipe_id)
    except ValueError as exc:
        raise RecipeNotFoundError(str(exc)) from exc
    path = recipe_file(run_id)
    if not store.exists(path):
        raise RecipeNotFoundError(f"recipe {recipe_id} not found")
    return store.read_product(path)


def read_track_entries(store: GitStore, recipe_id: str) -> list[dict]:
    """配方的战绩条目,按追加顺序。战绩文件不在(收录崩在中途)视为空。"""
    path = recipe_track_file(run_id_of(recipe_id))
    if not store.exists(path):
        return []
    return [dict(e) for e in store.read_product(path).payload.get("entries", [])]


def track_summary(entries: list[dict]) -> tuple[float | None, int]:
    """(mean_gain, n_reuse)。mean_gain 只统计 gain 非空的条目;一条都没有就是 None。
    n_reuse 是条目总数,含 gain 为空的条目。"""
    gains = [
        float(e["gain"]) for e in entries
        if isinstance(e.get("gain"), (int, float)) and not isinstance(e.get("gain"), bool)
    ]
    mean = sum(gains) / len(gains) if gains else None
    return mean, len(entries)


def _recipe_ids(store: GitStore) -> list[str]:
    root = store.repo_root / CONTROL_PLANE_ROOT / "recipes"
    if not root.is_dir():
        return []
    ids = []
    for p in root.iterdir():
        if not p.is_dir() or not p.name.startswith(RECIPE_PREFIX) or p.name == RECIPE_PREFIX:
            continue
        # 是否是配方以路径函数为准,不手拼文件名
        if store.exists(recipe_file(run_id_of(p.name))):
            ids.append(p.name)
    return sorted(ids)


def view_of(store: GitStore, env: Envelope) -> RecipeView:
    mean, n = track_summary(read_track_entries(store, env.id))
    return RecipeView(
        id=env.id,
        recipe_type=env.payload.get("recipe_type", ""),
        dimension=env.payload.get("dimension", ""),
        status=env.status,
        payload=env.payload,
        mean_gain=mean,
        n_reuse=n,
    )


def _track_key(v: RecipeView):
    # mean_gain 降序,没有增益数据的排最后;再 n_reuse 降序;再 id 升序
    return (v.mean_gain is None, -(v.mean_gain or 0.0), -v.n_reuse, v.id)


def find_recipes(
    store: GitStore,
    *,
    dimension: str | None = None,
    recipe_type: str | None = None,
    states: tuple[str, ...] = ("effective",),
    order: str = "track",
) -> list[RecipeView]:
    if order not in ORDERS:
        raise ValueError(f"order must be one of {list(ORDERS)}, got {order!r}")
    views: list[RecipeView] = []
    for recipe_id in _recipe_ids(store):
        env = get_recipe(store, recipe_id)
        if env.status not in states:
            continue
        if dimension is not None and env.payload.get("dimension") != dimension:
            continue
        if recipe_type is not None and env.payload.get("recipe_type") != recipe_type:
            continue
        views.append(view_of(store, env))
    if order == "track":
        views.sort(key=_track_key)
    else:
        views.sort(key=lambda v: v.id)
    return views


def _flatten(obj: object, prefix: str, out: dict) -> None:
    if isinstance(obj, dict) and obj:
        for k in obj:
            _flatten(obj[k], f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj


_MISSING = "<missing>"


def compare(store: GitStore, a: str, b: str) -> dict:
    """配置差异 + 战绩对比。差异按点分路径列出,列表整体比较;一侧没有该键记为 "<missing>"。"""
    ea, eb = get_recipe(store, a), get_recipe(store, b)
    fa: dict = {}
    fb: dict = {}
    _flatten({k: v for k, v in ea.payload.items() if k not in NON_CONFIG_KEYS}, "", fa)
    _flatten({k: v for k, v in eb.payload.items() if k not in NON_CONFIG_KEYS}, "", fb)
    diff = {
        path: {"a": fa.get(path, _MISSING), "b": fb.get(path, _MISSING)}
        for path in sorted(set(fa) | set(fb))
        if fa.get(path, _MISSING) != fb.get(path, _MISSING)
    }
    va, vb = view_of(store, ea), view_of(store, eb)
    return {
        "a": a,
        "b": b,
        "config_diff": diff,
        "track": {
            a: {"status": va.status, "mean_gain": va.mean_gain, "n_reuse": va.n_reuse},
            b: {"status": vb.status, "mean_gain": vb.mean_gain, "n_reuse": vb.n_reuse},
        },
    }
