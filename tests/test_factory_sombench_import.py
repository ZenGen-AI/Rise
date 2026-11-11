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


# ---------------- 导入:分类表、题库、变体族、索引 ----------------

from controlplane.gitstore import GitStore
from diagnostic.assets import read_item_pool, read_taxonomy
from factory.sombench import build_assets, import_assets, index_texts, sombench_parquet

STORY = (
    "下午三点，Livehouse 彩排刚结束，我作为键盘手兼临时舞监助理在后台核对当晚的曲单。"
    "主办方在前一天临时通知，原定返场曲《潮汐》因版权问题须替换为《北回归线》，这条变更只发到了我和吉他手陈澈的工作邮箱。"
)
SMALL_STORY = "彩排结束后，苏曼去开会，回来时笑着说一切照旧，我们继续调音。" * 3


def _row(row_id, *, dim="1.1.1", dim2="1.1", dim1="1", qtype="Q1", story=STORY):
    return {
        "story": story,
        "question": f"问题 {row_id}：在这一刻，他实际持有的信念是什么？" + "细节" * 12,
        "answer": {"correct_answers": [f"正确答案 {row_id} " + "甲" * 30],
                   "wrong_answers": [f"错误答案 {row_id} " + "乙" * 30]},
        "meta": {"id": row_id, "dim": dim, "dim2": dim2, "dim1": dim1, "qtype": qtype,
                 "q4_reference": ["评分参考 " + "丙" * 30] if qtype == "Q4" else None},
    }


ROWS = [
    _row("1.1.1_0_first_person|base|Q1"),
    _row("1.1.1_0_first_person|base|Q3|item1", qtype="Q3"),
    _row("1.1.1_0_first_person_varA|varA|Q1", story=SMALL_STORY),
    _row("1.1.1_0_first_person_varB|varB|Q1", story=SMALL_STORY),
    _row("1.2.1_0_third_person|base|Q4", dim="1.2.1", dim2="1.2", qtype="Q4"),
    _row("1.1.1_0_first_person|hardest|Q1"),
]


def _store(tmp_path):
    store = GitStore(str(tmp_path / "repo"))
    store.init()
    return store


def test_the_taxonomy_has_three_levels_and_no_g_probes():
    taxonomy, _ = build_assets(ROWS, DIGEST)
    assert taxonomy.l1_dimensions == ["1"]
    assert taxonomy.l2_dimensions == {"1": ["1.1", "1.2"]}
    assert taxonomy.l3_tasks["1.1"] == ["1.1.1"]
    assert taxonomy.l3_tasks["1.2"] == ["1.2.1"]
    assert taxonomy.g_probes == []


def test_every_row_becomes_an_item_and_none_carry_text():
    _, pool = build_assets(ROWS, DIGEST)
    assert [item.id for item in pool.items] == [row["meta"]["id"] for row in ROWS]
    assert all(item.scoring_mode == "someval" for item in pool.items)
    assert all(item.reference_answer is None and item.rubric is None for item in pool.items)
    by_id = {item.id: item for item in pool.items}
    assert by_id["1.2.1_0_third_person|base|Q4"].item_type == "open"
    assert by_id["1.1.1_0_first_person|base|Q1"].item_type == "mcq"


def test_only_var_a_and_var_b_join_variant_families():
    _, pool = build_assets(ROWS, DIGEST)
    assert [family.base_item_id for family in pool.variant_families] == ["1.1.1_0_first_person|base|Q1"]
    assert pool.variant_families[0].variant_item_ids == [
        "1.1.1_0_first_person_varA|varA|Q1", "1.1.1_0_first_person_varB|varB|Q1"]


def test_a_duplicate_id_is_refused():
    with pytest.raises(FactoryConfigError, match="duplicate"):
        build_assets(ROWS + [ROWS[0]], DIGEST)


def test_a_row_without_dimensions_is_refused():
    broken = _row("1.1.1_9_first_person|base|Q1")
    broken["meta"]["dim"] = ""
    with pytest.raises(FactoryConfigError, match="dim"):
        build_assets([broken], DIGEST)


def test_the_import_writes_assets_and_an_index_outside_the_tree(tmp_path):
    store = _store(tmp_path)
    index_path = tmp_path / "index" / "sombench.idx"
    result = import_assets(store, ROWS, dataset_sha256=DIGEST, index_path=index_path)
    assert result.items == len(ROWS) and result.families == 1
    assert read_taxonomy(store, result.taxonomy_version).version == result.taxonomy_version
    assert read_item_pool(store, result.item_pool_version).version == result.item_pool_version
    assert index_path.is_file() and index_path.read_bytes().startswith(b"SBIDX1\0\0")
    assert result.windows > 0
    # 索引不在仓库里
    assert not (tmp_path / "repo" / "index").exists()


def test_the_tree_contains_no_story_text(tmp_path):
    """树内资产序列化之后不含故事里的任何 20 字片段:这是 G8 的第一道防线。"""
    from diagnostic.contamination import normalize_text

    store = _store(tmp_path)
    result = import_assets(store, ROWS, dataset_sha256=DIGEST, index_path=tmp_path / "i.idx")
    texts = []
    for rel in ("control-plane/diagnostics/taxonomy_%s.yaml" % result.taxonomy_version,
                "control-plane/diagnostics/item_pool_%s.yaml" % result.item_pool_version):
        texts.append(normalize_text((tmp_path / "repo" / rel).read_text(encoding="utf-8")))
    blob = "".join(texts)
    normalized = normalize_text(STORY)
    assert normalized[:20] not in blob
    assert normalized[20:40] not in blob


def test_the_import_is_idempotent(tmp_path):
    store = _store(tmp_path)
    import_assets(store, ROWS, dataset_sha256=DIGEST, index_path=tmp_path / "i.idx")
    before = len(store.list_products("control-plane/diagnostics"))
    import_assets(store, ROWS, dataset_sha256=DIGEST, index_path=tmp_path / "i.idx")
    assert len(store.list_products("control-plane/diagnostics")) == before


def test_index_texts_covers_story_question_options_and_q4_reference():
    texts = index_texts(ROWS)
    assert STORY in texts
    assert any(text.startswith("问题 ") for text in texts)
    assert any(text.startswith("正确答案 ") for text in texts)
    assert any(text.startswith("错误答案 ") for text in texts)
    assert any(text.startswith("评分参考 ") for text in texts)


def test_the_parquet_path_is_below_the_checkout():
    assert sombench_parquet(Path("/x/SoMEval")).as_posix().endswith(
        "/datasets/SoMBench/test-00000-of-00001.parquet")


def test_the_real_dataset_imports_to_the_documented_shape(tmp_path):
    """真实数据集的一次性核对:3481 题、71 个 L3、529 个变体族。没有 SoMEval 检出目录时跳过。"""
    root = Path.home() / "develop" / "SoMEval"
    parquet_path = sombench_parquet(root)
    if not parquet_path.is_file():
        pytest.skip("SoMEval checkout without the SoMBench parquet")
    pytest.importorskip("pyarrow")
    from factory.sombench import read_rows

    store = _store(tmp_path)
    result = import_assets(store, read_rows(parquet_path),
                           dataset_sha256="5ace4bd4b872" + "0" * 52, index_path=tmp_path / "i.idx")
    pool = read_item_pool(store, result.item_pool_version)
    taxonomy = read_taxonomy(store, result.taxonomy_version)
    assert result.items == 3481
    assert len(taxonomy.l3_tasks) == 17
    assert sum(len(v) for v in taxonomy.l3_tasks.values()) == 71
    assert result.families == 529
    assert len(pool.items) == 3481
