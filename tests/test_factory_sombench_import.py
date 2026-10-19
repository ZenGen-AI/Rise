"""SoMBench 版本号:由数据集哈希前 12 位生成,不匹配就拒绝装配。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from factory.errors import FactoryConfigError
from factory.sombench import check_sombench_version, file_sha256, sombench_version

DIGEST = "5ace4bd4b8726e5b" + "0" * 48   # 64 位十六进制,前 12 位与真实数据集一致


def test_the_version_is_the_first_twelve_hex_digits_of_the_dataset_hash():
    assert sombench_version(DIGEST) == "sombench-5ace4bd4b872"


@pytest.mark.parametrize("bad", ["", "not-hex", "abc", "5ace4bd4b872zz"])
def test_a_hash_that_is_not_hexadecimal_is_refused(bad):
    with pytest.raises(FactoryConfigError):
        sombench_version(bad)


def test_a_matching_version_passes_and_a_stale_one_is_refused():
    version = sombench_version(DIGEST)
    check_sombench_version(version, DIGEST)
    with pytest.raises(FactoryConfigError, match="import-sombench"):
        check_sombench_version("sombench-000000000000", DIGEST)


def test_the_error_never_echoes_a_whole_hash_beyond_the_short_form():
    """错误里只出现 12 位版本号,不出现完整哈希。"""
    with pytest.raises(FactoryConfigError) as caught:
        check_sombench_version("sombench-000000000000", DIGEST)
    assert DIGEST not in str(caught.value)


def test_file_sha256_hashes_the_whole_file(tmp_path):
    path = tmp_path / "data.parquet"
    path.write_bytes(b"sombench" * 1000)
    assert file_sha256(path) == hashlib.sha256(b"sombench" * 1000).hexdigest()
    path.write_bytes(b"sombench")
    assert file_sha256(path) == hashlib.sha256(b"sombench").hexdigest()


def test_file_sha256_reads_in_chunks_correctly(tmp_path):
    """块大小远小于文件时,分块读不丢不重(用 2 MiB 以上的输入跨过默认块边界)。"""
    path = tmp_path / "big.parquet"
    payload = (bytes(range(256)) * 9000)          # 2.25 MiB
    path.write_bytes(payload)
    assert file_sha256(path) == hashlib.sha256(payload).hexdigest()
