"""SoMBench 导入:把 SoMEval 检出目录里的数据集变成树内资产与树外污染索引(设计 21 的 DX-16 到 DX-19)。

版本号由 parquet 的 sha256 前 12 位生成,数据集一变版本就跟着变;运行器与准入检查装配时重新
算哈希并与版本比对,对不上就拒绝。本模块只在 `import-sombench` 里被调用,parquet 读取要
`pyarrow`,放在函数内部导入,所以运行时的评测与准入检查不需要它。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from controlplane.gitstore import GitStore
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.contamination import build_index
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.taxonomy_loader import ItemPool
from factory.errors import FactoryConfigError

VERSION_PREFIX = "sombench-"
HASH_HEX = 12
TASK_CONFIG_NAME = "SoMBench"


def sombench_version(parquet_sha256: str) -> str:
    """数据集哈希前 12 位进版本号。判错的代价:同一个哈希前缀在两次不同内容上碰撞,版本会重名。"""
    hex_digest = str(parquet_sha256).strip().lower()
    if len(hex_digest) < HASH_HEX or any(ch not in "0123456789abcdef" for ch in hex_digest):
        raise FactoryConfigError("a dataset sha256 must be a hex string of at least 12 characters")
    return f"{VERSION_PREFIX}{hex_digest[:HASH_HEX]}"


def check_sombench_version(version: str, parquet_sha256: str) -> None:
    """资产版本必须由当前数据集算出。装配期调用,拒绝不匹配的资产与数据集组合。"""
    expected = sombench_version(parquet_sha256)
    if version != expected:
        raise FactoryConfigError(
            f"the diagnostic assets are versioned {version!r} but the dataset hashes to {expected!r}; "
            "re-run import-sombench"
        )


def file_sha256(path: Path) -> str:
    """整份文件的哈希。parquet 只有约 3 MB,一次读完。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SomebenchImport:
    """一次导入的结果。路径都相对仓库根;索引在树外,单独给绝对路径。"""

    taxonomy_version: str
    item_pool_version: str
    items: int
    families: int
    windows: int
    index_path: Path
    dataset_sha256: str


TEXT_FIELDS = ("story", "question")
ANSWER_FIELDS = ("correct_answers", "wrong_answers")


def sombench_parquet(root: Path) -> Path:
    return Path(root) / "datasets" / TASK_CONFIG_NAME / "test-00000-of-00001.parquet"


def _split_id(row_id: str) -> tuple[str, str, list[str]]:
    """`1.1.1_0_first_person|base|Q1` 切成 (头部, 变体, 其余段)。Q3 的 id 多一段 `|item1`。"""
    parts = row_id.split("|")
    if len(parts) < 3:
        raise FactoryConfigError(f"unexpected SoMBench id shape: {row_id!r}")
    return parts[0], parts[1], parts[2:]


