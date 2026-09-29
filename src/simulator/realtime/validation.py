from __future__ import annotations

from simulator.datamodel import Persona, RoleAssignment


def validate_create_session(
    role_assignment: RoleAssignment,
    personas: dict[str, Persona],
    client_driven_slot: str,
    *,
    max_turns_cap: int,
) -> str | None:
    """CreateSession 结构校验（纯函数）。返回第一条违规的可读原因；全部合法返回 None。

    这是 §2.6.1 "校验通过即直接进入 ACTIVE" 中"校验"的落地：必须在启动
    SessionDriver 之前执行，任何一条不通过都不得启动 driver——否则会出现
    未绑定的 client_driven_slot 导致无限自主轮次、缺失 persona 在 run_episode
    里抛 KeyError 被误判为 role_backend_failure 等问题。这些都是客户端输入
    错误（T2 类），应以 invalid_session 明确拒绝，而不是归因到服务端后端。
    """
    if not role_assignment.bindings:
        return "role_assignment has no bindings"

    bound_slots = role_assignment.slot_ids()
    if client_driven_slot not in bound_slots:
        return (
            f"client_driven_slot {client_driven_slot!r} is not among bound slots "
            f"{sorted(bound_slots)}"
        )

    for binding in role_assignment.bindings:
        if binding.persona_id not in personas:
            return f"binding for slot {binding.slot_id!r} references missing persona {binding.persona_id!r}"

    if role_assignment.max_turns <= 0:
        return f"max_turns must be positive, got {role_assignment.max_turns}"
    if role_assignment.max_turns > max_turns_cap:
        return f"max_turns {role_assignment.max_turns} exceeds cap {max_turns_cap}"

    return None
