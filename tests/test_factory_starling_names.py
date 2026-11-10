"""世界名、标签与 rise 设置(设计四第 3 步 Task 9;00 篇世界表与世界名规则,90 篇世界设置表)。

叶子继承那条用真 starling-serve 钉住:默认世界的 source lane 与 provider 的 enable_thinking 不是默认值时,
rise 建的世界与分叉出的世界仍与 rise 设置逐叶相等。
"""
from pathlib import Path

import pytest

from factory.errors import FactoryConfigError
from factory.memory_config import MemoryConfig
from factory.starling_names import (WORLD_NAME_MAX, deploy_world, empty_seed_world, labels, new_suffix, replay_world,
                                    rise_settings, rollout_world, root_label, seed_world, settings_digest)

# 8787 现在的默认世界(info()["default_settings"] 的两段,服务已删掉 base_url)与 key_set
EXTRACTION_8787 = {"provider": "openai", "provider_name": "default", "model": "deepseek-v4-pro", "dim": 0,
                   "max_tokens": 0, "timeout_ms": 120000, "thinking_type": ""}
EMBEDDING_8787 = {"provider": "openai", "provider_name": "embedder", "model": "text-embedding-v4", "dim": 1024,
                  "max_tokens": 0, "timeout_ms": 0, "thinking_type": ""}


def _info(extraction=EXTRACTION_8787, embedding=EMBEDDING_8787, key_set=None):
    defaults = {"vector_backend": "sqlite", "chat": dict(EXTRACTION_8787)}
    if extraction is not None:
        defaults["extraction"] = dict(extraction)
    if embedding is not None:
        defaults["embedding"] = dict(embedding)
    keys = {"extraction": True, "embedding": True, "chat": True} if key_set is None else key_set
    return {"version": "0.0.1", "commit": "c" * 40, "protocol": 1, "extraction_config": {},
            "default_settings": defaults, "key_set": keys}


@pytest.fixture
def external_config():
    return MemoryConfig(root=Path("/m"), timeout_ms=None, max_retries=None, url="http://127.0.0.1:8787",
                        token_file=Path("/t"))


@pytest.fixture
def memory_config():
    return MemoryConfig(root=Path("/m"), extraction_base_url="http://127.0.0.1:9/v1", extraction_model="x",
                        embedding_base_url="http://127.0.0.1:9/v1", embedding_model="e", embedding_dim=64)


def test_names_follow_the_world_table():
    assert deploy_world("rise", "s1") == "rise.deploy.s1"
    assert empty_seed_world("rise") == "rise.seed-empty"
    assert seed_world("rise", "k", "s1", 2) == "rise.seed.k.s1.v2"
    assert rollout_world("rise", "run-1-grpo3-s1-r1", "0a1b2c3d") == "rise.ro.run-1-grpo3-s1-r1.0a1b2c3d"
    assert replay_world("rise", "agent-round-m-v1", "s1", "0a1b2c3d") == "rise.verify.agent-round-m-v1.s1.0a1b2c3d"


def test_an_overlong_name_hashes_the_middle_id_and_keeps_the_rest():
    eid = "e" * 128
    name = rollout_world("rise", eid, "0a1b2c3d")
    assert len(name) <= 128 and name.startswith("rise.ro.") and name.endswith(".0a1b2c3d") and eid not in name


def test_overlong_seed_and_replay_names_hash_ids_longest_first_until_they_fit():
    long_seed, long_scenario = "k" * 100, "s" * 60
    name = seed_world("rise", long_seed, long_scenario, 3)
    assert len(name) <= WORLD_NAME_MAX and long_seed not in name and long_scenario in name   # 换掉最长的一段就够了
    both = seed_world("rise", "k" * 128, "s" * 128, 3)
    assert len(both) <= WORLD_NAME_MAX and "k" * 128 not in both and "s" * 128 not in both
    replay = replay_world("rise", "r" * 128, "s1", "0a1b2c3d")
    assert len(replay) <= WORLD_NAME_MAX and replay.endswith(".s1.0a1b2c3d")
    assert len(deploy_world("rise", "s" * 128)) <= WORLD_NAME_MAX
    assert rollout_world("rise", "e" * 128, "0a1b2c3d") != rollout_world("rise", "e" * 127 + "f", "0a1b2c3d")


def test_a_short_name_is_never_hashed():
    assert deploy_world("rise", "s" * 113) == "rise.deploy." + "s" * 113               # 刚好 125 个字符


def test_the_suffix_is_eight_hex_chars_and_changes_each_call():
    a, b = new_suffix(), new_suffix()
    assert len(a) == 8 and int(a, 16) >= 0 and a != b


def test_the_root_label_is_sixteen_hex_chars_of_the_resolved_path(tmp_path):
    a = root_label(tmp_path)
    assert len(a) == 16 and a == root_label(tmp_path / ".") and a != root_label(tmp_path / "other")