def _base_id(row_id: str) -> str:
    """变体行去掉 `_varA`/`_varB` 后缀、换回 base 段,得到它对应的基题 id。"""
    head, variant, rest = _split_id(row_id)
    if variant == "base":
        return row_id
    stripped = head
    for suffix in ("_varA", "_varB"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return "|".join([stripped, "base", *rest])


def _item_type(meta: dict) -> str:
    qtype = str(meta.get("qtype", ""))
    if qtype == "Q4":
        return "open"
    return "mcq"


def build_assets(rows: list[dict], dataset_sha256: str) -> tuple[DiagnosticTaxonomy, ItemPool]:
    """从 parquet 行建分类表与题库。题库只存 id、维度、题型、变体族:题面、选项、答案一律不进树。"""
    version = sombench_version(dataset_sha256)
    seen: set[str] = set()
    items: list[DiagnosticItem] = []
    l2_to_l3: dict[str, set[str]] = {}
    l1_to_l2: dict[str, set[str]] = {}
    variants: dict[str, list[str]] = {}

    for row in rows:
        meta = row.get("meta") or {}
        row_id = meta.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise FactoryConfigError("a SoMBench row has no meta.id")
        if row_id in seen:
            raise FactoryConfigError(f"duplicate SoMBench id: {row_id!r}")
        seen.add(row_id)
        l3 = meta.get("dim")
        l2 = meta.get("dim2")
        l1 = meta.get("dim1")
        if not all(isinstance(v, str) and v for v in (l3, l2, l1)):
            raise FactoryConfigError(f"SoMBench row {row_id!r} has no dim/dim2/dim1")
        l2_to_l3.setdefault(l2, set()).add(l3)
        l1_to_l2.setdefault(l1, set()).add(l2)
        variant = _split_id(row_id)[1]
        family_id = f"fam:{_base_id(row_id)}" if variant in ("varA", "varB") else None
        if variant in ("varA", "varB"):
            variants.setdefault(_base_id(row_id), []).append(row_id)
        items.append(DiagnosticItem(
            id=row_id, l3_task=l3, item_type=_item_type(meta), scoring_mode="someval",
            track="static", reference_answer=None, rubric=None, family_id=family_id,
        ))

    families = [
        VariantFamily(f"fam:{base}", base, sorted(children))
        for base, children in sorted(variants.items())
    ]
    taxonomy = DiagnosticTaxonomy(
        version=version,
        l1_dimensions=sorted(l1_to_l2),
        l2_dimensions={l1: sorted(children) for l1, children in sorted(l1_to_l2.items())},
        l3_tasks={l2: sorted(children) for l2, children in sorted(l2_to_l3.items())},
        g_probes=[],
    )
    pool = ItemPool(version=version, items=items, variant_families=families)
    return taxonomy, pool


def index_texts(rows: list[dict]) -> list[str]:
    """进污染索引的文本:故事、问题、全部选项、Q4 参考答案。题面之外的内容不进来,减少误命中。"""
    texts: list[str] = []
    for row in rows:
        for field in TEXT_FIELDS:
            value = row.get(field)
            if isinstance(value, str):
                texts.append(value)
        answer = row.get("answer") or {}
        for field in ANSWER_FIELDS:
            for value in answer.get(field) or ():
                if isinstance(value, str):
                    texts.append(value)
        for value in (row.get("meta") or {}).get("q4_reference") or ():
            if isinstance(value, str):
                texts.append(value)
    return texts


def read_rows(parquet_path: Path) -> list[dict]:
    """唯一用到 pyarrow 的地方。缺依赖时给出可执行的提示,不静默降级。"""
    try:
        import pyarrow.parquet as parquet
    except ImportError:
        raise FactoryConfigError(
            "import-sombench needs pyarrow; run it with `uv run --extra sombench`"
        ) from None
    return parquet.read_table(parquet_path).to_pylist()


def import_assets(
    store: GitStore, rows: list[dict], *, dataset_sha256: str, index_path: Path
) -> SomebenchImport:
    """写树内分类表与题库、写树外污染索引。内容相同不产生新提交。不读 parquet,便于脱离 pyarrow 测试。"""
    digest = dataset_sha256
    taxonomy, pool = build_assets(rows, digest)
    write_taxonomy(store, taxonomy)
    write_item_pool(store, pool)

    data = build_index(index_texts(rows), digest)
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(data)
    return SomebenchImport(
        taxonomy_version=taxonomy.version,
        item_pool_version=pool.version,
        items=len(pool.items),
        families=len(pool.variant_families),
        windows=len(data) // 8 - 6,
        index_path=index_path,
        dataset_sha256=digest,
    )


def import_sombench(store: GitStore, *, someval_root: Path, index_path: Path) -> SomebenchImport:
    """读 parquet 再导入。parquet 缺失或哈希算不出来都在动手写之前报。"""
    parquet_path = sombench_parquet(Path(someval_root))
    if not parquet_path.is_file():
        raise FactoryConfigError(f"the SoMBench parquet is not at {parquet_path}")
    return import_assets(store, read_rows(parquet_path), dataset_sha256=file_sha256(parquet_path),
                         index_path=index_path)
