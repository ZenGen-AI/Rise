"""统一的成本记账与失败留痕(闭环集成设计 §5.2,上游补充 D.4 / E.5)。

所有子系统经这两个函数写 run 目录里的 run_trace 与 failure_record,不各自拼产物。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import failure_record_file, run_trace_file

FAILURE_CATEGORIES = ("T1", "T2", "T3")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_segment(name: str, value: str) -> None:
    # run_id 拼进目录名(runs/<run>/),task_id 拼进文件名(failure-<task>.yaml):
    # 必须是单个非空路径段,不含 / 与 \,也不是 . 或 ..,否则会写到 runs/ 之外或错的位置
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or value in (".", "..")
    ):
        raise ValueError(f"{name} must be a single non-empty path segment, got {value!r}")


def _check_number(name: str, value: object, *, integer: bool) -> None:
    if value is None:
        return
    # bool 是 int 的子类,单独排除:tokens=True 一定是调用方写错了
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        kind = "an int" if integer else "a number"
        raise ValueError(f"{name} must be {kind} or None, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")


def record_cost(
    store: GitStore,
    run_id: str,
    *,
    task_id: str,
    cost_type: str,
    gpu_hours: float | None = None,
    tokens: int | None = None,
    calls: int | None = None,
    usd: float | None = None,
) -> None:
    """向 runs/<run>/run_trace.yaml 的 cost_records 追加一条 CostRecord。

    - 值未知(None)就省略该键,不写 null:observe.sum_costs 用 r.get("tokens", 0) 求和,null 会抛 TypeError。
    - 同一 (task_id, cost_type) 只记一次,先到者为准;重复调用不改文件、不产生提交。
    - run_trace 已存在时只改 cost_records,其余字段(wall_clock_s、log_refs、状态)原样保留。
    """
    _check_segment("run_id", run_id)
    _check_segment("task_id", task_id)
    if not isinstance(cost_type, str) or not cost_type:
        raise ValueError(f"cost_type must be a non-empty string, got {cost_type!r}")
    _check_number("gpu_hours", gpu_hours, integer=False)
    _check_number("tokens", tokens, integer=True)
    _check_number("calls", calls, integer=True)
    _check_number("usd", usd, integer=False)

    rel = run_trace_file(run_id)
    now = _now()
    if store.exists(rel):
        env = store.read_product(rel)
    else:
        env = Envelope(
            id=f"trace-{run_id}", type="run_trace", version=1, created_at=now,
            created_by=OwnerRef(), status="executed", schema_version=1,
            links=[run_id], payload={},
        )
    records = list(env.payload.get("cost_records", []))
    if any(r.get("task_id") == task_id and r.get("cost_type") == cost_type for r in records):
        return
    record: dict = {"run_id": run_id, "task_id": task_id, "cost_type": cost_type}
    for key, value in (("gpu_hours", gpu_hours), ("tokens", tokens), ("calls", calls), ("usd", usd)):
        if value is not None:
            record[key] = value
    record["recorded_at"] = now
    records.append(record)
    env.payload["cost_records"] = records
    store.write_product(rel, env, f"ledger: cost {run_id}/{task_id} ({cost_type})")


def record_failure(
    store: GitStore,
    run_id: str,
    *,
    task_id: str,
    category: str,
    point: str,
    retries: int,
    reason: str,
    log_ref: BigRef | None = None,
) -> str:
    """写一份 FailureRecord 产物(上游补充 E.5)到 runs/<run>/failure-<task>.yaml,返回路径。

    载荷:run_id、task_id、category(T1/T2/T3)、point(失败点:子系统.模块)、retries(已重试次数)、
    reason(最终判定)、log_refs(原始错误的 L3 引用,沿用 observe.trace 认识的键,无日志时为空列表)、
    recorded_at(时刻)。同一 (run_id, task_id) 只写一次:已存在则原样返回路径,不覆盖,
    也不报告"已存在"——调用方要区分,先自己 store.exists(failure_record_file(run_id, task_id))。
    """
    _check_segment("run_id", run_id)
    _check_segment("task_id", task_id)
    if category not in FAILURE_CATEGORIES:
        raise ValueError(f"category must be one of {FAILURE_CATEGORIES}, got {category!r}")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError(f"retries must be an int >= 0, got {retries!r}")
    rel = failure_record_file(run_id, task_id)
    if store.exists(rel):
        return rel
    now = _now()
    env = Envelope(
        id=f"failure-{run_id}-{task_id}", type="failure_record", version=1, created_at=now,
        created_by=OwnerRef(), status="executed", schema_version=1, links=[run_id],
        payload={
            "run_id": run_id,
            "task_id": task_id,
            "category": category,
            "point": point,
            "retries": retries,
            "reason": reason,
            "log_refs": [log_ref.to_dict()] if log_ref is not None else [],
            "recorded_at": now,
        },
    )
    store.write_product(rel, env, f"ledger: failure {run_id}/{task_id} ({category})")
    return rel
