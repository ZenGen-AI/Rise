"""ROLL 部署与容器:宿主机命令在本机真跑(sh 替 ssh),docker 换成一个记录参数、模拟状态的脚本。

覆盖:首次部署、二次跳过、内容被改后重装、改名撞上别人先装好的目录、挡路的坏目录、
依赖指纹不符、镜像不在、已存在的容器(在跑 / 停着 / 规格不符)、导入自检失败、围栏。
"""
from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

import compute.job_tool as job_tool
from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError
from compute.roll_deploy import DEPLOY_MARKER, SPEC_LABEL, ensure_roll_env, tree_digest
from compute.roll_env import Dependency, RollEnv
from compute.transport import SshTransport

pytestmark = pytest.mark.slow  # 真起子进程

COMMIT = "8b94504ef476373a05ce6b7196134b2d78cb0c42"
IMAGE = "sha256:" + "1" * 64

# 假 docker:状态放在 $FAKE_DOCKER_STATE(JSON),每次调用的 argv 追加进 $FAKE_DOCKER_LOG。
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
state_path, log = os.environ["FAKE_DOCKER_STATE"], os.environ["FAKE_DOCKER_LOG"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
def save():
    json.dump(state, open(state_path, "w"))
if args[:2] == ["image", "inspect"]:
    if args[-1] in state["images"]:
        print(args[-1]); sys.exit(0)
    sys.exit(1)
if args[0] == "inspect":
    c = state["containers"].get(args[-1])
    if c is None:
        sys.exit(1)
    print(("true" if c["running"] else "false") + " " + c["label"]); sys.exit(0)
if args[0] == "start":
    state["containers"][args[1]]["running"] = True; save(); sys.exit(0)
if args[0] == "run":
    name = args[args.index("--name") + 1]
    label = args[args.index("--label") + 1].split("=", 1)[1]
    state["containers"][name] = {"running": True, "label": label}; save(); print("cid"); sys.exit(0)
if args[0] == "exec":
    print(state.get("self_check", "ok")); sys.exit(0)
sys.exit(2)
'''


def _archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ROLL_FILES = {"roll/__init__.py": b"x", "mcore_adapter/src/mcore_adapter/__init__.py": b"y",
              "scripts/qwen38/run_sft_validation.py": b"z", "scripts/qwen38/configs/sft_lora.yaml": b"c"}


def _local_runner(argv, *, input, timeout):
    import subprocess

    from compute.transport import RunResult
    cmd = argv[argv.index("--") + 2]
    proc = subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True, timeout=timeout, check=False)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "docker.json"
    state.write_text(json.dumps({"images": [IMAGE], "containers": {}}))
    log = tmp_path / "docker.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))

    deps = tmp_path / "deps"
    deps_fp = []
    for name, mount in (("Megatron-LM", "/r2n/megatron"), ("fla", "/r2n/fla")):
        (deps / name / "pkg").mkdir(parents=True)
        (deps / name / "pkg" / "m.py").write_text(name)
        (deps / name / "pkg" / "__pycache__").mkdir()
        (deps / name / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\0")   # 字节码不进指纹
        fp = job_tool.cmd_hash_tree({"path": str(deps / name), "mode": "full", "skip_pycache": True})["sha256"]
        deps_fp.append(Dependency(name, mount, fp))
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "model").mkdir()
    endpoint = ComputeEndpoint(
        endpoint_id="local-1", work_root=str(work), roll_root="/unused", launch_prefix=(),
        base_model_path=str(tmp_path / "model"), heldout_jsonl="/unused/h.jsonl", sft_template="/unused/t.yaml",
        min_free_host_mem_gib=0.0, min_free_disk_gib=0.0, poll_seconds=0.1, job_timeout_seconds=10.0,
        host_deps_root=str(deps))
    archive = _archive(ROLL_FILES)
    env = RollEnv(roll_commit=COMMIT, roll_tree=tree_digest(archive), image_id=IMAGE, dependencies=tuple(deps_fp))
    client = ComputeClient(endpoint, SshTransport("local", runner=_local_runner))

    class World:
        pass

    w = World()
    w.client, w.endpoint, w.env, w.archive, w.work, w.deps = client, endpoint, env, archive, work, deps
    w.state, w.log = state, log
    w.docker_calls = lambda: [json.loads(line) for line in log.read_text().splitlines()]
    w.set_state = lambda **kw: state.write_text(json.dumps({**json.loads(state.read_text()), **kw}))
    return w


def _roll_dir(w) -> Path:
    return w.work / "deps" / f"roll-{COMMIT[:12]}"


def test_the_digest_of_an_archive_equals_the_helpers_hash_of_its_extraction(tmp_path):
    """跳过重复部署靠这条等式:本机从归档算的指纹 == 远端对解开目录算的指纹。"""
    files = {**ROLL_FILES, "roll/__pycache__/x.pyc": b"\0", "a/b/c.txt": b"deep"}
    archive = _archive(files)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path / "out", filter="data")
    helper = job_tool.cmd_hash_tree({"path": str(tmp_path / "out"), "mode": "full", "skip_pycache": True})
    assert tree_digest(archive) == helper["sha256"]


def test_first_use_installs_roll_starts_a_container_and_derives_the_endpoint(world):
    runtime = ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    roll_dir = _roll_dir(world)
    assert (roll_dir / "scripts/qwen38/run_sft_validation.py").read_bytes() == b"z"
    assert (roll_dir / DEPLOY_MARKER).read_text() == COMMIT
    assert not list(roll_dir.parent.glob("*.tmp-*"))
    ep = runtime.endpoint
    assert ep.roll_root == str(roll_dir)
    assert ep.sft_template == f"{roll_dir}/scripts/qwen38/configs/sft_lora.yaml"
    assert ep.launch_prefix == ("docker", "exec", "-i", runtime.container)
    assert runtime.container.startswith("forge-roll-")
    run = next(c for c in world.docker_calls() if c[0] == "run")
    joined = " ".join(run)
    assert f"{world.work}:{world.work}" in joined and f"{roll_dir}:{roll_dir}:ro" in joined
    assert f"{world.deps}/Megatron-LM:/r2n/megatron:ro" in joined and f"{world.deps}/fla:/r2n/fla:ro" in joined
    assert f"PYTHONPATH=/r2n/fla:{roll_dir}/mcore_adapter/src:{roll_dir}:/r2n/megatron" in run
    assert "NCCL_NVLS_ENABLE=0" in run and run[-2:] == [IMAGE, "infinity"]


def test_a_second_use_skips_the_install_and_reuses_the_running_container(world):
    first = ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    marker = (_roll_dir(world) / DEPLOY_MARKER).stat().st_mtime_ns
    second = ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert second.container == first.container
    assert (_roll_dir(world) / DEPLOY_MARKER).stat().st_mtime_ns == marker
    assert [c[0] for c in world.docker_calls()].count("run") == 1
    assert "start" not in [c[0] for c in world.docker_calls()]


def test_a_stopped_container_is_started_not_rebuilt(world):
    runtime = ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    state = json.loads(world.state.read_text())
    state["containers"][runtime.container]["running"] = False
    world.state.write_text(json.dumps(state))
    ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    calls = [c[0] for c in world.docker_calls()]
    assert calls.count("run") == 1 and calls.count("start") == 1


def test_a_container_of_the_same_name_from_another_spec_is_refused_and_left_alone(world):
    runtime = ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    state = json.loads(world.state.read_text())
    state["containers"][runtime.container]["label"] = "something-else"
    world.state.write_text(json.dumps(state))
    with pytest.raises(ComputeError, match="not built from this spec"):
        ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert not any(c[0] in ("rm", "stop", "kill") for c in world.docker_calls())


def test_a_tampered_deployment_is_reinstalled_only_if_it_is_not_in_the_way(world):
    """内容被改过:标记还在但指纹不符。原子改名不覆盖已有目录,所以装不上,抛错要人清,而不是悄悄用坏的。"""
    ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    (_roll_dir(world) / "roll" / "__init__.py").write_bytes(b"edited on the host")
    with pytest.raises(ComputeError, match="a human should remove it"):
        ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert not list(_roll_dir(world).parent.glob("*.tmp-*"))


def test_a_deployment_finished_by_someone_else_first_is_reused(world, monkeypatch):
    """两个进程同时装:后到的改名失败,但先到的那份正是钉住的版本,就用它,不报错。"""
    import compute.roll_deploy as deploy

    real_install = deploy._install

    def race(client, roll_dir, env, archive):
        real_install(client, roll_dir, env, archive)            # "别人"先装好
        real_install(client, roll_dir, env, archive)            # 我们的改名撞上它

    monkeypatch.setattr(deploy, "_install", race)
    ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert (_roll_dir(world) / DEPLOY_MARKER).read_text() == COMMIT
    assert not list(_roll_dir(world).parent.glob("*.tmp-*"))


def test_bytecode_written_into_the_deployment_does_not_force_a_reinstall(world):
    ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    (_roll_dir(world) / "roll" / "__pycache__").mkdir()
    (_roll_dir(world) / "roll" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"\0")
    ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert [c[0] for c in world.docker_calls()].count("run") == 1


def test_a_dependency_copy_that_does_not_match_its_fingerprint_is_refused(world):
    (world.deps / "fla" / "pkg" / "m.py").write_text("patched by hand")
    with pytest.raises(ComputeError, match="does not match the pinned fla source"):
        ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert not _roll_dir(world).exists()       # 依赖不对就不往下走


def test_a_missing_image_is_refused_before_anything_is_written(world):
    world.set_state(images=[])
    with pytest.raises(ComputeError, match="is not on the host"):
        ensure_roll_env(world.client, world.endpoint, world.env, world.archive)
    assert not _roll_dir(world).exists()


def test_a_container_importing_the_wrong_sources_is_refused(world):
    world.set_state(self_check="wrong:roll")
    with pytest.raises(ComputeError, match="imports the wrong sources"):
        ensure_roll_env(world.client, world.endpoint, world.env, world.archive)


def test_an_endpoint_without_host_deps_root_is_refused(world):
    import dataclasses
    with pytest.raises(ComputeError, match="host_deps_root"):
        ensure_roll_env(world.client, dataclasses.replace(world.endpoint, host_deps_root=None),
                        world.env, world.archive)


def test_an_archive_that_is_not_the_one_the_environment_was_built_for_is_refused(world):
    with pytest.raises(ComputeError, match="does not match the environment"):
        ensure_roll_env(world.client, world.endpoint, world.env, _archive({"roll/__init__.py": b"other"}))
    assert world.docker_calls() == []


def test_a_bad_commit_id_cannot_reach_a_remote_path():
    with pytest.raises(ValueError, match="40-character"):
        RollEnv(roll_commit="../../etc", roll_tree="0" * 64)


def test_the_environment_digest_changes_with_the_roll_commit_or_a_dependency():
    base = RollEnv(roll_commit=COMMIT, roll_tree="0" * 64)
    assert RollEnv(roll_commit="0" * 40, roll_tree="0" * 64).digest != base.digest
    other = (Dependency("Megatron-LM", "/r2n/megatron", "f" * 64), base.dependencies[1])
    assert RollEnv(roll_commit=COMMIT, roll_tree="0" * 64, dependencies=other).digest != base.digest
    assert base.record()["roll_env"] == base.digest and base.record()["roll_commit"] == COMMIT


def test_the_spec_label_is_what_the_container_is_matched_on():
    assert SPEC_LABEL == "rise.roll-spec"
