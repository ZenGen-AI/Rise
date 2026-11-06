"""数据类 __post_init__ 与 from_dict 共用的字段校验。失败一律抛 SimpleActioDataError,并指明是哪个字段。"""
from __future__ import annotations

import re

from simple_actio.errors import SimpleActioDataError

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def require_keys(d: object, what: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(d, dict):
        raise SimpleActioDataError(f"{what}: expected a dict, got {type(d).__name__}")
    missing = sorted(required - d.keys())
    if missing:
        raise SimpleActioDataError(f"{what}: missing keys {missing}")
    extra = sorted(d.keys() - required - optional)
    if extra:
        raise SimpleActioDataError(f"{what}: unknown keys {extra}")
    return d


def text(value: object, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SimpleActioDataError(f"{what}: expected a non-empty string, got {value!r}")
    return value


def prefixed_id(value: object, what: str, prefix: str) -> str:
    """产物 id:非空字符串且以 prefix 开头,且前缀之后还有内容。计划 1 的
    skill_file(skill_id) / memory_file(mem_id) 直接用 id 作文件名,不带前缀的 id
    会落到设计 §4.1 的 skill_library/skill-<id>.yaml、memory_library/mem-<id>.yaml 之外。"""
    v = text(value, what)
    if not v.startswith(prefix) or len(v) == len(prefix):
        raise SimpleActioDataError(f"{what}: must start with {prefix!r} followed by a name, got {v!r}")
    return v


def optional_text(value: object, what: str) -> str | None:
    return None if value is None else text(value, what)


def positive_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SimpleActioDataError(f"{what}: expected an int >= 1, got {value!r}")
    return value


def integer(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimpleActioDataError(f"{what}: expected an int, got {value!r}")
    return value


def text_tuple(value: object, what: str, *, non_empty: bool) -> tuple[str, ...]:
    """数据类字段:必须是 tuple(拒绝 str 与 list,字符串会被逐字当成多条),每项非空字符串。"""
    if not isinstance(value, tuple) or (non_empty and not value):
        raise SimpleActioDataError(
            f"{what}: expected a {'non-empty ' if non_empty else ''}tuple of strings, got {value!r}"
        )
    for i, v in enumerate(value):
        text(v, f"{what}[{i}]")
    return value


def unit_interval(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise SimpleActioDataError(f"{what}: expected a number in [0, 1], got {value!r}")
    return float(value)


def timestamp(value: object, what: str) -> str:
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        raise SimpleActioDataError(f"{what}: expected 'YYYY-MM-DDTHH:MM:SSZ', got {value!r}")
    return value
