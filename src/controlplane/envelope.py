from __future__ import annotations

from dataclasses import dataclass, field

CURRENT_SCHEMA_VERSION = 1


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


@dataclass
class BigRef:
    """大体量产物引用（不入 git 树）。fetch_hint 为 F.5 导入导出税，V1 可为 None。"""

    uri: str
    sha256: str
    size_bytes: int
    fetch_hint: dict | None = None  # {"type": ..., "url": ...} or None

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
