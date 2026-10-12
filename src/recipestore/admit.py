"""收录处理器(设计 §6.3):把 approved 的配方候选写进 recipes/。

顺序与重入:
  1. recipe.yaml(effective)   —— 不在才写
  2. lineage.yaml             —— 不在才写
  3. track_record.yaml(空)   —— 不在才写
  4. 候选 approved → effective —— 最后一步,它是 reconcile 的幂等键
崩在任一步之后重跑,已写的步骤跳过,未写的补上,最终树相同。
"""
from __future__ import annotations

import copy

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_failure
from controlplane.paths import (
    failure_record_file,
    recipe_candidate_file,
    recipe_file,
    recipe_lineage_file,
    recipe_track_file,
    run_dir,
)
from controlplane.reconcile import Action
from controlplane.states import REVIEW, assert_transition
from recipestore.ids import run_id_of
from recipestore.track import NOW, lineage_envelope, track_record_envelope

TASK_ID = "admit_recipe"
FAILURE_POINT = "recipestore.admit"


def _single_segment(run_id: str) -> bool:
    return "/" not in run_id and "\\" not in run_id and run_id not in (".", "..")


def _fail_once(store: GitStore, run_id: str, reason: str) -> None:
    """写一次 T2 失败记录。已写过、或 runs/<run>/ 不在(不新建 run 目录,Ruling 15)时什么都不做。"""
    if store.exists(failure_record_file(run_id, TASK_ID)):
        return  # 已记过;每轮重来不重复写
    if not (store.repo_root / run_dir(run_id)).is_dir():
        return
    record_failure(store, run_id, task_id=TASK_ID, category="T2", point=FAILURE_POINT, retries=0, reason=reason)


def has_repro_evidence(payload: dict) -> bool:
    """审核门的"证据在场"规则:候选必须带 repro,且 errors、warnings 都是列表。"""
    repro = payload.get("repro")
    return (
        isinstance(repro, dict)
        and isinstance(repro.get("errors"), list)
        and isinstance(repro.get("warnings"), list)
    )


def recipe_envelope(candidate: Envelope) -> Envelope:
    return Envelope(
        id=candidate.id, type="recipe", version=1, created_at=NOW, created_by=OwnerRef(),
        status="effective", schema_version=CURRENT_SCHEMA_VERSION,
        links=list(candidate.links), payload=copy.deepcopy(candidate.payload),
    )


def handle_admit_recipe(store: GitStore, action: Action) -> None:
    # 确定性的坏动作一律是空操作,不抛(Ruling 20):计划 8 调处理器时不接异常,
    # 抛了守护进程会退出,重启后同一动作再来,永久卡死。瞬态异常(OSError 等)照常上抛。
    recipe_id = action.target_id
    try:
        run_id = run_id_of(recipe_id)
    except ValueError:
        return  # 不是 recipe-<run> 形状:解析不出 run id,无处留痕
    if not _single_segment(run_id):
        return  # 拼进路径会越出 recipe_candidates/ 与 runs/
    cpath = recipe_candidate_file(run_id)
    if not store.exists(cpath):
        _fail_once(store, run_id, f"recipe candidate {recipe_id} not found")
        return
    candidate = store.read_product(cpath)
    if candidate.status == "effective":
        return  # 已收录
    if candidate.status != "approved":
        return  # 只收录 approved;其余状态是过期的动作,什么都不做
    if not has_repro_evidence(candidate.payload):
        _fail_once(store, run_id,
                   f"recipe candidate {recipe_id} is approved but carries no repro evidence; not admitted")
        return

    rpath = recipe_file(run_id)
    if not store.exists(rpath):
        store.write_product(rpath, recipe_envelope(candidate), f"recipestore: admit {recipe_id}")

    lpath = recipe_lineage_file(run_id)
    if not store.exists(lpath):
        lineage = {
            "recipe_id": recipe_id,
            "source_run_id": run_id,
            "admitted_from": candidate.id,
            "events": [{"event": "admitted", "run_id": run_id}],
        }
        store.write_product(lpath, lineage_envelope(recipe_id, lineage), f"recipestore: lineage {recipe_id}")

    tpath = recipe_track_file(run_id)
    if not store.exists(tpath):
        store.write_product(tpath, track_record_envelope(recipe_id, []), f"recipestore: track record {recipe_id}")

    candidate = store.read_product(cpath)
    assert_transition(REVIEW, candidate.status, "effective")
    candidate.status = "effective"
    store.write_product(cpath, candidate, f"recipestore: candidate {recipe_id} effective")
