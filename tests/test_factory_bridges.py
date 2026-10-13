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
