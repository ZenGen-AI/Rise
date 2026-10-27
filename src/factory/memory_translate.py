"""记忆异常的转换后端(场景二记忆设计 §10)。

simulator 不认识 actio.MemoryFailureError(依赖方向不允许导入 actio),只认 backend_timeout 里的两个
记忆基类。组合根给每个 Actio 实例外面套一层本模块的转换后端:接住 MemoryFailureError,按 transient
改抛 FactoryMemoryUnavailableError(T1)或 FactoryMemoryFailureError(T2),驱动器据此归
role_memory_unavailable 与 role_memory_failure。

只接 MemoryFailureError 这一个基类,所以 MemoryEventConflict、MemoryClosedError 也被映射,不会落进
驱动器的 role_backend_failure。模型后端自己的异常(例如 OpponentApiError)原样透传。
异常文字只含槽位与异常类名,不带原异常的文字:原文可能含端点地址。"""
from __future__ import annotations

from collections.abc import Mapping

from actio.instance import ActioInstance, instantiate
from actio.memory_port import MemoryBackend, MemoryFailureError
from simulator.datamodel import Persona, Scenario, TurnRecord
from simulator.realtime.backend_timeout import RoleMemoryFailureError, RoleMemoryUnavailableError

# 训练路的角色位不读技能库;与 mindagent.library 空库同一写法。
ROLE_LIBRARY_VERSION = {"skill": "0", "memory": "0"}


class FactoryMemoryUnavailableError(RoleMemoryUnavailableError):
    """角色位记忆后端瞬态失败(MemoryFailureError.transient 为真)。"""


class FactoryMemoryFailureError(RoleMemoryFailureError):
    """角色位记忆后端确定性失败(MemoryFailureError.transient 为假)。"""


class MemoryTranslatingBackend:
    """包一个 ActioInstance:respond 与 close 里的 MemoryFailureError 按 transient 改抛 simulator 的记忆基类。

    不实现 respond_guided:它是角色位最外层,服务端与轨迹源只调 respond。"""

    def __init__(self, instance: ActioInstance) -> None:
        self.instance = instance

    def respond(
        self, persona: Persona, scenario: Scenario, history: list[TurnRecord], speaker_slot: str,
    ) -> tuple[str, str | None, dict]:
        try:
            return self.instance.respond(persona, scenario, history, speaker_slot)
        except MemoryFailureError as exc:
            message = f"memory backend failed for slot {speaker_slot!r}: {type(exc).__name__}"
            if exc.transient:
                raise FactoryMemoryUnavailableError(message) from exc
            raise FactoryMemoryFailureError(message) from exc

    def close(self) -> None:
        """关实例的记忆端口。训练位副本关不上按确定性处理(设计 §7.2),瞬态也不例外。"""
        try:
            self.instance.close()
        except MemoryFailureError as exc:
            raise FactoryMemoryFailureError(
                f"memory backend failed while closing: {type(exc).__name__}"
            ) from exc


def memory_role_backend(
    inner: object, *, memory: MemoryBackend, episode_id: str, interlocutors: Mapping[str, str],
) -> MemoryTranslatingBackend:
    """在给定记忆端口上造一个角色位的 Actio 实例,外套转换后端。对手位与训练位都只经它造实例。"""
    instance = instantiate(
        inner,
        skills=(),
        library_version=ROLE_LIBRARY_VERSION,
        profile=None,
        memory=memory,
        episode_id=episode_id,
        interlocutors=dict(interlocutors),
    )
    return MemoryTranslatingBackend(instance)
