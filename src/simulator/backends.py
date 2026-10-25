from __future__ import annotations

from typing import Protocol

from simulator.datamodel import Persona, Scenario, RoleSlot, TurnRecord


class ModelBackend(Protocol):
    """驱动某个角色发言的模型后端。调用方（Recipe）为每个角色位指定具体后端
    （self_play / frozen_reference / external_api）。Runner 只依赖本协议，
    不关心背后是自博弈、冻结模型还是外部 API。"""

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        """返回 (utterance, inner_thought)。inner_thought 可为 None。"""
        ...


class GenerationBackend(Protocol):
    """模板生成后端。V1 用内部模板参数化；外部推理桥接后端留待后续计划实现，
    其凭证走环境变量变量名引用，不在本协议出现任何凭证值字段。"""

    def make_persona(self, seed: int) -> Persona: ...

    def make_scenario(self, seed: int, purpose: str) -> Scenario: ...


class ScriptedBackend:
    """ModelBackend 的 V1 确定性实现：按 slot_id 预置罐头话语队列，
    每次 respond 出队下一句。用于脱离真实 LLM 的 fixture 测试（§2.4）。"""

    def __init__(self, scripted: dict[str, list[str]], *, loop: bool = False) -> None:
        # 复制成可变游标，不改动调用方传入的列表
        self._queues: dict[str, list[str]] = {k: list(v) for k, v in scripted.items()}
        self._cursor: dict[str, int] = {k: 0 for k in self._queues}
        # loop 为真时剧本用尽后从头再来；为假时给出收场白（V1 原行为）
        self._loop = loop

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        queue = self._queues.get(speaker_slot, [])
        idx = self._cursor.get(speaker_slot, 0)
        if idx < len(queue):
            self._cursor[speaker_slot] = idx + 1
            return queue[idx], None
        if self._loop and queue:
            self._cursor[speaker_slot] = 1
            return queue[0], None
        return "(无更多发言)", None


_TRAIT_POOL = [
    ["谨慎", "共情"],
    ["果断", "直率"],
    ["好奇", "灵活"],
]
_MOTIVATION_POOL = ["维护团队和谐", "达成个人目标", "追求真相"]
_STYLE_POOL = ["温和、迂回", "简洁、有力", "热情、发散"]
_VALUES_POOL = [["集体优先"], ["效率优先"], ["公平优先"]]
_BACKGROUND_POOL = [
    "两位同事就项目方向产生分歧",
    "邻里因公共空间使用起争执",
    "团队复盘一次失败的发布",
]
_CONFLICT_POOL = ["资源有限下的方案取舍", "责任归属的界定", "短期与长期利益的权衡"]
_NORMS_POOL = [["不得人身攻击", "尊重对方发言"], ["就事论事"], ["保持礼貌"]]


class TemplateGenerationBackend:
    """GenerationBackend 的 V1 确定性实现：用内置模板池 + seed 取模选择。
    完全确定性，同 seed 同输出，便于 fixture 测试。"""

    def make_persona(self, seed: int) -> Persona:
        i = seed % len(_TRAIT_POOL)
        return Persona(
            id=f"pers-auto-{seed}",
            traits=list(_TRAIT_POOL[i]),
            motivation=_MOTIVATION_POOL[seed % len(_MOTIVATION_POOL)],
            speech_style=_STYLE_POOL[seed % len(_STYLE_POOL)],
            values=list(_VALUES_POOL[seed % len(_VALUES_POOL)]),
            source="auto",
        )

    def make_scenario(self, seed: int, purpose: str) -> Scenario:
        i = seed % len(_BACKGROUND_POOL)
        return Scenario(
            id=f"scen-auto-{seed}",
            background=_BACKGROUND_POOL[i],
            role_slots=[
                RoleSlot(slot_id="role_a", description="冲突的一方"),
                RoleSlot(slot_id="role_b", description="冲突的另一方"),
            ],
            conflict=_CONFLICT_POOL[seed % len(_CONFLICT_POOL)],
            norms=list(_NORMS_POOL[seed % len(_NORMS_POOL)]),
            purpose=purpose,
            source="auto",
        )
