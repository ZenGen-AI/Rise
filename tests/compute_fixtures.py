"""compute 相关测试的共用替身。

FakeClient 覆盖 RollExecutor 用到的 ComputeClient 方法,签名逐一对齐
`src/compute/client.py`:run_job、status、put、hash_tree、preflight。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable

from compute.client import JobResult, JobSpec, JobStatus
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeBusyError, HelperError

ENDPOINT_FIELDS = {
    "endpoint_id": "gpu-1",
    "work_root": "/srv/rise/work",
    "roll_root": "/srv/roll",
    "launch_prefix": (),
    "base_model_path": "/srv/models/qwen-4b",
    "heldout_jsonl": "/srv/roll/data/heldout.jsonl",
    "sft_template": "/srv/roll/configs/sft_lora.yaml",
    "min_free_host_mem_gib": 12.0,
    "min_free_disk_gib": 50.0,
    "poll_seconds": 0.01,
    "job_timeout_seconds": 60.0,
}


def fake_endpoint(**overrides) -> ComputeEndpoint:
    return ComputeEndpoint(**{**ENDPOINT_FIELDS, **overrides})


def metrics_log(first_step: int, last_step: int, *, loss: float = 0.5) -> str:
    """造一段形如 ROLL 逐步指标的日志(前缀见 roll_executor.METRIC_PREFIX)。"""
    return "".join(
        'metrics_tag: {"step": %d, "metrics": {"sft_train/loss": %r}}\n' % (step, loss - 0.01 * step)
        for step in range(first_step, last_step)
    )


class FakeClient:
    """内存替身。files 是 {路径: 字节},jobs 是 {job_id: JobStatus}。

    busy=True 时 preflight 抛 ComputeBusyError(模拟别人占着 GPU)。
    job_hook(spec) 在 run_job 里调用,用来模拟作业的副作用(写出存点目录)。
    """

    def __init__(
        self,
        *,
        busy: bool = False,
        log: str = "",
        exit_code: int = 0,
        job_hook: Callable[[str, JobSpec], None] | None = None,
    ) -> None:
        self.busy = busy
        self.log = log
        self.exit_code = exit_code
        self.job_hook = job_hook
        self.files: dict[str, bytes] = {}
        self.jobs: dict[str, JobStatus] = {}
        self.run_calls: list[tuple[str, JobSpec]] = []
        self.put_calls: list[str] = []
        self.preflight_calls = 0

    # --- 与 ComputeClient 对齐的方法 ---

    def preflight(self, *, own_holder: str | None = None) -> None:
        self.preflight_calls += 1
        if self.busy:
            raise ComputeBusyError("the GPU lease is held by job someone-else")

    def put(self, path: str, data: bytes, *, executable: bool = False) -> None:
        self.files[path] = data
        self.put_calls.append(path)

    def status(self, job_id: str) -> JobStatus:
        return self.jobs.get(job_id, JobStatus("missing", None, None))

    def hash_tree(self, path: str, mode: str) -> str:
        assert mode in ("sampled", "full"), mode
        under = {k: v for k, v in self.files.items() if k == path or k.startswith(path.rstrip("/") + "/")}
        if not under:
            raise HelperError("not_found", "no such directory")
        digest = hashlib.sha256()
        for key in sorted(under):
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(under[key] if mode == "full" else str(len(under[key])).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40) -> JobResult:
        self.run_calls.append((job_id, spec))
        if self.busy:
            raise ComputeBusyError(f"the GPU lease is held by job other-{job_id}")
        if self.job_hook is not None:
            self.job_hook(job_id, spec)
        self.jobs[job_id] = JobStatus("exited", self.exit_code, None)
        return JobResult(self.exit_code, self.log)

    # --- 测试辅助 ---

    def seed_dir(self, path: str, *, files: tuple[str, ...] = ("config.json",)) -> str:
        """造一个目录并返回它的 sampled 摘要。"""
        for name in files:
            self.files[f"{path.rstrip('/')}/{name}"] = b"x"
        return self.hash_tree(path, "sampled")
