"""桥接装配:从 control-plane/config/backends.yaml(配置信封)按角色取 BackendConfig,构造 CliBridge。

backends.yaml 的载荷形状(本计划裁决):{<角色>: <BackendConfig 原始字典>},角色 V1 有
planner(规划后端)与 agent(技能提案生成器)。缺文件、缺角色、字段不合法、缺凭证都抛
FactoryConfigError,信息里只有路径、角色、键名与变量名,不含凭证值。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from controlplane.config import (
    CredentialInGitTreeError,
    MissingCredentialError,
    load_backend_config,
    read_config,
)
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.errors import FactoryConfigError
from inference_bridge import CliBridge

BRIDGE_ROLES: tuple[str, ...] = ("planner", "agent")


def build_bridge(
    store: GitStore,
    role: str,
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
) -> CliBridge:
    """按角色构造命令行桥接。V1 只有命令行形态(R15):invocation_form 不是 cli 直接报错。"""
    if role not in BRIDGE_ROLES:
        raise FactoryConfigError(f"unknown bridge role {role!r}; choose from {list(BRIDGE_ROLES)}")
    rel = backends_config_file()
    try:
        payload = read_config(store, rel)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{rel} is missing; the {role!r} bridge needs a backend entry there"
        ) from None
    except CredentialInGitTreeError as exc:
        # 原信息只含键路径,不含值(计划 1 的 _check_no_secrets)
        raise FactoryConfigError(str(exc)) from None
    if role not in payload:
        raise FactoryConfigError(f"{rel} has no {role!r} entry (has: {sorted(payload)})")
    raw = payload[role]
    if not isinstance(raw, dict):
        raise FactoryConfigError(f"{rel}: {role!r} must be a mapping, got {type(raw).__name__}")
    try:
        cfg = load_backend_config(raw)
    except KeyError as exc:
        raise FactoryConfigError(f"{rel}: {role!r} is missing key {exc.args[0]!r}") from None
    except (ValueError, CredentialInGitTreeError) as exc:
        raise FactoryConfigError(f"{rel}: {role!r}: {exc}") from None
    if cfg.invocation_form != "cli":
        raise FactoryConfigError(
            f"{rel}: {role!r} uses invocation_form {cfg.invocation_form!r}; "
            "only 'cli' is implemented in V1 (R15)"
        )
    try:
        return CliBridge(
            cfg, store=store, log_dir=log_dir, env=dict(os.environ if env is None else env),
        )
    except MissingCredentialError as exc:
        # 信息只报变量名(controlplane.config.resolve_credential)
        raise FactoryConfigError(str(exc)) from None
    except ValueError as exc:
        raise FactoryConfigError(f"{rel}: {role!r}: {exc}") from None
