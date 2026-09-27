from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Persona:
    """人格模板（§2.3）。与 Scenario 完全解耦，通过 RoleAssignment 组合。"""

    id: str
    traits: list[str]
    motivation: str
    speech_style: str
    values: list[str]
    source: str = "auto"  # "auto" | "external"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "traits": list(self.traits),
            "motivation": self.motivation,
            "speech_style": self.speech_style,
            "values": list(self.values),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Persona:
        return cls(
            id=d["id"],
            traits=list(d["traits"]),
            motivation=d["motivation"],
            speech_style=d["speech_style"],
            values=list(d["values"]),
            source=d.get("source", "auto"),
        )


@dataclass
class RoleSlot:
    """Scenario 里的抽象角色位（尚未绑定具体 Persona）。"""

    slot_id: str
    description: str

    def to_dict(self) -> dict:
        return {"slot_id": self.slot_id, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> "RoleSlot":
        return cls(slot_id=d["slot_id"], description=d["description"])


@dataclass
class Scenario:
    """社会情境（§2.3）。purpose 决定难度分布；与 Persona 完全解耦。"""

    id: str
    background: str
    role_slots: list[RoleSlot]
    conflict: str
    norms: list[str]
    purpose: str  # "training" | "deployment_simulation"
    source: str = "auto"  # "auto" | "external"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "background": self.background,
            "role_slots": [r.to_dict() for r in self.role_slots],
            "conflict": self.conflict,
            "norms": list(self.norms),
            "purpose": self.purpose,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        return cls(
            id=d["id"],
            background=d["background"],
            role_slots=[RoleSlot.from_dict(r) for r in d["role_slots"]],
            conflict=d["conflict"],
            norms=list(d["norms"]),
            purpose=d["purpose"],
            source=d.get("source", "auto"),
        )

