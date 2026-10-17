"""远端助手、传输、引导、客户端,本机冒烟。

总耗时约 10s(助手在本机真起子进程、真等其退出)。不加自定义标记:仓库的 pytest 配置没有注册 markers,
全量命令带 -W error,未注册的标记会让收集阶段直接失败。
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from compute.client import NVIDIA_SMI, ComputeClient, JobSpec
from compute.endpoint import ENDPOINT_ID_PATTERN, LAUNCH_PREFIX_PROGRAMS, ComputeEndpoint, check_launch_prefix
from compute.errors import ComputeBusyError, ComputeError, HelperError, RemoteJobFailedError, RemoteJobTimeoutError
from compute.helper import HELPER_SHA256, helper_bytes
from compute.transport import RunResult, SshTransport

TOOL = Path(__file__).parent.parent / "src" / "compute" / "job_tool.py"
PYTHON = "python3"


# ===== 本机替身 =====

def local_runner(argv: list[str], *, input: bytes, timeout: float) -> RunResult:
    """把 ssh <别名> <cmd> 形态的命令行转为 /bin/sh -c '<cmd>' 真跑,绕过 ssh。"""
    assert argv[0] == "ssh" and len(argv) >= 3
    sep = argv.index("--")
    cmd = argv[sep + 2]  # SshTransport 已用 shlex.join 把远端命令拼成这一个字符串,不能再 join 一次
    proc = subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True,
                          timeout=timeout, check=False)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


# ===== endpoint =====

def test_endpoint_id_pattern_accepts_common_forms():
    for good in ("a", "gpu1", "dev.local", "prod-us-1", "h_2", "3090-test"):
        assert ENDPOINT_ID_PATTERN.match(good)
    for bad in ("-a", "x y", "ö", "a" * 65, ""):
        assert not ENDPOINT_ID_PATTERN.match(bad)


def test_launch_prefix_empty_or_docker_exec():
    assert check_launch_prefix([]) == ()
    assert check_launch_prefix(["docker", "exec", "cont"]) == ("docker", "exec", "cont")
    assert check_launch_prefix(["podman", "exec", "-it", "x"]) == ("podman", "exec", "-it", "x")
    with pytest.raises(ComputeError, match="empty or start with"):
        check_launch_prefix(["bash", "-c", "ls"])
    with pytest.raises(ComputeError, match=r"exec \.\.\. <container>"):
        check_launch_prefix(["docker", "run", "x"])
    with pytest.raises(ComputeError, match="non-empty strings"):
        check_launch_prefix(["", "exec"])  # type: ignore


def test_launch_prefix_program_whitelist_guards_against_arbitrary_injection():
    assert tuple(LAUNCH_PREFIX_PROGRAMS) == ("docker", "podman")


def test_endpoint_validates_work_root():
    base = {"endpoint_id": "a", "work_root": "/w", "roll_root": "/r", "launch_prefix": (),
            "base_model_path": "/m", "heldout_jsonl": "/h", "sft_template": "t",
            "min_free_host_mem_gib": 1.0, "min_free_disk_gib": 1.0, "poll_seconds": 1.0, "job_timeout_seconds": 60.0}
    ComputeEndpoint(**base)
    with pytest.raises(ComputeError, match="work_root"):
        ComputeEndpoint(**{**base, "work_root": "rel"})  # type: ignore
    with pytest.raises(ComputeError, match="work_root"):
        ComputeEndpoint(**{**base, "work_root": "/a/../etc"})  # type: ignore


# ===== transport =====

def test_ssh_transport_composes_batch_mode_command():
    tr = SshTransport("dev", connect_timeout_s=5.0)
    assert tr.ssh_argv(["ls", "-l"]) == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                         "--", "dev", "ls -l"]


def test_ssh_alias_cannot_start_with_dash_or_contain_quotes():
    SshTransport("valid_host.local")
    with pytest.raises(ComputeError, match="invalid form"):
        SshTransport("-evil")
    with pytest.raises(ComputeError, match="invalid form"):
        SshTransport("a b")


# ===== helper =====

def test_helper_sha256_matches_the_assistant_file():
    assert hashlib.sha256(TOOL.read_bytes()).hexdigest() == HELPER_SHA256


def test_helper_bytes_refuses_to_serve_a_tampered_file(tmp_path: Path):
    import compute.helper as mod
    real = mod._HELPER_FILE
    fake = tmp_path / "job_tool.py"
    fake.write_text("# evil")
    mod._HELPER_FILE = fake
    try:
        with pytest.raises(ComputeError, match="does not match the pinned sha256"):
            helper_bytes()
    finally:
        mod._HELPER_FILE = real


# ===== assistant 本机冒烟 =====

def test_assistant_start_runs_and_injects_secret_then_removes_it(tmp_path: Path):
    root = tmp_path / "work"
    spec = JobSpec(argv=(PYTHON, "-c", "import os;print('K=',os.environ['K']);print('done',flush=True)"),
                   env={"E": "v"}, secret_env="K")
    call = _make_caller(root)
    status = call("start", job_id="j1", spec=spec.to_wire(), secret="s3cr3t")
    assert status["state"] in ("running", "exited")  # 作业很短,start 返回时可能已经结束
    _wait(lambda: call("status", job_id="j1")["state"] != "running", 5.0)
    tail = call("tail", job_id="j1", lines=50)["text"]
    assert "K= s3cr3t" in tail and "done" in tail
    assert not (root / "jobs" / "j1" / "secret").exists()


def test_assistant_start_is_idempotent_with_the_same_spec(tmp_path: Path):
    root = tmp_path / "work"
    spec = {"argv": ["sleep", "5"], "secret_env": "K"}  # 带密钥必须声明 secret_env;sleep 保证第二次调用时还在跑
    call = _make_caller(root)
    call("start", job_id="j2", spec=spec, secret="first")
    status2 = call("start", job_id="j2", spec=spec, secret="rotated")  # 密钥不进规格哈希,不算冲突
    assert status2["state"] == "running"
    call("stop", job_id="j2", grace_s=5.0)


def test_assistant_start_refuses_a_different_spec_for_the_same_job_id(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="j3", spec={"argv": ["sleep", "1"]})
    err = call.raw("start", job_id="j3", spec={"argv": ["true"]})
    assert not err["ok"] and err["error"] == "spec_conflict"


def test_assistant_stop_terminates_then_reports_state(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="j4", spec={"argv": ["sleep", "600"]})
    assert call("status", job_id="j4")["state"] == "running"
    t = time.monotonic()
    result = call("stop", job_id="j4", grace_s=5.0)
    assert time.monotonic() - t < 6.0 and result["state"] == "exited" and result["signaled"]


def test_assistant_put_writes_executable_and_verifies_hash(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    data = b"#!/bin/sh\necho hi\n"
    call("put", path=str(root / "bin" / "x.sh"), content_b64=base64.b64encode(data).decode(),
         sha256=hashlib.sha256(data).hexdigest(), executable=True)
    written = root / "bin" / "x.sh"
    assert written.read_bytes() == data and (written.stat().st_mode & 0o111)
    err = call.raw("put", path=str(root / "bad"), content_b64=base64.b64encode(data).decode(), sha256="0" * 64)
    assert not err["ok"] and err["error"] == "hash_mismatch"


def test_assistant_put_forbids_path_outside_work_root(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    err = call.raw("put", path="/tmp/evil", content_b64="", sha256="e" * 64)
    assert not err["ok"] and err["error"] == "bad_path"


def test_assistant_hash_tree_full_and_sampled_are_identical_for_small_trees(tmp_path: Path):
    root = tmp_path / "work"
    d = root / "ckpt"
    d.mkdir(parents=True)
    (d / "a.json").write_text("{}")
    (d / "sub").mkdir()
    (d / "sub" / "w.pt").write_bytes(os.urandom(1000))
    call = _make_caller(root)
    full = call("hash-tree", path=str(d), mode="full")
    sampled = call("hash-tree", path=str(d), mode="sampled")
    assert full["sha256"] == sampled["sha256"] and full["files"] == 2


def test_assistant_hash_tree_changes_when_content_changes(tmp_path: Path):
    root = tmp_path / "work"
    d = root / "ckpt"
    d.mkdir(parents=True)
    (d / "a.json").write_text("{}")
    call = _make_caller(root)
    h1 = call("hash-tree", path=str(d), mode="full")["sha256"]
    (d / "a.json").write_text("{ }")
    h2 = call("hash-tree", path=str(d), mode="full")["sha256"]
    assert h1 != h2


def test_assistant_lease_acquire_release_peek(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    assert call("lease-peek")["holder"] is None
    assert call("lease-acquire", holder="A")["acquired"]
    assert call("lease-acquire", holder="A")["acquired"]  # 持有者再申请幂等
    assert not call("lease-acquire", holder="B")["acquired"]  # 新鲜,被占
    assert not call("lease-acquire", holder="B", grace_s=3600.0)["acquired"]  # 作业目录不存在,但仍在宽限期内
    time.sleep(0.1)
    acq = call("lease-acquire", holder="B", grace_s=0.05)  # 超宽限期,陈旧回收
    assert acq["acquired"] and acq["reclaimed"]
    assert call("lease-peek")["holder"] == "B"
    assert call("lease-release", holder="A")["holder"] == "B"  # 非持有者释放失败
    assert call("lease-release", holder="B")["released"]
    assert not list((root / "jobs").glob("*.stale-*"))  # 没有遗留的私有文件


def test_assistant_lease_holder_with_running_job_blocks_others(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="run1", spec={"argv": ["sleep", "30"]})
    assert call("lease-acquire", holder="run1")["acquired"]
    assert not call("lease-acquire", holder="other", grace_s=0.0)["acquired"]
    call("stop", job_id="run1", grace_s=5.0)
    _wait(lambda: call("status", job_id="run1")["state"] != "running", 7.0)
    assert call("lease-acquire", holder="other")["acquired"]


def test_assistant_probe_reports_memory_and_disk(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    p = call("probe")
    assert isinstance(p.get("mem_available_gib"), (int, float, type(None)))
    assert isinstance(p["disk_free_gib"], (int, float)) and p["disk_free_gib"] > 0


def test_assistant_unknown_command_returns_error(tmp_path: Path):
    err = _make_caller(tmp_path).raw("rm", {})
    assert not err["ok"] and err["error"] == "unknown_command"


def _make_caller(root: Path):
    root.mkdir(parents=True, exist_ok=True)

    class _Caller:
        def raw(self, cmd: str, req: dict | None = None, **fields: object) -> dict:
            """不断言 ok:预期失败的用例用它读错误应答。"""
            req = {**(req or {}), **fields}
            import json, subprocess
            req["work_root"] = str(root)
            out = subprocess.run(["/bin/sh", "-c", f"{PYTHON} {TOOL} {cmd}"], input=json.dumps(req),
                                 capture_output=True, text=True, timeout=60, check=False)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        def __call__(self, cmd: str, **fields: object) -> dict:
            reply = self.raw(cmd, **fields)
            assert reply["ok"], reply
            return reply

    return _Caller()


def _wait(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred() or time.monotonic() >= deadline:
            return pred()
        time.sleep(step)


# ===== client =====

def _endpoint(root: Path, **extra: object) -> ComputeEndpoint:
    base = {"endpoint_id": "test", "work_root": str(root / "w"), "roll_root": str(root / "r"),
            "launch_prefix": (), "base_model_path": str(root / "m"), "heldout_jsonl": str(root / "h.jsonl"),
            "sft_template": "t", "min_free_host_mem_gib": 0.5, "min_free_disk_gib": 1.0,
            "poll_seconds": 0.3, "job_timeout_seconds": 10.0}
    return ComputeEndpoint(**{**base, **extra})


def test_client_uploads_the_helper_once(tmp_path: Path):
    ep = _endpoint(tmp_path)
    tr = SshTransport("local", runner=local_runner)
    cl = ComputeClient(ep, tr)
    assert not cl._ready
    cl.ensure_helper()
    assert cl._ready
    helper_path = Path(ep.work_root) / "bin" / f"job_tool-{HELPER_SHA256[:8]}.py"
    assert helper_path.read_bytes() == helper_bytes()
    helper_path.write_text("# corrupted")
    cl.ensure_helper()  # 已标记 ready,不再上传
    assert helper_path.read_text() == "# corrupted"


def test_client_retries_helper_upload_when_remote_file_is_missing(tmp_path: Path):
    ep = _endpoint(tmp_path)
    tr = SshTransport("local", runner=local_runner)
    cl = ComputeClient(ep, tr)
    cl.ensure_helper()
    helper_path = Path(ep.work_root) / "bin" / f"job_tool-{HELPER_SHA256[:8]}.py"
    helper_path.unlink()
    cl._call("probe")  # 助手不在:_call 重新上传
    assert helper_path.exists()


def test_client_run_job_success_acquires_lease_and_releases_after(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    spec = JobSpec(argv=(PYTHON, "-c", "print('ok',flush=True)"))
    result = cl.run_job("job1", spec)
    assert result.exit_code == 0 and "ok" in result.log_tail
    assert cl.lease_holder()[0] is None


def test_client_run_job_nonzero_exit_releases_lease_and_raises(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    with pytest.raises(RemoteJobFailedError, match="exited with code 2") as exc:
        cl.run_job("job2", JobSpec(argv=(PYTHON, "-c", "import sys;print('boom');sys.exit(2)")))
    assert exc.value.exit_code == 2 and "boom" in exc.value.log_tail
    assert cl.lease_holder()[0] is None


def test_client_run_job_timeout_stops_the_job_and_releases_lease(tmp_path: Path):
    ep = _endpoint(tmp_path, job_timeout_seconds=2.0, poll_seconds=0.3)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    t = time.monotonic()
    with pytest.raises(RemoteJobTimeoutError, match="exceeded 2") as exc:
        cl.run_job("job3", JobSpec(argv=("sleep", "600")))
    assert 2.0 < time.monotonic() - t < 4.0
    assert cl.lease_holder()[0] is None


def test_client_run_job_lease_held_raises_busy(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    assert cl.lease_acquire("other")
    with pytest.raises(ComputeBusyError, match="held by job other"):
        cl.run_job("job4", JobSpec(argv=("true",)))
    cl.lease_release("other")


def test_client_run_job_is_idempotent_after_restart(tmp_path: Path):
    ep = _endpoint(tmp_path, job_timeout_seconds=30.0)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    spec = JobSpec(argv=("sleep", "2"))
    cl.start("job5", spec)  # 第一次启动
    assert cl.status("job5").state == "running"
    cl2 = ComputeClient(ep, SshTransport("local", runner=local_runner))  # 守护进程重启(新客户端)
    result = cl2.run_job("job5", spec)  # 同规格再调:接上已有作业
    assert result.exit_code == 0


def test_client_preflight_gpu_count_own_holder_skips_process_check(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    cl.lease_acquire("me")
    cl.start("me", JobSpec(argv=("sleep", "2")))
    import unittest.mock as mock
    real_call = cl._call

    def fake_call(command: str, *args: object, **fields: object) -> dict:
        if command == "probe":  # 开发机不一定是 Linux,没有 /proc/meminfo;资源数字在这里固定
            return {"ok": True, "mem_available_gib": 64.0, "disk_free_gib": 500.0}
        return real_call(command, *args, **fields)

    with mock.patch.object(cl, "_call", side_effect=fake_call), \
            mock.patch.object(cl, "gpu_process_count", return_value=1):
        cl.preflight(own_holder="me")  # 不抛异常:租约是自己的,GPU 上的进程就是本作业
        with pytest.raises(ComputeBusyError, match="held by job me"):
            cl.preflight()  # 没声明自己是持有者:先被租约拦下
        cl.stop("me")
        cl.lease_release("me")
        with pytest.raises(ComputeBusyError, match="1 compute process"):
            cl.preflight()  # 租约空了但 GPU 上还有进程:被进程数拦下(Ruling 8 只豁免自己的作业)


def test_client_preflight_lease_held_by_another_raises_busy(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    cl.lease_acquire("other")
    with pytest.raises(ComputeBusyError, match="held by job other"):
        cl.preflight()
    cl.lease_release("other")


def test_nvidia_smi_command_has_no_injection_vector():
    """nvidia-smi 命令是硬编码元组(没有任何占位符),所以输入数据无法注入它。"""
    assert NVIDIA_SMI == ("nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader")


