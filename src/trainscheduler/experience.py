"""策略经验的纯函数层(设计 4、6.1、6.3)。

不读写产物树,不导入业务包。签名含扰动值;分桶只含扰动键名,值收进 value_examples。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any

ATTR_HIGH = "high"
ATTR_MEDIUM = "medium"
ATTR_LOW = "low"
ATTRIBUTIONS = (ATTR_HIGH, ATTR_MEDIUM, ATTR_LOW)

STATUS_INSUFFICIENT = "insufficient"
STATUS_EFFECTIVE = "effective"
STATUS_DEPRECATED = "deprecated"
STATUS_STALE = "stale"
STATUSES = (STATUS_INSUFFICIENT, STATUS_EFFECTIVE, STATUS_DEPRECATED, STATUS_STALE)

MIN_SUPPORT = 2
MAX_CONTRADICTIONS = 5

# 维度取自诊断载荷,要拼进文件名:只允许单段、无控制字符、不以点开头
_DIMENSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# dimension, action, mode, 扰动键名, 源配方, 对照基线
Bucket = tuple[str, str, str, tuple[str, ...], str, str]


def safe_dimension(dimension: object) -> str | None:
    """能安全拼进文件名的维度;不合法返回 None。"""
    if not isinstance(dimension, str) or not _DIMENSION.fullmatch(dimension):
        return None
    if ".." in dimension:
        return None
    return dimension


def _canonical_value(value: object) -> dict[str, Any]:
    """把扰动值规范化成可比较、可序列化的形状。int 与 float 同值等价。"""
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"perturbation value {value!r} is not a finite number")
        text = repr(number)
        if text.endswith(".0"):
            text = text[:-2]   # 整数值显示成 4,不是 4.0
        return {"num": text}
    if isinstance(value, str):
        return {"str": value}
    raise ValueError(
        f"perturbation value {value!r} must be a string, a number or a bool"
    )


def strategy_key(
    *, dimension: str, action: str, mode: str, perturbation: dict, source_recipe_id: str
) -> dict:
    """精确签名:扰动带值。键按名字排序,值规范化,所以同输入同签名。"""
    if not isinstance(perturbation, dict):
        raise ValueError(f"perturbation must be a mapping, got {type(perturbation).__name__}")
    values = {k: _canonical_value(v) for k, v in sorted(perturbation.items())}
    return {
        "dimension": dimension,
        "action": action,
        "mode": mode,
        "perturbation": values,
        "source_recipe_id": source_recipe_id,
    }


def bucket_key(key: dict, baseline_model_id: str) -> Bucket:
    """分桶键:扰动只看键名不看值;对照基线进桶,否则不同基座的同类策略会撞同一个 id。"""
    return (
        key["dimension"],
        key["action"],
        key["mode"],
        tuple(sorted(key["perturbation"])),
        key["source_recipe_id"],
        baseline_model_id,
    )


def digest(bucket: Bucket) -> str:
    blob = json.dumps(bucket, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def entry_id(dimension: str | None, bucket: Bucket) -> str:
    name = safe_dimension(dimension)
    suffix = digest(bucket)
    return f"exp-{name}-{suffix}" if name is not None else f"exp-{suffix}"


def attribution_for(action: str, perturbation: dict) -> str:
    """只有单键扰动的 clone 能把结果归到一个变量上。"""
    if action == "clone" and isinstance(perturbation, dict) and len(perturbation) == 1:
        return ATTR_MEDIUM
    return ATTR_LOW


def _value_examples(records: list[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        for name, encoded in record["strategy_key"]["perturbation"].items():
            shown = (
                str(encoded["bool"]).lower() if "bool" in encoded
                else encoded["num"] if "num" in encoded
                else encoded["str"]
            )
            per_key = counts.setdefault(name, {})
            per_key[shown] = per_key.get(shown, 0) + 1
    return {name: dict(sorted(values.items())) for name, values in sorted(counts.items())}


@dataclass(frozen=True)
class EntryState:
    entry_id: str
    bucket: Bucket
    support: int
    wins: int
    losses: int
    mean_gain: float
    contradictions: tuple[dict, ...]
    value_examples: dict
    status: str
    evidence_scope: str
    baseline_model_id: str
    benchmark_version: str
    overrides: dict = field(default_factory=dict)

    def with_overrides(self, overrides: dict) -> EntryState:
        """人写过的字段原样保留。status 只认四个已知取值;不认识的键不应用,仍留在覆盖表里让人看见。"""
        known = {f.name for f in fields(self)} - {"overrides"}
        changes: dict = {}
        for name, value in overrides.items():
            if name not in known:
                continue
            if name == "status" and value not in STATUSES:
                continue
            changes[name] = value
        return replace(self, overrides=dict(overrides), **changes)

    def to_payload(self) -> dict:
        return {
            "strategy_key": {
                "dimension": self.bucket[0], "action": self.bucket[1], "mode": self.bucket[2],
                # 条目按扰动键名分桶;具体的值在 value_examples 里
                "perturbation_keys": list(self.bucket[3]),
                "source_recipe_id": self.bucket[4],
            },
            "support": self.support,
            "wins": self.wins,
            "losses": self.losses,
            "mean_gain": self.mean_gain,
            "contradictions": [dict(c) for c in self.contradictions],
            "value_examples": dict(self.value_examples),
            "status": self.status,
            "evidence_scope": self.evidence_scope,
            "baseline_model_id": self.baseline_model_id,
            "benchmark_version": self.benchmark_version,
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_payload(cls, entry_id_: str, payload: dict) -> EntryState:
        key = payload["strategy_key"]
        bucket = bucket_key(
            {
                "dimension": key["dimension"], "action": key["action"], "mode": key["mode"],
                "perturbation": {name: None for name in key["perturbation_keys"]},
                "source_recipe_id": key["source_recipe_id"],
            },
            payload["baseline_model_id"],
        )
        return cls(
            entry_id=entry_id_,
            bucket=bucket,
            support=payload["support"],
            wins=payload["wins"],
            losses=payload["losses"],
            mean_gain=payload["mean_gain"],
            contradictions=tuple(payload["contradictions"]),
            value_examples=payload["value_examples"],
            status=payload["status"],
            evidence_scope=payload["evidence_scope"],
            baseline_model_id=payload["baseline_model_id"],
            benchmark_version=payload["benchmark_version"],
            overrides=payload.get("overrides", {}),
        )


def _eligible(record: dict, *, benchmark_version: str) -> bool:
    """能不能进聚合:设计 6.3 的资格表。缺对照基线的记录跳过,不让一条坏记录终止整次重算。"""
    if record.get("attribution") != ATTR_MEDIUM:
        return False
    outcome, evidence = record.get("outcome") or {}, record.get("evidence") or {}
    if not isinstance(outcome, dict) or not isinstance(evidence, dict):
        return False
    base = evidence.get("baseline_model_id")
    if not isinstance(base, str) or not base:
        return False
    gain = outcome.get("gain")
    if isinstance(gain, bool) or not isinstance(gain, (int, float)):
        return False
    if not math.isfinite(float(gain)):
        return False
    if evidence.get("grade") != "real":
        return False
    return evidence.get("benchmark_version") == benchmark_version


def _run_order(run_id: str) -> tuple[int, str]:
    match = re.match(r"^run-([0-9]+)$", run_id)
    return (int(match.group(1)), run_id) if match else (-1, run_id)


def recompute_entries(
    records: list[dict],
    existing: list[EntryState],
    *,
    benchmark_version: str,
) -> list[EntryState]:
    """由全部记录与基准版本重算条目。纯函数。

    - 只有 attribution 为 medium、有可比增益、real 证据、同基准、且带对照基线的记录参与聚合。
    - 每个对照基线各自成桶:不同基座的证据不会互相覆盖。
    - 求和与反例排序都按 run 编号数值序,与输入顺序无关。
    - 废弃阈值用全部反例计数;载荷只存最近五条。平均增益不为正也不给 effective。
    - 本轮没有匹配记录、而旧条目属于别的基准版本时,旧条目转 stale,但人写过的字段不动。
    """
    grouped: dict[Bucket, list[dict]] = {}
    for record in records:
        if not isinstance(record, dict) or not _eligible(record, benchmark_version=benchmark_version):
            continue
        base = record["evidence"]["baseline_model_id"]
        grouped.setdefault(bucket_key(record["strategy_key"], base), []).append(record)

    out: list[EntryState] = []
    for bucket, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda r: _run_order(r["run_id"]))
        gains = [float(r["outcome"]["gain"]) for r in ordered]
        wins = sum(1 for g in gains if g > 0)
        losses = sum(1 for g in gains if g < 0)
        mean = round(sum(gains) / len(gains), 6)
        all_contradictions = [
            {
                "run_id": r["run_id"],
                "gain": float(r["outcome"]["gain"]),
                "regression_kinds": sorted(r["outcome"].get("regressions") or []),
            }
            for r in ordered
            if float(r["outcome"]["gain"]) < 0 or (r["outcome"].get("regressions") or [])
        ]
        # 阈值用全部反例计数;只把最近五条存进载荷
        contradictions = tuple(all_contradictions[-MAX_CONTRADICTIONS:])
        support = len(ordered)
        if support < MIN_SUPPORT:
            status = STATUS_INSUFFICIENT
        elif len(all_contradictions) * 2 >= support:
            status = STATUS_DEPRECATED
        elif mean <= 0:
            # 支持数够但平均增益不为正:它没有变好,不给注入
            status = STATUS_DEPRECATED
        else:
            status = STATUS_EFFECTIVE
        dimension = bucket[0]
        state = EntryState(
            entry_id=entry_id(dimension, bucket),
            bucket=bucket,
            support=support,
            wins=wins,
            losses=losses,
            mean_gain=mean,
            contradictions=contradictions,
            value_examples=_value_examples(ordered),
            status=status,
            evidence_scope="real",
            baseline_model_id=bucket[5],
            benchmark_version=benchmark_version,
        )
        previous = next((e for e in existing if e.entry_id == state.entry_id), None)
        if previous is not None:
            # 数值字段取新算的,覆盖表沿用旧的:人写过的状态不会被重算冲掉
            state = state.with_overrides(previous.overrides)
        out.append(state)

    touched = {state.entry_id for state in out}
    for previous in existing:
        if previous.entry_id in touched:
            continue
        if previous.benchmark_version != benchmark_version:
            # 转 stale 也不能盖掉人写过的状态
            out.append(replace(previous, status=STATUS_STALE).with_overrides(previous.overrides))
    return out
