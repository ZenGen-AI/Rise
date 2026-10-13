"""ActioInstance:绑定模型后端、技能、记忆、推理经验与库版本的智能体实例(闭环集成设计 §8.1)。

respond 的参数与 ModelBackend 相同、返回三元组 (utterance, inner_thought, trace),
所以 runner 可以直接把它当后端用(R12),MindAgent 的客户端循环也直接调用它。
本模块不导入 simulator.backends:后端协议在这里按结构重新声明。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from actio.errors import ActioDataError
from actio.experience import ExperienceActivation, ReasoningExperience, activate
from actio.memory import MemoryEntry, retrieve
from actio.signals import SignalRegistry, default_registry
from actio.skills import RouteResult, Skill, check_library, route
from actio.trace import build_trace, normalize_library_version
from actio.triggers import InvalidTriggerError, validate_trigger
from simulator.datamodel import Persona, Scenario, TurnRecord


class PlainBackend(Protocol):
    """与 simulator.backends.ModelBackend 同形:返回 (utterance, inner_thought)。"""

    def respond(
        self, persona: Persona, scenario: Scenario, history: list[TurnRecord], speaker_slot: str,
    ) -> tuple[str, str | None]: ...


@runtime_checkable
class GuidedBackend(Protocol):
    """带指导语的后端协议:guidance 是 Actio 渲染出的指令文本列表(可能为空)。"""

    def respond_guided(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
        guidance: list[str],
    ) -> tuple[str, str | None]: ...


class IgnoreGuidance:
    """把普通后端包装成 GuidedBackend:忽略指导语,原样调用 respond。"""

    def __init__(self, inner: PlainBackend) -> None:
        self.inner = inner

    def respond_guided(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
        guidance: list[str],
    ) -> tuple[str, str | None]:
        return self.inner.respond(persona, scenario, history, speaker_slot)


def as_guided(backend: object) -> GuidedBackend:
    """有 respond_guided 的原样返回;只有 respond 的套 IgnoreGuidance;都没有抛 TypeError。"""
    if isinstance(backend, GuidedBackend):
        return backend
    if callable(getattr(backend, "respond", None)):
        return IgnoreGuidance(backend)
    raise TypeError(f"model_backend must have respond() or respond_guided(), got {type(backend).__name__}")


def render_guidance(
    selected: Skill | None,
    experience: ExperienceActivation | None,
    memory_hits: list[MemoryEntry],
) -> list[str]:
    """指导语的固定顺序:选中技能的策略、推理经验的当前一级、检索到的记忆。"""
    lines: list[str] = []
    if selected is not None:
        lines += [f"[skill:{selected.id}] {text}" for text in selected.policy_instructions]
    if experience is not None:
        lines += [
            f"[experience:{experience.experience_id}/{experience.tier}] {text}"
            for text in experience.instructions
        ]
    lines += [f"[memory:{m.id}] {m.subject}: {m.content}" for m in memory_hits]
    return lines


def _memory_query(history: list[TurnRecord], speaker_slot: str) -> str:
    """V1 的记忆查询词:对方最后一句;对方还没说话时为空串(检索不到任何条目)。"""
    for t in reversed(history):
        if t.speaker_slot != speaker_slot:
            return t.utterance
    return ""


@dataclass(frozen=True)
class ActioInstance:
    backend: GuidedBackend
    skills: tuple[Skill, ...]
    memories: tuple[MemoryEntry, ...]
    experiences: tuple[ReasoningExperience, ...]
    library_version: dict
    profile: dict | None
    registry: SignalRegistry
    memory_limit: int

    def decide(self, history: list[TurnRecord], speaker_slot: str) -> tuple[RouteResult, ExperienceActivation | None, list[MemoryEntry]]:
        """只做路由、经验与记忆,不调模型后端。纯函数(给定实例与输入)。"""
        routed = route(self.skills, history, speaker_slot, self.registry)
        exp = activate(self.experiences, history, speaker_slot, self.registry)
        hits = retrieve(self.memories, _memory_query(history, speaker_slot), limit=self.memory_limit)
        return routed, exp, hits

    def respond(
        self, persona: Persona, scenario: Scenario, history: list[TurnRecord], speaker_slot: str,
    ) -> tuple[str, str | None, dict]:
        routed, exp, hits = self.decide(history, speaker_slot)
        guidance = render_guidance(routed.selected, exp, hits)
        reply = self.backend.respond_guided(persona, scenario, deepcopy(history), speaker_slot, guidance)
        if not isinstance(reply, (tuple, list)) or len(reply) != 2:
            raise TypeError(f"guided backend must return (utterance, inner_thought), got {reply!r}")
        trace = build_trace(
            signals=routed.signals,
            candidates=routed.candidates,
            selected=routed.selected.id if routed.selected is not None else None,
            memory_hits=[m.id for m in hits],
            experience=None if exp is None else (exp.experience_id, exp.tier),
            library_version=self.library_version,
        )
        return reply[0], reply[1], trace


def instantiate(
    model_backend: object,
    *,
    skills: list[Skill] | tuple[Skill, ...],
    memories: list[MemoryEntry] | tuple[MemoryEntry, ...],
    library_version: dict,
    profile: dict | None,
    experiences: list[ReasoningExperience] | tuple[ReasoningExperience, ...] = (),
    registry: SignalRegistry | None = None,
    memory_limit: int = 3,
) -> ActioInstance:
    """组装实例。Actio 不决定用哪个库版本:skills、memories、experiences 由调用方按
    library_version 读好传进来(补充 A.4)。registry 缺省为 default_registry()。
    profile 是调用方的画像元数据,V1 只随实例保存(深拷贝),不参与路由与指导语。"""
    reg = registry if registry is not None else default_registry()
    skills = tuple(skills)
    check_library(skills, reg)
    memories = tuple(memories)
    for m in memories:
        if not isinstance(m, MemoryEntry):
            raise ActioDataError(f"expected MemoryEntry, got {type(m).__name__}")
    experiences = tuple(experiences)
    seen: set[str] = set()
    for e in experiences:
        if not isinstance(e, ReasoningExperience):
            raise ActioDataError(f"expected ReasoningExperience, got {type(e).__name__}")
        if e.id in seen:
            raise ActioDataError(f"duplicate experience id {e.id!r}")
        seen.add(e.id)
        for what, trig in (("applies_when", e.applies_when), ("primary", e.primary.when), ("secondary", e.secondary.when)):
            try:
                validate_trigger(trig, reg)
            except InvalidTriggerError as exc:
                raise ActioDataError(f"experience {e.id}.{what}: {exc}") from None
    if isinstance(memory_limit, bool) or not isinstance(memory_limit, int) or memory_limit < 0:
        raise ActioDataError(f"memory_limit: expected a non-negative int, got {memory_limit!r}")
    if profile is not None and not isinstance(profile, dict):
        raise ActioDataError(f"profile: expected a dict or None, got {type(profile).__name__}")
    return ActioInstance(
        backend=as_guided(model_backend),
        skills=skills,
        memories=memories,
        experiences=experiences,
        library_version=normalize_library_version(library_version),
        profile=deepcopy(profile),
        registry=reg,
        memory_limit=memory_limit,
    )
