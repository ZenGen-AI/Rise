from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Callable

from simulator.datamodel import (
    Persona,
    Scenario,
    RoleAssignment,
    Episode,
    TurnRecord,
)
from simulator.backends import ModelBackend


class RoleSlotMismatchError(ValueError):
    """RoleAssignment 引用了 Scenario 中不存在的角色位。"""


def run_episode(
    scenario: Scenario,
    assignment: RoleAssignment,
    personas: dict[str, Persona],
    backends: dict[str, ModelBackend],
    episode_id: str,
    stop_predicate: "Callable[[list], bool] | None" = None,
    on_turn: "Callable[[TurnRecord], None] | None" = None,
) -> Episode:
    """Simulation Runner 核心（§2.4 纯函数）：给定情境、角色分配、人格池、
    每个角色位的模型后端替身，驱动多轮交互产出全新 Episode。

    纯函数：不做任何 IO；同输入同输出。批量模块与实时服务模块共享本核心
    （补充 C.2），区别只在外层包装（异步落盘 vs 同步会话状态维持）。

    校验（fail-loud）：
    - assignment 的每个 slot_id 必须存在于 scenario.role_slots，否则 RoleSlotMismatchError
    - 每个 binding 的 persona_id 必须存在于 personas，否则 KeyError
    """
    valid_slots = {r.slot_id for r in scenario.role_slots}
    for slot in assignment.slot_ids():
        if slot not in valid_slots:
            raise RoleSlotMismatchError(
                f"RoleAssignment 引用了 Scenario 中不存在的角色位: {slot}"
            )

    if not assignment.bindings:
        raise RoleSlotMismatchError(
            "RoleAssignment 未包含任何 binding，无法驱动交互"
        )

    turns: list[TurnRecord] = []
    n_roles = len(assignment.bindings)
    for turn_index in range(assignment.max_turns):
        binding = assignment.bindings[turn_index % n_roles]
        if binding.persona_id not in personas:
            raise KeyError(
                f"binding 引用了不存在的 persona: {binding.persona_id}"
            )
        persona = personas[binding.persona_id]
        backend = backends[binding.slot_id]
        # 每个后端只能看到自己角色位的内部推理/激活轨迹：其他角色位的这两项在
        # 传给它的 history 里被抹掉，否则对手能偷看被训练模型的"内心独白"，训练
        # 出的策略学的就是"对手作弊"条件下的行为。完整记录仍留在 turns/Episode
        # 里。两类轮次都必须是副本：自己角色位的轮次也要深拷贝（activation_trace
        # 是可变 dict），否则后端改自己的历史会直接污染 Episode。
        history = [
            deepcopy(t) if t.speaker_slot == binding.slot_id
            else replace(t, inner_thought=None, activation_trace=None)
            for t in turns
        ]
        utterance, inner_thought = backend.respond(
            persona, scenario, history, binding.slot_id
        )
        turns.append(
            TurnRecord(
                turn_index=turn_index,
                speaker_slot=binding.slot_id,
                utterance=utterance,
                inner_thought=inner_thought,
                activation_trace=None,  # V1 裸模型：无激活轨迹（§2.6.3）
            )
        )
        if on_turn is not None:
            on_turn(turns[-1])
        if stop_predicate is not None and stop_predicate(turns):
            return Episode(
                id=episode_id, scenario_id=scenario.id,
                role_assignment=assignment, turns=turns,
                terminated_reason="goal_reached",
            )

    return Episode(
        id=episode_id,
        scenario_id=scenario.id,
        role_assignment=assignment,
        turns=turns,
        terminated_reason="max_turns",
    )
