"""树外的本机评测配置(设计 21 的 DX-20)。

评测用的端点地址与密钥只在这里:文件路径由环境变量 `RISE_EVAL_CONFIG` 给出,必须在仓库
之外、权限必须是 0600。报错只写键名与路径,不回显任何取值。密钥只经子进程环境变量交给
SoMEval,不进命令行、日志、报告与树。
"""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from factory.configio import key_names
from factory.errors import FactoryConfigError

EVAL_CONFIG_ENV = "RISE_EVAL_CONFIG"
DEFAULT_SERVE_PORT = 18081
DEFAULT_CONTENT_NONE_RATIO_MAX = 0.01

REQUIRED_KEYS = (
    "someval_root", "someval_python", "work_root", "index_path",
    "judge_base_url", "judge_api_key",
)
OPTIONAL_KEYS = ("serve_port", "content_none_ratio_max")
SECRET_KEYS = ("judge_api_key",)


@dataclass(frozen=True)
class EvalConfig:
    """本机评测配置。路径都是绝对路径;密钥只在内存里,`repr` 不含它。"""

    someval_root: Path
    someval_python: Path
    work_root: Path
    index_path: Path
    judge_base_url: str
    judge_api_key: str
    serve_port: int = DEFAULT_SERVE_PORT
    content_none_ratio_max: float = DEFAULT_CONTENT_NONE_RATIO_MAX

    def __repr__(self) -> str:
        return (
            f"EvalConfig(someval_root={str(self.someval_root)!r}, work_root={str(self.work_root)!r}, "
            f"serve_port={self.serve_port}, judge_api_key=<redacted>)"
        )


def _check_private(path: Path) -> None:
    """文件必须只有属主可读写。符号链接、非普通文件、权限过宽都拒绝。"""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise FactoryConfigError(f"{EVAL_CONFIG_ENV} must point at a regular file, not a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise FactoryConfigError(f"{EVAL_CONFIG_ENV} must point at a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise FactoryConfigError(
            f"{path} must not be readable by group or others; chmod 600 it before starting"
        )


def _check_absolute(key: str, value: object, *, repo_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise FactoryConfigError(f"eval config: {key} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise FactoryConfigError(f"eval config: {key} must be an absolute path")
    try:
        path.relative_to(repo_root)
    except ValueError:
        return path
    raise FactoryConfigError(f"eval config: {key} must live outside the control-plane repository")


def read_eval_config(
    *, env: Mapping[str, str] | None = None, repo_root: Path | None = None
) -> EvalConfig:
    """读并校验本机评测配置。缺变量、缺文件、权限过宽、键缺失或多余、路径落在仓库里,都拒绝。"""
    environ = os.environ if env is None else env
    raw = environ.get(EVAL_CONFIG_ENV)
    if not raw:
        raise FactoryConfigError(
            f"{EVAL_CONFIG_ENV} is not set; --evaluator someval needs it to locate the evaluation settings"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise FactoryConfigError(f"{EVAL_CONFIG_ENV} must be an absolute path")
    if not path.is_file():
        raise FactoryConfigError(f"{path} does not exist")
    _check_private(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # ValueError:非法编码,以及 PyYAML 的 timestamp 构造器遇到 2024-13-45 这类值时抛的(它不是 YAMLError)
        raise FactoryConfigError(f"{path} cannot be read as YAML ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise FactoryConfigError(f"{path} must be a mapping")
    keys = set(data)
    missing = set(REQUIRED_KEYS) - keys
    unknown = keys - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
    if missing or unknown:
        raise FactoryConfigError(
            f"{path}: missing {key_names(missing)}, unknown {key_names(unknown)}"
        )

    root = Path(repo_root or Path.cwd()).resolve()
    for key in ("someval_root", "work_root", "index_path", "someval_python"):
        if key == "someval_python":
            if not isinstance(data[key], str) or not data[key]:
                raise FactoryConfigError("eval config: someval_python must be a non-empty string")
            continue
        _check_absolute(key, data[key], repo_root=root)
    for key in ("judge_base_url", "judge_api_key"):
        if not isinstance(data[key], str) or not data[key]:
            raise FactoryConfigError(f"eval config: {key} must be a non-empty string (value not shown)")

    port = data.get("serve_port", DEFAULT_SERVE_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise FactoryConfigError("eval config: serve_port must be an integer in 1..65535")
    ratio = data.get("content_none_ratio_max", DEFAULT_CONTENT_NONE_RATIO_MAX)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not (0.0 <= ratio <= 1.0):
        raise FactoryConfigError("eval config: content_none_ratio_max must be a number in 0..1")

    return EvalConfig(
        someval_root=Path(data["someval_root"]),
        someval_python=Path(data["someval_python"]),
        work_root=Path(data["work_root"]),
        index_path=Path(data["index_path"]),
        judge_base_url=data["judge_base_url"],
        judge_api_key=data["judge_api_key"],
        serve_port=port,
        content_none_ratio_max=float(ratio),
    )
