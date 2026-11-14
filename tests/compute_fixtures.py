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


def training_artifacts(out_dir: str, last_step: int, *, losses=None, stamp: str = "20261018-010530") -> dict[str, bytes]:
    """真实训练作业结束时留在输出目录里的文件(照 B1 在 .181 上的实测布局)。

    - completion.json:训练脚本自己报的存点位置与实际步数。存点在 <out_dir>/checkpoints/<时间戳>/
      checkpoint-<N>,时间戳是 ROLL 框架无条件追加的(base_config.py),调用方事先猜不到,只能读这份文件。
    - pipeline/worker_state_pipeline.json:存点里的 log_history,每步一条完整指标。键保留 `@sum` 后缀
      (strip_at_tag 只作用于 stdout 日志,不作用于这里)。
    """
    import json

    losses = list(losses) if losses is not None else [2.6 - 0.1 * i for i in range(last_step)]
    ckpt = f"{out_dir}/checkpoints/{stamp}/checkpoint-{last_step - 1}"
    history = [{"sft_train/loss@sum": loss, "system/step": i} for i, loss in enumerate(losses)]
    return {
        f"{out_dir}/completion.json": json.dumps(
            {"first_step": 0, "last_step": last_step - 1, "updates": last_step, "checkpoint": ckpt}
        ).encode("utf-8"),
        f"{ckpt}/pipeline/worker_state_pipeline.json": json.dumps(
            {"kv": {}, "log_history": history, "step": last_step - 1}
        ).encode("utf-8"),
        f"{ckpt}/mca_config.json": b"{}",
    }


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
        self.run_calls: list[tuple[str, JobSpec, bool]] = []  # (job_id, spec, lease)
        self.put_calls: list[str] = []
        self.preflight_calls = 0
        self.preflight_holders: list[str | None] = []
        self.lease_holder_reply: tuple[str | None, bool] = (None, False)
        self.rename_calls: list[tuple[str, str]] = []
        self.rm_tmp_calls: list[str] = []
        self.mem_available_gib = 32.0

    # --- 与 ComputeClient 对齐的方法 ---

    def preflight(self, *, own_holder: str | None = None) -> None:
        self.preflight_calls += 1
        self.preflight_holders.append(own_holder)
        if self.busy:
            raise ComputeBusyError("the GPU lease is held by job someone-else")

    def lease_holder(self) -> tuple[str | None, bool]:
        return self.lease_holder_reply

    def put(self, path: str, data: bytes, *, executable: bool = False) -> None:
        self.files[path] = data
        self.put_calls.append(path)

    def status(self, job_id: str) -> JobStatus:
        return self.jobs.get(job_id, JobStatus("missing", None, None))

    def hash_tree(self, path: str, mode: str, *, exclude: tuple[str, ...] = ()) -> str:
        assert mode in ("sampled", "full"), mode
        top = path.rstrip("/")
        # 与真实助手一致:按相对路径入哈希,所以同样内容的两个目录哈希相同(tmp 与改名后的正式目录)
        under = {k[len(top) + 1:]: v for k, v in self.files.items() if k.startswith(top + "/")}
        if path in self.files:
            under = {"": self.files[path]}
        if not under:
            raise HelperError("not_found", "no such directory")
        digest = hashlib.sha256()
        for key in sorted(k for k in under if k not in exclude):
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(under[key] if mode == "full" else str(len(under[key])).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40, lease: bool = True) -> JobResult:
        self.run_calls.append((job_id, spec, lease))
        if self.busy and lease:
            raise ComputeBusyError(f"the GPU lease is held by job other-{job_id}")
        if self.job_hook is not None:
            self.job_hook(job_id, spec)
        self.jobs[job_id] = JobStatus("exited", self.exit_code, None)
        return JobResult(self.exit_code, self.log)

    def get(self, path: str) -> bytes:
        if path not in self.files:
            raise HelperError("not_found", "no such file")
        return self.files[path]

    def rename(self, src: str, dst: str) -> None:
        under = {k: v for k, v in self.files.items()
                 if k == src or k.startswith(src.rstrip("/") + "/")}
        if not under:
            raise HelperError("not_found", "no such source path")
        if any(k == dst or k.startswith(dst.rstrip("/") + "/") for k in self.files):
            raise HelperError("exists", "the destination already exists")
        for key, value in under.items():
            self.files[dst + key[len(src):]] = value
            del self.files[key]
        self.rename_calls.append((src, dst))

    def rm_tmp(self, path: str) -> bool:
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if ".tmp-" not in name:
            raise HelperError("bad_path", "rm-tmp only removes '<name>.tmp-<token>'")
        under = [k for k in self.files
                 if k == path or k.startswith(path.rstrip("/") + "/")]
        for key in under:
            del self.files[key]
        self.rm_tmp_calls.append(path)
        return bool(under)

    def probe(self) -> dict:
        return {"mem_available_gib": self.mem_available_gib}

    # --- 测试辅助 ---

    def seed_dir(self, path: str, *, files: tuple[str, ...] = ("config.json",)) -> str:
        """造一个目录并返回它的 sampled 摘要。"""
        for name in files:
            self.files[f"{path.rstrip('/')}/{name}"] = b"x"
        return self.hash_tree(path, "sampled")
