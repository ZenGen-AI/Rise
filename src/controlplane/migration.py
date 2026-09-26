from __future__ import annotations

from typing import Callable

from controlplane.envelope import Envelope, CURRENT_SCHEMA_VERSION
from controlplane.validate import validate_envelope

Migrator = Callable[[dict], dict]

# 键为源版本号 N，值为把 vN 升到 vN+1 的纯函数。V1 无历史版本，空链。
MIGRATORS: dict[int, Migrator] = {}


class UnknownSchemaVersionError(Exception):
    """产物 schema_version 高于当前版本，或缺少对应迁移器。"""


def migrate(d: dict) -> dict:
    """惰性迁移：把任意历史版本产物在内存里升到当前版本。不改写 git 树原文件。"""
    version = d["schema_version"]
    if version > CURRENT_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(
            f"schema_version {version} > current {CURRENT_SCHEMA_VERSION}"
        )
    current = d
    while current["schema_version"] < CURRENT_SCHEMA_VERSION:
        v = current["schema_version"]
        migrator = MIGRATORS.get(v)
        if migrator is None:
            raise UnknownSchemaVersionError(f"no migrator registered for schema_version {v}")
        current = migrator(current)
    return current


def load_product(d: dict) -> Envelope:
    """业务读取产物的唯一入口：迁移 → 校验 → 反序列化。禁止绕过本函数直接反序列化。"""
    migrated = migrate(d)
    validate_envelope(migrated)
    return Envelope.from_dict(migrated)
