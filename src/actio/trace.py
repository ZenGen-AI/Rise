"""激活轨迹的精确模式(计划 6 的对外契约,计划 7 的经验分析器消费它)。

轨迹要经 protobuf Struct 传输,而 Struct 把所有数字存成 double:整数 3 回来是 3.0。
所以模式里**没有任何数字**:叶子只有 str、None、bool,容器只有 list 与 dict。
这样经过真实 gRPC 往返后,轨迹逐字段、逐类型相等,消费方不需要做规范化。

v2 在 v1 的八个键之外多一个 memory 块,三个值全是字符串;其中的计数也是十进制字符串
(理由与 library_version 相同:Struct 把数字存成 double)。validate_trace 同时接受 v1 与 v2。
"""
from __future__ import annotations

import re

from actio.errors import ActioDataError

TRACE_SCHEMA_V1 = "actio.trace.v1"
TRACE_SCHEMA_V2 = "actio.trace.v2"
TIE_BREAK_RULE = "priority_desc,version_desc,id_asc"
TRACE_KEYS = (
    "schema", "signals", "candidates", "selected", "conflict",
    "memory_hits", "experience", "library_version",
)
TRACE_KEYS_V2 = TRACE_KEYS + ("memory",)
MEMORY_TRACE_KEYS = ("digest_before", "remembered", "extraction_failed")
# 计数的唯一写法:显式 ASCII 数字、无前导零(实例写的是 str(int))。不用 str.isdecimal(),它接受非 ASCII 数字。
_COUNT = re.compile(r"0|[1-9][0-9]*")


def build_trace(
    *,
    signals: list[str] | tuple[str, ...],
    candidates: list[str] | tuple[str, ...],
    selected: str | None,
    memory_hits: list[str] | tuple[str, ...],
    experience: tuple[str, str] | None,
    library_version: dict[str, str],
    memory: dict[str, str] | None = None,
) -> dict:
    """按模式组装一份新的轨迹字典。conflict 由 candidates 推出:命中不止一个时为
    {"skills": candidates, "rule": TIE_BREAK_RULE},否则为 None。
    memory 为 None 产出 v1;否则产出 v2 并把 memory 复制进去。"""
    trace = {
        "schema": TRACE_SCHEMA_V1 if memory is None else TRACE_SCHEMA_V2,
        "signals": list(signals),
        "candidates": list(candidates),
        "selected": selected,
        "conflict": (
            {"skills": list(candidates), "rule": TIE_BREAK_RULE} if len(candidates) > 1 else None
        ),
        "memory_hits": list(memory_hits),
        "experience": (
            None if experience is None else {"id": experience[0], "tier": experience[1]}
        ),
        "library_version": {"skill": library_version["skill"], "memory": library_version["memory"]},
    }
    if memory is not None:
        trace["memory"] = dict(memory)
    validate_trace(trace)
    return trace


def _str_list(value: object, what: str, *, prefix: str = "", ascending: bool = False) -> list[str]:
    """非空字符串列表,不得重复;给了 prefix 时每项都以它开头;ascending 时必须严格升序。"""
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ActioDataError(f"trace.{what}: expected a list of non-empty strings, got {value!r}")
    if len(set(value)) != len(value):
        raise ActioDataError(f"trace.{what}: duplicate entries in {value!r}")
    if prefix and any(not v.startswith(prefix) or v == prefix for v in value):
        raise ActioDataError(f"trace.{what}: every id must start with {prefix!r}, got {value!r}")
    if ascending and value != sorted(value):
        raise ActioDataError(f"trace.{what}: expected ascending order, got {value!r}")
    return value


