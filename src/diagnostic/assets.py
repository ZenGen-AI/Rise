"""诊断资产(分类表与题库)作为信封产物的读写。

controlplane.paths 只给了 taxonomy_file / item_pool_file 两个路径函数,没有读写代码;这里补上。
产物类型:diagnostic_taxonomy、diagnostic_item_pool;状态 effective(登记即生效,不走审核门)。
载荷就是 DiagnosticTaxonomy.to_dict() / ItemPool.to_dict()。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from diagnostic.datamodel import DiagnosticTaxonomy
from diagnostic.paths import item_pool_file, taxonomy_file
from diagnostic.taxonomy_loader import ItemPool

TAXONOMY_TYPE = "diagnostic_taxonomy"
ITEM_POOL_TYPE = "diagnostic_item_pool"

_NOW = "2026-10-09T00:00:00Z"


class DiagnosticAssetError(Exception):
    """诊断资产缺失,或那个路径上放的不是对应类型的产物。"""


def _envelope(id_: str, type_: str, payload: dict) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=_NOW, created_by=OwnerRef(),
        status="effective", schema_version=CURRENT_SCHEMA_VERSION, links=[], payload=payload,
    )


def _read(store: GitStore, rel: str, expected_type: str) -> Envelope:
    if not store.exists(rel):
        raise DiagnosticAssetError(f"diagnostic asset missing: {rel}")
    env = store.read_product(rel)
    if env.type != expected_type:
        raise DiagnosticAssetError(f"{rel} is a {env.type!r} product, not {expected_type!r}")
    return env


def write_taxonomy(store: GitStore, taxonomy: DiagnosticTaxonomy) -> str:
    rel = taxonomy_file(taxonomy.version)
    store.write_product(
        rel, _envelope(f"taxonomy-{taxonomy.version}", TAXONOMY_TYPE, taxonomy.to_dict()),
        f"diagnostic: taxonomy {taxonomy.version}",
    )
    return rel


def read_taxonomy(store: GitStore, version: str) -> DiagnosticTaxonomy:
    taxonomy = DiagnosticTaxonomy.from_dict(_read(store, taxonomy_file(version), TAXONOMY_TYPE).payload)
    if taxonomy.version != version:
        raise DiagnosticAssetError(
            f"{taxonomy_file(version)} carries taxonomy version {taxonomy.version!r}, not {version!r}"
        )
    return taxonomy


def write_item_pool(store: GitStore, pool: ItemPool) -> str:
    rel = item_pool_file(pool.version)
    store.write_product(
        rel, _envelope(f"item-pool-{pool.version}", ITEM_POOL_TYPE, pool.to_dict()),
        f"diagnostic: item pool {pool.version}",
    )
    return rel


def read_item_pool(store: GitStore, version: str) -> ItemPool:
    pool = ItemPool.from_dict(_read(store, item_pool_file(version), ITEM_POOL_TYPE).payload)
    if pool.version != version:
        raise DiagnosticAssetError(
            f"{item_pool_file(version)} carries item pool version {pool.version!r}, not {version!r}"
        )
    return pool
