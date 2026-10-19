"""vLLM 推理服务的命令拼装(设计 DX-13 到 DX-15)。

只做一件事:把配置变成一个 `JobSpec`。纯函数,不碰网络,不认识评测。服务本身是一个不会退出的
远端作业,由 `ComputeClient` 的 start/status/stop 托管(见 service.py)。

每个固定参数都有来历:
- NCCL_NVLS_ENABLE=0:这类主机的 NVLS 多播绑定会失败(NCCL 报 CUDA error 401),vLLM 建张量并行
  通信组时抛错,进程在加载模型之前就退出。真机实测:关掉之后一次起来。
- 只绑 127.0.0.1:vLLM 的 HTTP 接口没有别的访问控制,同机其他容器共用主机网络。
- 接入密钥走 secret_env:密钥不进命令行(进程列表里看得到)、不进日志。
- --enforce-eager:跳过图捕获,启动快;吞吐不是这个组件现在要回答的问题。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from compute.client import JobSpec
from compute.endpoint import is_clean_abs_path
from compute.errors import ComputeError
from compute.sizing import DEFAULT_OVERHEAD_GIB, MemorySizing, check_memory_fits

SECRET_ENV = "VLLM_API_KEY"
BASE_MODEL_NAME = "base"
_ADAPTER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class VllmServeConfig:
    model_path: str
    adapter_name: str
    adapter_path: str
    remote_port: int
    tensor_parallel_size: int = 8       # 取决于主机的卡数,所以是配置项而不是常量
    max_lora_rank: int = 64
    max_model_len: int = 4096
    gpu_memory_utilization: float = 0.85
    enforce_eager: bool = True          # 关图优化:启动快、显存峰值低,代价是解码慢(见 sizing.py)
    # 权重体积(GiB),只用于起服务前的核算;None 表示不知道,跳过预检
    model_weights_gib: float | None = None
    # 非权重显存开销,只用于起服务前的核算,不进命令行;留 0 表示跳过预检(见 sizing.py)
    memory_overhead_gib: float = DEFAULT_OVERHEAD_GIB

    def __post_init__(self) -> None:
        for name in ("model_path", "adapter_path"):
            if not is_clean_abs_path(getattr(self, name)):
                raise ComputeError(f"{name} must be an absolute path below / without '..'")
        if not isinstance(self.adapter_name, str) or not _ADAPTER_NAME.match(self.adapter_name):
            raise ComputeError("adapter_name has an invalid form")
        if self.adapter_name == BASE_MODEL_NAME:
            raise ComputeError(f"adapter_name must differ from the base model name {BASE_MODEL_NAME!r}")
        if not (isinstance(self.remote_port, int) and not isinstance(self.remote_port, bool)
                and 1024 <= self.remote_port <= 65535):
            raise ComputeError("remote_port must be an integer in [1024, 65535]")
        for name in ("tensor_parallel_size", "max_lora_rank", "max_model_len"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ComputeError(f"{name} must be a positive integer")
        if (isinstance(self.gpu_memory_utilization, bool)
                or not isinstance(self.gpu_memory_utilization, (int, float))
                or not 0 < self.gpu_memory_utilization <= 1):
            raise ComputeError("gpu_memory_utilization must be in (0, 1]")


def build_serve_job(cfg: VllmServeConfig) -> JobSpec:
    argv = (
        "vllm", "serve", cfg.model_path,
        "--served-model-name", BASE_MODEL_NAME,
        "--host", "127.0.0.1", "--port", str(cfg.remote_port),
        "--tensor-parallel-size", str(cfg.tensor_parallel_size),
        "--enable-expert-parallel",
        "--enable-lora",
        "--max-lora-rank", str(cfg.max_lora_rank),
        "--max-loras", "1",
        "--lora-modules", f"{cfg.adapter_name}={cfg.adapter_path}",
        "--trust-remote-code",
        "--max-model-len", str(cfg.max_model_len),
        *(["--enforce-eager"] if cfg.enforce_eager else []),
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
    )
    return JobSpec(argv=argv, env={"NCCL_NVLS_ENABLE": "0"}, secret_env=SECRET_ENV)
