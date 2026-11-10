"""`bind` 按起点摘要钉住种子版本,解析器按标签找种子(90 篇 `StarlingTrainingWorlds` 变化表的 `bind` 行、00 篇
"按标签找种子"、30 篇失败表)。

真服务用外部模式:模块共用一个 starling-serve,每条用例一个世界名前缀。种子由 `build_seeds` 建,它另开一个会话;
拉起模式下夹具自己的服务占着归属锁,`build_seeds` 会被拒绝。`root` 在 tmp_path 下,假抽取与嵌入端点,只放占位密钥。"""
from __future__ import annotations

import contextlib
import itertools
import os

import pytest
import yaml

from controlplane.config import write_config
from controlplane.daemon import DirectiveFailedError
from controlplane.gitstore import GitStore
from factory.agent import AGENT_CONFIG
from factory.defaults import DEFAULT_AGENT_CONFIG
from factory.memory_config import MEMORY_CONFIG_ENV
from simple_actio.memory_port import MemoryFailureError


@pytest.fixture(scope="module")
def fake():
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_fakes import FakeOpenAI
    with FakeOpenAI(dim=256) as f:
        yield f


@pytest.fixture(scope="module")
def service(tmp_path_factory, fake):
    """模拟看板服务:默认世界的抽取与嵌入绑到指向假端点的 provider(外部模式的端点跟它走)。"""
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import provider_starling_json, start_service
    proc = start_service(tmp_path_factory.mktemp("svc"), starling_json=provider_starling_json(fake))
    yield proc
    proc.kill()


@pytest.fixture
def seed_env(tmp_path, service, fake):
    """`seed_env(scenarios=("s1",), admin_wrap=None)` 返回 `(worlds, resolver, make_seed)`:同一个会话上的
    `StarlingTrainingWorlds` 与解析器;`make_seed(seed_id, sid, text, new_version=False)` 往部署世界写一条事件后调
    `build_seeds`,返回 `SeedResult`。`admin_wrap(admin)` 给了就包住训练世界的管理调用。"""
    from factory import starling_names
    from factory.memory_runtime import StarlingTrainingWorlds, open_memory_service, starling_memory_resolver
    from factory.seed import build_seeds
    from simple_actio.memory_starling import StarlingDeploymentStore
    from starling_service import unique_prefix

    stack = contextlib.ExitStack()
    keys = itertools.count(1)

    def make(scenarios=("s1",), admin_wrap=None):
        store = GitStore(str(tmp_path / "cp"))
        store.init()
        write_config(store, AGENT_CONFIG, "agent", dict(DEFAULT_AGENT_CONFIG, scenario_ids=list(scenarios)),
                     "test: agent.yaml")
        path = tmp_path / "memory.yaml"
        path.write_text(yaml.safe_dump({            # 外部模式:端点跟服务的默认世界走
            "root": str(tmp_path / "mem"), "url": service.url, "token_file": str(service.token_path),
            "world_prefix": unique_prefix(),
        }), encoding="utf-8")
        os.chmod(path, 0o600)
        env = {MEMORY_CONFIG_ENV: str(path), "PATH": os.environ["PATH"]}
        handle = open_memory_service(store.repo_root, env)
        stack.callback(handle.close)
        session = handle.session
        admin = admin_wrap(session.admin) if admin_wrap is not None else None
        worlds = StarlingTrainingWorlds(session, prefix=handle.prefix, root_label=handle.root_label,
                                        settings=handle.settings, empty_seed=handle.empty_seed,
                                        close_timeout_s=0.2, held_wait_s=0.1, admin=admin)
        resolver = starling_memory_resolver(session, prefix=handle.prefix, root_label=handle.root_label,
                                            empty_seed_world=handle.empty_seed[0], runtime=handle.runtime)
        deploy = StarlingDeploymentStore(session, names=starling_names, prefix=handle.prefix,
                                         root_label=handle.root_label, settings=handle.settings,
                                         marker_dir=handle.config.root / "deploy",
                                         empty_seed_digest=handle.empty_seed[1])

        def make_seed(seed_id, sid, text, new_version=False):
            deploy.remember_event(sid, "pa", f"k#{next(keys)}@pa", text, interlocutor="pb")
            (result,) = build_seeds(store.repo_root, env, seed_id=seed_id, scenario_ids=[sid], snapshot_id=None,
                                    new_version=new_version)
            return result

        return worlds, resolver, make_seed

    yield make
    stack.close()


