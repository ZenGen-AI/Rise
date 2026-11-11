"""桥接装配:backends.yaml(配置信封)→ BackendConfig → CliBridge。缺什么明说,凭证值绝不外露。"""
import sys

import pytest

from controlplane.config import write_config
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.bridges import BRIDGE_ROLES, build_bridge
from factory.errors import FactoryConfigError
from inference_bridge import CliBridge

SECRET = "sekrit-value-0042"
VAR = "RF_FACTORY_TEST_TOKEN"


def _backend(**kw):
    raw = {
        "backend": "fake_cli", "invocation_form": "cli", "model_name": "m1",
        "prompt_template_version": "v1", "token_env": VAR,
        "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
        "command": [sys.executable, "-c", f"import os; print('reply', os.environ['{VAR}'])"],
        "timeout_s": 30,
    }
    raw.update(kw)
    return raw


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _write(store, payload):
    write_config(store, backends_config_file(), "backends", payload, "test: backends")


def test_roles():
    assert BRIDGE_ROLES == ("planner", "agent")


def test_a_configured_role_builds_a_cli_bridge_that_redacts_its_secret(store, tmp_path):
    _write(store, {"planner": _backend()})
    bridge = build_bridge(store, "planner", env={VAR: SECRET}, log_dir=tmp_path / "logs")
    assert isinstance(bridge, CliBridge)
    result = bridge.complete("hi", run_id="run-001", task_id="plan-1")
    assert result.text == "reply ***REDACTED***"
    log = (tmp_path / "logs" / "run-001" / "plan-1-1.log").read_text(encoding="utf-8")
    assert SECRET not in log


def test_token_env_null_needs_no_credential(store, tmp_path):
    _write(store, {"agent": _backend(token_env=None, command=[sys.executable, "-c", "print('ok')"])})
    bridge = build_bridge(store, "agent", env={}, log_dir=tmp_path / "logs")
    assert bridge.complete("x", run_id="run-001", task_id="t").text == "ok"


def test_missing_backends_file(store):
    with pytest.raises(FactoryConfigError, match="backends.yaml is missing"):
        build_bridge(store, "planner", env={})


def test_missing_role(store):
    _write(store, {"agent": _backend()})
    with pytest.raises(FactoryConfigError, match=r"no 'planner' entry \(has: \['agent'\]\)"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_unknown_role(store):
    with pytest.raises(FactoryConfigError, match="unknown bridge role"):
        build_bridge(store, "judge", env={})


def test_missing_key(store):
    raw = _backend()
    del raw["model_name"]
    _write(store, {"planner": raw})
    with pytest.raises(FactoryConfigError, match="missing key 'model_name'"):
        build_bridge(store, "planner", env={VAR: SECRET})


@pytest.mark.parametrize("bad", [{"command": []}, {"timeout_s": 0}, {"token_env": ""},
                                 {"retry_policy": {"max_attempts": 0}}])
def test_invalid_fields(store, bad):
    _write(store, {"planner": _backend(**bad)})
    with pytest.raises(FactoryConfigError, match="'planner'"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_api_form_is_not_implemented(store):
    _write(store, {"planner": _backend(invocation_form="api")})
    with pytest.raises(FactoryConfigError, match="only 'cli'"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_missing_credential_names_the_variable_only(store):
    _write(store, {"planner": _backend()})
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: ""})
    assert VAR in str(exc.value)
    assert exc.value.__cause__ is None


def test_a_credential_value_written_into_the_tree_is_refused_without_echoing_it(store):
    raw = _backend()
    raw["token"] = SECRET
    env = Envelope(id="backends", type="config", version=1, created_at="t", created_by=OwnerRef(),
                   status="effective", schema_version=1, payload={"planner": raw})
    store.write_product(backends_config_file(), env, "hand-written leak")
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert "token" in str(exc.value)
    assert SECRET not in str(exc.value)


# ---- 任务 102 的测试缺口:链式异常的抑制标志、命中的字段名、非映射载荷、env=None 回退、子进程输出捕获 ----

def _failing_command(code=3):
    """子进程把凭证同时写进 stdout 与 stderr 再以非零码退出。"""
    body = (f"import os, sys; v = os.environ['{VAR}']; "
            f"sys.stderr.write('auth failed for ' + v); sys.stdout.write('partial ' + v); sys.exit({code})")
    return [sys.executable, "-c", body]


@pytest.mark.parametrize("make", [
    lambda s: _write(s, {"planner": _backend(command=[])}),
    lambda s: _write(s, {"planner": _backend(retry_policy={"max_attempts": 0})}),
    lambda s: _write(s, {"planner": _backend(model_name=5)}),
    lambda s: _write(s, {"planner": {k: v for k, v in _backend().items() if k != "model_name"}}),
], ids=["load-valueerror", "bridge-valueerror", "bridge-typeerror", "missing-key"])
def test_a_translated_error_hides_the_original_from_the_chain(store, make):
    """补任务 102 的缺口:转译后的 FactoryConfigError 不挂 __cause__,且 __suppress_context__ 为真,原异常文字不会经回溯带出。"""
    make(store)
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


def test_a_missing_credential_error_also_hides_the_original_from_the_chain(store):
    """补任务 102 的缺口:缺凭证那条转译同样抑制上下文(现有用例只断言 __cause__ 为空)。"""
    _write(store, {"planner": _backend()})
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={})
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


