"""只读子命令:trace、cost 直接用 controlplane.observe,logs 按 run 目录里的 log_refs 打印日志。

三者都只读树与本机文件,不写产物、不提交。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from controlplane.gitstore import UNREADABLE_PRODUCT_ERRORS, GitStore
from controlplane.observe import cost, trace
from controlplane.paths import run_dir

__all__ = ["collect_log_refs", "cost", "render_logs", "trace"]


def _local_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))   # RFC 8089:空格等字符在 file:// 地址里是百分号编码的


def _matches_task(payload: dict, uri: str, task_id: str) -> bool:
    # 失败记录的载荷带 task_id;CliBridge 的日志文件名是 <task_id>-<k>.log
    if payload.get("task_id") == task_id:
        return True
    path = _local_path(uri)
    name = path.name if path is not None else uri.rsplit("/", 1)[-1]
    return re.fullmatch(rf"{re.escape(task_id)}-\d+\.log", name) is not None


def _well_formed(ref) -> bool:
    return isinstance(ref, dict) and isinstance(ref.get("uri"), str) and bool(ref["uri"])


def _scan(store: GitStore, run_id: str, task_id: str | None) -> tuple[list[tuple[str, dict]], list[str]]:
    """(合规的引用, 问题行)。logs 是排查问题时最需要的命令:树里有坏东西时它自己不能崩,
    读不出的产物与畸形的引用都点名列出,其余照常打印。"""
    refs: list[tuple[str, dict]] = []
    problems: list[str] = []
    for rel in store.list_products(run_dir(run_id)):
        try:
            payload = store.read_product(rel).payload
        except UNREADABLE_PRODUCT_ERRORS as exc:
            problems.append(f"== {rel} [unreadable] {type(exc).__name__}; its log_refs are not shown")
            continue
        raw = payload.get("log_refs", []) or []
        if not isinstance(raw, list):
            problems.append(f"== {rel} log_refs [malformed] not a list")
            continue
        for ref in raw:
            if not _well_formed(ref):
                # 畸形引用判不出属于哪个任务(文件名要从 uri 取),只在载荷自己带着该任务时列出
                if task_id is None or payload.get("task_id") == task_id:
                    problems.append(f"== {rel} log_ref [malformed] needs a non-empty string 'uri'")
                continue
            if task_id is None or _matches_task(payload, ref["uri"], task_id):
                refs.append((rel, ref))
    return refs, problems


def collect_log_refs(store: GitStore, run_id: str, *, task_id: str | None = None) -> list[tuple[str, dict]]:
    """run 目录下(不递归)每个产物的 payload["log_refs"],按产物路径、再按出现顺序。
    返回 [(产物相对路径, 引用字典)]。task_id 非空时只留属于该任务的引用。
    读不出的产物与畸形的引用不在返回值里,render_logs 会把它们点名列出。"""
    return _scan(store, run_id, task_id)[0]


def render_logs(store: GitStore, run_id: str, *, task_id: str | None = None) -> tuple[str, int]:
    """返回 (要打印的文本, 退出码)。

    退出码:0 = 每个引用都打印出来了(或没有引用);1 = run 不存在,或至少一个引用不可达、哈希对不上。
    不可达的引用明说 unreachable,不静默省略;哈希对不上时照样打印内容,但标 sha256 mismatch。"""
    if not store.list_products(run_dir(run_id)):
        return f"run {run_id}: no products found", 1
    refs, problems = _scan(store, run_id, task_id)
    scope = f" for task {task_id}" if task_id is not None else ""
    if not refs and not problems:
        return f"run {run_id}: no log_refs{scope}", 0
    lines = [f"run {run_id} logs{scope}", *problems]
    code = 1 if problems else 0
    for rel, ref in refs:
        uri = ref["uri"]
        path = _local_path(uri)
        if path is None or not path.is_file():
            lines.append(f"== {rel} log_ref [unreachable] {uri}")
            lines.append("   (not reachable from this machine; contents not shown)")
            code = 1
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:   # 权限、文件在 is_file 之后被删:同样是本机读不到
            lines.append(f"== {rel} log_ref [unreachable] {uri}")
            lines.append(f"   (cannot be read from this machine: {type(exc).__name__}; contents not shown)")
            code = 1
            continue
        mark = "ok"
        if ref.get("sha256") and hashlib.sha256(data).hexdigest() != ref["sha256"]:
            mark = "sha256 mismatch"
            code = 1
        lines.append(f"== {rel} log_ref [{mark}] {uri}")
        lines.append(data.decode("utf-8", errors="replace").rstrip("\n"))
    return "\n".join(lines), code
