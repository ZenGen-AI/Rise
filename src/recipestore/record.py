"""战绩处理器(设计 §6.3):一个用过配方的 run 到达终态后,把它的结果记进配方战绩。

流程:读指令的 recipe_ref → 读配方 → 读候选与诊断 → compute_gain 与退化标记 →
追加战绩(按 run_id 幂等)→ 按 verdict 决定是否 deprecate → 最后写 recipe_track 标记。
标记是 reconcile 的幂等键,所以放在最后:崩在中途重跑,前面各步都是幂等的,能收敛。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    candidate_checkpoint_file,
    planner_decision_file,
    recipe_track_note_file,
    run_dir,
)
from controlplane.reconcile import Action
from recipestore.gain import compute_gain, read_report_payload, regression_flags
from recipestore.library import RecipeNotFoundError, get_recipe, read_track_entries
from recipestore.track import (
    DEPRECATE,
    NOW,
    USED_AS,
    append_track_entry,
    deprecate,
    deprecated_by,
    verdict,
)

TERMINAL_CANDIDATE_STATES = ("effective", "rejected")
RECORDED = "recorded"
SKIPPED = "skipped"
DEFAULT_USED_AS = "clone"
DECISION_PREFIX = "planner-"  # 计划 5 的决策 id:planner-<源 run>


def _note(store: GitStore, run_id: str, payload: dict) -> None:
    env = Envelope(
        id=f"recipe-track-{run_id}", type="recipe_track_note", version=1, created_at=NOW,
        created_by=OwnerRef(), status="executed", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id] + ([payload["recipe_id"]] if payload.get("recipe_id") else []),
        payload=payload,
    )
    store.write_product(recipe_track_note_file(run_id), env, f"recipestore: track note {run_id} ({payload['result']})")


def _skip(store: GitStore, run_id: str, recipe_id: str, reason: str) -> None:
    _note(store, run_id, {"recipe_id": recipe_id, "result": SKIPPED, "reason": reason,
                          "entry": None, "verdict": None, "deprecated": False})


def _action_of(payload: dict) -> str:
    action = (payload.get("decision") or {}).get("action")
    return action if action in USED_AS else DEFAULT_USED_AS


def _decision_named_by_directive(store: GitStore, run_id: str) -> dict | None:
    """本 run 指令的 planner_decision_ref(planner-<源 run>)点名的那份决策的载荷;读不到返回 None。"""
    dpath = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(dpath):
        return None
    ref = store.read_product(dpath).payload.get("planner_decision_ref")
    if not isinstance(ref, str) or not ref.startswith(DECISION_PREFIX):
        return None
    source = ref[len(DECISION_PREFIX):]
    if not source or "/" in source or "\\" in source or source in (".", ".."):
        return None
    path = planner_decision_file(source)
    if not store.exists(path):
        return None
    return store.read_product(path).payload


def used_as_for(store: GitStore, run_id: str) -> str:
    """产出本 run 指令的那份规划决策的 action;没有决策(人工指令)或 action 不是 clone/replay 时记 clone。

    以指令的 planner_decision_ref 为准(计划 5 Ruling 14:失败决策分配过的 run id 会被再分配,
    两份决策可能有相同的 next_run_id)。指令没有这个键或点名的决策不在时,退路是扫 runs/,
    只认 executed 且 next_run_id 是本 run 的决策(只有它真写过指令)。"""
    named = _decision_named_by_directive(store, run_id)
    if named is not None:
        return _action_of(named)
    runs_root = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if runs_root.is_dir():
        for source in sorted(p.name for p in runs_root.iterdir() if p.is_dir()):
            path = planner_decision_file(source)
            if not store.exists(path):
                continue
            env = store.read_product(path)
            if env.status != "executed" or env.payload.get("next_run_id") != run_id:
                continue
            return _action_of(env.payload)
    return DEFAULT_USED_AS


def handle_record_track(store: GitStore, action: Action) -> None:
    run_id = action.target_id
    if store.exists(recipe_track_note_file(run_id)):
        return  # 已处理

    dpath = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(dpath):
        # 不写标记:runs/<run>/ 可能根本不存在,写了会新建一个只有标记的 run 目录,
        # 参与"最大 run"判定与 run id 分配(Ruling 15)
        return
    recipe_id = store.read_product(dpath).payload.get("recipe_ref") or ""
    if not isinstance(recipe_id, str) or not recipe_id:
        _skip(store, run_id, "", f"directive {run_id} has no recipe_ref")
        return
    try:
        recipe = get_recipe(store, recipe_id)
    except RecipeNotFoundError:
        _skip(store, run_id, recipe_id, f"recipe {recipe_id} not found")
        return

    cpath = candidate_checkpoint_file(run_id)
    if not store.exists(cpath):
        return  # 还没有候选:过早的动作,什么都不写,留给以后
    candidate = store.read_product(cpath)
    if candidate.status not in TERMINAL_CANDIDATE_STATES:
        return  # 候选还没到终态,同上

    report = read_report_payload(store, run_id)
    if report is None:
        return  # 还没有诊断,同上
    if diagnostic_grade(report) == "stub":
        _skip(store, run_id, recipe_id, f"diagnostic report for {run_id} is a stub")
        return

    dimension = recipe.payload.get("dimension", "")
    gain = compute_gain(store, run_id, dimension)
    entry = {
        "run_id": run_id,
        "gain": gain.gain,
        "promoted": candidate.status == "effective",
        "regressed": bool(regression_flags(report)),
        "used_as": used_as_for(store, run_id),
    }
    append_track_entry(store, recipe_id, entry)

    decided = verdict(read_track_entries(store, recipe_id))
    if decided == DEPRECATE and get_recipe(store, recipe_id).status == "effective":
        deprecate(store, recipe_id, reason=f"latest track entry ({run_id}) regressed", run_id=run_id)
    # 以 lineage 为准:崩在"配方已置 deprecated、标记未写"之后重跑,仍记为本 run 触发
    deprecated = deprecated_by(store, recipe_id) == run_id

    _note(store, run_id, {
        "recipe_id": recipe_id,
        "result": RECORDED,
        "reason": gain.reason,
        "entry": entry,
        "verdict": decided,
        "deprecated": deprecated,
    })
