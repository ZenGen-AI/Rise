"""计算端点配置读取:缺文件、信封不合、载荷畸形、键缺失或多余、正常多端点。

信封必须是 type="config"(read_config 只认 CONFIG_TYPE),端点列表放在 payload 里。
"""
from __future__ import annotations

import pytest
from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from factory.compute_config import COMPUTE_ENDPOINTS_FILE, read_endpoints_config
from factory.errors import FactoryConfigError

_ITEM = {
    "endpoint_id": "gpu-1",
    "work_root": "/srv/rise/work",
    "roll_root": "/srv/roll",
    "launch_prefix": [],
    "base_model_path": "/srv/models/qwen-4b",
    "heldout_jsonl": "/srv/roll/data/heldout.jsonl",
    "sft_template": "/srv/roll/configs/sft_lora.yaml",
    "min_free_host_mem_gib": 12.0,
    "min_free_disk_gib": 50.0,
    "poll_seconds": 10.0,
    "job_timeout_seconds": 3600.0,
}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _write(store: GitStore, payload, *, type_: str = "config") -> None:
    env = Envelope(
        id="cfg-compute-endpoints", type=type_, version=1, created_at="2026-10-15T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload=payload,
    )
    store.write_product(COMPUTE_ENDPOINTS_FILE, env, "test: compute endpoints")


def test_missing_file_is_a_factory_config_error(store):
    with pytest.raises(FactoryConfigError, match=r"control-plane/compute/endpoints\.yaml is missing"):
        read_endpoints_config(store)


def test_wrong_envelope_type_is_rejected(store):
    _write(store, {"endpoints": []}, type_="compute_endpoints")
    with pytest.raises(FactoryConfigError, match=r"is not a configuration product"):
        read_endpoints_config(store)


def test_endpoints_must_be_a_list(store):
    _write(store, {"endpoints": {"gpu-1": _ITEM}})
    with pytest.raises(FactoryConfigError, match=r"payload\.endpoints must be a list"):
        read_endpoints_config(store)


def test_item_must_be_a_mapping(store):
    _write(store, {"endpoints": ["nope"]})
    with pytest.raises(FactoryConfigError, match=r"item 0 must be a mapping"):
        read_endpoints_config(store)


def test_missing_key_is_named(store):
    _write(store, {"endpoints": [{k: v for k, v in _ITEM.items() if k != "work_root"}]})
    with pytest.raises(FactoryConfigError, match=r"missing \['work_root'\]"):
        read_endpoints_config(store)


def test_unknown_key_is_named(store):
    _write(store, {"endpoints": [{**_ITEM, "tail_lines": 50}]})
    with pytest.raises(FactoryConfigError, match=r"unknown \['tail_lines'\]"):
        read_endpoints_config(store)


def test_malformed_value_is_named(store):
    _write(store, {"endpoints": [{**_ITEM, "endpoint_id": "-bad"}]})
    with pytest.raises(FactoryConfigError, match=r"item 0: endpoint_id has an invalid form"):
        read_endpoints_config(store)


def test_success_reads_two_endpoints(store):
    _write(store, {"endpoints": [_ITEM, {**_ITEM, "endpoint_id": "gpu-2", "poll_seconds": 5.0}]})
    eps = read_endpoints_config(store)
    assert [e.endpoint_id for e in eps] == ["gpu-1", "gpu-2"]
    assert eps[0].work_root == "/srv/rise/work"
    assert eps[1].poll_seconds == 5.0
    assert eps[0].launch_prefix == ()


# ---------------- 本机主机映射与装配注入 ----------------

from factory.compute_config import HOSTS_ENV, build_compute_client, read_host_map


def _hosts_file(tmp_path, text):
    path = tmp_path / "hosts.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_host_map_missing_env_names_the_variable(tmp_path):
    with pytest.raises(FactoryConfigError, match=rf"{HOSTS_ENV} is not set"):
        read_host_map(env={})


def test_host_map_missing_file_names_the_path(tmp_path):
    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(FactoryConfigError, match=r"cannot be read as a compute host map"):
        read_host_map(env={HOSTS_ENV: missing})


def test_host_map_must_be_a_mapping_of_strings(tmp_path):
    path = _hosts_file(tmp_path, "- just\n- a list\n")
    with pytest.raises(FactoryConfigError, match=r"must be a mapping of endpoint_id to ssh alias"):
        read_host_map(env={HOSTS_ENV: path})


def test_host_map_reads_the_aliases(tmp_path):
    path = _hosts_file(tmp_path, "gpu-1: '.181'\nmock-gpu: localhost\n")
    assert read_host_map(env={HOSTS_ENV: path}) == {"gpu-1": ".181", "mock-gpu": "localhost"}


def test_unknown_endpoint_in_the_map_is_a_config_error(tmp_path):
    path = _hosts_file(tmp_path, "other: somewhere\n")
    with pytest.raises(FactoryConfigError, match=rf"{HOSTS_ENV} has no entry for endpoint 'gpu-1'"):
        build_compute_client(ComputeEndpoint(**_ITEM), read_host_map(env={HOSTS_ENV: path}))


def test_build_compute_client_wires_the_ssh_transport(tmp_path):
    # 别名要过 transport._ALIAS(首字符必须是字母数字或下划线),所以这里用 ~/.ssh/config 里
    # 真实条目的形状;规格里的 `.181` 是主机简称,不是别名。
    path = _hosts_file(tmp_path, "gpu-1: gpu-181\n")
    client = build_compute_client(ComputeEndpoint(**_ITEM), read_host_map(env={HOSTS_ENV: path}))
    assert isinstance(client, ComputeClient)
    assert client.endpoint.endpoint_id == "gpu-1"
    assert client._transport.ssh_argv(["python3", "-c", "1"])[:6] == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--",
    ]


@pytest.mark.parametrize("alias", ["-oProxyCommand=touch /tmp/pwned", "a b", "has'quote"])
def test_a_malformed_ssh_alias_is_a_config_error(tmp_path, alias):
    """别名来自本机映射文件,会进 ssh 的 argv。以 `-` 开头的会被 ssh 当选项读
    (ProxyCommand 能执行任意命令),所以 transport._ALIAS 拒绝它;装配期就要翻成
    FactoryConfigError,不能让 ComputeError 漏到调用方。

    空别名不走这条分支:read_host_map 的"值必须是非空字符串"先把它拦下。"""
    path = _hosts_file(tmp_path, f"gpu-1: {alias!r}\n")
    with pytest.raises(FactoryConfigError, match=r"the ssh alias for endpoint 'gpu-1' has an invalid form"):
        build_compute_client(ComputeEndpoint(**_ITEM), read_host_map(env={HOSTS_ENV: path}))


def test_install_defaults_does_not_install_endpoints(tmp_path):
    """远端路径没有合理默认值:默认值安装器不得写端点配置,缺了就拒绝装配。"""
    from factory.defaults import install_defaults

    store = GitStore(str(tmp_path / "cp"))
    store.init()
    paths = install_defaults(store)
    assert COMPUTE_ENDPOINTS_FILE not in paths
    assert not store.exists(COMPUTE_ENDPOINTS_FILE)
    with pytest.raises(FactoryConfigError, match=r"is missing; real backends need it"):
        read_endpoints_config(store)
