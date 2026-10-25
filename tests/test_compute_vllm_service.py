"""vLLM 服务的命令拼装:纯函数,不碰网络。每条断言都对应 V3 在真机上踩过或验证过的事。"""
from __future__ import annotations

import pytest

from compute.errors import ComputeError
from compute.vllm_service import SECRET_ENV, VllmServeConfig, build_serve_job

BASE = "/data/models/Qwen3.8-Flash-Next"
ADAPTER = "/data/work/exports/683fbdd6"


def _cfg(**kw) -> VllmServeConfig:
    base = dict(model_path=BASE, adapters=(("forge", ADAPTER),), remote_port=18081)
    base.update(kw)
    return VllmServeConfig(**base)


def _flag(argv, name):
    """取 `--name value` 里的 value;布尔开关(后面跟的是另一个 --flag 或没有)返回 True。"""
    i = argv.index(name)
    nxt = argv[i + 1] if i + 1 < len(argv) else None
    return True if nxt is None or nxt.startswith("--") else nxt


def _values(argv, name):
    """取 `--name v1 v2 ...` 里的全部取值,到下一个 `--flag` 或末尾为止(vLLM 的 --lora-modules 是 nargs='+')。"""
    i = argv.index(name) + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--"):
        out.append(argv[i])
        i += 1
    return out


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


@pytest.mark.parametrize("adapters", [
    (("forge", "not/absolute"),),
    (("forge", "/data/../x"),),
    (("has space", ADAPTER),),
    (("a=b", ADAPTER),),              # 会破坏 name=path 的拼法
    (("base", ADAPTER),),             # 与基座同名,请求会分不清
    (("", ADAPTER),),
    (("forge", "/data/a=b"),),        # vLLM 按 '=' 切 name=path,路径里再有 '=' 会切错
    (("forge", "/data/a,b"),),        # 含 ',' 的取值 vLLM 会当 JSON 解析,必然失败
    ((("a", "/p"), ("a", "/q"))),     # 名字重复
    (("a", "/p"), ("b", "/q"), ("c", "/r")),   # 一个进程至多两个适配器
    ("forge=/x",),                    # 不是 (名字, 路径) 对
    (("forge",),),
    (("forge", 7),),
    (("forge", ADAPTER, "extra"),),
    [("forge", ADAPTER)],             # 必须是元组,配置是冻结值
])
def test_an_adapter_list_that_could_corrupt_the_command_is_refused(adapters):
    with pytest.raises(ComputeError):
        _cfg(adapters=adapters)


def test_values_from_the_config_never_become_flags():
    """路径或名字以 `--` 开头会被 vLLM 当成另一个参数。校验要在它们进 argv 之前拦住。"""
    with pytest.raises(ComputeError):
        _cfg(adapters=(("--api-key", ADAPTER),))
    with pytest.raises(ComputeError):
        _cfg(model_path="--help")


# ---------------- 多适配器 ----------------
# --lora-modules 的写法依据:vLLM 把列表型的配置项声明成 nargs='+'(vllm/engine/arg_utils.py 的 get_kwargs),
# lora_modules 是 list[LoRAModulePath];LoRAParserAction 对每个取值项:含 '=' 且不含 ',' 的按 name=path 切,
# 其余按 JSON 解析。所以多个适配器是「一个 --lora-modules 后面跟多个独立的 name=path」,
# 逗号拼接的 "a=/x,b=/y" 会被当成 JSON 而报错。官方文档的写法也是 `--lora-modules {name}={path} {name}={path}`。

def test_two_adapters_are_separate_items_after_one_lora_modules_flag():
    argv = list(build_serve_job(_cfg(adapters=(("trained", "/a"), ("opponent", "/b")))).argv)
    assert argv.count("--lora-modules") == 1
    assert _values(argv, "--lora-modules") == ["trained=/a", "opponent=/b"]
    assert not any("," in item for item in argv)       # 绝不是逗号拼接
    assert _flag(argv, "--max-loras") == "2"
    assert _flag(argv, "--enable-lora") is True
    assert _flag(argv, "--max-lora-rank") == "64"


def test_one_adapter_keeps_the_old_command_line():
    argv = list(build_serve_job(_cfg()).argv)
    assert _values(argv, "--lora-modules") == [f"forge={ADAPTER}"]
    assert _flag(argv, "--max-loras") == "1"


def test_without_adapters_none_of_the_four_lora_flags_is_emitted():
    argv = list(build_serve_job(_cfg(adapters=())).argv)
    for flag in ("--enable-lora", "--max-lora-rank", "--max-loras", "--lora-modules"):
        assert flag not in argv
    assert _flag(argv, "--served-model-name") == "base"      # 基座照常服务


def test_the_config_has_no_single_adapter_fields_any_more():
    cfg = _cfg()
    assert not hasattr(cfg, "adapter_name") and not hasattr(cfg, "adapter_path")