def test_rise_settings_write_every_leaf_and_never_name_a_provider(memory_config):
    s = rise_settings(memory_config)
    assert set(s) == {"extraction", "embedding", "chat", "consolidation", "gist_thresholds", "vector_backend",
                      "source_lane", "background"}
    for role in ("extraction", "embedding"):
        assert set(s[role]) == {"provider", "provider_name", "base_url", "model", "dim", "max_tokens",
                                "timeout_ms", "max_retries", "enable_thinking", "thinking_type"}
        assert s[role]["provider_name"] == "" and s[role]["enable_thinking"] is False
    assert s["extraction"]["dim"] == 0 and s["embedding"]["dim"] == 64
    assert len(s["source_lane"]) == 7 and s["source_lane"]["enabled"] is False
    assert s["background"] == {"tick_interval_s": 0, "ingest": False,
                               "auto_snapshot": {"on_session": False, "interval_hours": 0}}
    assert settings_digest(s) == settings_digest(dict(reversed(list(s.items()))))     # 与键序无关
    assert rise_settings(memory_config) is not s                                      # 每次一份新字典


def test_external_rise_settings_copy_the_default_worlds_endpoints_without_an_address(external_config):
    s = rise_settings(external_config, _info())
    assert s["extraction"] == {**EXTRACTION_8787, "enable_thinking": False}          # 服务没给就补 False
    assert s["embedding"] == {**EMBEDDING_8787, "enable_thinking": False}
    assert "base_url" not in s["extraction"] and "base_url" not in s["embedding"]
    assert "max_retries" not in s["extraction"]                                        # 服务没给就不写,C++ 缺省 3
    assert (s["chat"], s["consolidation"], s["gist_thresholds"]) == (None, None, None)
    assert s["source_lane"]["enabled"] is False and s["background"]["ingest"] is False
    assert set(s) == set(rise_settings(MemoryConfig(root=Path("/m"), extraction_base_url="http://127.0.0.1:9/v1",
                                                    extraction_model="x", embedding_base_url="http://127.0.0.1:9/v1",
                                                    embedding_model="e", embedding_dim=64)))


def test_external_rise_settings_keep_the_services_own_leaves_and_drop_a_stray_address(external_config):
    tuned = {**EXTRACTION_8787, "max_retries": 0, "enable_thinking": True, "base_url": "https://leak.example/v1"}
    s = rise_settings(external_config, _info(extraction=tuned))
    assert s["extraction"]["max_retries"] == 0 and s["extraction"]["enable_thinking"] is True
    assert "base_url" not in s["extraction"]
    info = _info()
    rise_settings(external_config, info)["extraction"]["model"] = "changed"
    assert info["default_settings"]["extraction"]["model"] == "deepseek-v4-pro"       # 不改服务给的那份


@pytest.mark.parametrize("info, role", [
    (_info(extraction=None), "extraction"),                                             # 角色没绑定
    (_info(embedding=None), "embedding"),
    (_info(extraction={**EXTRACTION_8787, "provider_name": ""}), "extraction"),         # 显式端点,不是 provider
    (_info(key_set={"extraction": False, "embedding": True}), "extraction"),            # 没有密钥
    (_info(key_set={"extraction": True}), "embedding"),
])
def test_an_unbound_or_keyless_role_is_a_config_error_naming_the_role(external_config, info, role):
    with pytest.raises(FactoryConfigError) as caught:
        rise_settings(external_config, info)
    msg = str(caught.value)
    assert f"default world's {role} role has no provider bound or no key" in msg and "dashboard" in msg


@pytest.mark.parametrize("dim", [0, -1, None, True])
def test_an_embedding_endpoint_without_a_positive_dim_is_a_config_error(external_config, dim):
    with pytest.raises(FactoryConfigError, match="embedding endpoint has no positive dim"):
        rise_settings(external_config, _info(embedding={**EMBEDDING_8787, "dim": dim}))


def test_external_rise_settings_need_the_service_info(external_config):
    with pytest.raises(ValueError, match="info"):
        rise_settings(external_config)


def test_labels_always_carry_repo_and_root():
    assert labels("rise", "abcd", "deploy", scenario_id="s1") == {"repo": "rise", "root": "abcd", "kind": "deploy",
                                                                 "scenario_id": "s1"}


@pytest.fixture(scope="module")
def tuned_service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import start_service
    proc = start_service(tmp_path_factory.mktemp("tuned"), starling_json={
        "providers": {"p": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                            "enable_thinking": True}},
        "roles": {"extraction": "p"},
        "source_lane_enabled": True, "source_lane_k_source": 7, "source_lane_tau_source": 0.4,
    })
    yield proc
    proc.kill()


def test_worlds_built_and_forked_under_a_tuned_default_world_still_match_rise_settings(tuned_service, memory_config):
    import starling_client
    settings = rise_settings(memory_config)
    client = starling_client.connect(tuned_service.url, token=tuned_service.token)
    try:
        default = client.info()["default_settings"]                                   # 先证明默认世界真被调过
        assert default["source_lane"]["k_source"] == 7 and default["extraction"]["enable_thinking"] is True
        client.create_world("rise.deploy.s1", labels={"repo": "rise", "kind": "deploy"}, settings=settings)
        client.open("rise.deploy.s1", holder="pa", settings=settings).close()        # 不报 world_settings_mismatch
        snap = client.snapshot("rise.deploy.s1", note="t")
        client.fork("rise.deploy.s1", "rise.verify.r.s1.00000000", snapshot_id=snap["snapshot_id"],
                    reclaimable=True, retention_group="r", settings=settings)
        client.open("rise.verify.r.s1.00000000", holder="pa", read_only=True, settings=settings).close()
    finally:
        client.close()
