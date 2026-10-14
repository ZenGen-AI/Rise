"""任务 116:任务 115 审查提出的窗口边界与两种形状。

- 只是以 in 两个字母结尾的词(如 within),后面隔着很长一段空白再跟两个数字的括号组,以前会被保留:
  切出来的窗口让 ^ 在文本中间也匹配。现在检查窗口之前的真实文本,只有整个单词 in 才算。
- 花括号组从来不是校验信息自己写的区间,in {1, 2} 要藏起来。
- 单独一个带符号或小数的数字不是区间,要藏起来。
- 一个很长的括号组不能让遮盖变慢。
"""
import time

import pytest

from factory import configio

GAP = " " * 14

KEPT = [
    pytest.param("pad pad in" + GAP + "[1, 2]", "pad pad in" + GAP + "[1, 2]", id="long-gap-inside-the-window"),
    pytest.param("in [3, 4]", "in [3, 4]", id="a-value-that-looks-authored-is-kept-by-design"),
]


@pytest.mark.parametrize("text,expected", KEPT)
def test_what_is_kept(text, expected):
    assert configio.redact_message(text) == expected


HIDDEN = [
    pytest.param("within" + GAP + "[1, 2]", "within" + GAP + "<value>", id="word-ending-in-in-with-a-long-gap"),
    pytest.param("within" + GAP + " [1, 2]", "within" + GAP + " <value>", id="the-same-with-fifteen-blanks"),
    pytest.param("pad pad in" + GAP + " [1, 2]", "pad pad in" + GAP + " <value>", id="gap-beyond-the-window"),
    pytest.param("in {1, 2}", "in <value>", id="braces"),
    pytest.param("in [-5]", "in <value>", id="lone-signed-number"),
    pytest.param("in [1.5]", "in <value>", id="lone-decimal-number"),
]


@pytest.mark.parametrize("text,expected", HIDDEN)
def test_what_is_hidden(text, expected):
    assert configio.redact_message(text) == expected


LONG = [
    pytest.param("in [" + "1" * 100000 + "]", id="one-long-run-of-digits"),
    pytest.param("in [" + "1, " * 50000 + "1]", id="many-numbers-in-one-group"),
]


@pytest.mark.parametrize("text", LONG)
def test_one_very_long_group_is_hidden_without_slowing_down(text):
    start = time.perf_counter()
    assert configio.redact_message(text) == "in <value>"
    assert time.perf_counter() - start < 5.0
