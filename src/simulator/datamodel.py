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
