"""推理经验:提炼后的可复用推理模式与三级回退(主策略 → 次策略 → 保底策略)。

V1 只做数据模型与纯函数,不做复杂路由(闭环集成设计 §8.1)。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from simple_actio import _check
from simple_actio.errors import SimpleActioDataError
from simple_actio.signals import SignalRegistry
from simple_actio.triggers import InvalidTriggerError, evaluate_trigger, validate_trigger
from simulator.datamodel import TurnRecord

TIERS = ("primary", "secondary", "fallback")


@dataclass(frozen=True)
class Tier:
    """回退链上的一级。when 为 None 表示无条件(保底策略必须无条件)。"""

    instructions: tuple[str, ...]
    when: dict | None = None

    def __post_init__(self) -> None:
        _check.text_tuple(self.instructions, "tier.instructions", non_empty=True)
        if self.when is not None:
            _trigger(self.when, "tier.when")

    def to_dict(self) -> dict:
        return {"when": deepcopy(self.when), "instructions": list(self.instructions)}


@dataclass(frozen=True)
class ReasoningExperience:
    id: str
    version: int
    pattern: str
    applies_when: dict
    primary: Tier
    secondary: Tier
    fallback: Tier

    def __post_init__(self) -> None:
        """直接构造与 from_dict 走同一套校验。"""
        eid = _check.text(self.id, "experience.id")
        _check.positive_int(self.version, f"experience {eid}.version")
        _check.text(self.pattern, f"experience {eid}.pattern")
        _trigger(self.applies_when, f"experience {eid}.applies_when")
        for name in TIERS:
            tier = getattr(self, name)
            if not isinstance(tier, Tier):
                raise SimpleActioDataError(f"experience {eid}.tiers.{name}: expected Tier, got {type(tier).__name__}")
            if name == "fallback" and tier.when is not None:
                raise SimpleActioDataError(f"experience {eid}.tiers.fallback.when must be null (fallback is unconditional)")
            if name != "fallback" and tier.when is None:
                raise SimpleActioDataError(f"experience {eid}.tiers.{name}.when must be a trigger")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "pattern": self.pattern,
            "applies_when": deepcopy(self.applies_when),
            "tiers": {name: getattr(self, name).to_dict() for name in TIERS},
        }

    @classmethod
    def from_dict(cls, d: object) -> "ReasoningExperience":
        d = _check.require_keys(d, "experience", {"id", "version", "pattern", "applies_when", "tiers"})
        eid = _check.text(d["id"], "experience.id")
        tiers = _check.require_keys(d["tiers"], f"experience {eid}.tiers", set(TIERS))
        parsed = {}
        for name in TIERS:
            raw = _check.require_keys(tiers[name], f"experience {eid}.tiers.{name}", {"when", "instructions"})
            instructions = raw["instructions"]
            if not isinstance(instructions, list):
                raise SimpleActioDataError(f"experience {eid}.tiers.{name}.instructions: expected a list, got {instructions!r}")
            try:
                parsed[name] = Tier(instructions=tuple(instructions), when=deepcopy(raw["when"]))
            except SimpleActioDataError as exc:
                raise SimpleActioDataError(f"experience {eid}.tiers.{name}: {exc}") from None
        # 其余字段值的校验在 __post_init__ 里
        return cls(
            id=eid,
            version=d["version"],
            pattern=d["pattern"],
            applies_when=deepcopy(d["applies_when"]),
            **parsed,
        )


def _trigger(node: object, what: str) -> None:
    try:
        validate_trigger(node)
    except InvalidTriggerError as exc:
        raise SimpleActioDataError(f"{what}: {exc}") from None


@dataclass(frozen=True)
class ExperienceActivation:
    experience_id: str
    tier: str  # "primary" | "secondary" | "fallback"
    instructions: tuple[str, ...]


def select_tier(
    exp: ReasoningExperience, history: list[TurnRecord], speaker_slot: str, registry: SignalRegistry,
) -> ExperienceActivation:
    """三级回退:主策略条件成立用主策略,否则次策略条件成立用次策略,否则用保底策略。"""
    for name in ("primary", "secondary"):
        tier: Tier = getattr(exp, name)
        if evaluate_trigger(tier.when, history, speaker_slot, registry):
            return ExperienceActivation(exp.id, name, tier.instructions)
    return ExperienceActivation(exp.id, "fallback", exp.fallback.instructions)


def activate(
    experiences: list[ReasoningExperience] | tuple[ReasoningExperience, ...],
    history: list[TurnRecord],
    speaker_slot: str,
    registry: SignalRegistry,
) -> ExperienceActivation | None:
    """applies_when 成立的经验里取 (版本号降序, id) 排第一的那条,再做三级回退。都不成立返回 None。"""
    applicable = sorted(
        (e for e in experiences if evaluate_trigger(e.applies_when, history, speaker_slot, registry)),
        key=lambda e: (-e.version, e.id),
    )
    if not applicable:
        return None
    return select_tier(applicable[0], history, speaker_slot, registry)
