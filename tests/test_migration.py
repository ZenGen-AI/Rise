import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.migration import migrate, load_product, MIGRATORS, UnknownSchemaVersionError


def _v1_dict() -> dict:
    return Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
    ).to_dict()


def test_v1_chain_is_empty():
    assert MIGRATORS == {}


def test_current_version_passthrough():
    d = _v1_dict()
    assert migrate(d) == d


def test_load_product_returns_envelope():
    env = load_product(_v1_dict())
    assert isinstance(env, Envelope)
    assert env.id == "run-001"


def test_future_version_beyond_current_raises():
    d = _v1_dict()
    d["schema_version"] = 99
    with pytest.raises(UnknownSchemaVersionError):
        migrate(d)


def test_migrate_applies_registered_migrator(monkeypatch):
    # 模拟未来存在 v1->v2 迁移器：读取路径必须能把旧版本升上来
    def v1_to_v2(doc: dict) -> dict:
        doc = dict(doc)
        doc["schema_version"] = 2
        doc.setdefault("payload", {})["migrated"] = True
        return doc

    monkeypatch.setattr("controlplane.migration.MIGRATORS", {1: v1_to_v2})
    monkeypatch.setattr("controlplane.migration.CURRENT_SCHEMA_VERSION", 2)
    out = migrate(_v1_dict())
    assert out["schema_version"] == 2
    assert out["payload"]["migrated"] is True