@pytest.mark.parametrize("bad,field", [
    ({"command": []}, "command"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"token_env": ""}, "token_env"),
    ({"retry_policy": {"max_attempts": 0}}, "max_attempts"),
    ({"retry_policy": {"backoff_base_s": -1}}, "backoff_base_s"),
])
def test_an_invalid_field_is_named_in_the_message_not_only_the_role(store, bad, field):
    """补任务 102 的缺口:非法字段的信息要点名字段(现有参数化只匹配角色名 'planner')。
    max_attempts 与 backoff_base_s 是 CliBridge 构造时才查的,这里一并钉住它们走同一条转译。"""
    _write(store, {"planner": _backend(**bad)})
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert "'planner'" in str(exc.value) and field in str(exc.value)


def test_a_role_entry_that_is_not_a_mapping_names_the_type_only(store):
    """补任务 102 的缺口:角色条目不是映射(这里是字符串)。信息只写类型名,不回显条目内容。"""
    _write(store, {"planner": SECRET})
    with pytest.raises(FactoryConfigError, match=r"'planner' must be a mapping, got str") as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert SECRET not in str(exc.value)


def test_without_an_env_argument_the_credential_comes_from_the_process_environment(store, monkeypatch):
    """补任务 102 的缺口:env=None 回退到 os.environ,凭证照样脱敏。"""
    monkeypatch.setenv(VAR, SECRET)
    _write(store, {"planner": _backend()})
    bridge = build_bridge(store, "planner")
    assert bridge.complete("hi", run_id="run-001", task_id="t").text == "reply ***REDACTED***"


def test_a_credential_key_written_into_the_tree_is_named_by_its_path_without_its_value(store):
    """补任务 102 的缺口:凭证值字段被拒时信息给出键路径(payload.planner.token),而不只是 token 一个词;大小写不敏感。"""
    raw = _backend()
    raw["API_KEY"] = SECRET
    env = Envelope(id="backends", type="config", version=1, created_at="t", created_by=OwnerRef(),
                   status="effective", schema_version=1, payload={"planner": raw})
    store.write_product(backends_config_file(), env, "hand-written leak")
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert "payload.planner.API_KEY" in str(exc.value) and SECRET not in str(exc.value)


def test_a_failing_subprocess_error_and_log_carry_neither_stream_unredacted(store, tmp_path):
    """补任务 102 的缺口:子进程的 stdout 与 stderr 都被捕获;失败时异常里的 stderr 尾巴与落盘日志都已脱敏。"""
    from inference_bridge.base import BridgeError

    _write(store, {"planner": _backend(command=_failing_command())})
    bridge = build_bridge(store, "planner", env={VAR: SECRET}, log_dir=tmp_path / "logs")
    with pytest.raises(BridgeError) as exc:
        bridge.complete("hi", run_id="run-001", task_id="plan-1")
    assert exc.value.category == "T1" and "exit code 3" in str(exc.value)
    assert "auth failed for ***REDACTED***" in str(exc.value) and SECRET not in str(exc.value)
    log = (tmp_path / "logs" / "run-001" / "plan-1-1.log").read_text(encoding="utf-8")
    assert "partial ***REDACTED***" in log and "auth failed for ***REDACTED***" in log
    assert SECRET not in log
