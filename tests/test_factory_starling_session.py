"""服务会话(设计四 Task 10、12;90 篇运行时身份表、"服务会话 ServiceSession"一节、FC-42、FC-46、FC-47)。"""
from pathlib import Path

import pytest

from factory.memory_config import MemoryConfig
from factory.starling_names import settings_digest
from factory.starling_session import service_runtime
from simple_actio.trace import TRACE_SCHEMA_V2

INFO = {"version": "0.0.1", "commit": "c" * 40, "protocol": 1, "extraction_config": {"b": 1, "a": [2]}}


@pytest.fixture
def memory_config():
    return MemoryConfig(root=Path("/m"), extraction_base_url="http://127.0.0.1:9/v1", extraction_model="x",
                        embedding_base_url="http://127.0.0.1:9/v1", embedding_model="e", embedding_dim=64)


def test_the_runtime_identity_keeps_five_top_level_keys_and_names_the_service(memory_config):
    rt = service_runtime(INFO, {"x": 1}, memory_config)
    assert set(rt) == {"backend", "starling_version", "extraction", "embedding", "trace_schema"}
    assert rt["backend"] == "starling" and rt["trace_schema"] == TRACE_SCHEMA_V2
    assert rt["starling_version"] == f"0.0.1+git:{'c' * 40}+protocol:1"
    assert rt["extraction"]["world_settings"] and len(rt["extraction"]["prompts"]) == 64
    assert rt["extraction"]["world_settings"] == settings_digest({"x": 1})
    assert rt["extraction"] == {"base_url": "http://127.0.0.1:9/v1", "model": "x", "timeout_ms": 4000,
                                "max_retries": 0, "prompts": rt["extraction"]["prompts"],
                                "world_settings": settings_digest({"x": 1})}
    assert rt["embedding"] == {"base_url": "http://127.0.0.1:9/v1", "model": "e", "dim": 64,
                               "timeout_ms": 4000, "max_retries": 0}


def test_the_identity_changes_with_the_extraction_config_and_the_settings(memory_config):
    a = service_runtime(INFO, {"x": 1}, memory_config)
    assert a != service_runtime({**INFO, "extraction_config": {"b": 2}}, {"x": 1}, memory_config)
    assert a != service_runtime(INFO, {"x": 2}, memory_config)
    assert a == service_runtime({**INFO, "extraction_config": {"a": [2], "b": 1}}, {"x": 1}, memory_config)


def test_the_runtime_identity_is_a_fresh_dict_each_call(memory_config):
    a = service_runtime(INFO, {"x": 1}, memory_config)
    a["extraction"]["model"] = "changed"
    assert service_runtime(INFO, {"x": 1}, memory_config)["extraction"]["model"] == "x"
