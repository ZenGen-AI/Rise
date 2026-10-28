"""ActioInstance:绑定模型后端、技能、记忆端口、推理经验与库版本的智能体实例(闭环集成设计 §8.1,
场景二记忆设计 §4)。

respond 的参数与 ModelBackend 相同、返回三元组 (utterance, inner_thought, trace),
所以 runner 可以直接把它当后端用(R12),MindAgent 的客户端循环也直接调用它。
本模块不导入 simulator.backends:后端协议在这里按结构重新声明。
记忆经 MemoryBackend 端口读(召回)与摘要;非只读实例在回复之后把对方的新发言写进端口(remember,
抽取失败在 episode 内重试一次)。每次 respond 的轨迹都是 v2,带 memory 块。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypeVar, runtime_checkable

from actio import _check
from actio.errors import ActioDataError
from actio.experience import ExperienceActivation, ReasoningExperience, activate
from actio.memory import MemoryEntry, TupleMemory
from actio.memory_port import MemoryBackend, MemoryEvent, MemoryFailureError, MemoryHit
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
    memory_hits: list[MemoryHit],
) -> list[str]:
    """指导语的固定顺序:选中技能的策略、推理经验的当前一级、召回的记忆。

    后端把指导语一行一条拼进 system 消息,所以每条文本里的行界(str.splitlines 认的全集,不只 \\n)
    各折成一个空格:记忆原文来自对方发言,否则一个换行就能伪造出一条 [skill:...] 要点。
    折完只剩空白的条目整条丢掉,不产出空行。"""
    candidates: list[tuple[str, str]] = []
    if selected is not None:
        candidates += [(f"[skill:{selected.id}]", text) for text in selected.policy_instructions]
    if experience is not None:
        candidates += [
            (f"[experience:{experience.experience_id}/{experience.tier}]", text)
            for text in experience.instructions
        ]
    candidates += [(f"[memory:{h.id}]", h.text) for h in memory_hits]
    lines: list[str] = []
    for tag, text in candidates:
        folded = " ".join(text.splitlines())
        if folded.strip():
            # 标签里的 id 也来自库或端口,整行一起折,保证一条就是一行。
            lines.append(" ".join(f"{tag} {folded}".splitlines()))
    return lines


def _memory_query(history: list[TurnRecord], speaker_slot: str) -> str:
    """V1 的记忆查询词:对方最后一句;对方还没说话时为空串(此时 decide 不调 recall)。"""
    for t in reversed(history):
        if t.speaker_slot != speaker_slot:
            return t.utterance
    return ""


_T = TypeVar("_T")


def _guard(call: Callable[[], _T]) -> _T:
    """读路径上的端口调用:MemoryFailureError 原样上抛(transient 不被改写);其余异常一律按永久失败
    改抛 MemoryFailureError,只报类名,不带可能含端点或密钥的原文(Starling 之外的异常都非瞬态)。"""
    try:
        return call()
    except MemoryFailureError:
        raise
    except Exception as exc:
        raise MemoryFailureError(type(exc).__name__, transient=False) from None


@dataclass
class _MemoryState:
    """每条 episode 的可变计数。配置字段不可变、这些计数可变,所以分开放。
    seen_turn:写路径已处理到的回合序号;retry:上一轮没写成、要在下一轮重试的事件;
    remembered / extraction_failed:本实例累计的记住条数与抽取失败次数。"""

    seen_turn: int = -1
    retry: tuple[MemoryEvent, ...] = ()
    remembered: int = 0
    extraction_failed: int = 0


@dataclass(frozen=True)
class ActioInstance:
    """配置不可变,每 episode 的计数在 _state 里。一个带写入的实例(replay_read_only 为假)只服务
    一条 episode(episode_id 在构造时定),并且只许一条线程用;只读实例没有这两条限制。"""

    backend: GuidedBackend
    skills: tuple[Skill, ...]
    memory: MemoryBackend
    experiences: tuple[ReasoningExperience, ...]
    library_version: dict
    profile: dict | None
    registry: SignalRegistry
    memory_limit: int
    replay_read_only: bool
    episode_id: str | None
    interlocutors: dict[str, str]
    _state: _MemoryState = field(default_factory=_MemoryState, repr=False, compare=False)

    def decide(
        self, history: list[TurnRecord], speaker_slot: str,
    ) -> tuple[RouteResult, ExperienceActivation | None, list[MemoryHit]]:
        """路由、经验与记忆召回,不调模型后端、不写库;召回经端口,所以不再是纯函数。
        对方还没开口时查询词为空,不调 recall。"""
        routed = route(self.skills, history, speaker_slot, self.registry)
        exp = activate(self.experiences, history, speaker_slot, self.registry)
        query = _memory_query(history, speaker_slot)
        hits = _guard(lambda: self.memory.recall(query, limit=self.memory_limit)) if query else []
        return routed, exp, hits

    def respond(
        self, persona: Persona, scenario: Scenario, history: list[TurnRecord], speaker_slot: str,
    ) -> tuple[str, str | None, dict]:
        digest_before = _guard(self.memory.digest)
        routed, exp, hits = self.decide(history, speaker_slot)
        guidance = render_guidance(routed.selected, exp, hits)
        reply = self.backend.respond_guided(persona, scenario, deepcopy(history), speaker_slot, guidance)
        if not isinstance(reply, (tuple, list)) or len(reply) != 2:
            raise TypeError(f"guided backend must return (utterance, inner_thought), got {reply!r}")
        if not self.replay_read_only:
            self._remember_new(history, speaker_slot)
        trace = build_trace(
            signals=routed.signals,
            candidates=routed.candidates,
            selected=routed.selected.id if routed.selected is not None else None,
            memory_hits=[h.id for h in hits],
            experience=None if exp is None else (exp.experience_id, exp.tier),
            library_version=self.library_version,
            memory={
                "digest_before": digest_before,
                "remembered": str(self._state.remembered),
                "extraction_failed": str(self._state.extraction_failed),
            },
        )
        return reply[0], reply[1], trace

    def _remember_new(self, history: list[TurnRecord], speaker_slot: str) -> None:
        """写路径:先重试上一轮抽取失败的事件,再写 history 里对方的新发言,最后 embed_pending 一次。
        事件 key 用 history 里的 turn_index 而不是实例计数:重放与断线重连后重建的 history 给出同一批 key,
        端口的去重才拦得住。抽取失败只计数:来自旧重试表的丢掉(episode 内只重试一次,行留 pending,
        由 MindAgent 在 episode 最终结束时处理),来自新发言的进下一轮的重试表。不调 tick。
        状态在 remember 全部返回之后一次提交:中途抛出时 seen_turn、重试表与计数都不动,调用方重试
        同一份 history 会重发同样的事件(同 key 同文本,端口幂等),不丢事件也不重复计数。"""
        state = self._state
        fresh: list[MemoryEvent] = []
        for t in history:
            if t.turn_index > state.seen_turn and t.speaker_slot != speaker_slot:
                pid = self.interlocutors.get(t.speaker_slot, t.speaker_slot)
                fresh.append(MemoryEvent(f"{self.episode_id}#{t.turn_index}", f"{pid}：{t.utterance}", pid))

        def store(event: MemoryEvent) -> bool:
            return _guard(lambda: self.memory.remember(event)).failed

        remembered = failed = 0
        retry: list[MemoryEvent] = []
        for event in state.retry:
            if store(event):
                failed += 1
            else:
                remembered += 1
        for event in fresh:
            if store(event):
                failed += 1
                retry.append(event)
            else:
                remembered += 1
        state.remembered += remembered
        state.extraction_failed += failed
        state.retry = tuple(retry)
        if history:
            # 单调:调用方给了更短的 history(比如重连重建到一半)也不回退,已写过的回合不再重发
            state.seen_turn = max(state.seen_turn, max(t.turn_index for t in history))
        _guard(self.memory.embed_pending)

    def close(self) -> None:
        """关掉记忆端口。不放弃任何事件:放弃由 MindAgent 在 episode 最终结束时经部署管理面做。"""
        self.memory.close()


def instantiate(
    model_backend: object,
    *,
    skills: list[Skill] | tuple[Skill, ...],
    library_version: dict,
    profile: dict | None,
    memories: list[MemoryEntry] | tuple[MemoryEntry, ...] | None = None,
    memory: MemoryBackend | None = None,
    episode_id: str | None = None,
    interlocutors: dict[str, str] | None = None,
    replay_read_only: bool | None = None,
    experiences: list[ReasoningExperience] | tuple[ReasoningExperience, ...] = (),
    registry: SignalRegistry | None = None,
    memory_limit: int = 3,
) -> ActioInstance:
    """组装实例。Actio 不决定用哪个库版本:skills、记忆、experiences 由调用方按
    library_version 读好传进来(补充 A.4)。registry 缺省为 default_registry()。
    profile 是调用方的画像元数据,V1 只随实例保存(深拷贝),不参与路由与指导语。

    记忆二选一:memories= 是简写,包成内存替身 TupleMemory,默认只读;memory= 直接给一个
    MemoryBackend 端口,默认会写库。replay_read_only 显式给出时以它为准。会写库的实例必须有
    episode_id(事件 key 的前缀),不论记忆是怎么给的。interlocutors 是 slot -> 对话者人格 id。"""
    reg = registry if registry is not None else default_registry()
    skills = tuple(skills)
    check_library(skills, reg)
    if (memories is None) == (memory is None):
        raise ActioDataError("exactly one of memories= and memory= is required")
    if memories is not None:
        port: MemoryBackend = TupleMemory(tuple(memories))  # 条目类型与同版本冲突在这里查
    else:
        if not isinstance(memory, MemoryBackend):
            raise ActioDataError(f"memory: expected a MemoryBackend, got {type(memory).__name__}")
        port = memory
    if replay_read_only is None:
        replay_read_only = memories is not None
    elif not isinstance(replay_read_only, bool):
        raise ActioDataError(f"replay_read_only: expected a bool or None, got {replay_read_only!r}")
    if episode_id is not None:
        _check.text(episode_id, "episode_id")
    elif not replay_read_only:
        raise ActioDataError("episode_id: required when the instance writes memory")
    if interlocutors is None:
        interlocutors = {}
    elif not isinstance(interlocutors, dict):
        raise ActioDataError(f"interlocutors: expected a dict or None, got {type(interlocutors).__name__}")
    else:
        for slot, persona_id in interlocutors.items():
            _check.text(slot, "interlocutors key")
            _check.text(persona_id, f"interlocutors[{slot!r}]")
        interlocutors = dict(interlocutors)
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
        memory=port,
        experiences=experiences,
        library_version=normalize_library_version(library_version),
        profile=deepcopy(profile),
        registry=reg,
        memory_limit=memory_limit,
        replay_read_only=replay_read_only,
        episode_id=episode_id,
        interlocutors=interlocutors,
    )
