"""vLLM 推理服务的命令拼装(设计 DX-13 到 DX-15)。

只做一件事:把配置变成一个 `JobSpec`。纯函数,不碰网络,不认识评测。服务本身是一个不会退出的
远端作业,由 `ComputeClient` 的 start/status/stop 托管(见 service.py)。

每个固定参数都有来历:
- NCCL_NVLS_ENABLE=0:这类主机的 NVLS 多播绑定会失败(NCCL 报 CUDA error 401),vLLM 建张量并行
  通信组时抛错,进程在加载模型之前就退出。真机实测:关掉之后一次起来。
- 只绑 127.0.0.1:vLLM 的 HTTP 接口没有别的访问控制,同机其他容器共用主机网络。
- 接入密钥走 secret_env:密钥不进命令行(进程列表里看得到)、不进日志。
- --enforce-eager:跳过图捕获,启动快;吞吐不是这个组件现在要回答的问题。
- --lora-modules 的多适配器写法:一个 flag 后面跟多个独立的 name=path(vLLM 把列表型配置项声明成
  nargs='+',LoRAParserAction 对每一项:含 '=' 且不含 ',' 的按 name=path 切,其余按 JSON 解析)。
  逗号拼接的 "a=/x,b=/y" 会被当成 JSON 而启动失败,所以不用。依据见 tests/test_compute_vllm_service.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from compute.client import JobSpec
from compute.endpoint import is_clean_abs_path
from compute.errors import ComputeError
from compute.sizing import DEFAULT_OVERHEAD_GIB

SECRET_ENV = "VLLM_API_KEY"
BASE_MODEL_NAME = "base"
_ADAPTER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_ADAPTERS = 2    # 设计:一个 vLLM 进程至多挂两个 LoRA(被训练的策略一个、对手一个)


@dataclass(frozen=True)
class VllmServeConfig:
    model_path: str
    # ((名字, 导出路径), ...),至多 MAX_ADAPTERS 个;空表示只服务基座,不开 LoRA
    adapters: tuple[tuple[str, str], ...]
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
        if not is_clean_abs_path(self.model_path):
            raise ComputeError("model_path must be an absolute path below / without '..'")
        self._check_adapters()
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

    def _check_adapters(self) -> None:
        adapters = self.adapters
        if not isinstance(adapters, tuple):
            raise ComputeError("adapters must be a tuple of (name, path) pairs")
        if len(adapters) > MAX_ADAPTERS:
            raise ComputeError(f"at most {MAX_ADAPTERS} adapters can be served by one vLLM process")
        names: set[str] = set()
        for pair in adapters:
            if not (isinstance(pair, tuple) and len(pair) == 2):
                raise ComputeError("every adapter must be a (name, path) pair")
            name, path = pair
            if not isinstance(name, str) or not _ADAPTER_NAME.match(name):
                raise ComputeError("adapter name has an invalid form")
            if name == BASE_MODEL_NAME:
                raise ComputeError(f"adapter name must differ from the base model name {BASE_MODEL_NAME!r}")
            if name in names:
                raise ComputeError("adapter names must be unique")
            names.add(name)
            if not is_clean_abs_path(path):
                raise ComputeError("adapter path must be an absolute path below / without '..'")
            if "=" in path or "," in path:
                # vLLM 按 '=' 切 name=path、含 ',' 的项当 JSON 解析:这两个字符会让服务在启动时才报错
                raise ComputeError("adapter path must not contain '=' or ','")


def _lora_args(cfg: VllmServeConfig) -> tuple[str, ...]:
    if not cfg.adapters:
        return ()
    return (
        "--enable-lora",
        "--max-lora-rank", str(cfg.max_lora_rank),
        "--max-loras", str(len(cfg.adapters)),
        # 一个 flag 后面跟多个独立的 name=path(nargs='+'),不是逗号拼接
        "--lora-modules", *(f"{name}={path}" for name, path in cfg.adapters),
    )


def build_serve_job(cfg: VllmServeConfig) -> JobSpec:
    argv = (
        "vllm", "serve", cfg.model_path,
        "--served-model-name", BASE_MODEL_NAME,
        "--host", "127.0.0.1", "--port", str(cfg.remote_port),
        "--tensor-parallel-size", str(cfg.tensor_parallel_size),
        "--enable-expert-parallel",
        *_lora_args(cfg),
        "--trust-remote-code",
        "--max-model-len", str(cfg.max_model_len),
        *(["--enforce-eager"] if cfg.enforce_eager else []),
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
    )
    return JobSpec(argv=argv, env={"NCCL_NVLS_ENABLE": "0"}, secret_env=SECRET_ENV)
