"""vLLM 服务的命令拼装:纯函数,不碰网络。每条断言都对应 V3 在真机上踩过或验证过的事。"""
from __future__ import annotations

import pytest

from compute.errors import ComputeError
from compute.vllm_service import SECRET_ENV, VllmServeConfig, build_serve_job

BASE = "/data/models/Qwen3.8-Flash-Next"
ADAPTER = "/data/work/exports/683fbdd6"


def _cfg(**kw) -> VllmServeConfig:
    base = dict(model_path=BASE, adapter_name="forge", adapter_path=ADAPTER, remote_port=18081)
    base.update(kw)
    return VllmServeConfig(**base)


def _flag(argv, name):
    """取 `--name value` 里的 value;布尔开关(后面跟的是另一个 --flag 或没有)返回 True。"""
    i = argv.index(name)
    nxt = argv[i + 1] if i + 1 < len(argv) else None
    return True if nxt is None or nxt.startswith("--") else nxt


def test_the_command_serves_the_base_model_with_the_adapter_as_a_lora_module():
    spec = build_serve_job(_cfg())
    argv = list(spec.argv)
    assert argv[:3] == ["vllm", "serve", BASE]
    assert _flag(argv, "--served-model-name") == "base"
    assert _flag(argv, "--lora-modules") == f"forge={ADAPTER}"
    assert _flag(argv, "--enable-lora") is True


def test_nvls_is_always_turned_off():
    """V3 在这类主机上第一次起服务就死在 NVLS 多播绑定(CUDA error 401),进程在加载模型前退出。"""
    assert build_serve_job(_cfg()).env["NCCL_NVLS_ENABLE"] == "0"


def test_the_server_binds_loopback_only():
    """vLLM 的 HTTP 接口没有别的访问控制,同机的其他容器共用主机网络,监听外网卡就是对它们开放。"""
    argv = list(build_serve_job(_cfg()).argv)
    assert _flag(argv, "--host") == "127.0.0.1"
    assert _flag(argv, "--port") == "18081"


def test_the_api_key_travels_by_the_secret_channel_not_the_command_line():
    spec = build_serve_job(_cfg())
    assert spec.secret_env == SECRET_ENV == "VLLM_API_KEY"
    assert SECRET_ENV not in spec.env          # 作业规格里只有变量名,没有取值
    assert "--api-key" not in spec.argv        # vLLM 也认命令行参数,但那会让密钥进进程列表


def test_parallelism_follows_the_config_because_it_follows_the_host():
    argv = list(build_serve_job(_cfg(tensor_parallel_size=4)).argv)
    assert _flag(argv, "--tensor-parallel-size") == "4"
    assert _flag(argv, "--max-lora-rank") == "64"
    assert _flag(argv, "--max-model-len") == "4096"


def test_the_working_directory_is_not_invented():
    """vllm serve 不需要特定的工作目录;不传 cwd 就用作业目录的默认,不去猜一个路径。"""
    assert build_serve_job(_cfg()).cwd is None


@pytest.mark.parametrize("field,value", [
    ("model_path", "relative/path"),
    ("model_path", "/data/../etc"),
    ("adapter_path", "not/absolute"),
    ("adapter_path", "/data/../x"),
    ("adapter_name", "has space"),
    ("adapter_name", "a=b"),          # 会破坏 name=path 的拼法
    ("adapter_name", "base"),         # 与基座同名,请求会分不清
    ("adapter_name", ""),
    ("remote_port", 0),
    ("remote_port", 70000),
    ("tensor_parallel_size", 0),
    ("max_model_len", 0),
    ("gpu_memory_utilization", 1.5),
    ("gpu_memory_utilization", 0),
])
def test_a_config_that_could_corrupt_the_command_is_refused(field, value):
    with pytest.raises(ComputeError):
        _cfg(**{field: value})


def test_values_from_the_config_never_become_flags():
    """路径或名字以 `--` 开头会被 vLLM 当成另一个参数。校验要在它们进 argv 之前拦住。"""
    with pytest.raises(ComputeError):
        _cfg(adapter_name="--api-key")
    with pytest.raises(ComputeError):
        _cfg(model_path="--help")
