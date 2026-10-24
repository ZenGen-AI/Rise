"""策略经验的读写层(设计 4、5、6)。唯一的 I/O 入口;增益只经 recipes_port 转导。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from controlplane import ledger
from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    candidate_checkpoint_file,
    planner_decision_file,
    run_dir,
    run_trace_file,
    strategy_entry_file,
    strategy_experience_note_file,
)
from trainscheduler.config import SchedulerConfig
from trainscheduler.run_ids import list_run_dirs
from trainscheduler.experience import (
    EntryState,
    attribution_for,
    bucket_key,
    recompute_entries,
    safe_dimension,
    strategy_key,
)
from trainscheduler.recipes_port import compute_gain, read_report_payload, regression_flags

_log = logging.getLogger("trainscheduler.experience")

NOW = "2026-10-20T00:00:00Z"
NOTE_TYPE = "strategy_experience_note"
ENTRY_TYPE = "strategy_entry"
ENTRY_STATUS = "executed"
RENDER_BUDGET = 900
EXPERIENCE_DIR = "control-plane/experience"


@dataclass(frozen=True)
class StrategyExperienceSummary:
    entries: tuple
    rendered: tuple[str, ...]
    truncated: int
    ignored_records: tuple[str, ...]


def _cost(store: GitStore, run_id: str) -> dict:
    """调用次数求和;没有记录时写 None(不写 0,0 会被读成免费)。"""
    path = run_trace_file(run_id)
    if not store.exists(path):
        return {"calls": None}
    records = store.read_product(path).payload.get("cost_records") or []
    calls = [r.get("calls") for r in records if isinstance(r.get("calls"), (int, float))]
    return {"calls": int(sum(calls)) if calls else None}


def _decision_that_produced(store: GitStore, run_id: str) -> dict | None:
    """找到产出这一轮的决策。

    不能读 runs/<run_id>/planner_decision.yaml:那是"这一轮结束后为下一轮做的规划",
    在规划这一轮的当下还不存在(plan_handler 只有在它为某 run 尚无决策时才做新规划)。
    真正的来源是指令里的 planner_decision_ref——它指向的决策,其 next_run_id 正是这一轮。
    人写的指令没有这个引用,返回 None(人写的命令没有策略可归因)。
    """
    path = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(path):
        return None
    ref = store.read_product(path).payload.get("planner_decision_ref")
    if not isinstance(ref, str) or not ref.startswith("planner-"):
        return None
    source = ref[len("planner-"):]
    decision_path = planner_decision_file(source)
    if not store.exists(decision_path):
        return None
    decision = store.read_product(decision_path).payload.get("decision")
    return decision if isinstance(decision, dict) else None


def collect_record(store: GitStore, run_id: str) -> tuple[dict | None, list[str]]:
    """把一轮压成记录载荷。返回 (载荷或 None, 忽略原因)。只读。"""
    ignored: list[str] = []
    decision = _decision_that_produced(store, run_id)
    if decision is None:
        return None, [f"{run_id}: no planner decision produced this run"]

    payload = read_report_payload(store, run_id)
    if payload is None:
        return None, [f"{run_id}: no diagnostic report"]
    grade = payload.get("backends")
    if payload.get("stub") or grade not in ("mock", "real"):
        return None, [f"{run_id}: diagnostic report is not usable evidence"]

    dimension = decision.get("target_dimension")
    if not safe_dimension(dimension):
        # 失败关闭:不安全的维度不进记录。记成 "None" 会造出一个假的桶,比丢掉这一轮更糟
        return None, [f"{run_id}: dimension {dimension!r} is not a safe path segment"]

    action = str(decision.get("action") or "")
    perturbation = decision.get("perturbation") or {}
    if not isinstance(perturbation, dict):
        return None, [f"{run_id}: perturbation is not a mapping"]

    promoted = False
    candidate_path = candidate_checkpoint_file(run_id)
    if store.exists(candidate_path):
        promoted = store.read_product(candidate_path).status == "effective"

    gain_result = compute_gain(store, run_id, dimension)
    record = {
        "run_id": run_id,
        "strategy_key": strategy_key(
            dimension=str(dimension),
            action=action,
            mode=str(decision.get("mode") or ""),
            perturbation=perturbation,
            source_recipe_id=str(decision.get("source_recipe_id") or ""),
        ),
        "outcome": {
            "gain": gain_result.gain,
            "promoted": promoted,
            "regressions": sorted(
                {flag.get("kind") for flag in regression_flags(payload) if flag.get("kind")}
            ),
            "cost": _cost(store, run_id),
        },
        "evidence": {
            "grade": grade,
            # 对照基线取自报告:增益是"候选减它",条目必须按它分桶,而不是按当前晋级模型
            "baseline_ref": payload.get("baseline_ref"),
            "baseline_model_id": payload.get("baseline_ref"),
            "benchmark_version": payload.get("benchmark_version"),
        },
        "attribution": attribution_for(action, perturbation),
        "ignored": ignored,
    }
    return record, ignored


def _safe_rel_path(rel: str) -> bool:
    """写入前确认路径落在产物树内:不以 / 开头、不含 ..、不是绝对路径或控制字符。"""
    if not isinstance(rel, str) or not rel:
        return False
    if rel.startswith("/") or "\\" in rel:
        return False
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return parts[0] == CONTROL_PLANE_ROOT and not any(ch in rel for ch in "\x00\n\r")


def _write(store: GitStore, rel: str, *, id_: str, type_: str, links: list[str],
           payload: dict, message: str) -> None:
    """唯一的写入点。路径不安全就拒绝,不依赖调用方事先校验。"""
    if not _safe_rel_path(rel):
        raise ValueError(f"refusing to write outside the artifact tree: {rel!r}")
    env = Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=ENTRY_STATUS, schema_version=CURRENT_SCHEMA_VERSION,
        links=links, payload=payload,
    )
    store.write_product(rel, env, message)


def load_entries(store: GitStore) -> tuple[list[EntryState], list[str]]:
    """读全部能解析的条目。返回 (条目, 读不出的文件名)。

    读不出的条目不许静默消失:调用方要把它当"不许覆盖"的名单,否则一次手编坏文件
    会在下一次重算里被无痕重建,人写过的覆盖表随之丢失。
    """
    entries: list[EntryState] = []
    broken: list[str] = []
    for rel in store.list_products(EXPERIENCE_DIR):
        try:
            env = store.read_product(rel)
            entries.append(EntryState.from_payload(env.id, env.payload))
        except Exception:                       # YAML 解析失败、缺键、类型错,一律当读不出
            broken.append(rel)
    return entries, broken


def _record_is_usable(record: object) -> bool:
    """记录必须正好具备 recompute_entries 消费的形状;缺一项就整条跳过,不让它终止整次重算。"""
    if not isinstance(record, dict):
        return False
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return False
    evidence, outcome = record.get("evidence"), record.get("outcome")
    if not isinstance(evidence, dict) or not isinstance(outcome, dict):
        return False
    base = evidence.get("baseline_model_id")
    if not isinstance(base, str) or not base:
        return False
    key = record.get("strategy_key")
    if not isinstance(key, dict):
        return False
    # 桶键要拼进 dict 的键:不可哈希的值(如列表)会在 grouped.setdefault 处抛 TypeError
    for name in ("dimension", "action", "mode", "source_recipe_id"):
        if not isinstance(key.get(name), str):
            return False
    names = key.get("perturbation_keys", key.get("perturbation"))
    if not isinstance(names, dict) and not isinstance(names, list):
        return False
    try:
        bucket_key(key, base)
    except (KeyError, TypeError, ValueError):
        return False
    regressions = outcome.get("regressions")
    if regressions is not None and not isinstance(regressions, list):
        return False
    perturbation = key.get("perturbation")
    if not isinstance(perturbation, dict):
        return False
    for encoded in perturbation.values():
        # _value_examples 只会读 bool / num / str 三种标记之一;缺了它会在聚合处 KeyError
        if not isinstance(encoded, dict):
            return False
        marks = [m for m in ("bool", "num", "str") if m in encoded]
        if len(marks) != 1 or not isinstance(encoded[marks[0]], (bool, str)):
            return False
    return True


def _all_records(store: GitStore) -> tuple[list[dict], list[str]]:
    """全部可用记录。list_products 不递归,所以先列 run 目录再逐目录读记录文件。"""
    records: list[dict] = []
    ignored: list[str] = []
    for run_id in list_run_dirs(store):
        rel = strategy_experience_note_file(run_id)
        if not store.exists(rel):
            continue
        try:
            env = store.read_product(rel)
        except Exception:                       # 半截 YAML 之类:跳过,不抛
            ignored.append(f"{rel}: unreadable file")
            continue
        if not _record_is_usable(env.payload):
            ignored.append(f"{env.id}: unusable record payload")
            continue
        records.append(env.payload)
    return records, ignored


def record_and_recompute(store: GitStore, run_id: str, *, config: SchedulerConfig) -> None:
    """落账并在同一调用内重算条目。开关关闭时不写任何东西。

    任何失败都写成一条失败记录再上抛:调用方会吞掉这个异常(落账不能挡住规划),
    若不留痕,这一轮的经验就永久消失,而且没有任何线索。
    """
    if not config.strategy_experience:
        return
    try:
        _record_and_recompute(store, run_id)
    except Exception as exc:
        _note_failure(store, run_id, exc)
        raise


def _note_failure(store: GitStore, run_id: str, exc: BaseException) -> None:
    """把落账失败写进 run 目录。不抛:留痕本身失败也不能挡住规划。"""
    try:
        ledger.record_failure(
            store, run_id, task_id="strategy-experience", category="T3",
            point="trainscheduler.experience_port", retries=0,
            reason=f"{type(exc).__name__}: {exc}"[:300],
        )
    except Exception:
        pass


def _record_and_recompute(store: GitStore, run_id: str) -> None:
    record, ignored = collect_record(store, run_id)
    if record is None:
        for reason in ignored:
            _log.warning("strategy experience not recorded: %s", reason)
        return
    _write(
        store, strategy_experience_note_file(run_id),
        id_=f"strategy-{run_id}", type_=NOTE_TYPE, links=[run_id],
        payload=record, message=f"trainscheduler: strategy experience for {run_id}",
    )

    benchmark = (record["evidence"] or {}).get("benchmark_version")
    if not isinstance(benchmark, str):
        return
    records, _ = _all_records(store)
    existing, broken = load_entries(store)
    # 读不出的条目按文件名记下来:id 相同的那条不许被重算覆盖
    broken_ids = {rel.rsplit("/", 1)[-1][:-5] for rel in broken}
    states = recompute_entries(records, existing, benchmark_version=benchmark)
    for state in states:
        if state.entry_id in broken_ids:
            continue                            # 手改坏的文件留给人看,不覆盖
        previous = next((e for e in existing if e.entry_id == state.entry_id), None)
        if previous is not None and previous.to_payload() == state.to_payload():
            continue
        _write(
            store, strategy_entry_file(state.entry_id),
            id_=state.entry_id, type_=ENTRY_TYPE, links=[run_id],
            payload=state.to_payload(), message=f"trainscheduler: strategy entry {state.entry_id}",
        )


def _render(entry: EntryState) -> str:
    """value_examples 是 {键名: {值: 次数}};渲染成 name=值×次数,值按字符串排序。"""
    values = [
        f"{name}=" + "/".join(f"{value}×{count}" for value, count in counts.items())
        for name, counts in entry.value_examples.items()
    ]
    shown = ", ".join(values) if values else "无"
    # 源配方必须出现:同一扰动在不同配方上的证据不可混用
    recipe = entry.bucket[4] or "无配方"
    return (
        f"策略 {entry.bucket[1]} {entry.bucket[2]} 配方 {recipe} 扰动 {shown}"
        f" · 证据 {entry.support} 条 · 平均增益 {entry.mean_gain:+.3f}"
        f" · 反例 {len(entry.contradictions)}"
    )


def select_entries(
    store: GitStore, *, dimension: str, benchmark_version: str,
    base_model_id: str, limit: int = 3,
) -> StrategyExperienceSummary:
    """筛可用条目,排序截断,渲染。从不抛:任何读不出的文件都降级成 ignored。"""
    _, ignored = _all_records(store)
    entries, broken = load_entries(store)
    usable = [
        e for e in entries
        if e.status == "effective" and e.bucket[0] == dimension
        and e.benchmark_version == benchmark_version
        and e.baseline_model_id == base_model_id
    ]
    usable.sort(key=lambda e: (-e.support, -e.mean_gain, e.entry_id))
    limit = max(0, limit)
    chosen: list = []
    lines: list[str] = []
    used = 0
    for entry in usable[:limit]:
        line = _render(entry)
        if used + len(line) > RENDER_BUDGET:
            break                               # 预算用尽就停,不放矮的进来挤掉高的
        chosen.append(entry)
        lines.append(line)
        used += len(line)
    truncated = len(usable) - len(chosen)
    return StrategyExperienceSummary(tuple(chosen), tuple(lines), truncated,
                                     tuple(ignored) + tuple(broken))
