"""SoMBench 导入:把 SoMEval 检出目录里的数据集变成树内资产与树外污染索引(设计 21 的 DX-16 到 DX-19)。

版本号由 parquet 的 sha256 前 12 位生成,数据集一变版本就跟着变;运行器与准入检查装配时重新
算哈希并与版本比对,对不上就拒绝。本模块只在 `import-sombench` 里被调用,parquet 读取要
`pyarrow`,放在函数内部导入,所以运行时的评测与准入检查不需要它。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

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
