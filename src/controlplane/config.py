from __future__ import annotations

import math
from dataclasses import dataclass, field

# 出现在 git 树配置里即视为凭证泄漏的字段名（spec 补充 G.3）
_FORBIDDEN_SECRET_KEYS = {"token", "api_key", "apikey", "password", "secret", "private_key"}

_REDACTED = "***REDACTED***"

# 子进程默认能看到的环境变量名;BackendConfig.env_allowlist 为 None 时用它(设计 BR-8)。
# 凭证变量不在其中,由 token_env 单独加入。
DEFAULT_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
)


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
    # 环境变量名，如 "ANTHROPIC_API_KEY"。显式 None 表示该工具用自带登录态，不解析凭证（设计 R15）。
    token_env: str | None
    retry_policy: dict
    # 命令行形态的参数模板（argv 列表，不经 shell）。None 表示没有命令行形态。
    command: list[str] | None = field(default=None)
    timeout_s: float = 120.0
    # 子进程隔离(任务 26 用):env_allowlist 为 None 时用 DEFAULT_ENV_ALLOWLIST;workdir 目前只有 "empty_tmp"。
    env_allowlist: list[str] | None = None
    workdir: str = "empty_tmp"


def _load_command(raw: dict) -> list[str] | None:
    command = raw.get("command")
    if command is None:
        return None
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a for a in command)
    ):
        raise ValueError(f"command must be a non-empty list of non-empty strings, got {command!r}")
    return list(command)


def _load_timeout(raw: dict) -> float:
    timeout = raw.get("timeout_s", 120.0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError(f"timeout_s must be a finite positive number, got {timeout!r}")
    return float(timeout)


def _load_env_allowlist(raw: dict) -> list[str] | None:
    allowlist = raw.get("env_allowlist")
    if allowlist is None:
        return None  # 用 DEFAULT_ENV_ALLOWLIST
    if not isinstance(allowlist, list) or not all(isinstance(n, str) and n for n in allowlist):
        raise ValueError(f"env_allowlist must be null or a list of non-empty strings, got {allowlist!r}")
    return list(allowlist)


def _load_workdir(raw: dict) -> str:
    workdir = raw.get("workdir", "empty_tmp")
    if workdir != "empty_tmp":
        raise ValueError(f"workdir must be 'empty_tmp', got {workdir!r}")
    return workdir


def _is_secret_key(key: object) -> bool:
    """凭证值字段名按小写比较:Token、API_KEY 与 token、api_key 一样被拒。"""
    return isinstance(key, str) and key.lower() in _FORBIDDEN_SECRET_KEYS


def load_backend_config(raw: dict) -> BackendConfig:
    leaked = {k for k in raw if _is_secret_key(k)}
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields found in git-tree config: {sorted(leaked)}; "
            "store only the env var name via 'token_env'"
        )
    token_env = raw["token_env"]  # 键必须在场；值可以是变量名或显式 null
    if token_env is not None and (not isinstance(token_env, str) or not token_env):
        raise ValueError(f"token_env must be an env var name or null, got {token_env!r}")
    return BackendConfig(
        backend=raw["backend"],
        invocation_form=raw["invocation_form"],
        model_name=raw["model_name"],
        prompt_template_version=raw["prompt_template_version"],
        token_env=token_env,
        retry_policy=raw.get("retry_policy", {}),
        command=_load_command(raw),
        timeout_s=_load_timeout(raw),
        env_allowlist=_load_env_allowlist(raw),
        workdir=_load_workdir(raw),
    )


def resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str | None:
    """按变量名从环境解析凭证。缺失/为空即明确失败，禁止静默降级。
    token_env 为 None（工具自带登录态）时返回 None，不读环境。"""
    if cfg.token_env is None:
        return None
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
