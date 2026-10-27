from __future__ import annotations

import re

from simulator.datamodel import Persona, RoleAssignment

# 逻辑 episode id 的字符集只含 ASCII,所以字符数就是字节数。
_EPISODE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


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

    slot_order = [b.slot_id for b in role_assignment.bindings]
    duplicated = sorted({s for s in slot_order if slot_order.count(s) > 1})
    if duplicated:
        return f"duplicate slot_id in bindings: {duplicated}"

    bound_slots = role_assignment.slot_ids()
    if client_driven_slot not in bound_slots:
        return (
            f"client_driven_slot {client_driven_slot!r} is not among bound slots "
            f"{sorted(bound_slots)}"
        )

    for binding in role_assignment.bindings:
        if binding.persona_id not in personas:
            return f"binding for slot {binding.slot_id!r} references missing persona {binding.persona_id!r}"
        if binding.model_ref is not None and not binding.model_ref:
            return f"binding for slot {binding.slot_id!r} has an empty model_ref"
        if binding.model_backend == "self_play" and binding.model_ref is not None:
            return (
                f"binding for slot {binding.slot_id!r} is self_play and must not "
                f"carry model_ref {binding.model_ref!r}"
            )

    if role_assignment.max_turns <= 0:
        return f"max_turns must be positive, got {role_assignment.max_turns}"
    if role_assignment.max_turns > max_turns_cap:
        return f"max_turns {role_assignment.max_turns} exceeds cap {max_turns_cap}"

    return None


def validate_episode_id(episode_id: str) -> str | None:
    """CreateSession.episode_id 的格式校验（纯函数）。合格返回 None，否则返回可读原因。

    episode_id 会被用作落盘路径的一段，所以只许 [A-Za-z0-9._-]、1 到 128 字符，
    且不是 "." 或 ".."。必须用 fullmatch：re.match 加 `$` 会放行末尾的换行。
    空串在这里也是不合格；调用方把空串当作"没给"，只对非空的 id 调本函数。"""
    if not _EPISODE_ID.fullmatch(episode_id):
        return "must be 1 to 128 characters from [A-Za-z0-9._-]"
    if episode_id in (".", ".."):
        return f"{episode_id!r} is not allowed"
    return None
