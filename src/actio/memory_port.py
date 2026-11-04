"""记忆端口:值类型、异常与两个协议(场景二记忆设计 §4.1,设计三 §3.2 与 §6)。

actio 只认这里的两个协议,不认任何具体的记忆后端。`MemoryBackend` 是一个主体(人格)在一个世界里
打开的一个端口,`DeploymentMemoryStore` 是部署管理面,按世界 id 管库。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# 世界 id 与主体(人格 id)拼进库文件名之前的统一校验,两个部署管理面都用,两者共用这一份白名单。
PERSONA_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class MemoryHit:
    """一次 recall 的一条命中。id 是内容 id(mem- 前缀),同一 recall 结果里不重复。"""

    id: str
    text: str


@dataclass(frozen=True)
class MemoryEvent:
    """一件待记住的事。key 是事件的稳定身份;没有 seq,序号由库在写事件行时分配,实例不计数。"""

    key: str
    text: str
    interlocutor: str


@dataclass(frozen=True)
class RememberOutcome:
    """remember 的结果:stored 是这次落下的语句 id;failed 为真表示抽取失败,category 取
    Starling 的 failure_category,没有失败时为 None。"""

    stored: tuple[str, ...]
    failed: bool
    category: str | None


class MemoryFailureError(Exception):
    """记忆后端失败。transient 区分瞬态(可重试)与永久失败。

    刻意不继承 ActioDataError/ValueError:驱动器与轨迹源按 transient 分 T1/T2,数据错误另有分支。
    异常信息只放异常类名或 failure_category,不含端点地址与密钥。"""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class MemoryEventConflict(MemoryFailureError):
    """同 key 已存在且文本不同,或该 key 已放弃。transient 恒为假。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


class MemoryClosedError(MemoryFailureError):
    """端口关闭后再调 recall/remember/embed_pending/digest。transient 恒为假。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


@runtime_checkable
class MemoryBackend(Protocol):
    """一个主体在一个世界里的一个记忆端口:世界与主体在打开时绑定,方法签名里没有它们。
    isinstance 只按方法名检查。"""

    def recall(self, query: str, *, limit: int) -> list[MemoryHit]:
        """按 query 取最多 limit 条命中。结果里的 id 不重复。"""
        ...

    def remember(self, event: MemoryEvent) -> RememberOutcome:
        """记住一件事。同 key 同文本重写返回同一结果;同 key 异文本,或该 key 已放弃,抛
        MemoryEventConflict。"""
        ...

    def embed_pending(self) -> int:
        """只做嵌入,返回成功条数;有失败就抛瞬态 MemoryFailureError(transient=True)。"""
        ...

    def digest(self) -> str:
        """逻辑摘要:sha256 小写十六进制。"""
        ...

    def close(self) -> None:
        """只关库,不放弃任何事件。关闭后再调 recall/remember/embed_pending/digest 抛
        MemoryClosedError;再调 close 是空操作(调用方的 finally 可能各关一次)。"""
        ...


@runtime_checkable
class DeploymentMemoryStore(Protocol):
    """部署管理面:按世界 id 打开(世界, 主体)端口、登记与放弃事件、备份与恢复。世界级操作不分主体。
    isinstance 只按方法名检查。"""

    def open(self, world_id: str, holder: str) -> MemoryBackend:
        """打开该世界里 holder 的记忆端口。"""
        ...

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """以事件键登记并记住一件事,记在 holder 名下。"""
        ...

    def abandon_event(self, world_id: str, key: str) -> None:
        """放弃一个待处理事件。事件键已带主体,所以不分主体。"""
        ...

    def pending_events(self, world_id: str) -> list[str]:
        """该世界尚未处理完的事件键,不分主体。"""
        ...

    def backup_to(self, world_id: str, path: Path) -> str:
        """备份整个世界到 path,返回副本的逻辑摘要。"""
        ...

    def restore_from(self, world_id: str, path: Path) -> str:
        """从 path 恢复整个世界,返回恢复后的逻辑摘要。该世界还有任何主体的端口开着时抛
        MemoryFailureError(transient=False);别的世界开着端口不影响。"""
        ...

    def digest(self, world_id: str) -> str:
        """该世界的逻辑摘要:sha256 小写十六进制,覆盖全部主体。"""
        ...

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 返回 False。端口数由管理面在
        实例内计数,别的线程开关的端口也算,所以组合根只能造一个管理面。"""
        ...
