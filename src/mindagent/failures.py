"""失败记录的守卫与轮次产物的容错读取。两个处理器都经这里写失败记录,不直接调 record_failure。"""
from __future__ import annotations

from controlplane.envelope import Envelope
from controlplane.gitstore import UNREADABLE_PRODUCT_ERRORS, GitStore
from controlplane.ledger import record_failure
from controlplane.paths import failure_record_file, run_dir

# 产物"文件在、内容读不出"时的异常,定义见 gitstore.UNREADABLE_PRODUCT_ERRORS(各处同一份);
# OSError 不在其中,原样上抛(瞬态)。
UNREADABLE = UNREADABLE_PRODUCT_ERRORS


def read_or_none(store: GitStore, rel: str) -> Envelope | None:
    """读一份产物;文件不存在或读不出(UNREADABLE)时返回 None。调用方用 store.exists 区分两者。"""
    if not store.exists(rel):
        return None
    try:
        return store.read_product(rel)
    except UNREADABLE:
        return None


def _single_segment(run_id: str) -> bool:
    return "/" not in run_id and "\\" not in run_id and run_id not in (".", "..")


def fail_once(
    store: GitStore, run_id: object, *, task_id: str, category: str, point: str, retries: int, reason: str,
) -> bool:
    """写一次失败记录,返回是否写了。与计划 3 的 _fail_once 同一守卫:run_id 不是非空的单个路径段、
    runs/<run>/ 目录不在(不凭空新建 run 目录)、或该 (run, task) 已记过,都什么都不写,原因只留在
    调用方自己的产物上。"""
    if not isinstance(run_id, str) or not run_id or not _single_segment(run_id):
        return False
    if not (store.repo_root / run_dir(run_id)).is_dir():
        return False
    if store.exists(failure_record_file(run_id, task_id)):
        return False
    record_failure(store, run_id, task_id=task_id, category=category, point=point,
                   retries=retries, reason=reason)
    return True


def recorded_category(store: GitStore, run_id: object, task_id: str) -> str | None:
    """该 (run, task) 已有且读得出的失败记录的类别(T1/T2/T3);run_id 过不了 fail_once 的同一守卫、记录不在
    或读不出,都返回 None。路径与 fail_once 同一个 failure_record_file。"""
    if not isinstance(run_id, str) or not run_id or not _single_segment(run_id):
        return None
    env = read_or_none(store, failure_record_file(run_id, task_id))
    if env is None:
        return None
    category = env.payload.get("category")
    return category if isinstance(category, str) else None
