from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

CURRENT_SCHEMA_VERSION = 1

# sha256 的唯一口径:64 位小写十六进制,即 hashlib.sha256().hexdigest() 的形状。
# 训练配置、配方校验、mock 执行器与转换器都引用这一份(设计 CP-16)。
# 用 \Z 而不是 $:match 时 $ 还会匹配结尾换行之前,带 "\n" 的值会被放行。
SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")


class InvalidBigRefError(TypeError):
    """BigRef 的字段不合规。

    继承 TypeError 而不是 ValueError:各读树点已把 KeyError、TypeError、AttributeError 当成
    「确定性读不出」(指令 failed、轮次 failed);ValueError 会从这些分支漏出去,在派发器里被
    当成瞬态失败,指令保持 pending、每个 tick 重派。
    """


def _shown(value) -> str:
    """错误信息会进日志与失败记录:超长的坏值只露头部。"""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


@dataclass
class OwnerRef:
    """归属标识（F.5 多租户税）。V1 单开发者恒为默认值，不参与逻辑判断。"""

    owner: str = "default"
    visibility: str = "shared"  # "private" | "shared"

    def to_dict(self) -> dict:
        return {"owner": self.owner, "visibility": self.visibility}

    @classmethod
    def from_dict(cls, d: dict) -> "OwnerRef":
        return cls(owner=d["owner"], visibility=d["visibility"])


@dataclass(frozen=True)
class BigRef:
    """大体量产物引用（不入 git 树）。fetch_hint 为 F.5 导入导出税，V1 可为 None。

    构造即校验且不可变(设计 I8、CP-16):任何实例的 sha256 都是 64 位小写十六进制,
    所以可以直接拼进路径与作业参数。dataclasses.replace 也走构造,同样校验。
    """

    uri: str
    sha256: str
    size_bytes: int
    fetch_hint: dict | None = None  # {"type": ..., "url": ...} or None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri:
            raise InvalidBigRefError(f"BigRef.uri must be a non-empty str, got {_shown(self.uri)}")
        if not isinstance(self.sha256, str) or not SHA256_HEX.match(self.sha256):
            raise InvalidBigRefError(
                f"BigRef.sha256 must be 64 lowercase hex chars, got {_shown(self.sha256)}"
            )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise InvalidBigRefError(
                f"BigRef.size_bytes must be an int >= 0 (0 when unknown), got {_shown(self.size_bytes)}"
            )
        if self.fetch_hint is not None and not isinstance(self.fetch_hint, dict):
            raise InvalidBigRefError(
                f"BigRef.fetch_hint must be a dict or None, got {type(self.fetch_hint).__name__}"
            )

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "fetch_hint": self.fetch_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BigRef":
        return cls(
            uri=d["uri"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            fetch_hint=d.get("fetch_hint"),
        )


@dataclass
class Envelope:
    """所有产物的通用信封。schema_version 为 F.5 迁移税，必须存在。"""

    id: str
    type: str
    version: int
    created_at: str
    created_by: OwnerRef
    status: str
    schema_version: int
    links: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "created_at": self.created_at,
            "created_by": self.created_by.to_dict(),
            "status": self.status,
            "schema_version": self.schema_version,
            "links": list(self.links),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Envelope":
        return cls(
            id=d["id"],
            type=d["type"],
            version=d["version"],
            created_at=d["created_at"],
            created_by=OwnerRef.from_dict(d["created_by"]),
            status=d["status"],
            schema_version=d["schema_version"],
            links=list(d.get("links", [])),
            payload=d.get("payload", {}),
        )


def bump(new: Envelope, previous: Envelope | None) -> Envelope:
    """改写一个已有产物、载荷形状变了时用:返回 `new` 的副本,`version` 取上一份的 `version + 1`
    (`new` 自带的版本号更大就保留,版本号只升不降)。`previous` 为 None 是第一次写,原样返回 `new`。

    只动信封的 `version`(同一个产物的第几版),不动 `schema_version`:后者是迁移器链的版本,升了读取方会
    抛 UnknownSchemaVersionError。读取方只要求 `version` 是不小于 1 的整数,所以历史版本号的产物照常读得出。
    不改入参;载荷与链接是 `new` 的同一批对象,调用方此后不应再改它们。"""
    if previous is None:
        return new
    return replace(new, version=max(new.version, previous.version + 1))
