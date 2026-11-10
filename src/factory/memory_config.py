"""树外的本机记忆配置(设计二 5.1、5.3 节,F15)。

与 `eval_config.py` 同一做法:文件路径由环境变量 `RISE_MEMORY_CONFIG` 给出,必须在控制面仓库之外、
权限必须是 0600。变量没设或为空串,返回 `None`,装配期照计划 A 的替身走;设了,配置有任何问题都抛
`FactoryConfigError`(命令行译成退出码 2),不退回替身。报错只写键名、路径与变量名,不回显任何取值:
端点地址可能带 `user:password@`。密钥不在这份文件里:拉起模式下从 `OPENAI_API_KEY` 读并照传给服务进程,
外部模式下在服务进程的环境里。

本模块不导入 `starling` 与 `starling_client`,安装信息一律经 `importlib.metadata` 读。
"""
from __future__ import annotations

import importlib.metadata
import ipaddress
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from factory.configio import key_names
from factory.errors import FactoryConfigError

MEMORY_CONFIG_ENV = "RISE_MEMORY_CONFIG"
API_KEY_ENV = "OPENAI_API_KEY"
CLIENT_DIST = "starling-client"
STARLING_DIST = "starling-memory"
DEFAULT_TIMEOUT_MS = 4000
DEFAULT_MAX_RETRIES = 0
# 一次记忆网络调用(含重试)的最坏耗时上限:timeout_ms × (max_retries + 1) 不得超过它。外层时限都按"每次至多 4 秒"
# 折算,写死在三处:factory.memory_runtime.DEPLOY_MEMORY_TURN_TIMEOUT_S(部署 40 秒)、
# factory.interact_policy.OPPONENT_TIMEOUT_S(对手 60 秒,低于 INTERACT_BACKEND_TIMEOUT_S 的 90 秒)、
# factory.wire.INTERACT_TURN_TIMEOUT_S(--interact 150 秒)。
# 放宽这里之前先改那三处,否则内层不再先于外层超时。
MAX_MEMORY_CALL_MS = 4000

REQUIRED_KEYS = (
    "root", "extraction_base_url", "extraction_model",
    "embedding_base_url", "embedding_model", "embedding_dim",
)
OPTIONAL_KEYS = ("timeout_ms", "max_retries", "url", "token_file", "world_prefix")
DEFAULT_WORLD_PREFIX = "rise"
# 世界名前缀(00 篇世界名规则):不含 `.`,所以世界名能无歧义地拆开。单一来源在这里,`factory.starling_names` 转出
WORLD_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")

_OWNER_FILE = ".owner"
_OWNER_POLL_S = 0.02


@dataclass(frozen=True)
class MemoryConfig:
    """本机记忆配置。`root` 是绝对路径;两个 `*_base_url` 存规范化后的值(不带 `user:password@`)。

    接记忆服务后:`url` 与 `token_file` 同给同不给;给了是外部模式(连用户在跑的看板服务),都不给是拉起模式。
    `url` 存规范化后的值。`world_prefix` 是世界名前缀。"""

    root: Path
    extraction_base_url: str
    extraction_model: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_retries: int = DEFAULT_MAX_RETRIES
    url: str | None = None
    token_file: Path | None = None
    world_prefix: str = DEFAULT_WORLD_PREFIX

    @property
    def mode(self) -> str:
        """`external`(给了 `url`)或 `spawn`(rise 自己拉起服务)。"""
        return "external" if self.url is not None else "spawn"


