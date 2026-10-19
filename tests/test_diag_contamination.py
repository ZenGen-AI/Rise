"""污染索引与训练数据准入检查:逐字拷贝被抓到,删一句仍被抓到,无关中文不被误伤。"""
from __future__ import annotations

import hashlib
import json

import pytest

from diagnostic.contamination import (
    HEADER,
    build_index,
    count_hits,
    load_index,
    make_train_gate,
    normalize_text,
    parse_train_jsonl,
    shingles,
)

DATASET = "5ace4bd4b8726e5b" + "0" * 48
STORY = (
    "下午三点，Livehouse 彩排刚结束，我作为键盘手兼临时舞监助理在后台核对当晚的曲单。"
    "主办方在前一天临时通知，原定返场曲《潮汐》因版权问题须替换为《北回归线》，这条变更只发到了我和吉他手陈澈的工作邮箱。"
    "彩排前，乐队经理苏曼被叫去小会议室与赞助方单独谈了二十分钟，出来后她神情如常，我没敢问。"
)


def _index(*texts):
    return load_index(build_index(texts, DATASET), DATASET)


def test_normalization_drops_punctuation_and_whitespace_but_keeps_chinese():
    assert normalize_text("周屿 说：“按原计划吧！”\n") == "周屿说按原计划吧"


def test_short_text_has_no_windows():
    assert shingles("短句") == set()


def test_a_long_text_has_one_window_per_position():
    text = "".join(chr(0x4E00 + i) for i in range(25))   # 25 个互不相同的字
    assert len(shingles(text)) == 25 - 20 + 1


def test_index_round_trips_and_sorts():
    index = _index("甲" * 30)
    assert list(index) == sorted(index)
    assert count_hits(index, "甲" * 30) == 11


def test_a_verbatim_copy_is_caught():
    index = _index(STORY)
    assert count_hits(index, STORY) > 100


def test_deleting_the_last_sentence_is_still_caught():
    """词元相似度对中文做不到这件事:整段被当成一个词,删一句后相似度归零。"""
    index = _index(STORY)
    trimmed = STORY.split("。")[0] + "。"
    assert count_hits(index, trimmed) > 0


def test_unrelated_chinese_is_not_flagged():
    index = _index(STORY)
    assert count_hits(index, "今天天气不错，我们去公园散步，顺便买了两杯咖啡。") == 0


def test_a_different_dataset_is_refused():
    built = build_index([STORY], DATASET)
    with pytest.raises(ValueError, match="different dataset"):
        load_index(built, "0" * 64)


def test_a_bad_header_is_refused():
    with pytest.raises(ValueError, match="bad header"):
        load_index(b"nope", DATASET)


def test_a_truncated_body_is_refused():
    built = build_index([STORY], DATASET)
    with pytest.raises(ValueError, match="truncated"):
        load_index(built[:-8], DATASET)


def test_the_index_does_not_store_the_text():
    built = build_index([STORY], DATASET)
    assert STORY.encode("utf-8") not in built
    assert normalize_text(STORY)[:20].encode("utf-8") not in built


def _rows(*pairs):
    return "".join(json.dumps({"instruction": i, "output": o}, ensure_ascii=False) + "\n" for i, o in pairs)


def test_a_clean_upload_passes():
    gate = make_train_gate(_index(STORY))
    assert gate(_rows(("请回答下面的问题。", "好的，我明白了。")).encode()) == (0, 0)


def test_a_contaminated_upload_reports_counts_not_text():
    gate = make_train_gate(_index(STORY))
    data = _rows(("请回答下面的问题。", STORY), ("无关的一行。", "好。")).encode()
    bad_lines, hits = gate(data)
    assert bad_lines == 1 and hits > 0


def test_two_contaminated_rows_are_counted_separately():
    gate = make_train_gate(_index(STORY))
    data = _rows(("第一行", STORY), ("第二行", STORY)).encode()
    assert gate(data)[0] == 2


def test_a_blank_line_is_skipped():
    gate = make_train_gate(_index(STORY))
    assert gate(b'\n{"instruction": "a", "output": "b"}\n\n') == (0, 0)


def test_malformed_json_is_a_value_error():
    with pytest.raises(ValueError, match="not JSON"):
        parse_train_jsonl(b"{nope}\n")


def test_a_missing_output_is_a_value_error():
    with pytest.raises(ValueError, match="instruction and output"):
        parse_train_jsonl(b'{"instruction": "a"}\n')


def test_the_header_layout_is_fixed():
    built = build_index([STORY], DATASET)
    assert built[:8] == b"SBIDX1\0\0"
    assert int.from_bytes(built[8:16], "little") == len(built[HEADER:]) // 8
    assert built[16:48] == bytes.fromhex(DATASET)


def test_the_index_matches_the_documented_size():
    """真实 SoMBench 建出来的索引约 5.8 MiB;这里用合成文本确认量级换算正确。"""
    text = "".join(chr(0x4E00 + i) for i in range(2000))
    built = build_index([text], DATASET)
    assert len(built) == HEADER + 1981 * 8


def test_sha_helper_is_stable(tmp_path):
    path = tmp_path / "x"
    path.write_bytes(b"abc")
    assert hashlib.sha256(b"abc").hexdigest() == _file_hash(path)


def _file_hash(path):
    from factory.sombench import file_sha256

    return file_sha256(path)