def validate_trace(trace: object) -> None:
    """不合模式抛 ActioDataError。消费方(计划 7)读落盘 episode 时也用它。v1 与 v2 都接受。"""
    if not isinstance(trace, dict):
        raise ActioDataError(f"trace: expected a dict, got {type(trace).__name__}")
    schema = trace.get("schema")
    if schema == TRACE_SCHEMA_V1:
        keys = TRACE_KEYS
    elif schema == TRACE_SCHEMA_V2:
        keys = TRACE_KEYS_V2
    else:
        raise ActioDataError(
            f"trace.schema: expected {TRACE_SCHEMA_V1!r} or {TRACE_SCHEMA_V2!r}, got {schema!r}"
        )
    if set(trace) != set(keys):
        raise ActioDataError(f"trace: expected exactly keys {sorted(keys)}, got {sorted(trace)}")
    _str_list(trace["signals"], "signals", ascending=True)
    candidates = _str_list(trace["candidates"], "candidates", prefix="skill-")
    selected = trace["selected"]
    if candidates:
        if selected != candidates[0]:
            raise ActioDataError(f"trace.selected: expected the first candidate {candidates[0]!r}, got {selected!r}")
    elif selected is not None:
        raise ActioDataError(f"trace.selected: expected None with no candidates, got {selected!r}")
    expected_conflict = {"skills": candidates, "rule": TIE_BREAK_RULE} if len(candidates) > 1 else None
    if trace["conflict"] != expected_conflict:
        raise ActioDataError(f"trace.conflict: expected {expected_conflict!r}, got {trace['conflict']!r}")
    _str_list(trace["memory_hits"], "memory_hits", prefix="mem-")
    exp = trace["experience"]
    if exp is not None:
        if (
            not isinstance(exp, dict) or set(exp) != {"id", "tier"}
            or not isinstance(exp["id"], str) or not exp["id"]
            or exp["tier"] not in ("primary", "secondary", "fallback")
        ):
            raise ActioDataError(f"trace.experience: expected None or {{id, tier}}, got {exp!r}")
    lv = trace["library_version"]
    if (
        not isinstance(lv, dict) or set(lv) != {"skill", "memory"}
        or any(not isinstance(v, str) or not v for v in lv.values())
    ):
        raise ActioDataError(f"trace.library_version: expected {{skill: str, memory: str}}, got {lv!r}")
    if schema == TRACE_SCHEMA_V2:
        _validate_memory_block(trace["memory"])


def _validate_memory_block(block: object) -> None:
    """v2 的 memory 块:键恰为 MEMORY_TRACE_KEYS,三个值都是非空 str,两个计数是无前导零的 ASCII 十进制。"""
    if not isinstance(block, dict) or set(block) != set(MEMORY_TRACE_KEYS):
        raise ActioDataError(f"trace.memory: expected exactly keys {sorted(MEMORY_TRACE_KEYS)}, got {block!r}")
    for key in MEMORY_TRACE_KEYS:
        v = block[key]
        if not isinstance(v, str) or not v:
            raise ActioDataError(f"trace.memory.{key}: expected a non-empty string, got {v!r}")
    for key in ("remembered", "extraction_failed"):
        if not _COUNT.fullmatch(block[key]):
            raise ActioDataError(
                f"trace.memory.{key}: expected a decimal count without leading zeros, got {block[key]!r}"
            )


def normalize_library_version(library_version: object) -> dict[str, str]:
    """instantiate 接受 {"skill": x, "memory": y},x、y 是非空字符串或非负整数;
    一律规范成十进制字符串(整数 3 → "3"),这样它进轨迹后不受 Struct 的浮点化影响。"""
    if not isinstance(library_version, dict) or set(library_version) != {"skill", "memory"}:
        raise ActioDataError(f"library_version: expected {{skill, memory}}, got {library_version!r}")
    out: dict[str, str] = {}
    for key in ("skill", "memory"):
        v = library_version[key]
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ActioDataError(f"library_version.{key}: expected str or int, got {v!r}")
        if isinstance(v, int):
            if v < 0:
                raise ActioDataError(f"library_version.{key}: expected a non-negative int, got {v!r}")
            v = str(v)
        if not v:
            raise ActioDataError(f"library_version.{key}: expected a non-empty string")
        out[key] = v
    return out
