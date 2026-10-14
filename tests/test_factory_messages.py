"""工厂转述给操作者的错误信息(Codex 复核第 1、6 条):校验失败只点名字段、不回显配置取值,
字段形状错也是一行错误与退出码 2,不漏回溯。标识符类字段(版本号、变量名、路径里的名字)出现在信息里是有意的。"""
import sys
from pathlib import Path

import pytest

from controlplane.config import write_config
from controlplane.envelope import Envelope, OwnerRef
from controlplane.paths import backends_config_file
from diagnostic.assets import ITEM_POOL_TYPE
from diagnostic.paths import item_pool_file
from factory.agent import AGENT_CONFIG
from factory.cli import main
from factory.configio import describe
from factory.defaults import DEFAULT_AGENT_CONFIG, DEFAULT_EVALUATOR_CONFIG, DEFAULT_ITEM_POOL
from factory.evaluation import EVALUATOR_CONFIG
from factory_fixtures import closed_loop_repo, git_head, tree_files

SECRET = "sekrit-probe-0042"


def _backend(**kw):
    raw = {"backend": "fake_cli", "invocation_form": "cli", "model_name": "m1", "prompt_template_version": "v1",
           "token_env": None, "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
           "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 30}
    raw.update(kw)
    return raw


def _backends(**kw):
    return lambda s: write_config(s, backends_config_file(), "backends", {"planner": _backend(**kw)}, "test: backends")


def _evaluator(**kw):
    return lambda s: write_config(s, EVALUATOR_CONFIG, "evaluator", {**DEFAULT_EVALUATOR_CONFIG, **kw}, "test: evaluator")


def _agent(**kw):
    return lambda s: write_config(s, AGENT_CONFIG, "agent", {**DEFAULT_AGENT_CONFIG, **kw}, "test: agent")


def _nested_variant_ids(store):
    """题库里某个变体族的 variant_item_ids 写成嵌套列表:读得进来,算变体集合时才炸。"""
    payload = DEFAULT_ITEM_POOL.to_dict()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "variant_item_ids":
                    o[k] = [["x"]]
                    return True
                if walk(v):
                    return True
        elif isinstance(o, list):
            return any(walk(v) for v in o)
        return False

    assert walk(payload)
    version = DEFAULT_EVALUATOR_CONFIG["item_pool_version"]
    rel = item_pool_file(version)
    env = Envelope(id=f"item-pool-{version}", type=ITEM_POOL_TYPE, version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="effective", schema_version=1, payload=payload)
    store.write_product(rel, env, f"test: {Path(rel).name} with nested variant ids")


def _run(store, enable, capsys):
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable], env={})
    return code, capsys.readouterr().err


def _check(tmp_path, capsys, enable, write, fragment, *, secret_absent):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    write(store)
    head, files = git_head(store), tree_files(store)
    code, err = _run(store, enable, capsys)
    assert code == 2
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert fragment in err
    if secret_absent:
        assert SECRET not in err
    assert (git_head(store), tree_files(store)) == (head, files)


VALUES = [
    pytest.param("plan", _backends(timeout_s=SECRET), "timeout_s", id="backends-timeout"),
    pytest.param("plan", _backends(command=SECRET), "command", id="backends-command"),
    pytest.param("plan", _backends(env_allowlist=SECRET), "env_allowlist", id="backends-env-allowlist"),
    pytest.param("plan", _backends(workdir=SECRET), "workdir", id="backends-workdir"),
    pytest.param("plan", _backends(retry_policy={"max_attempts": SECRET, "backoff_base_s": 0}), "max_attempts",
                 id="backends-retry-attempts"),
    pytest.param("plan", _backends(retry_policy={"max_attempts": 1, "backoff_base_s": SECRET}), "backoff_base_s",
                 id="backends-retry-backoff"),
    pytest.param("plan", _backends(invocation_form=SECRET), "only 'cli'", id="backends-invocation-form"),
    pytest.param("plan", _evaluator(tier=SECRET), "tier", id="evaluator-tier"),
    pytest.param("plan", _evaluator(sampling_k=SECRET), "sampling_k", id="evaluator-sampling-k"),
    pytest.param("agent", _agent(max_turns=SECRET), "max_turns", id="agent-max-turns"),
    pytest.param("agent", _agent(min_ratio=SECRET), "min_ratio", id="agent-min-ratio"),
    pytest.param("agent", _agent(scenario_ids=[SECRET, SECRET]), "scenario_ids", id="agent-repeated-scenarios"),
]


@pytest.mark.parametrize("enable,write,fragment", VALUES)
def test_a_bad_value_is_named_by_its_field_and_never_echoed(tmp_path, capsys, enable, write, fragment):
    _check(tmp_path, capsys, enable, write, fragment, secret_absent=True)


SHAPES = [
    pytest.param("plan", _backends(retry_policy=None), "AttributeError", id="backends-retry-policy-null"),
    pytest.param("plan", _backends(retry_policy=SECRET), "AttributeError", id="backends-retry-policy-string"),
    pytest.param("plan", _backends(model_name=5), "TypeError", id="backends-model-name-number"),
    pytest.param("plan", _nested_variant_ids, "diagnostic assets", id="item-pool-nested-variant-ids"),
]


@pytest.mark.parametrize("enable,write,fragment", SHAPES)
def test_a_field_with_the_wrong_shape_is_a_one_line_error_not_a_traceback(tmp_path, capsys, enable, write, fragment):
    _check(tmp_path, capsys, enable, write, fragment, secret_absent=True)


@pytest.mark.parametrize("exc,expected", [
    (ValueError("max_turns must be an int >= 1, got 'sekrit'"), "ValueError: max_turns must be an int >= 1"),
    (ValueError("no value in this one"), "ValueError: no value in this one"),
    (KeyError("slot_id"), "KeyError: 'slot_id'"),
    (KeyError("sekrit value!"), "KeyError"),
    (TypeError("sekrit inside"), "TypeError"),
    (AttributeError("'str' object has no attribute 'get'"), "AttributeError"),
])
def test_describe_keeps_the_class_and_the_expected_shape_but_not_the_value(exc, expected):
    assert describe(exc) == expected
