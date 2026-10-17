"""RollLoraConverter:幂等、产物形状、tmp 清理、内存预检、哈希不符。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.roll_converter import EXPORT_FORMAT, EXPORT_MARKER, RollLoraConverter

CKPT = "/srv/rise/work/runs/run-1/j-a1/checkpoint-1"
SHA = "a1b2c3d4" + "0" * 56


def _ref(sha: str = SHA) -> BigRef:
    return BigRef(uri=f"ssh://gpu-1{CKPT}", sha256=sha, size_bytes=0,
                  fetch_hint={"type": "roll_ssh", "format": "roll_lora_legacy"})


def _exporting_client(**kw) -> FakeClient:
    """作业跑起来就往 --output 指的 tmp 目录里写几个文件。"""
    client = FakeClient(**kw)

    def hook(job_id, spec):
        out = spec.argv[spec.argv.index("--output") + 1]
        for name in ("adapter_model.safetensors", "adapter_config.json"):
            client.files[f"{out}/{name}"] = b"weights"

    client.job_hook = hook
    return client


def _converter(client: FakeClient) -> RollLoraConverter:
    return RollLoraConverter(client=client, endpoint=fake_endpoint())


# ---------------- 正常转换 ----------------

def test_convert_writes_the_export_and_returns_an_inference_ref():
    client = _exporting_client()
    ref = _converter(client).convert(_ref())
    assert ref.uri == f"ssh://gpu-1/srv/rise/work/exports/{SHA[:8]}"
    assert not ref.uri.endswith(".hf")  # 只有 MockConverter 看后缀(设计 §6.2 第 4 步)
    assert len(ref.sha256) == 64
    assert ref.fetch_hint == {
        "type": "roll_ssh",
        "format": EXPORT_FORMAT,
        "base_model_path": "/srv/models/qwen-4b",
        "requires": "native-vllm-lora",
        "source_checkpoint_sha256": SHA,
    }


def test_the_export_job_is_cpu_only_and_takes_no_lease():
    """设计 §6.2 第 1 步:CUDA_VISIBLE_DEVICES 置空,不占 GPU 租约。"""
    client = _exporting_client()
    _converter(client).convert(_ref())
    job_id, spec, lease = client.run_calls[0]
    assert spec.env["CUDA_VISIBLE_DEVICES"] == ""
    assert lease is False
    assert "--checkpoint" in spec.argv and CKPT in spec.argv


def test_the_marker_lands_inside_the_final_directory():
    client = _exporting_client()
    ref = _converter(client).convert(_ref())
    marker = client.files[f"/srv/rise/work/exports/{SHA[:8]}/{EXPORT_MARKER}"]
    assert marker.decode("ascii") == ref.sha256


def test_nothing_is_left_in_a_tmp_directory():
    client = _exporting_client()
    _converter(client).convert(_ref())
    assert not [p for p in client.files if ".tmp-" in p]


# ---------------- 幂等 ----------------

def test_convert_is_idempotent_and_does_not_rerun_the_job():
    client = _exporting_client()
    converter = _converter(client)
    first = converter.convert(_ref())
    second = converter.convert(_ref())
    assert first.uri == second.uri and first.sha256 == second.sha256
    assert len(client.run_calls) == 1, "已有带标记的产物时不该再跑一次作业"


def test_a_tampered_export_is_a_corrupt_error():
    """标记在而内容变了:抛 CheckpointCorruptError,不重做(设计 §6.2 第 2 步;分级沿用现状)。"""
    client = _exporting_client()
    converter = _converter(client)
    converter.convert(_ref())
    client.files[f"/srv/rise/work/exports/{SHA[:8]}/adapter_config.json"] = b"tampered"
    with pytest.raises(CheckpointCorruptError, match=r"but its contents hash to"):
        converter.convert(_ref())


def test_a_marker_that_is_not_a_sha256_is_rejected():
    client = _exporting_client()
    client.files[f"/srv/rise/work/exports/{SHA[:8]}/{EXPORT_MARKER}"] = b"nonsense"
    with pytest.raises(CheckpointCorruptError, match=r"does not hold a sha256"):
        _converter(client).convert(_ref())


# ---------------- 失败与清理 ----------------

def test_a_failed_job_leaves_no_tmp_behind():
    client = _exporting_client()

    def boom(job_id, spec):
        raise HelperError("internal", "the export crashed")

    client.job_hook = boom
    with pytest.raises(HelperError):
        _converter(client).convert(_ref())
    assert client.rm_tmp_calls, "失败后要清掉自己造的 tmp"
    assert not [p for p in client.files if ".tmp-" in p]


def test_cleanup_never_touches_a_finished_export():
    """清理范围严格限制在自己的 tmp(设计 §6.2 第 2 步)。"""
    client = _exporting_client()
    converter = _converter(client)
    converter.convert(_ref())
    done = f"/srv/rise/work/exports/{SHA[:8]}"
    with pytest.raises(HelperError):
        client.rm_tmp(done)  # 正式目录名字不含 .tmp-,助手会拒
    assert f"{done}/{EXPORT_MARKER}" in client.files


def test_low_host_memory_defers():
    client = _exporting_client()
    client.mem_available_gib = 4.0  # 端点阈值是 12.0
    with pytest.raises(ComputeBusyError, match=r"host memory is free"):
        _converter(client).convert(_ref())
    assert not client.run_calls, "内存不够时不该起作业"


# ---------------- 输入校验 ----------------

def test_mock_refs_are_refused():
    with pytest.raises(CheckpointCorruptError, match=r"expects an ssh:// reference"):
        _converter(FakeClient()).convert(
            BigRef(uri="mock://ckpt/" + "a" * 64 + "@3", sha256="a" * 64, size_bytes=0)
        )


def test_a_reference_without_a_sha256_is_refused():
    with pytest.raises(CheckpointCorruptError, match=r"no usable sha256"):
        _converter(FakeClient()).convert(
            BigRef(uri=f"ssh://gpu-1{CKPT}", sha256="", size_bytes=0)
        )
