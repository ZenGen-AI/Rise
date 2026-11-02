"""计算端点的配置读取与装配注入(真实后端设计 §3.1)。

两处配置各读各的:
- 树内 `control-plane/compute/endpoints.yaml`(config 信封,与 `evaluator.yaml` 同一条路径),
  读出 `compute.ComputeEndpoint` 列表。缺文件、信封不合、键缺失或多余、取值不合数据类约束,
  一律 `FactoryConfigError`,装配期就报,不等首次 reconcile。
- 本机主机映射:路径由环境变量 `RISMFORGE_COMPUTE_HOSTS` 指定,内容是 `{endpoint_id: ssh 别名}`,
  放在仓库之外,凭据都在 `~/.ssh/config` 里。

`read_tree_config` 返回 payload 的深拷贝,不是 Envelope;信封 type 与 schema_version 已由
`read_config` 与迁移链把关,这里不重复判。
"""
from __future__ import annotations

import os
from collections.abc import Mapping

import yaml

from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError
from compute.transport import SshTransport
from controlplane.gitstore import GitStore
from controlplane.paths import compute_endpoints_file
from factory.configio import key_names, read_tree_config
from factory.errors import FactoryConfigError

COMPUTE_ENDPOINTS_FILE = compute_endpoints_file()
HOSTS_ENV = "RISMFORGE_COMPUTE_HOSTS"

_ENDPOINT_KEYS = (
    "endpoint_id", "work_root", "roll_root", "launch_prefix", "base_model_path",
    "heldout_jsonl", "sft_template", "min_free_host_mem_gib", "min_free_disk_gib",
    "poll_seconds", "job_timeout_seconds",
)
# 可选键:不写时取数据类的缺省。host_deps_root 只有 --executor roll 要,缺了在装配期报错(factory.wire)。
_OPTIONAL_ENDPOINT_KEYS = ("host_deps_root",)


def read_endpoints_config(store: GitStore) -> list[ComputeEndpoint]:
    """树内端点列表。远端路径没有合理默认值,缺了就拒绝装配真实后端。"""
    try:
        payload = read_tree_config(store, COMPUTE_ENDPOINTS_FILE)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{COMPUTE_ENDPOINTS_FILE} is missing; real backends need it to build compute endpoints"
        ) from None
    # 信封 schema 要求 payload 是映射,端点列表放在 endpoints 键下;缺键与非列表同一句报错。
    items = payload.get("endpoints")
    if not isinstance(items, list):
        raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} payload.endpoints must be a list")
    endpoints: list[ComputeEndpoint] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} item {index} must be a mapping")
        keys = set(item)
        missing = key_names(set(_ENDPOINT_KEYS) - keys)
        unknown = key_names(keys - set(_ENDPOINT_KEYS) - set(_OPTIONAL_ENDPOINT_KEYS))
        if missing or unknown:
            raise FactoryConfigError(
                f"{COMPUTE_ENDPOINTS_FILE} item {index}: missing {missing}, unknown {unknown}"
            )
        try:
            endpoints.append(ComputeEndpoint(**item))
        except ComputeError as exc:
            raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} item {index}: {exc}") from None
    return endpoints


def select_endpoint(endpoints: list[ComputeEndpoint], name: str | None) -> ComputeEndpoint:
    """按 `--endpoint` 在树内端点里选一个。只有一个端点时可以不点名;多于一个必须点名。

    报错列出现有端点的 id:它们来自 git 树里的配置,不含秘密,而且「点名点错了」时
    最有用的信息就是正确答案有哪些。"""
    if not endpoints:
        raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} defines no endpoints")
    ids = [e.endpoint_id for e in endpoints]
    if name is None:
        if len(endpoints) == 1:
            return endpoints[0]
        raise FactoryConfigError(
            f"{COMPUTE_ENDPOINTS_FILE} defines {len(endpoints)} endpoints {ids}; "
            "name one with --endpoint"
        )
    for endpoint in endpoints:
        if endpoint.endpoint_id == name:
            return endpoint
    raise FactoryConfigError(f"no endpoint {name!r} in {COMPUTE_ENDPOINTS_FILE}; it defines {ids}")


def read_host_map(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """本机主机映射 `{endpoint_id: ssh 别名}`。路径由 `RISMFORGE_COMPUTE_HOSTS` 指定。

    映射放在仓库之外,别名指向 `~/.ssh/config` 的主机条目,所以两处配置都不存凭据。
    报错只写环境变量名与路径,不回显内容。
    """
    environ = os.environ if env is None else env
    path = environ.get(HOSTS_ENV)
    if not path:
        raise FactoryConfigError(
            f"{HOSTS_ENV} is not set; real backends need it to locate the endpoint hosts"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError:
        raise FactoryConfigError(f"{path} cannot be read as a compute host map (OSError)") from None
    except yaml.YAMLError:
        raise FactoryConfigError(f"{path} cannot be read as a compute host map (YAMLError)") from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()
    ):
        raise FactoryConfigError(f"{path} must be a mapping of endpoint_id to ssh alias")
    return dict(data)


def build_compute_client(
    endpoint: ComputeEndpoint,
    hosts: Mapping[str, str],
    *,
    python: str = "python3",
) -> ComputeClient:
    """按端点与主机映射装配一个 `ComputeClient`。别名只在映射里,不进 git 树。"""
    alias = hosts.get(endpoint.endpoint_id)
    if alias is None:
        raise FactoryConfigError(
            f"{HOSTS_ENV} has no entry for endpoint {endpoint.endpoint_id!r}"
        )
    try:
        transport = SshTransport(alias)
    except ComputeError:
        raise FactoryConfigError(
            f"the ssh alias for endpoint {endpoint.endpoint_id!r} has an invalid form"
        ) from None
    return ComputeClient(endpoint, transport, python=python)
