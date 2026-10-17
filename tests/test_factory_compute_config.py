"""计算端点配置读取:缺文件、信封不合、载荷畸形、键缺失或多余、正常多端点。

信封必须是 type="config"(read_config 只认 CONFIG_TYPE),端点列表放在 payload 里。
"""
from __future__ import annotations

import pytest
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
