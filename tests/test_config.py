import pytest

from controlplane.config import (
    load_backend_config,
    resolve_credential,
    redact,
    MissingCredentialError,
    CredentialInGitTreeError,
)


def _raw() -> dict:
    return {
        "backend": "claude_cli",
        "invocation_form": "cli",
        "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1",
        "token_env": "ANTHROPIC_API_KEY",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
    }


def test_load_backend_config_keeps_only_var_name():
    cfg = load_backend_config(_raw())
    assert cfg.token_env == "ANTHROPIC_API_KEY"
    # 配置对象里没有任何存放凭证值的字段
    assert not hasattr(cfg, "token")


def test_config_with_literal_token_is_rejected():
    raw = _raw()
    raw["token"] = "sk-ant-secret"
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(raw)


def test_config_with_api_key_field_is_rejected():
    raw = _raw()
    raw["api_key"] = "sk-ant-secret"
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(raw)


def test_resolve_credential_reads_env():
    cfg = load_backend_config(_raw())
    assert resolve_credential(cfg, {"ANTHROPIC_API_KEY": "sk-live"}) == "sk-live"


def test_missing_credential_raises_with_var_name():
    cfg = load_backend_config(_raw())
    with pytest.raises(MissingCredentialError) as exc:
        resolve_credential(cfg, {})
    # 错误必须指明缺哪个环境变量，不得静默降级
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_empty_credential_also_raises():
    cfg = load_backend_config(_raw())
    with pytest.raises(MissingCredentialError):
        resolve_credential(cfg, {"ANTHROPIC_API_KEY": ""})


def test_redact_removes_secret_from_log():
    log = "calling api with token sk-live-123 and retry"
    out = redact(log, ["sk-live-123"])
    assert "sk-live-123" not in out
    assert "***REDACTED***" in out


def test_redact_ignores_empty_secrets():
    log = "no secrets here"
    assert redact(log, ["", None]) == log
