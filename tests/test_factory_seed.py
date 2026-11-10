"""子命令 `factory seed`(90 篇 `factory seed` 的规则表、FC-44)。

真服务两种模式都跑:外部模式连本模块共用的一个 starling-serve(每条用例一个世界名前缀,互不相见);拉起模式由 rise
自己拉起服务(数据目录与 HOME 在 `root/service` 下),用完停掉。`root` 都在 tmp_path 下,抽取与嵌入走本机回环的假端点,
只放占位密钥。不碰用户的 ~/.starling 与 8787 上的看板服务。"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess

import pytest
import yaml

from controlplane.config import write_config
from controlplane.gitstore import GitStore
from controlplane.lock import hold_file_lock
from factory.agent import AGENT_CONFIG
from factory.cli import main
from factory.defaults import DEFAULT_AGENT_CONFIG
from factory.memory_config import MEMORY_CONFIG_ENV
from factory_fixtures import held_lock

SPAWN, EXTERNAL = "spawn", "external"


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
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _memory_env(tmp_path, fake, mode, service=None):
    """记忆配置写在仓库之外(0600)。外部模式给 `url`、`token_file` 与本条用例独有的前缀;拉起模式用缺省前缀 `rise`。"""
    from starling_service import unique_prefix

    payload = {"root": str(tmp_path / "mem"), "extraction_base_url": fake.base_url, "extraction_model": "fake-extract",
               "embedding_base_url": fake.base_url, "embedding_model": "fake-embed", "embedding_dim": 256}
    env = {"PATH": os.environ["PATH"]}
    if mode == EXTERNAL:            # 端点跟服务的默认世界走,配置里不写
        payload = {"root": payload["root"], "url": service.url, "token_file": str(service.token_path),
                   "world_prefix": unique_prefix()}
    else:
        env["OPENAI_API_KEY"] = "offline-test-key"
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    env[MEMORY_CONFIG_ENV] = str(path)
    return env, payload.get("world_prefix", "rise")


@contextlib.contextmanager
def _deploy_store(repo_root, env):
    """经 `open_memory_service` 打开服务,在其上造部署管理面;退出时关会话(拉起模式下停服务)。"""
    from factory import starling_names
    from factory.memory_runtime import open_memory_service
    from simple_actio.memory_starling import StarlingDeploymentStore

    handle = open_memory_service(repo_root, env)
    try:
        yield StarlingDeploymentStore(handle.session, names=starling_names, prefix=handle.prefix,
                                      root_label=handle.root_label, settings=handle.settings,
                                      marker_dir=handle.config.root / "deploy", empty_seed_digest=handle.empty_seed[1]), \
            handle
    finally:
        handle.close()


@pytest.fixture
def seeded_repo(store, tmp_path, fake, request):
    """`seeded_repo(scenarios, built=None, in_flight=None, mode=EXTERNAL)`:写 `agent.yaml`,写记忆配置,在 `built`
    (缺省同 `scenarios`)的部署世界里各写一条事件;`in_flight` 给了就在那个情境留一份起点快照的在途记录。
    返回 `(store, env, 第一个世界的摘要)`;前缀在 `env["_prefix"]`(不是环境变量,`main` 只读 RISE_MEMORY_CONFIG)。"""
    def make(scenarios, built=None, in_flight=None, mode=EXTERNAL):
        svc = request.getfixturevalue("service") if mode == EXTERNAL else None
        write_config(store, AGENT_CONFIG, "agent", dict(DEFAULT_AGENT_CONFIG, scenario_ids=list(scenarios)),
                     "test: agent.yaml")
        env, prefix = _memory_env(tmp_path, fake, mode, svc)
        digests = []
        with _deploy_store(store.repo_root, env) as (deploy, _):
            for sid in (scenarios if built is None else built):
                deploy.remember_event(sid, "pa", "k#0@pa", f"pb：{sid} 的第一件事", interlocutor="pb")
                digests.append(deploy.digest(sid))
        if in_flight is not None:
            from mindagent.memory_layout import MemoryLayout, SnapshotRecord
            layout = MemoryLayout(tmp_path / "mem")
            layout.write_record(layout.snapshot("round-1", "ep-1"), SnapshotRecord(in_flight, "snap-x", "d" * 64))
        return store, {**env, "_prefix": prefix}, digests[0] if digests else None

    return make


def _git_state(store):
    def git(*a):
        return subprocess.run(["git", "-C", str(store.repo_root), *a], capture_output=True, text=True,
                              check=True).stdout
    return git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=all")


def _seed_rows(service, prefix):
    import httpx
    with httpx.Client(base_url=service.url, headers={"Authorization": f"Bearer {service.token}"}, trust_env=False,
                      timeout=30.0) as http:
        out = http.post("/api/v1/client/admin/list_worlds",
                        json={"labels": {"repo": prefix, "kind": "seed"}, "page": 1, "page_size": 50,
                              "with_digest": True})
        out.raise_for_status()
        return {w["name"]: w for w in out.json()["worlds"]}


@pytest.mark.parametrize("mode", [SPAWN, EXTERNAL])
def test_a_seed_is_forked_from_the_deploy_world_with_rise_labels(seeded_repo, capsys, mode, tmp_path):
    store, env, deploy_digest = seeded_repo(scenarios=["s1"], mode=mode)
    prefix = env["_prefix"]
    capsys.readouterr()
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line == f"{prefix}.seed.k.s1.v1 {deploy_digest}"
    if mode == SPAWN:                                                  # 用完停服务、放归属锁
        assert not (tmp_path / "mem" / "service.pid").exists()
        with hold_file_lock(tmp_path / "mem" / "service.owner.lock"):
            pass


def test_the_seed_world_carries_the_seed_labels_and_flags(seeded_repo, service, capsys, tmp_path):
    from factory import starling_names
    store, env, digest = seeded_repo(scenarios=["s1"])
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 0
    row = _seed_rows(service, env["_prefix"])[f"{env['_prefix']}.seed.k.s1.v1"]
    assert row["labels"] == {"repo": env["_prefix"], "root": starling_names.root_label(tmp_path / "mem"),
                             "kind": "seed", "seed_id": "k", "scenario_id": "s1", "version": "1"}
    assert row["seed"] is True and row["reclaimable"] is False and row["digest"] == digest


@pytest.mark.parametrize("mode", [SPAWN, EXTERNAL])
def test_a_second_seed_needs_new_version_and_then_becomes_v2(seeded_repo, capsys, mode):
    store, env, _ = seeded_repo(scenarios=["s1"], mode=mode)
    prefix = env["_prefix"]
    args = ["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"]
    assert main(args, env=env) == 0 and main(args, env=env) == 2
    err = capsys.readouterr().err
    assert f"{prefix}.seed.k.s1.v1" in err and "--new-version" in err
    assert main([*args, "--new-version"], env=env) == 0
    assert capsys.readouterr().out.split()[0] == f"{prefix}.seed.k.s1.v2"


def test_scenarios_are_handled_in_id_order_one_line_each(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1", "s2"])
    prefix = env["_prefix"]
    capsys.readouterr()
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s2", "--scenario", "s1"],
                env=env) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == [
        f"{prefix}.seed.k.s1.v1", f"{prefix}.seed.k.s2.v1"]


def test_a_snapshot_id_forks_that_snapshot_not_the_current_state(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"])
    with _deploy_store(store.repo_root, env) as (deploy, _):
        snap = deploy.snapshot("s1", note="before the second event")
        deploy.remember_event("s1", "pa", "k#1@pa", "pb：第二件事", interlocutor="pb")
        assert deploy.digest("s1") != snap.digest
    capsys.readouterr()
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1", "--snapshot", snap.snapshot_id],
                env=env) == 0
    assert capsys.readouterr().out.split()[1] == snap.digest


def test_a_world_with_an_in_flight_episode_is_refused(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"], in_flight="s1")      # 留一份 deploy-*.rec.json
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2
    assert "in-flight episode" in capsys.readouterr().err


def test_a_held_world_lock_is_refused(seeded_repo, tmp_path, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"])
    with hold_file_lock(tmp_path / "mem" / "deploy" / "s1.lock"):
        assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2
    assert "a deployment round or gate 2 is using world s1" in capsys.readouterr().err


@pytest.mark.parametrize("args, why", [
    (["--scenario", "s9"], "agent.yaml"),
    (["--scenario", "s1", "--scenario", "s2", "--snapshot", "snap-x"], "--snapshot"),
    (["--scenario", "s2"], "does not exist"),                            # 部署世界还没建
])
def test_bad_requests_exit_2(seeded_repo, capsys, args, why):
    store, env, _ = seeded_repo(scenarios=["s1", "s2"], built=["s1"])
    capsys.readouterr()
    assert main(["seed", str(store.repo_root), "--seed-id", "k", *args], env=env) == 2
    assert why in capsys.readouterr().err


def test_a_bad_seed_id_is_refused_before_the_service_is_touched(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"])
    assert main(["seed", str(store.repo_root), "--seed-id", "../k", "--scenario", "s1"], env=env) == 2
    assert "--seed-id" in capsys.readouterr().err


def test_the_stand_in_has_no_seeds(store, capsys):
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env={}) == 2
    assert "stand-in" in capsys.readouterr().err


def test_seed_takes_no_repo_lock_and_writes_nothing_to_the_tree(seeded_repo, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"])
    before = _git_state(store)
    with held_lock(store.repo_root):
        assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 0
    assert _git_state(store) == before


def test_a_spawned_service_in_use_by_another_rise_process_is_exit_2(seeded_repo, tmp_path, capsys):
    store, env, _ = seeded_repo(scenarios=["s1"], mode=SPAWN)
    with hold_file_lock(tmp_path / "mem" / "service.owner.lock"):
        assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2
    assert "another rise process is using the spawned memory service" in capsys.readouterr().err


def _timing_out_fork(monkeypatch, *, really_fork: bool):
    """`fork` 回 service_timeout;`really_fork` 为真时服务端先建好了世界。"""
    import factory.starling_session as ss
    from simple_actio.memory_port import MemoryFailureError

    real = ss.ServiceSession.admin

    def admin(self, op, body):
        if op != "fork":
            return real(self, op, body)
        if really_fork:
            real(self, op, body)
        exc = MemoryFailureError("starling service_timeout (ServiceTimeout)", transient=True)
        exc.code = "service_timeout"
        raise exc

    monkeypatch.setattr(ss.ServiceSession, "admin", admin)


def test_a_fork_that_timed_out_after_the_world_was_built_counts_as_success(seeded_repo, capsys, monkeypatch):
    store, env, digest = seeded_repo(scenarios=["s1"])
    _timing_out_fork(monkeypatch, really_fork=True)
    capsys.readouterr()
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 0
    assert capsys.readouterr().out.split() == [f"{env['_prefix']}.seed.k.s1.v1", digest]


def test_a_fork_that_timed_out_without_a_world_asks_for_a_rerun(seeded_repo, capsys, monkeypatch):
    store, env, _ = seeded_repo(scenarios=["s1"])
    _timing_out_fork(monkeypatch, really_fork=False)
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1"], env=env) == 2
    err = capsys.readouterr().err
    assert "timed out" in err and "run factory seed again" in err


def test_failure_text_carries_no_token_endpoint_or_memory_root(seeded_repo, service, tmp_path, fake, capsys):
    store, env, _ = seeded_repo(scenarios=["s1", "s2"], built=["s1"])
    assert main(["seed", str(store.repo_root), "--seed-id", "k", "--scenario", "s1", "--scenario", "s2"],
                env=env) == 2
    err = capsys.readouterr().err
    assert "already built" in err and f"{env['_prefix']}.seed.k.s1.v1" in err       # 前一个情境已建成,照实说
    for secret in (service.token, service.url, fake.base_url, str(tmp_path)):
        assert secret not in err
    assert json.dumps(service.token)[1:-1] not in err