def test_bind_pins_the_version_whose_digest_was_resolved_even_after_a_newer_one(seed_env):
    worlds, resolver, make_seed = seed_env()
    v1 = make_seed("k", "s1", text="pb：第一版")
    digests, _ = resolver("k", ("s1",))
    assert digests == {"s1": v1.digest}
    v2 = make_seed("k", "s1", text="pb：第二版", new_version=True)      # 解析之后有人建了 v2
    assert v2.digest != v1.digest
    worlds.bind("run-1", "k", digests)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert port.digest() == v1.digest                                    # 在跑的 run 不换起点
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_the_resolver_takes_the_newest_version_and_the_rollout_carries_the_seed_id(seed_env):
    worlds, resolver, make_seed = seed_env()
    make_seed("k", "s1", text="pb：第一版")
    v2 = make_seed("k", "s1", text="pb：第二版", new_version=True)
    digests, runtime = resolver("k", ("s1",))
    assert digests == {"s1": v2.digest} and runtime["backend"] == "starling"
    worlds.bind("run-1", "k", digests)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    try:
        name = port._remote.world
        rows = worlds._session.admin("list_worlds", {"labels": {"repo": worlds.prefix, "kind": "rollout"},
                                                     "page": 1, "page_size": 50, "with_digest": False})["worlds"]
        (row,) = [w for w in rows if w["name"] == name]
        assert row["labels"]["seed_id"] == "k" and row["fork_of_world"] == v2.world
    finally:
        worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_no_seed_with_the_resolved_digest_is_a_permanent_failure(seed_env):
    worlds, resolver, make_seed = seed_env()
    make_seed("k", "s1", text="pb：一")
    with pytest.raises(MemoryFailureError) as info:
        worlds.bind("run-1", "k", {"s1": "0" * 64})
    assert info.value.transient is False and "s1" in str(info.value)
    with pytest.raises(KeyError):                                        # 没绑成:表里没有这个 run
        worlds.seed_of("run-1")


def test_a_scenario_without_a_seed_starts_from_the_empty_seed(seed_env):
    worlds, resolver, make_seed = seed_env(scenarios=("s1", "s2"))
    make_seed("k", "s1", text="pb：一")
    digests, _ = resolver("k", ("s1", "s2"))
    assert digests["s2"] == worlds.empty_seed[1]
    worlds.bind("run-1", "k", digests)
    port = worlds.open_rollout("run-1", "s2", "run-1-grpo1-s2-r1", "pa")
    assert port.digest() == worlds.empty_seed[1]
    worlds.close_rollout("run-1-grpo1-s2-r1", [port])


def test_a_seed_id_with_no_world_at_all_fails_the_directive(seed_env):
    _, resolver, _ = seed_env()
    with pytest.raises(DirectiveFailedError, match="no seed world under this id"):
        resolver("nope", ("s1",))


def test_a_run_without_a_seed_pins_the_empty_seed_for_every_scenario(seed_env):
    worlds, resolver, _ = seed_env()
    digests, _ = resolver(None, ("s1",))
    worlds.bind("run-1", None, digests)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert port.digest() == worlds.empty_seed[1]
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def _failing_fork(code=None, digest=None):
    """包住管理调用:`fork` 抛带 `code` 的永久失败,或照常做完却回一个别的摘要。"""
    def wrap(admin):
        def call(op, body):
            if op != "fork":
                return admin(op, body)
            if code is not None:
                exc = MemoryFailureError(f"starling {code} (WorldError)", transient=False)
                exc.code = code
                raise exc
            out = admin(op, body)
            return {**out, "digest": digest}
        return call
    return wrap


def test_a_seed_behind_the_schema_is_permanent_and_says_to_upgrade_it_in_the_dashboard(seed_env):
    worlds, resolver, make_seed = seed_env(admin_wrap=_failing_fork(code="world_schema_behind"))
    make_seed("k", "s1", text="pb：一")
    worlds.bind("run-1", "k", resolver("k", ("s1",))[0])
    with pytest.raises(MemoryFailureError) as info:
        worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert info.value.transient is False and "upgrade the seed in the dashboard" in str(info.value)
    assert getattr(info.value, "code", None) == "world_schema_behind"


def test_a_fork_whose_digest_is_not_the_pinned_one_is_permanent(seed_env):
    worlds, resolver, make_seed = seed_env(admin_wrap=_failing_fork(digest="f" * 64))
    make_seed("k", "s1", text="pb：一")
    worlds.bind("run-1", "k", resolver("k", ("s1",))[0])
    with pytest.raises(MemoryFailureError, match="does not match the pinned start") as info:
        worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert info.value.transient is False
