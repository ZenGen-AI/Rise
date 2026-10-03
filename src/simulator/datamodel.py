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


@dataclass
class RoleBinding:
    """把一个抽象角色位绑定到具体 Persona + 具体模型后端（§2.2 关键接口约定）。
    model_backend 由调用方指定，SocialSimulator 内部不写死。"""

    slot_id: str
    persona_id: str
    model_backend: str  # "self_play" | "frozen_reference" | "external_api"
    # 具体模型制品引用(如 "zengen-v1")。frozen_reference / external_api 必填,
    # self_play 必须为 None。None 时不进 to_dict,既有 Episode 的序列化与哈希保持不变。
    model_ref: str | None = None

    def to_dict(self) -> dict:
        d = {
            "slot_id": self.slot_id,
            "persona_id": self.persona_id,
            "model_backend": self.model_backend,
        }
        if self.model_ref is not None:
            d["model_ref"] = self.model_ref
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RoleBinding":
        return cls(
            slot_id=d["slot_id"],
            persona_id=d["persona_id"],
            model_backend=d["model_backend"],
            model_ref=d.get("model_ref"),
        )


@dataclass
class RoleAssignment:
    """角色分配（Runner 的核心输入）：将 Scenario 的角色位与 Personas + 模型后端绑定。"""

    scenario_id: str
    bindings: list[RoleBinding]
    max_turns: int

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "bindings": [b.to_dict() for b in self.bindings],
            "max_turns": self.max_turns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoleAssignment":
        return cls(
            scenario_id=d["scenario_id"],
            bindings=[RoleBinding.from_dict(b) for b in d["bindings"]],
            max_turns=d["max_turns"],
        )

    def slot_ids(self) -> set[str]:
        """返回所有被绑定的角色位 ID。"""
        return {b.slot_id for b in self.bindings}


@dataclass
class TurnRecord:
    """单次交互记录（在剧集中按顺序追加）。"""

    turn_index: int
    speaker_slot: str
    utterance: str
    inner_thought: str | None = None
    activation_trace: dict | None = None

    def to_dict(self) -> dict:
        return {
            "turn_index": self.turn_index,
            "speaker_slot": self.speaker_slot,
            "utterance": self.utterance,
            "inner_thought": self.inner_thought,
            "activation_trace": self.activation_trace,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TurnRecord":
        return cls(
            turn_index=d["turn_index"],
            speaker_slot=d["speaker_slot"],
            utterance=d["utterance"],
            inner_thought=d.get("inner_thought"),
            activation_trace=d.get("activation_trace"),
        )


@dataclass
class Episode:
    """单次剧集运行结果：Scenario × RoleAssignment 的一次实例化。"""

    id: str
    scenario_id: str
    role_assignment: RoleAssignment
    turns: list[TurnRecord]
    terminated_reason: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "role_assignment": self.role_assignment.to_dict(),
            "turns": [t.to_dict() for t in self.turns],
            "terminated_reason": self.terminated_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        return cls(
            id=d["id"],
            scenario_id=d["scenario_id"],
            role_assignment=RoleAssignment.from_dict(d["role_assignment"]),
            turns=[TurnRecord.from_dict(t) for t in d["turns"]],
            terminated_reason=d["terminated_reason"],
        )

