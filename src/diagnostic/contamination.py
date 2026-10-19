"""污染索引与训练数据准入检查(设计 21 的 DX-19):SoMBench 的文本不进训练。

索引只存 20 字滑窗的 8 字节哈希,不存原文。判定是逐字重叠,改写与释义抓不到,这条限制写进设计。
"""
from __future__ import annotations

import json
import unicodedata
from array import array
from collections.abc import Iterable
from pathlib import Path

WINDOW = 20
MAGIC = b"SBIDX1\0\0"
HEADER = 8 + 8 + 32          # 魔数 + 窗口数 + 数据集 sha256
_DIGEST_SIZE = 8


def normalize_text(text: str) -> str:
    """NFKC 规范化、转小写、只留字母数字(含汉字)。空白与标点都丢掉,所以换行、全角标点不影响判定。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


def _window_digest(window: str) -> int:
    import hashlib

    return int.from_bytes(hashlib.blake2b(window.encode("utf-8"), digest_size=_DIGEST_SIZE).digest(), "little")


def shingles(text: str, *, window: int = WINDOW) -> set[int]:
    """每个 20 字窗口的哈希。规范化后短于窗口长度的文本没有窗口。"""
    normalized = normalize_text(text)
    if len(normalized) < window:
        return set()
    return {_window_digest(normalized[i:i + window]) for i in range(len(normalized) - window + 1)}


def build_index(texts: Iterable[str], dataset_sha256: str) -> bytes:
    """升序去重的窗口哈希 + 头部。头部记录数据集哈希,读取时与现算的比对,数据集换了就拒绝。"""
    digest = bytes.fromhex(dataset_sha256)
    if len(digest) != 32:
        raise ValueError("dataset_sha256 must be 64 hexadecimal characters")
    windows: set[int] = set()
    for text in texts:
        windows.update(shingles(text))
    ordered = array("Q", sorted(windows))
    header = MAGIC + len(ordered).to_bytes(8, "little") + digest
    return header + ordered.tobytes()


def load_index(data: bytes, dataset_sha256: str) -> array:
    """校验魔数、长度与数据集哈希后返回升序的窗口哈希。任何不符都拒绝,不静默降级。"""
    if len(data) < HEADER or data[:8] != MAGIC:
        raise ValueError("the contamination index has a bad header")
    count = int.from_bytes(data[8:16], "little")
    if len(data) != HEADER + count * 8:
        raise ValueError("the contamination index has a truncated or padded body")
    if data[16:48] != bytes.fromhex(dataset_sha256):
        raise ValueError("the contamination index was built from a different dataset")
    index = array("Q")
    index.frombytes(data[HEADER:])
    return index


def load_index_file(path: Path, dataset_sha256: str) -> array:
    return load_index(Path(path).read_bytes(), dataset_sha256)


def count_hits(index: array, text: str, *, window: int = WINDOW) -> int:
    """这段文本里落在索引中的窗口数。索引升序,用二分查找,近似直线地扫一遍。"""
    from bisect import bisect_left

    normalized = normalize_text(text)
    if len(normalized) < window:
        return 0
    hits = 0
    for i in range(len(normalized) - window + 1):
        digest = _window_digest(normalized[i:i + window])
        pos = bisect_left(index, digest)
        if pos < len(index) and index[pos] == digest:
            hits += 1
    return hits


def parse_train_jsonl(data: bytes) -> list[tuple[str, str]]:
    """每行一个对象,取 instruction 与 output。坏数据抛 ValueError:调用方按确定性失败处理。"""
    rows: list[tuple[str, str]] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"train.jsonl line {number} is not JSON: {exc.msg}") from None
        if not isinstance(record, dict):
            raise ValueError(f"train.jsonl line {number} is not an object")
        instruction, output = record.get("instruction"), record.get("output")
        if not isinstance(instruction, str) or not isinstance(output, str):
            raise ValueError(f"train.jsonl line {number} needs string instruction and output")
        rows.append((instruction, output))
    return rows


def make_train_gate(index: array, *, window: int = WINDOW):
    """返回准入检查:收整份 train.jsonl 字节,返回 (有命中的行数, 命中窗口总数)。数据坏了抛 ValueError。

    原因里不带任何文本:命中就整条指令失败,人只需要知道有多少。
    """

    def gate(data: bytes) -> tuple[int, int]:
        bad_lines = 0
        hits = 0
        for instruction, output in parse_train_jsonl(data):
            row_hits = count_hits(index, instruction, window=window) + count_hits(index, output, window=window)
            if row_hits:
                bad_lines += 1
                hits += row_hits
        return bad_lines, hits

    return gate
