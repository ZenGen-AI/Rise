"""诊断资产(分类表与题库)作为信封产物的读写。

controlplane.paths 只给了 taxonomy_file / item_pool_file 两个路径函数,没有读写代码;这里补上。
产物类型:diagnostic_taxonomy、diagnostic_item_pool;状态 effective(登记即生效,不走审核门)。
载荷就是 DiagnosticTaxonomy.to_dict() / ItemPool.to_dict()。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from diagnostic.datamodel import DiagnosticTaxonomy
from diagnostic.paths import item_pool_file, taxonomy_file
from diagnostic.taxonomy_loader import ItemPool

TAXONOMY_TYPE = "diagnostic_taxonomy"
ITEM_POOL_TYPE = "diagnostic_item_pool"


class _Versioned(Protocol):
    @property
    def version(self) -> str: ...


_A = TypeVar("_A", bound=_Versioned)

# 资产信封的 created_at。值不能改:install-defaults 按"已提交内容相同"判断能否重装,值一变,
# 已装过默认资产的仓库就会被拒绝。
_ASSET_CREATED_AT = "2026-10-09T00:00:00Z"


class DiagnosticAssetError(Exception):
    """诊断资产缺失,或那个路径上放的不是对应类型的产物。"""


def _envelope(id_: str, type_: str, payload: dict) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=_ASSET_CREATED_AT, created_by=OwnerRef(),
        status="effective", schema_version=CURRENT_SCHEMA_VERSION, links=[], payload=payload,
    )


def _read(store: GitStore, rel: str, expected_type: str) -> Envelope:
    if not store.exists(rel):
        raise DiagnosticAssetError(f"diagnostic asset missing: {rel}")
    env = store.read_product(rel)
    if env.type != expected_type:
        raise DiagnosticAssetError(f"{rel} is a {env.type!r} product, not {expected_type!r}")
    return env


def _write_asset(store: GitStore, rel: str, id_: str, type_: str, label: str, version: str, payload: dict) -> str:
    """写一份资产产物,提交信息是 diagnostic: <label> <version>。返回树内路径。"""
    store.write_product(rel, _envelope(id_, type_, payload), f"diagnostic: {label} {version}")
    return rel


def _read_asset(
    store: GitStore, rel: str, type_: str, label: str, version: str, from_dict: Callable[[dict], _A],
) -> _A:
    """读一份资产产物并核对载荷里的版本号与路径上的一致。"""
    asset = from_dict(_read(store, rel, type_).payload)
    if asset.version != version:
        raise DiagnosticAssetError(f"{rel} carries {label} version {asset.version!r}, not {version!r}")
    return asset


def write_taxonomy(store: GitStore, taxonomy: DiagnosticTaxonomy) -> str:
    v = taxonomy.version
    return _write_asset(store, taxonomy_file(v), f"taxonomy-{v}", TAXONOMY_TYPE, "taxonomy", v, taxonomy.to_dict())


def read_taxonomy(store: GitStore, version: str) -> DiagnosticTaxonomy:
    return _read_asset(
        store, taxonomy_file(version), TAXONOMY_TYPE, "taxonomy", version, DiagnosticTaxonomy.from_dict,
    )


def write_item_pool(store: GitStore, pool: ItemPool) -> str:
    v = pool.version
    return _write_asset(store, item_pool_file(v), f"item-pool-{v}", ITEM_POOL_TYPE, "item pool", v, pool.to_dict())


def read_item_pool(store: GitStore, version: str) -> ItemPool:
    return _read_asset(store, item_pool_file(version), ITEM_POOL_TYPE, "item pool", version, ItemPool.from_dict)
