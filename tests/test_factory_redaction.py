"""Codex 最后一次复审的 P1(任务 115):数字范围的例外不能放过真正的取值。

redact_message 之前把"只有数字、逗号、点、正负号、e、E 和空白"的括号组都当成范围保留,
于是 backend: [123456789] 这样的取值会被 CliBridge 的信息带到标准错误。现在只有紧跟在单词 in 之后、
恰好是两个数字的括号组才保留(校验信息自己写的区间,如 in [0, 1]),其余括号组一律换成 <value>。
"""

import pytest

from controlplane.config import write_config
from controlplane.paths import backends_config_file
from factory import configio
from factory.cli import main
from factory_fixtures import closed_loop_repo, git_head, tree_files

NUMBER = "123456789"

KEPT = [
    "min_ratio must be a number in [0, 1]",
    "scheduler.cost_gain_window must be in [1, 100]",
    "must be in [-5, 5.5]",
    "must be in [1,2]",
    "must be in (0, 1)",
    "in [0, 1]",
]


@pytest.mark.parametrize("text", KEPT)
def test_a_range_that_the_message_itself_writes_is_kept(text):
    assert configio.redact_message(text) == text


HIDDEN = [
    ("backend [123456789] has no 'command' template", "backend <value> has no <value> template"),
    ("pin [123456]", "pin <value>"),
    ("must be in [123456789]", "must be in <value>"),
    ("x [1, 2] y", "x <value> y"),
    ("must be in [1, 2, 3]", "must be in <value>"),
    ("must be in [1e5, 2]", "must be in <value>"),
    ("within [1, 2]", "within <value>"),
    ("must be in [1, 'x']", "must be in <value>"),
    ("must be in []", "must be in <value>"),
    ("must be in [,]", "must be in <value>"),
]


@pytest.mark.parametrize("text,expected", HIDDEN)
def test_every_other_bracket_group_is_hidden_even_when_it_holds_only_digits(text, expected):
    assert configio.redact_message(text) == expected


def test_describe_hides_a_numeric_list_and_keeps_the_range():
    assert configio.describe(ValueError("backend [123456789] has no 'command' template")) == \
        "ValueError: backend <value> has no <value> template"
    assert configio.describe(ValueError("min_ratio must be a number in [0, 1], got [123456789]")) == \
        "ValueError: min_ratio must be a number in [0, 1]"


def test_many_groups_that_are_hidden_take_linear_time():
    assert configio.redact_message("[1] " * 20000) == "<value> " * 20000


def test_many_groups_that_are_kept_take_linear_time():
    text = "in [1, 2] " * 20000
    assert configio.redact_message(text) == text


def test_a_numeric_list_in_the_backend_field_is_not_echoed(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    raw = {"backend": [int(NUMBER)], "invocation_form": "cli", "model_name": "m1", "prompt_template_version": "v1",
           "token_env": None, "retry_policy": {"max_attempts": 1, "backoff_base_s": 0}, "timeout_s": 30}
    write_config(store, backends_config_file(), "backends", {"planner": raw}, "test: a list in the backend field")
    head, files = git_head(store), tree_files(store)
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={})
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert "has no" in err
    assert NUMBER not in err
    assert (git_head(store), tree_files(store)) == (head, files)
