from __future__ import annotations

from dataclasses import dataclass

# 出现在 git 树配置里即视为凭证泄漏的字段名（spec 补充 G.3）
_FORBIDDEN_SECRET_KEYS = {"token", "api_key", "apikey", "password", "secret", "private_key"}

_REDACTED = "***REDACTED***"


class MissingCredentialError(Exception):
    """按变量名解析凭证失败：环境变量缺失或为空。"""


class CredentialInGitTreeError(Exception):
    """git 树配置里出现了凭证值本身，而非变量名引用。"""


@dataclass(frozen=True)
class BackendConfig:
    """外部推理后端配置（L-A 层，进 git 树）。只存凭证变量名，不存值。"""

    backend: str
    invocation_form: str  # "cli" | "api"
    model_name: str
    prompt_template_version: str
    token_env: str  # 环境变量名，如 "ANTHROPIC_API_KEY"
    retry_policy: dict


def load_backend_config(raw: dict) -> BackendConfig:
    leaked = _FORBIDDEN_SECRET_KEYS & set(raw.keys())
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields found in git-tree config: {sorted(leaked)}; "
            "store only the env var name via 'token_env'"
        )
    return BackendConfig(
        backend=raw["backend"],
        invocation_form=raw["invocation_form"],
        model_name=raw["model_name"],
        prompt_template_version=raw["prompt_template_version"],
        token_env=raw["token_env"],
        retry_policy=raw.get("retry_policy", {}),
    )


def resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str:
    """按变量名从环境解析凭证。缺失/为空即明确失败，禁止静默降级。"""
    value = env.get(cfg.token_env)
    if not value:
        raise MissingCredentialError(
            f"environment variable {cfg.token_env} is not set or empty; "
            f"required by backend {cfg.backend}"
        )
    return value


def redact(text: str, secrets: list[str | None]) -> str:
    """日志脱敏：把秘密值替换掉。L3 记录外部 CLI 请求/响应前必须过它。"""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, _REDACTED)
    return out