def _check_private(path: Path, *, key: str | None = None) -> None:
    """文件必须只有属主可读写。符号链接、非普通文件、权限过宽都拒绝。

    `key` 给了(配置里的键,如 `token_file`),报错写键名不写路径:那个路径是配置的取值。"""
    what = f"memory config: {key}" if key is not None else MEMORY_CONFIG_ENV
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise FactoryConfigError(f"{what} must point at a regular file, not a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise FactoryConfigError(f"{what} must point at a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        shown = what if key is not None else str(path)
        raise FactoryConfigError(
            f"{shown} must not be readable by group or others; chmod 600 it before starting"
        )


def _is_inside(path: Path, repo_root: Path) -> bool:
    """`path` 解开符号链接后是否落在 `repo_root` 之下(两边都 resolve,防符号链接绕进仓库)。"""
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def _check_root(value: object, *, repo_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise FactoryConfigError("memory config: root must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise FactoryConfigError("memory config: root must be an absolute path")
    if _is_inside(path, repo_root):
        raise FactoryConfigError("memory config: root must live outside the control-plane repository")
    # 记忆根目录只属于一个仓库,启动清扫会删 <root>/runs 下的所有子目录:根不能是仓库的上级
    if _is_inside(repo_root, path):
        raise FactoryConfigError("memory config: root must not contain the control-plane repository")
    return path


def _check_model(key: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise FactoryConfigError(f"memory config: {key} must be a non-empty string")
    return value


def _check_int(key: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        kind = "a positive integer" if minimum == 1 else "a non-negative integer"
        raise FactoryConfigError(f"memory config: {key} must be {kind}")
    return value


def normalize_base_url(key: str, value: object) -> str:
    """校验并规范化端点地址:小写 scheme 与主机,去掉路径末尾的 `/`。报错只写键名,不含取值。

    Starling 在它后面直接拼 `/chat/completions` 与 `/embeddings`,所以带 query 或 fragment 的地址拒绝。
    """
    if not isinstance(value, str) or not value:
        raise FactoryConfigError(f"memory config: {key} must be a non-empty URL")
    shape = f"memory config: {key} must be an http(s) endpoint URL without query or fragment (value not shown)"
    try:
        parts = urlsplit(value)
        username, password, host, port = parts.username, parts.password, parts.hostname, parts.port
    except ValueError:
        # urlsplit 与 .port 的 ValueError 信息会带上取值,这里不转述
        raise FactoryConfigError(shape) from None
    if username is not None or password is not None:
        raise FactoryConfigError(f"memory config: {key} must not carry user:password@ (value not shown)")
    if parts.scheme not in ("http", "https") or not host or parts.query or parts.fragment:
        raise FactoryConfigError(shape)
    if port is not None and not 1 <= port <= 65535:       # urlsplit 已拒绝 >65535,这里实际多拦 0
        raise FactoryConfigError(shape)
    if ":" in host:                                       # IPv6:hostname 去掉了方括号,拼回去
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parts.scheme}://{host.lower()}{suffix}{parts.path.rstrip('/')}"


_LOOPBACK_URL = "memory config: url must be an http URL on the loopback interface"


def _check_service_url(value: object) -> str:
    """记忆服务地址:http、主机是回环地址(`127.0.0.0/8`、`::1`、`localhost`)。报错不回显取值。"""
    url = normalize_base_url("url", value)              # 不许 userinfo、query、fragment
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "http":
        raise FactoryConfigError(_LOOPBACK_URL)
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise FactoryConfigError(_LOOPBACK_URL)
    return url


def _check_token_file(value: object, *, repo_root: Path) -> Path:
    """看板服务的 token 文件:绝对路径、仓库外、0600 的普通文件。报错只写键名,不写路径。"""
    if not isinstance(value, str) or not value:
        raise FactoryConfigError("memory config: token_file must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise FactoryConfigError("memory config: token_file must be an absolute path")
    if _is_inside(path, repo_root):
        raise FactoryConfigError("memory config: token_file must live outside the control-plane repository")
    if not os.path.lexists(path):
        raise FactoryConfigError("memory config: token_file does not exist")
    _check_private(path, key="token_file")
    return path


def _check_world_prefix(value: object) -> str:
    if not isinstance(value, str) or not WORLD_PREFIX_PATTERN.fullmatch(value):
        raise FactoryConfigError("memory config: world_prefix must match [A-Za-z0-9][A-Za-z0-9_-]{0,15}")
    return value


def read_memory_config(*, env: Mapping[str, str] | None = None, repo_root: Path) -> MemoryConfig | None:
    """读并校验树外记忆配置。变量没设或为空串返回 `None`(替身);其余不合格一律 `FactoryConfigError`。"""
    environ = os.environ if env is None else env
    raw = environ.get(MEMORY_CONFIG_ENV)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise FactoryConfigError(f"{MEMORY_CONFIG_ENV} must be an absolute path")
    if not path.is_file():
        raise FactoryConfigError(f"{path} does not exist")
    _check_private(path)
    if _is_inside(path, repo_root):
        raise FactoryConfigError(f"{path} must live outside the control-plane repository")
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
        raise FactoryConfigError(f"{path}: missing {key_names(missing)}, unknown {key_names(unknown)}")

    root = _check_root(data["root"], repo_root=repo_root)
    extraction_base_url = normalize_base_url("extraction_base_url", data["extraction_base_url"])
    embedding_base_url = normalize_base_url("embedding_base_url", data["embedding_base_url"])
    extraction_model = _check_model("extraction_model", data["extraction_model"])
    embedding_model = _check_model("embedding_model", data["embedding_model"])
    embedding_dim = _check_int("embedding_dim", data["embedding_dim"], minimum=1)
    timeout_ms = _check_int("timeout_ms", data.get("timeout_ms", DEFAULT_TIMEOUT_MS), minimum=1)
    max_retries = _check_int("max_retries", data.get("max_retries", DEFAULT_MAX_RETRIES), minimum=0)
    if timeout_ms * (max_retries + 1) > MAX_MEMORY_CALL_MS:
        raise FactoryConfigError(
            f"memory config: timeout_ms × (max_retries + 1) must not exceed {MAX_MEMORY_CALL_MS} ms; "
            "outer turn budgets assume at most 4 s per memory call"
        )
    if ("url" in data) != ("token_file" in data):
        raise FactoryConfigError("memory config: url and token_file must be given together (or both left out)")
    url = _check_service_url(data["url"]) if "url" in data else None
    token_file = _check_token_file(data["token_file"], repo_root=repo_root) if "token_file" in data else None
    world_prefix = _check_world_prefix(data.get("world_prefix", DEFAULT_WORLD_PREFIX))
    return MemoryConfig(
        root=root, extraction_base_url=extraction_base_url, extraction_model=extraction_model,
        embedding_base_url=embedding_base_url, embedding_model=embedding_model,
        embedding_dim=embedding_dim, timeout_ms=timeout_ms, max_retries=max_retries,
        url=url, token_file=token_file, world_prefix=world_prefix,
    )


def check_api_key(env: Mapping[str, str]) -> None:
    """密钥变量必须非空。只报变量名。"""
    if not (env.get(API_KEY_ENV) or "").strip():
        raise FactoryConfigError(f"{API_KEY_ENV} is empty or unset; the Starling memory reads its key from it")


def _publish_owner(root: Path, owner: Path, content: bytes) -> bool:
    """先写临时文件、fsync,再用 `os.link` 发布成 `.owner`:`.owner` 要么不存在,要么内容完整。

    目标已存在时 `os.link` 抛 `FileExistsError`,保住了 `O_EXCL` 的"只有一个赢家"。返回 True 表示这次
    建成了;False 表示别人已经建了。不论成败,临时文件都删掉(含 Ctrl-C)。
    """
    tmp = root / f"{_OWNER_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, owner)
        except FileExistsError:
            return False
        return True
    except OSError as exc:
        raise FactoryConfigError(f"memory root {root}: cannot write {_OWNER_FILE} ({type(exc).__name__})") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def claim_memory_root(root: Path, repo_root: Path, *, read_back_timeout_s: float = 2.0) -> None:
    """首次使用时把 `root` 记在 `repo_root` 名下;已被另一个仓库占用就拒绝,信息写两个路径。

    `.owner` 由临时文件经 `os.link` 发布,两个进程同时首次使用时只有一个发布得成,且发布出来的就是完整内容。
    另一个读回比对。旧版本(直接 `O_EXCL` 建 `.owner`)被打断时会留下 0 字节的文件:读到空就每 20 毫秒
    再读,至多 `read_back_timeout_s`,仍空就拒绝,信息写出该删哪个文件。
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FactoryConfigError(f"memory root {root}: cannot be created ({type(exc).__name__})") from None
    mine = repo_root.resolve()
    me = f"{mine}\n"
    try:
        content = me.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FactoryConfigError(f"repository path cannot be recorded as UTF-8 ({type(exc).__name__})") from None
    owner = root / _OWNER_FILE
    deadline = time.monotonic() + read_back_timeout_s
    while True:
        if _publish_owner(root, owner, content):
            return
        try:
            theirs = owner.read_text(encoding="utf-8")
        except FileNotFoundError:
            theirs = ""                                    # 有人把它删了:回到循环头再发布
        except (OSError, UnicodeDecodeError) as exc:
            raise FactoryConfigError(f"memory root {root}: {_OWNER_FILE} cannot be read ({type(exc).__name__})") from None
        if theirs:
            if theirs == me:
                return
            raise FactoryConfigError(f"memory root {root} belongs to {theirs.strip()}, not {mine}")
        if time.monotonic() >= deadline:
            raise FactoryConfigError(
                f"memory root {root}: {_OWNER_FILE} is empty; "
                f"if no other repository uses this root, delete {owner} and retry"
            )
        time.sleep(_OWNER_POLL_S)


def _direct_url(dist: importlib.metadata.Distribution) -> dict:
    text = dist.read_text("direct_url.json")
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise FactoryConfigError("Starling's direct_url.json is not a JSON object; cannot identify the installed code")
    return data


def client_install_commit(dist_name: str, *,
                          distribution: importlib.metadata.Distribution | None = None) -> str:
    """已装发行包 `dist_name`(`starling-client` 或 `starling-memory`)的 git 提交(90 篇第 15、16 条)。

    未安装、editable、`direct_url.json` 里没有 `vcs_info.commit_id`,都抛 `FactoryConfigError`:editable 的代码会在
    指令运行中改变;没有提交就无法与服务 `info()` 报的提交比对。不再按 RECORD 认代码(`record:` 来源已取消)。"""
    dist = distribution
    if dist is None:
        try:
            dist = importlib.metadata.distribution(dist_name)
        except importlib.metadata.PackageNotFoundError:
            raise FactoryConfigError(
                f"{MEMORY_CONFIG_ENV} is set but {dist_name} is not installed; "
                "run pip install '.[memory]' (or uv sync --extra memory)"
            ) from None
    direct = _direct_url(dist)
    dir_info, vcs_info = direct.get("dir_info"), direct.get("vcs_info")
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        raise FactoryConfigError(
            f"{dist_name} is an editable install; its code can change under a running directive, "
            "so install it from git (pip install '.[memory]')"
        )
    commit = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    if not isinstance(commit, str) or not commit:
        raise FactoryConfigError(
            f"{dist_name} was not installed from a git commit; rise compares that commit with the memory service's, "
            "so install it from git (pip install '.[memory]')"
        )
    return commit
