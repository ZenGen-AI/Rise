"""技能(结构化:触发条件 + 响应策略 + 层级 + 版本)与路由(闭环集成设计 §8.1)。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from actio import _check
from actio.errors import ActioDataError
from actio.signals import SignalRegistry
from actio.triggers import InvalidTriggerError, evaluate_trigger, validate_trigger
from simulator.datamodel import TurnRecord

LEVELS = ("macro", "micro")
SKILL_ID_PREFIX = "skill-"


@dataclass(frozen=True)
class Skill:
    """一条技能。policy_instructions 是响应策略:渲染成指导语交给模型后端的指令文本。
    priority 越大越优先;version 是该技能自己的版本号(>= 1)。"""

    id: str
    version: int
    level: str
    trigger: dict
    policy_instructions: tuple[str, ...]
    priority: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        """直接构造与 from_dict 走同一套校验:不合格的技能根本构造不出来。"""
        sid = _check.prefixed_id(self.id, "skill.id", SKILL_ID_PREFIX)
        _check.positive_int(self.version, f"skill {sid}.version")
        if self.level not in LEVELS:
            raise ActioDataError(f"skill {sid}: level must be one of {list(LEVELS)}, got {self.level!r}")
        try:
            validate_trigger(self.trigger)
        except InvalidTriggerError as exc:
            raise ActioDataError(f"skill {sid}: {exc}") from None
        _check.text_tuple(self.policy_instructions, f"skill {sid}.policy.instructions", non_empty=True)
        _check.integer(self.priority, f"skill {sid}.priority")
        _check.text(self.description, f"skill {sid}.description", allow_empty=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "level": self.level,
            "priority": self.priority,
            "trigger": deepcopy(self.trigger),
            "policy": {"instructions": list(self.policy_instructions)},
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: object) -> "Skill":
        d = _check.require_keys(
            d, "skill", {"id", "version", "level", "trigger", "policy"}, {"priority", "description"},
        )
        sid = _check.prefixed_id(d["id"], "skill.id", SKILL_ID_PREFIX)
        policy = _check.require_keys(d["policy"], f"skill {sid}.policy", {"instructions"})
        instructions = policy["instructions"]
        if not isinstance(instructions, list):
            raise ActioDataError(f"skill {sid}.policy.instructions: expected a list, got {instructions!r}")
        # 字段值的校验全在 __post_init__ 里;这里只查键、把 YAML 的 list 换成 tuple
        return cls(
            id=sid,
            version=d["version"],
            level=d["level"],
            trigger=deepcopy(d["trigger"]),
            policy_instructions=tuple(instructions),
            priority=d.get("priority", 0),
            description=d.get("description", ""),
        )


@dataclass(frozen=True)
class RouteResult:
    """一次路由的结果。candidates 是命中的技能 id,已按路由次序排好(第一个就是 selected);
    conflict 为真表示命中了不止一个;signals 是此刻为真的已注册信号名(按名字排序)。"""

    candidates: tuple[str, ...]
    selected: Skill | None
    signals: tuple[str, ...] = field(default=())

    @property
    def conflict(self) -> bool:
        return len(self.candidates) > 1


def route_order(skill: Skill) -> tuple:
    """路由排序键:优先级高者先,再版本号大者先,再按 id 字典序。"""
    return (-skill.priority, -skill.version, skill.id)


def check_library(skills: list[Skill] | tuple[Skill, ...], registry: SignalRegistry) -> None:
    """一组技能能否一起加载:都是 Skill(字段已由构造校验)、id 不重复、
    触发条件引用的信号都已注册。"""
    seen: set[str] = set()
    for s in skills:
        if not isinstance(s, Skill):
            raise ActioDataError(f"expected Skill, got {type(s).__name__}")
        if s.id in seen:
            raise ActioDataError(f"duplicate skill id {s.id!r}")
        seen.add(s.id)
        try:
            validate_trigger(s.trigger, registry)
        except InvalidTriggerError as exc:
            raise ActioDataError(f"skill {s.id}: {exc}") from None


def route(
    skills: list[Skill] | tuple[Skill, ...],
    history: list[TurnRecord],
    speaker_slot: str,
    registry: SignalRegistry,
) -> RouteResult:
    """纯函数:求出所有命中的技能,按 route_order 排序,选第一个。没有命中则 selected 为 None。"""
    hits = sorted(
        (s for s in skills if evaluate_trigger(s.trigger, history, speaker_slot, registry)),
        key=route_order,
    )
    return RouteResult(
        candidates=tuple(s.id for s in hits),
        selected=hits[0] if hits else None,
        signals=tuple(registry.fired(history, speaker_slot)),
    )
