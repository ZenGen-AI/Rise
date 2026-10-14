"""Codex 第二次复审的四条 P1(任务 114)。

- 校验信息里的取值出现在 ", got " 之前,或信息里根本没有这个分隔符时,describe 仍要藏住它:
  引号括起的字符串与括号括起的一组都换成 <value>,数字范围保留。
- 信封的 type 字段被误填成凭证时,信息不写它的取值,只写文件路径。
- YAML 里的超大整数让 float 转换抛 OverflowError,也要变成一行错误与退出码 2。
- 同一个映射里字符串键和整数键混着,给键名排序会抛 TypeError,也要变成一行错误与退出码 2。
"""
import sys

import pytest

from controlplane.config import write_config
from controlplane.envelope import Envelope, OwnerRef
from controlplane.paths import backends_config_file, scheduler_config_file, training_defaults_file
from factory import configio
from factory.agent import AGENT_CONFIG
from factory.cli import main
from factory.defaults import DEFAULT_AGENT_CONFIG, DEFAULT_EVALUATOR_CONFIG
from factory.evaluation import EVALUATOR_CONFIG
from factory_fixtures import closed_loop_repo, git_head, tree_files
from trainscheduler.config import default_training_defaults_payload, load_scheduler_config

SECRET = "sekrit_review_probe"
HEURISTIC = ("--planner-backend", "heuristic")
HUGE = 10 ** 400


def _backend(**kw):
    raw = {"backend": "fake_cli", "invocation_form": "cli", "model_name": "m1", "prompt_template_version": "v1",
           "token_env": None, "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
           "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 30}
    raw.update(kw)
    return raw


def _put(store, rel, type_, payload):
    """把 rel 换成一份指定 type 的信封(先删旧文件,再经 write_product 写入并提交)。"""
    path = store.repo_root / rel
    if path.exists():
        path.unlink()
    env = Envelope(id="probe", type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="effective", schema_version=1, payload=payload)
    store.write_product(rel, env, f"test: {rel} with a wrong type")


def _check(tmp_path, capsys, break_it, enable, extra=(), fragment=None):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    break_it(store)
    head, files = git_head(store), tree_files(store)
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable, *extra], env={})
    err = capsys.readouterr().err
    assert code == 2, err
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert SECRET not in err
    if fragment is not None:
        assert fragment in err
    assert (git_head(store), tree_files(store)) == (head, files)


def _hidden_components(store):
    payload = default_training_defaults_payload()
    payload["training_config"]["reward"]["components"] = [SECRET]
    write_config(store, training_defaults_file(), "training-defaults", payload, "test: reward components")


def test_a_value_before_the_got_marker_is_hidden_too(tmp_path, capsys):
    _check(tmp_path, capsys, _hidden_components, "plan", HEURISTIC, fragment="reward.outcome_rules")


TYPED = [
    pytest.param(scheduler_config_file(), lambda s: load_scheduler_config(s).to_dict(), "plan", HEURISTIC,
                 "scheduler.yaml", id="scheduler"),
    pytest.param(EVALUATOR_CONFIG, lambda s: dict(DEFAULT_EVALUATOR_CONFIG), "plan", HEURISTIC,
                 "evaluator.yaml", id="evaluator"),
    pytest.param(AGENT_CONFIG, lambda s: dict(DEFAULT_AGENT_CONFIG), "agent", (), "agent.yaml", id="agent"),
    pytest.param(backends_config_file(), lambda s: {"planner": _backend()}, "plan", (), "backends.yaml",
                 id="backends"),
]


@pytest.mark.parametrize("rel,payload_of,enable,extra,fragment", TYPED)
def test_a_wrong_envelope_type_is_not_echoed(tmp_path, capsys, rel, payload_of, enable, extra, fragment):
    def retype(store):
        _put(store, rel, SECRET, payload_of(store))

    _check(tmp_path, capsys, retype, enable, extra, fragment=fragment)


@pytest.mark.parametrize("change", [
    pytest.param({"timeout_s": HUGE}, id="timeout"),
    pytest.param({"retry_policy": {"max_attempts": 1, "backoff_base_s": HUGE}}, id="backoff"),
])
def test_an_integer_too_large_for_a_float_is_a_one_line_error(tmp_path, capsys, change):
    def write(store):
        write_config(store, backends_config_file(), "backends", {"planner": _backend(**change)}, "test: huge number")

    _check(tmp_path, capsys, write, "plan", fragment="OverflowError")


MIXED = [
    pytest.param(lambda s: write_config(s, EVALUATOR_CONFIG, "evaluator",
                                        {**DEFAULT_EVALUATOR_CONFIG, "extra": 1, 1: 2}, "test: mixed keys"),
                 "plan", HEURISTIC, "unknown", id="evaluator"),
    pytest.param(lambda s: write_config(s, AGENT_CONFIG, "agent",
                                        {**DEFAULT_AGENT_CONFIG, "extra": 1, 1: 2}, "test: mixed keys"),
                 "agent", (), "unknown", id="agent"),
    pytest.param(lambda s: write_config(s, backends_config_file(), "backends",
                                        {"agent": _backend(), 1: _backend()}, "test: mixed keys"),
                 "plan", (), "has no 'planner' entry", id="backends"),
]


@pytest.mark.parametrize("write,enable,extra,fragment", MIXED)
def test_mixed_key_types_are_a_one_line_error(tmp_path, capsys, write, enable, extra, fragment):
    _check(tmp_path, capsys, write, enable, extra, fragment=fragment)


REDACT = [
    pytest.param("plain words only", "plain words only", id="plain"),
    pytest.param("key 'sekrit' is bad", "key <value> is bad", id="single-quoted"),
    pytest.param('key "it\'s sekrit" is bad', "key <value> is bad", id="double-quoted-with-an-apostrophe"),
    pytest.param("keys must equal components ['sekrit', 'x']", "keys must equal components <value>", id="list"),
    pytest.param("nested [['a', ('b', {'c': 1})], 'd'] tail", "nested <value> tail", id="nested"),
    pytest.param("unclosed ['sekrit", "unclosed <value>", id="unclosed-list"),
    pytest.param("unclosed 'sekrit", "unclosed <value>", id="unclosed-quote"),
    pytest.param("escaped 'a\\'b sekrit' after", "escaped <value> after", id="escaped-quote"),
    pytest.param("min_ratio must be a number in [0, 1]", "min_ratio must be a number in [0, 1]",
                 id="numeric-range-kept"),
    pytest.param("range [0, 'x'] bad", "range <value> bad", id="mixed-list"),
]


@pytest.mark.parametrize("text,expected", REDACT)
def test_redact_message(text, expected):
    assert configio.redact_message(text) == expected


DESCRIBE = [
    pytest.param(ValueError("reward.outcome_rules keys must equal components ['sekrit']"),
                 "ValueError: reward.outcome_rules keys must equal components <value>", id="value-before-the-marker"),
    pytest.param(ValueError("tier 'sekrit' is bad, got 'sekrit'"), "ValueError: tier <value> is bad",
                 id="cut-then-redact"),
    pytest.param(ValueError("min_ratio must be a number in [0, 1], got 5"),
                 "ValueError: min_ratio must be a number in [0, 1]", id="range-survives"),
]


@pytest.mark.parametrize("exc,expected", DESCRIBE)
def test_describe_hides_values_wherever_they_sit(exc, expected):
    assert configio.describe(exc) == expected
