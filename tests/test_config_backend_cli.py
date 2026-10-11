import pytest

from controlplane.config import (
    BackendConfig,
    CredentialInGitTreeError,
    MissingCredentialError,
    load_backend_config,
    resolve_credential,
)


def _raw(**over) -> dict:
    raw = {
        "backend": "claude_cli",
        "invocation_form": "cli",
        "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1",
        "token_env": "ANTHROPIC_API_KEY",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
    }
    raw.update(over)
    return raw


def test_old_style_config_gets_no_command_and_the_default_timeout():
    cfg = load_backend_config(_raw())
    assert cfg.command is None
    assert cfg.timeout_s == 120.0


def test_command_and_timeout_are_loaded():
    cfg = load_backend_config(_raw(command=["claude", "-p", "--model", "{model}"], timeout_s=30))
    assert cfg.command == ["claude", "-p", "--model", "{model}"]
    assert cfg.timeout_s == 30.0 and isinstance(cfg.timeout_s, float)


def test_the_loaded_command_is_a_copy_of_the_raw_list():
    raw = _raw(command=["claude", "-p"])
    cfg = load_backend_config(raw)
    raw["command"].append("--evil")
    assert cfg.command == ["claude", "-p"]


@pytest.mark.parametrize(
    "command", [[], "claude -p", ["claude", ""], ["claude", 3], {"argv": ["claude"]}],
)
def test_a_malformed_command_is_rejected(command):
    with pytest.raises(ValueError, match="command"):
        load_backend_config(_raw(command=command))


@pytest.mark.parametrize("timeout_s", [0, -1, "30", True, float("nan"), float("inf")])
def test_a_malformed_timeout_is_rejected(timeout_s):
    with pytest.raises(ValueError, match="timeout_s"):
        load_backend_config(_raw(timeout_s=timeout_s))


def test_token_env_may_be_null_for_tools_with_their_own_login():
    cfg = load_backend_config(_raw(token_env=None))
    assert cfg.token_env is None
    assert resolve_credential(cfg, {}) is None
    assert resolve_credential(cfg, {"ANTHROPIC_API_KEY": "sk-live"}) is None


def test_token_env_key_is_still_required():
    raw = _raw()
    del raw["token_env"]
    with pytest.raises(KeyError):
        load_backend_config(raw)


@pytest.mark.parametrize("token_env", ["", 7])
def test_token_env_must_be_a_name_or_null(token_env):
    with pytest.raises(ValueError, match="token_env"):
        load_backend_config(_raw(token_env=token_env))


def test_a_named_but_missing_variable_still_fails_loudly():
    with pytest.raises(MissingCredentialError, match="ANTHROPIC_API_KEY"):
        resolve_credential(load_backend_config(_raw()), {})


@pytest.mark.parametrize(
    "key", ["token", "api_key", "apikey", "password", "secret", "private_key", "Token", "API_KEY"],
)
def test_credential_value_fields_are_still_rejected_with_the_new_fields_present(key):
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(_raw(token_env=None, command=["codex", "exec"], **{key: "x"}))


def test_the_config_object_has_no_field_for_a_credential_value():
    names = set(BackendConfig.__dataclass_fields__)
    assert names == {
        "backend", "invocation_form", "model_name", "prompt_template_version",
        "token_env", "retry_policy", "command", "timeout_s", "env_allowlist", "workdir",
    }


def test_old_style_config_gets_the_default_isolation_settings():
    cfg = load_backend_config(_raw())
    assert cfg.env_allowlist is None
    assert cfg.workdir == "empty_tmp"


def test_the_default_env_allowlist_is_the_documented_set():
    from controlplane.config import DEFAULT_ENV_ALLOWLIST  # 放在用例里:它缺失时不拖垮整个文件的收集

    assert DEFAULT_ENV_ALLOWLIST == (
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    )


@pytest.mark.parametrize("allowlist", [None, ["PATH", "HOME"], []], ids=["none", "names", "empty"])
def test_a_valid_env_allowlist_is_loaded(allowlist):
    assert load_backend_config(_raw(env_allowlist=allowlist)).env_allowlist == allowlist


def test_the_loaded_env_allowlist_is_a_copy_of_the_raw_list():
    raw = _raw(env_allowlist=["PATH"])
    cfg = load_backend_config(raw)
    raw["env_allowlist"].append("EVIL")
    assert cfg.env_allowlist == ["PATH"]


@pytest.mark.parametrize(
    "allowlist", ["PATH", ["PATH", 3], ["PATH", ""], ("PATH", "HOME")],
    ids=["string", "non-string-item", "empty-name", "tuple"],
)
def test_a_malformed_env_allowlist_is_rejected(allowlist):
    with pytest.raises(ValueError, match="env_allowlist"):
        load_backend_config(_raw(env_allowlist=allowlist))


def test_workdir_empty_tmp_is_accepted_explicitly():
    assert load_backend_config(_raw(workdir="empty_tmp")).workdir == "empty_tmp"


@pytest.mark.parametrize("workdir", ["inherit", "/tmp", "", None])
def test_an_unknown_workdir_is_rejected(workdir):
    with pytest.raises(ValueError, match="workdir"):
        load_backend_config(_raw(workdir=workdir))
