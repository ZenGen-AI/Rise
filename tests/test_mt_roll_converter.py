"""RollLoraConverter:幂等、产物形状、tmp 清理、内存预检、哈希不符。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef, InvalidBigRefError
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.roll_converter import EXPORT_FORMAT, EXPORT_MARKER, EXPORT_PROGRAM, RollLoraConverter

CKPT = "/srv/rise/work/runs/run-1/j-a1/checkpoint-1"
SHA = "a1b2c3d4" + "0" * 56


def _ref(sha: str = SHA) -> BigRef:
    return BigRef(uri=f"ssh://gpu-1{CKPT}", sha256=sha, size_bytes=0,
                  fetch_hint={"type": "roll_ssh", "format": "roll_lora_legacy"})


def _exporting_client(**kw) -> FakeClient:
    """作业跑起来就往 ROLL_EXPORT_OUT 指的 tmp 目录里写几个文件。"""
    client = FakeClient(**kw)

    def hook(job_id, spec):
        out = spec.env["ROLL_EXPORT_OUT"]
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
    assert spec.env["ROLL_EXPORT_ADAPTER"] == CKPT


def test_the_export_is_a_python_call_with_paths_passed_by_environment():
    """ROLL4ZenGen 的导出是 convert_checkpoint_to_hf 的 Python 调用,没有命令行入口(B1/B2 实测,
    此前假定的 scripts/export_lora_for_vllm.py 不存在)。路径只经 env 传,不拼进 argv:
    argv 里是固定的程序文本,不含任何来自引用的字符串,也就不存在被它们影响的解析面。"""
    client = _exporting_client()
    _converter(client).convert(_ref())
    _, spec, _ = client.run_calls[0]
    assert spec.argv[:2] == ("python3", "-c")
    assert len(spec.argv) == 3 and spec.argv[2] == EXPORT_PROGRAM
    for value in (CKPT, "/srv/models/qwen-4b", "exports"):
        assert not any(value in part for part in spec.argv)
    assert spec.env["ROLL_EXPORT_BASE"] == "/srv/models/qwen-4b"
    assert spec.env["ROLL_EXPORT_OUT"].startswith("/srv/rise/work/exports/")
    assert spec.cwd == "/srv/roll"


def test_the_export_program_calls_the_real_entry_point_with_the_arguments_b2_proved():
    """B2 在 .181 上实测可用的调用:convert_checkpoint_to_hf,low_mem=False(适配器不支持流式),
    verbose=False。README 里的 LoRAHFConverter(...) 示例已过时:__init__ 现在要求 verbose,照抄会抛 TypeError。"""
    assert "convert_checkpoint_to_hf" in EXPORT_PROGRAM
    assert "low_mem=False" in EXPORT_PROGRAM and "verbose=False" in EXPORT_PROGRAM
    assert "adapter_name_or_path" in EXPORT_PROGRAM
    assert "LoRAHFConverter(" not in EXPORT_PROGRAM
    compile(EXPORT_PROGRAM, "<export-program>", "exec")  # 至少是合法的 Python


def test_a_changed_export_program_changes_the_job_identity(monkeypatch):
    """job_id 包含程序文本的哈希:改了导出程序就是另一个作业,不会撞上旧程序留下的成功记录
    (助手对「同 spec 的已存在作业」是直接返回当前状态,不重跑)。"""
    from mindtrainer import roll_converter

    before = roll_converter._job_identity("a" * 64, "/t/x.tmp-1")
    monkeypatch.setattr(roll_converter, "PROGRAM_SHA8", "deadbeef")
    after = roll_converter._job_identity("a" * 64, "/t/x.tmp-1")
    assert before != after
    # 同一个程序、同一个输入 -> 同一个 id(幂等重试要能接上)
    monkeypatch.undo()
    assert roll_converter._job_identity("a" * 64, "/t/x.tmp-1") == before


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


def test_a_reference_without_a_sha256_cannot_even_be_formed():
    """坏 sha256 在构造 BigRef 时就被拒(I8),到不了转换器。"""
    with pytest.raises(InvalidBigRefError, match="sha256"):
        BigRef(uri=f"ssh://gpu-1{CKPT}", sha256="", size_bytes=0)


# ---------------- 路径围栏 ----------------

def test_a_sha256_that_is_not_hex_cannot_escape_the_work_root():
    """sha256[:8] 直接进远端路径:64 个字符的 '../../..' 会让产物目录逃出 work_root。
    BigRef 在构造时就拒绝它(I8),所以这样的引用根本到不了转换器;转换器自己的检查留作纵深防御。"""
    traversal = "../../.." + "/x" * 28  # 64 字符,非十六进制
    assert len(traversal) == 64
    with pytest.raises(InvalidBigRefError, match="sha256"):
        BigRef(uri=f"ssh://gpu-1{CKPT}", sha256=traversal, size_bytes=0)


@pytest.mark.parametrize("uri", [
    "ssh://gpu-1/etc/shadow",                     # work_root 之外的绝对路径
    "ssh://gpu-1/srv/rise/work/../../etc/shadow",  # 用 .. 绕出去
    "ssh://gpu-1/srv/rise/workshop/ckpt",          # 前缀像 work_root 但不是它的子目录
])
def test_a_checkpoint_outside_the_work_root_is_refused(uri):
    """这个路径作为 --checkpoint 进导出作业的 argv,助手的路径校验看不到它。"""
    client = _exporting_client()
    with pytest.raises(CheckpointCorruptError, match=r"points outside the endpoint work root"):
        _converter(client).convert(BigRef(uri=uri, sha256=SHA, size_bytes=0))
    assert client.run_calls == []


def test_a_reference_for_another_endpoint_is_refused():
    """同一条路径在别的主机上是另一份东西,不能拿本端点的客户端去转。"""
    client = _exporting_client()
    with pytest.raises(CheckpointCorruptError, match=r"names endpoint 'gpu-2'"):
        _converter(client).convert(BigRef(uri=f"ssh://gpu-2{CKPT}", sha256=SHA, size_bytes=0))
    assert client.run_calls == []
