"""ComputeClient:经传输调用远端助手,翻译应答,提供 run_job 与 preflight(真实后端设计 §3.2、§3.3)。

不认识训练与评测。run_job 的重入语义:租约持有者就是 job_id,start 对同规格幂等,所以守护进程重启后
再调同一个 run_job 会接上正在跑的作业。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from compute.endpoint import ComputeEndpoint
from compute.errors import (ComputeBusyError, HelperError, RemoteJobFailedError,
                            RemoteJobTimeoutError, TransportError)
from compute.helper import BOOTSTRAP, helper_bytes, remote_helper_path
from compute.transport import RunResult, Transport

_STDERR_TAIL = 300
_TRANSIENT_CODES = ("job_starting", "internal")  # 助手返回这些时重试有意义,按 TransportError 走延后通道
NVIDIA_SMI = ("nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader")
NVIDIA_SMI_MEMORY = ("nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits")


@dataclass(frozen=True)
class JobSpec:
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: str | None = None  # 密钥经 stdin 传给 start,作业里以这个变量名读到;不进 spec 哈希

    def to_wire(self) -> dict:
        return {"argv": list(self.argv), "cwd": self.cwd, "env": dict(self.env), "secret_env": self.secret_env}


@dataclass(frozen=True)
class JobStatus:
    state: str  # missing | running | exited | lost
    exit_code: int | None
    spec_sha256: str | None


@dataclass(frozen=True)
class JobResult:
    exit_code: int
    log_tail: str


def _tail_of(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()[-_STDERR_TAIL:]


class ComputeClient:
    def __init__(self, endpoint: ComputeEndpoint, transport: Transport, *, python: str = "python3",
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.endpoint = endpoint
        self._transport = transport
        # 推理服务的隧道要用同一个 ssh 别名;别名只在传输层与本机映射里,不进 git 树。
        self.alias = getattr(transport, "alias", None)
        self._python = python
        self._sleep = sleep
        self._clock = clock
        self._ready = False

    # ---------- 助手调用 ----------

    def _remote(self, argv: Sequence[str]) -> list[str]:
        return [*self.endpoint.launch_prefix, *argv]

    def ensure_helper(self) -> None:
        """把助手送上远端(内容寻址、幂等、只写 work_root);同一个客户端只做一次。"""
        if self._ready:
            return
        data = helper_bytes()
        path = remote_helper_path(self.endpoint.work_root)
        result = self._transport.run(
            self._remote([self._python, "-c", BOOTSTRAP, path.rsplit("/", 1)[0], path,
                          hashlib.sha256(data).hexdigest()]),
            stdin=data, timeout=120.0)
        if result.returncode != 0:
            raise TransportError(f"uploading the remote helper failed (exit {result.returncode}): "
                                 f"{_tail_of(result.stderr)}")
        self._ready = True

    def _invoke(self, command: str, request: dict, timeout: float) -> RunResult:
        argv = self._remote([self._python, remote_helper_path(self.endpoint.work_root), command])
        return self._transport.run(argv, stdin=json.dumps(request).encode("utf-8"), timeout=timeout)

    def _call(self, command: str, timeout: float = 60.0, **fields: object) -> dict:
        request = {"work_root": self.endpoint.work_root, **fields}
        for attempt in (1, 2):
            self.ensure_helper()
            result = self._invoke(command, request, timeout)
            lines = [ln for ln in result.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
            if result.returncode == 0 and lines:
                break
            if attempt == 1:
                self._ready = False  # 远端助手文件可能被清掉了:重新上传再试一次
                continue
            raise TransportError(f"remote helper call '{command}' failed (exit {result.returncode}): "
                                 f"{_tail_of(result.stderr)}")
        try:
            reply = json.loads(lines[-1])
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise TransportError(f"remote helper call '{command}' returned unparseable output")
        if not reply["ok"]:
            code, detail = str(reply.get("error", "unknown")), str(reply.get("detail", ""))
            if code in _TRANSIENT_CODES:
                raise TransportError(f"remote helper call '{command}' failed transiently ({code}): {detail}")
            raise HelperError(code, detail)
        return reply

    # ---------- 作业 ----------

    def start(self, job_id: str, spec: JobSpec, *, secret: str | None = None) -> JobStatus:
        fields: dict = {"job_id": job_id, "spec": spec.to_wire()}
        if secret is not None:
            fields["secret"] = secret
        return self._status_of(self._call("start", **fields))

    def status(self, job_id: str) -> JobStatus:
        return self._status_of(self._call("status", job_id=job_id))

    def tail(self, job_id: str, lines: int = 50) -> str:
        return str(self._call("tail", job_id=job_id, lines=lines)["text"])

    def stop(self, job_id: str, *, grace_s: float = 10.0) -> JobStatus:
        return self._status_of(self._call("stop", timeout=grace_s + 30.0, job_id=job_id, grace_s=grace_s))

    @staticmethod
    def _status_of(reply: dict) -> JobStatus:
        return JobStatus(reply["state"], reply.get("exit_code"), reply.get("spec_sha256"))

    # ---------- 文件 ----------

    def put(self, path: str, data: bytes, *, executable: bool = False) -> None:
        self._call("put", path=path, content_b64=base64.b64encode(data).decode("ascii"),
                   sha256=hashlib.sha256(data).hexdigest(), executable=executable)

    def hash_tree(self, path: str, mode: str, *, exclude: Sequence[str] = ()) -> str:
        """exclude:不进哈希的相对文件名(例如转换产物里记录这个哈希的完成标记)。"""
        extra = {"exclude": list(exclude)} if exclude else {}
        return str(self._call("hash-tree", timeout=3600.0, path=path, mode=mode, **extra)["sha256"])

    def get(self, path: str) -> bytes:
        """读一个小文件(完成标记、tracker)。文件不在抛 HelperError(code='not_found')。"""
        reply = self._call("get", path=path)
        data = base64.b64decode(str(reply["content_b64"]).encode("ascii"), validate=True)
        if hashlib.sha256(data).hexdigest() != reply["sha256"]:
            raise TransportError("the remote helper returned a file whose sha256 does not match")
        return data

    def rename(self, src: str, dst: str) -> None:
        """原子改名。目标已存在抛 HelperError(code='exists')。"""
        self._call("rename", src=src, dst=dst)

    def rm_tmp(self, path: str) -> bool:
        """删掉自己造的 '<name>.tmp-<token>' 目录。别的形状抛 HelperError(code='bad_path')。"""
        return bool(self._call("rm-tmp", path=path)["removed"])

    # ---------- 租约与资源 ----------

    def lease_acquire(self, holder: str, *, grace_s: float | None = None) -> bool:
        extra = {} if grace_s is None else {"grace_s": grace_s}
        return bool(self._call("lease-acquire", holder=holder, **extra)["acquired"])

    def lease_release(self, holder: str) -> bool:
        return bool(self._call("lease-release", holder=holder)["released"])

    def lease_holder(self) -> tuple[str | None, bool]:
        reply = self._call("lease-peek")
        return reply["holder"], bool(reply["stale"])

    def gpu_process_count(self) -> int:
        """宿主机上的计算进程数(不经 launch_prefix:容器内通常看不到别的容器的进程)。只数,不记名字。"""
        result = self._transport.run(list(NVIDIA_SMI), timeout=60.0)
        if result.returncode != 0:
            raise TransportError(f"nvidia-smi failed (exit {result.returncode}): {_tail_of(result.stderr)}")
        return sum(1 for ln in result.stdout.decode("utf-8", "replace").splitlines() if ln.strip())

    def gpu_memory_gib(self) -> tuple[float, int]:
        """每张卡的总显存(GiB)与卡数,取各卡的最小值,宿主机视角。

        读不到或数字不合形态抛 TransportError:瞬态语义,由调用方决定是否延后。"""
        result = self._transport.run(list(NVIDIA_SMI_MEMORY), timeout=60.0)
        if result.returncode != 0:
            raise TransportError(f"nvidia-smi failed (exit {result.returncode}): {_tail_of(result.stderr)}")
        values = []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                try:
                    values.append(float(line))
                except ValueError:
                    raise TransportError("nvidia-smi reported a memory figure that is not a number") from None
        if not values:
            raise TransportError("the host did not report any GPU memory")
        return min(values) / 1024.0, len(values)

    def preflight(self, *, own_holder: str | None = None) -> None:
        """GPU、内存、磁盘检查;不过抛 ComputeBusyError。不取租约、不终止任何进程(设计 §3.3、D8)。

        唯一的写是首次调用时把助手送上远端(Ruling 7)。租约持有者就是 own_holder 时视为本作业,
        跳过"无计算进程"检查(Ruling 8)。"""
        holder, stale = self.lease_holder()
        if holder and not stale and holder != own_holder:
            raise ComputeBusyError(f"the GPU lease is held by job {holder}")
        probe = self._call("probe")
        mem, disk = probe.get("mem_available_gib"), probe.get("disk_free_gib")
        if mem is None or disk is None:
            raise TransportError("the host did not report memory or disk figures")
        if mem < self.endpoint.min_free_host_mem_gib:
            raise ComputeBusyError(f"free host memory {mem:g} GiB is below {self.endpoint.min_free_host_mem_gib:g}")
        if disk < self.endpoint.min_free_disk_gib:
            raise ComputeBusyError(f"free disk {disk:g} GiB is below {self.endpoint.min_free_disk_gib:g}")
        if not (holder and holder == own_holder):
            count = self.gpu_process_count()
            if count:
                raise ComputeBusyError(f"{count} compute process(es) are running on the GPU host")

    # ---------- 整段作业 ----------

    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40, lease: bool = True) -> JobResult:
        """取租约、启动(幂等)、轮询到结束、释放租约。非零退出抛 RemoteJobFailedError,超时先 stop 再抛
        RemoteJobTimeoutError,租约被占抛 ComputeBusyError。传输错误原样抛出且不释放租约:作业还在远端跑,
        下一次调用会接上它。超时从本次调用算起,守护进程重启后重新计时。
        lease=False 给纯 CPU 作业(转换器):不取也不释放 GPU 租约。"""
        if lease and not self.lease_acquire(job_id):
            holder, _ = self.lease_holder()
            raise ComputeBusyError(f"the GPU lease is held by job {holder or 'unknown'}")
        started = self._clock()
        status = self.start(job_id, spec, secret=secret)
        while status.state == "running":
            remaining = self.endpoint.job_timeout_seconds - (self._clock() - started)
            if remaining <= 0:
                self.stop(job_id)
                if lease:
                    self.lease_release(job_id)
                raise RemoteJobTimeoutError(f"job {job_id} exceeded {self.endpoint.job_timeout_seconds:g}s")
            self._sleep(min(self.endpoint.poll_seconds, remaining))
            status = self.status(job_id)
        tail = self.tail(job_id, tail_lines)
        if lease:
            self.lease_release(job_id)
        if status.state == "exited" and status.exit_code == 0:
            return JobResult(0, tail)
        what = f"exited with code {status.exit_code}" if status.state == "exited" else f"is {status.state}"
        raise RemoteJobFailedError(f"job {job_id} {what}", exit_code=status.exit_code, log_tail=tail)
