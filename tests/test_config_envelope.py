import pytest

from controlplane.config import (
    ConfigTypeError,
    CredentialInGitTreeError,
    load_backend_config,
    read_config,
    write_config,
)
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file, run_dir, scheduler_config_file


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


def _commits(store):
    # 新建的 store 还没有提交,rev-parse HEAD 会失败;用提交数比较"没有新提交"
    return store._git("rev-list", "--all", "--count").strip()


def _backend():
    return {
        "backend": "claude_cli", "invocation_form": "cli", "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1", "token_env": None,
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
        "command": ["claude", "-p", "--model", "{model}"], "timeout_s": 60,
    }


def test_write_then_read_round_trips_the_payload(store):
    payload = {"confirm_directives": True, "perturbable": ["sft_steps"], "min_gain": 0.01}
    write_config(store, scheduler_config_file(), "scheduler", payload, "human: scheduler config")
    assert read_config(store, scheduler_config_file()) == payload
    env = store.read_product(scheduler_config_file())
    assert (env.id, env.type, env.status, env.version) == ("scheduler", "config", "effective", 1)


def test_read_config_returns_a_copy(store):
    write_config(store, scheduler_config_file(), "scheduler", {"perturbable": ["a"]}, "m")
    read_config(store, scheduler_config_file())["perturbable"].append("b")
    assert read_config(store, scheduler_config_file()) == {"perturbable": ["a"]}


def test_write_config_does_not_keep_a_reference_to_the_callers_dict(store):
    payload = {"perturbable": ["a"]}
    write_config(store, scheduler_config_file(), "scheduler", payload, "m")
    payload["perturbable"].append("b")
    assert read_config(store, scheduler_config_file()) == {"perturbable": ["a"]}


def test_rewriting_bumps_the_version_and_identical_rewrite_is_a_no_op(store):
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.01}, "m1")
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.02}, "m2")
    assert store.read_product(scheduler_config_file()).version == 2
    head = _head(store)
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.02}, "m3")
    assert _head(store) == head
    assert store.read_product(scheduler_config_file()).version == 2


def test_backends_config_read_back_loads_as_backend_config(store):
    write_config(store, backends_config_file(), "backends", {"planner": _backend()}, "m")
    cfg = load_backend_config(read_config(store, backends_config_file())["planner"])
    assert cfg.token_env is None and cfg.command == ["claude", "-p", "--model", "{model}"]


def test_reading_a_missing_config_raises_file_not_found(store):
    with pytest.raises(FileNotFoundError):
        read_config(store, scheduler_config_file())


def test_reading_a_product_that_is_not_a_config_is_refused(store):
    env = Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload={},
    )
    store.write_product(scheduler_config_file(), env, "oops")
    with pytest.raises(ConfigTypeError, match="directive"):
        read_config(store, scheduler_config_file())


def test_overwriting_a_non_config_product_is_refused(store):
    env = Envelope(
        id="x", type="prompt_template", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload={},
    )
    store.write_product(scheduler_config_file(), env, "m")
    with pytest.raises(ConfigTypeError):
        write_config(store, scheduler_config_file(), "scheduler", {}, "m")


@pytest.mark.parametrize(
    "rel_path",
    [
        f"{run_dir('run-1')}/scheduler.yaml", "control-plane/config/scheduler.yml", "elsewhere/config/x.yaml",
        "control-plane/config/../runs/x.yaml", "control-plane/config/./scheduler.yaml",
        "control-plane/config//scheduler.yaml", "",
    ],
)
def test_configs_live_under_the_config_dir(store, rel_path):
    with pytest.raises(ValueError, match="control-plane/config/"):
        write_config(store, rel_path, "x", {}, "m")


@pytest.mark.parametrize("bad", [[], "text", None])
def test_payload_must_be_a_mapping(store, bad):
    with pytest.raises(ValueError, match="payload"):
        write_config(store, scheduler_config_file(), "scheduler", bad, "m")


def test_a_credential_value_at_any_depth_is_refused_before_writing(store):
    bad = {"planner": {**_backend(), "nested": [{"api_key": "sk-ant-secret"}]}}
    with pytest.raises(CredentialInGitTreeError) as exc:
        write_config(store, backends_config_file(), "backends", bad, "m")
    assert "sk-ant-secret" not in str(exc.value)
    assert not store.exists(backends_config_file())


def test_a_leaked_credential_already_in_the_tree_is_refused_on_read(store):
    env = Envelope(
        id="backends", type="config", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        payload={"planner": {"token": "sk-ant-secret"}},
    )
    store.write_product(backends_config_file(), env, "hand-written")
    with pytest.raises(CredentialInGitTreeError):
        read_config(store, backends_config_file())


def test_a_dot_dot_path_writes_nothing(store):
    before = _commits(store)
    with pytest.raises(ValueError):
        write_config(store, "control-plane/config/../runs/x.yaml", "x", {}, "m")
    assert _commits(store) == before
    assert not (store.repo_root / "control-plane" / "runs" / "x.yaml").exists()


@pytest.mark.parametrize("config_id", ["", None])
def test_config_id_must_not_be_empty(store, config_id):
    with pytest.raises(ValueError, match="config_id"):
        write_config(store, scheduler_config_file(), config_id, {}, "m")
    assert not store.exists(scheduler_config_file())


def test_credential_field_names_are_matched_case_insensitively(store):
    with pytest.raises(CredentialInGitTreeError) as exc:
        write_config(store, backends_config_file(), "backends", {"planner": {"API_KEY": "sk-x"}}, "m")
    assert "API_KEY" in str(exc.value) and "sk-x" not in str(exc.value)
