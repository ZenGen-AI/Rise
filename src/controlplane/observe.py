from __future__ import annotations

from urllib.parse import urlparse
from pathlib import Path

from controlplane.gitstore import GitStore
from controlplane.paths import run_dir


def sum_costs(records: list[dict]) -> dict:
    """纯函数汇总。只加总物理量，不换算金额（单价表可选，见 spec D.4）。"""
    return {
        "gpu_hours": sum(r.get("gpu_hours", 0.0) for r in records),
        "tokens": sum(r.get("tokens", 0) for r in records),
    }


def _reachable(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return False
    return Path(parsed.path).is_file()


def trace(store: GitStore, run_id: str) -> str:
    """打印该轮全链路：各产物状态 + links 溯源 + L2/L3 引用可达性。只读。"""
    lines: list[str] = [f"run {run_id}"]
    products = store.list_products(run_dir(run_id))
    if not products:
        return f"run {run_id}: no products found"
    for rel in products:
        env = store.read_product(rel)
        links = ",".join(env.links) if env.links else "-"
        lines.append(f"  {env.type:<22} [{env.status}] id={env.id} links={links}")
        for ref in env.payload.get("log_refs", []):
            uri = ref["uri"]
            mark = "ok" if _reachable(uri) else "unreachable"
            lines.append(f"      log_ref [{mark}] {uri}")
        for ref in env.payload.get("metric_stream_refs", []):
            uri = ref["uri"]
            mark = "ok" if _reachable(uri) else "unreachable"
            lines.append(f"      metric_ref [{mark}] {uri}")
    return "\n".join(lines)


def cost(store: GitStore, run_id: str) -> str:
    """打印本轮成本明细与汇总。只读，不重新计算真相。"""
    rel = f"{run_dir(run_id)}/run_trace.yaml"
    if not store.exists(rel):
        return f"run {run_id}: no run_trace (nothing recorded yet)"
    env = store.read_product(rel)
    records = env.payload.get("cost_records", [])
    lines = [f"run {run_id} cost detail"]
    for r in records:
        lines.append(
            f"  {r.get('task_id', '-'):<10} {r.get('cost_type', '-'):<20} "
            f"gpu_hours={r.get('gpu_hours', 0.0)} tokens={r.get('tokens', 0)}"
        )
    total = sum_costs(records)
    lines.append(f"  total: gpu_hours={total['gpu_hours']} tokens={total['tokens']}")
    return "\n".join(lines)
