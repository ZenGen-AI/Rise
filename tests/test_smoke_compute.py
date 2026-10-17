"""scripts/smoke_compute.py:上真机前的冒烟脚本自身要靠谱。

脚本用 --local 把 ssh 换成本机 sh,所以这里不连任何远端。验证的是:七步按序跑、第一个 FAIL 就停、
退出码对、失败信息说人话。真机上的行为(预检、助手上传、真实训练脚本在不在)只能到真机上看。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import compute_endpoints_file

SCRIPT = Path(__file__).parent.parent / "scripts" / "smoke_compute.py"
LINUX = Path("/proc/meminfo").exists()


@pytest.fixture
def world(tmp_path):
    """一个假的"远端":本机上的一棵目录树,加指向它的端点配置与主机映射。"""
    for sub in ("model", "roll/scripts", "work"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "model" / "config.json").write_text("{}")
    for rel in ("roll/run_sft_validation.py", "roll/scripts/export_lora_for_vllm.py", "sft.yaml", "heldout.jsonl"):
        (tmp_path / rel).write_text("")
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    item = {
        "endpoint_id": "local-1", "work_root": str(tmp_path / "work"), "roll_root": str(tmp_path / "roll"),
        "launch_prefix": [], "base_model_path": str(tmp_path / "model"),
        "heldout_jsonl": str(tmp_path / "heldout.jsonl"), "sft_template": str(tmp_path / "sft.yaml"),
        "min_free_host_mem_gib": 0.1, "min_free_disk_gib": 0.1, "poll_seconds": 0.3, "job_timeout_seconds": 60.0,
    }
    store.write_product(compute_endpoints_file(), Envelope(
        id="cfg-compute-endpoints", type="config", version=1, created_at="2026-10-17T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload={"endpoints": [item]}), "endpoints")
    hosts = tmp_path / "hosts.yaml"
    hosts.write_text("local-1: whatever\n")
    return tmp_path, str(tmp_path / "cp"), {**os.environ, "RISMFORGE_COMPUTE_HOSTS": str(hosts)}


def _smoke(repo, env, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), repo, "--local", *extra],
                          capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"})


def _steps(out):
    """['[1] OK   配置…', …] -> [(1, 'OK'), …]"""
    return [(int(line[1]), line[4:8].strip()) for line in out.splitlines() if line.startswith("[")]


def test_all_seven_steps_pass_against_a_complete_world(world):
    _, repo, env = world
    r = _smoke(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _steps(r.stdout) == [(n, "OK") for n in range(1, 8)]
    assert "全部通过" in r.stdout


def test_the_script_never_asks_for_the_gpu_lease(world, monkeypatch):
    """冒烟不得占 GPU 租约:否则一个空作业就会挡住别人的训练。

    不能事后查 gpu.lease 在不在——run_job 结束时会释放租约,取了和没取事后看起来一样。
    所以在客户端一侧记下 lease_acquire 被调用过没有。"""
    import importlib.util

    from compute.client import ComputeClient

    acquired = []
    original = ComputeClient.lease_acquire

    def spy(self, holder, **kw):
        acquired.append(holder)
        return original(self, holder, **kw)

    monkeypatch.setattr(ComputeClient, "lease_acquire", spy)
    tmp, repo, env = world
    spec = importlib.util.spec_from_file_location("smoke_compute_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("RISMFORGE_COMPUTE_HOSTS", env["RISMFORGE_COMPUTE_HOSTS"])
    assert module.run(repo, None, local=True) == 0
    assert acquired == []  # 一次都没取


@pytest.mark.parametrize("break_what,step", [
    ("model", 6),
    ("export", 7),
])
def test_the_first_failure_stops_the_run_and_exits_1(world, break_what, step):
    tmp, repo, env = world
    if break_what == "model":
        (tmp / "model" / "config.json").unlink()
        (tmp / "model").rmdir()
    else:
        (tmp / "roll" / "scripts" / "export_lora_for_vllm.py").unlink()
    r = _smoke(repo, env)
    assert r.returncode == 1
    done = _steps(r.stdout)
    assert done[-1] == (step, "FAIL")
    assert len(done) == step  # 失败之后一步都没再跑
    assert f"在第 {step} 步" in r.stderr


def test_a_host_map_without_the_endpoint_fails_at_step_one(world):
    tmp, repo, env = world
    (tmp / "hosts.yaml").write_text("other: x\n")
    r = _smoke(repo, env)
    assert r.returncode == 1
    assert _steps(r.stdout) == [(1, "FAIL")]
    assert "has no entry for endpoint 'local-1'" in r.stdout


def test_a_missing_hosts_variable_fails_at_step_one_naming_the_variable(world):
    _, repo, env = world
    env = {k: v for k, v in env.items() if k != "RISMFORGE_COMPUTE_HOSTS"}
    r = _smoke(repo, env)
    assert r.returncode == 1 and "RISMFORGE_COMPUTE_HOSTS is not set" in r.stdout


def test_an_unknown_endpoint_name_lists_what_exists(world):
    _, repo, env = world
    r = _smoke(repo, env, "--endpoint", "nope")
    assert r.returncode == 1 and "no endpoint 'nope'" in r.stdout and "local-1" in r.stdout


@pytest.mark.skipif(LINUX, reason="只在没有 /proc/meminfo 的开发机上才走跳过分支")
def test_the_preflight_step_says_when_it_is_skipped_rather_than_pretending(world):
    _, repo, env = world
    r = _smoke(repo, env)
    line = next(line for line in r.stdout.splitlines() if line.startswith("[4]"))
    assert "跳过" in line and "/proc/meminfo" in line


def test_the_script_deletes_nothing_it_did_not_create(world):
    """冒烟会在 work_root 留下 bin/ 与 jobs/smoke-*/,这是有意的:不清理,留给人。"""
    tmp, repo, env = world
    sentinel = tmp / "work" / "precious.txt"
    sentinel.write_text("do not delete")
    assert _smoke(repo, env).returncode == 0
    assert sentinel.read_text() == "do not delete"
    assert list((tmp / "work" / "jobs").glob("smoke-*"))
