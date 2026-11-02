"""端点描述(冻结数据类)与 launch_prefix 白名单(真实后端设计 §3.1、§3.2)。

主机定位(ssh 别名)不在这里:它留在本机文件里,由 SshTransport 持有。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from compute.errors import ComputeError

ENDPOINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LAUNCH_PREFIX_PROGRAMS = ("docker", "podman")


def check_launch_prefix(prefix: Sequence[str]) -> tuple[str, ...]:
    """空列表(裸机),或 `<docker|podman> exec ... <容器>`;其余一律拒绝。

    它来自 git 树,会被原样拼在远端命令前,所以放宽白名单要改代码并经评审(设计 §3.2)。
    必须是 exec:`run` 每次起新容器,会破坏"所有调用在同一个 PID 命名空间"的前提。
    报错只写约束,不回显取值。"""
    if not isinstance(prefix, (list, tuple)) or not all(isinstance(a, str) and a and "\0" not in a for a in prefix):
        raise ComputeError("launch_prefix must be a list of non-empty strings")
    if not prefix:
        return ()
    if prefix[0] not in LAUNCH_PREFIX_PROGRAMS:
        raise ComputeError("launch_prefix must be empty or start with docker or podman")
    if len(prefix) < 3 or prefix[1] != "exec":
        raise ComputeError("launch_prefix must have the form '<docker|podman> exec ... <container>'")
    return tuple(prefix)


def is_clean_abs_path(path: object) -> bool:
    return (isinstance(path, str) and path.startswith("/") and "\0" not in path
            and ".." not in path.split("/") and path.rstrip("/") != "")


@dataclass(frozen=True)
class ComputeEndpoint:
    endpoint_id: str
    work_root: str
    roll_root: str
    launch_prefix: tuple[str, ...]
    base_model_path: str
    heldout_jsonl: str
    sft_template: str
    min_free_host_mem_gib: float
    min_free_disk_gib: float
    poll_seconds: float
    job_timeout_seconds: float
    # 依赖源码副本(Megatron-LM、fla)的父目录,宿主机路径。只有 --executor roll 用它,
    # 由 compute.roll_deploy 只读挂进系统建的容器;旧的 endpoints.yaml 不写这个键也照常读。
    host_deps_root: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "launch_prefix", check_launch_prefix(self.launch_prefix))
        if not isinstance(self.endpoint_id, str) or not ENDPOINT_ID_PATTERN.match(self.endpoint_id):
            raise ComputeError("endpoint_id has an invalid form")
        for name in ("work_root", "roll_root", "base_model_path", "heldout_jsonl"):
            if not is_clean_abs_path(getattr(self, name)):
                raise ComputeError(f"{name} must be an absolute path below / without '..'")
        if self.host_deps_root is not None and not is_clean_abs_path(self.host_deps_root):
            raise ComputeError("host_deps_root must be an absolute path below / without '..'")
        if not isinstance(self.sft_template, str) or not self.sft_template:
            raise ComputeError("sft_template must be a non-empty string")
        for name in ("min_free_host_mem_gib", "min_free_disk_gib"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ComputeError(f"{name} must be a non-negative number")
        for name in ("poll_seconds", "job_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ComputeError(f"{name} must be a positive number")
