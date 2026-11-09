"""记忆端口:值类型、异常与两个协议(场景二记忆设计 §4.1,设计三 §3.2 与 §6)。

simple_actio 只认这里的两个协议,不认任何具体的记忆后端。`MemoryBackend` 是一个主体(人格)在一个世界里
打开的一个端口,`DeploymentMemoryStore` 是部署管理面,按世界 id 管库、按快照 id 管快照(40 篇 5.1 节)。"""
from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PendingKey:
    """一条待处理事件:事件键与写下它的主体。同一 key 在两个主体名下是两条事件,放弃要指明主体。"""

    key: str
    holder: str


@dataclass(frozen=True)
class SnapshotRef:
    """一份 held 快照:id 由管理面在拍快照时定,digest 是快照时世界的逻辑摘要。"""

    snapshot_id: str
    digest: str


class MemoryFailureError(Exception):
    """记忆后端失败。transient 区分瞬态(可重试)与永久失败。

    刻意不继承 SimpleActioDataError/ValueError:驱动器与轨迹源按 transient 分 T1/T2,数据错误另有分支。
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
    """部署管理面:按世界 id 打开(世界, 主体)端口、登记与放弃事件、拍快照与恢复;从快照分叉只读的重放世界。
    登记、放弃事件与开端口分主体,列待处理事件、摘要、快照与恢复按整个世界。
    `open`、`remember_event`、`digest`、`wait_closed` 只收部署世界 id;重放世界只经 `open_replay` 打开,
    两种世界由调用点区分,不按名字前缀推断(40 篇 AC-34)。
    isinstance 只按方法名检查。"""

    def open(self, world_id: str, holder: str) -> MemoryBackend:
        """打开该部署世界里 holder 的可写记忆端口。"""
        ...

    def remember_event(
        self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str,
    ) -> RememberOutcome:
        """以事件键登记并记住一件事,记在 holder 名下。"""
        ...

    def abandon_event(self, world_id: str, holder: str, key: str) -> None:
        """放弃 holder 名下的一个待处理事件。同一 key 在两个主体名下是两条事件,只动这一条;找不到抛
        MemoryFailureError(transient=False),已提交抛 MemoryEventConflict,已放弃是空操作。"""
        ...

    def pending_events(self, world_id: str) -> list[PendingKey]:
        """该世界全部主体尚未处理完的事件,按 seq 升序。主体为空串的行判永久失败。"""
        ...

    def snapshot(self, world_id: str, *, note: str) -> SnapshotRef:
        """拍整个世界的 held 快照,返回快照 id 与快照时的摘要。note 让人认得出这份快照。"""
        ...

    def restore(self, world_id: str, snapshot_id: str) -> str:
        """把整个世界恢复到这份快照,返回恢复后的逻辑摘要。该世界还有任何主体的端口开着、快照不在,都抛
        MemoryFailureError(transient=False);别的世界开着端口不影响。"""
        ...

    def delete_snapshot(self, world_id: str, snapshot_id: str) -> None:
        """删一份快照。快照已不在是空操作(两个实现都幂等)。"""
        ...

    def fork_replay(self, world_id: str, snapshot_id: str, *, round_id: str) -> str:
        """从快照分叉一个重放世界,返回它的完整世界名 `<前缀>.verify.<round_id>.<world_id>.<后缀>`;
        调用方原样传给 `open_replay` 与 `release_replay`。"""
        ...

    def release_replay(self, replay_world_id: str) -> None:
        """不再用这个重放世界。"""
        ...

    def open_replay(self, replay_world: str, holder: str) -> MemoryBackend:
        """在 `fork_replay` 返回的重放世界上开 holder 的只读端口:写入抛永久 MemoryFailureError。
        世界不在时抛永久失败,不建。一个重放世界上可以开多个只读端口。"""
        ...

    def digest(self, world_id: str) -> str:
        """该世界的逻辑摘要:sha256 小写十六进制,覆盖全部主体。"""
        ...

    def wait_closed(self, world_id: str, timeout_s: float) -> bool:
        """等该部署世界打开着的端口数降到 0:到了返回 True,超过 timeout_s 返回 False。端口数由管理面在
        实例内按完整世界名计数,别的线程开关的端口也算,所以组合根只能造一个管理面;重放世界的端口记在
        重放世界名下,不挡部署世界。"""
        ...
