"""RollExecutor:load 的三种引用、存点回认、阶段作业、指标对账、逐步方法拒绝。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint, metrics_log
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.roll_executor import MetricsMissingError, RollExecutor, _render_rows
from mindtrainer.training_config import InvalidTrainingConfigError
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

RUN = "run-1"


def _episode(ep_id="e0", *, turns=(("a", "u1"), ("b", "v1"), ("a", "u2"))):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=4,
    )
    records = [TurnRecord(i, slot, text) for i, (slot, text) in enumerate(turns)]
    return Episode(ep_id, "s1", assignment, records, "max_turns")


def _executor(client: FakeClient, **kw) -> RollExecutor:
    return RollExecutor(client=client, endpoint=fake_endpoint(**kw), run_id=RUN)


def _base_ref(client: FakeClient) -> BigRef:
    endpoint = fake_endpoint()
    digest = client.seed_dir(endpoint.base_model_path)
    return BigRef(
        uri=f"ssh://{endpoint.endpoint_id}{endpoint.base_model_path}",
        sha256=digest, size_bytes=0, fetch_hint={"type": "roll_ssh"},
    )


# ---------------- 数据渲染 ----------------

def test_render_rows_supervises_every_trained_turn():
    """被训练角色位说了 2 句就出 2 行,instruction 是该句之前的全部发言。"""
    rows = _render_rows(_episode(), "a")
    assert rows == [
        {"instruction": "", "output": "u1"},
        {"instruction": "a: u1\nb: v1", "output": "u2"},
    ]


def test_render_rows_ignores_other_slots():
    rows = _render_rows(_episode(turns=(("b", "v1"), ("b", "v2"))), "a")
    assert rows == []


# ---------------- load ----------------

def test_load_accepts_the_base_model():
    client = FakeClient()
    executor = _executor(client)
    executor.load(_base_ref(client))  # 不抛即通过:起点基座不过 checkpoint_intact


def test_load_rejects_mock_refs():
    """mock:// 引用:load 抛 CheckpointCorruptError(重试不会好;分级沿用现状,见 Task 7 Step 2)。"""
    executor = _executor(FakeClient())
    ref = BigRef(uri="mock://ckpt/" + "a" * 64 + "@3", sha256="a" * 64, size_bytes=0)
    with pytest.raises(CheckpointCorruptError, match=r"expects an ssh:// reference"):
        executor.load(ref)


def test_load_rejects_adapter_exports():
    """适配器导出不是训练起点:本轮只支持单轮(设计 §5.1)。"""
    executor = _executor(FakeClient())
    ref = BigRef(
        uri="ssh://gpu-1/srv/rise/work/exports/aabbccdd", sha256="b" * 64, size_bytes=0,
        fetch_hint={"type": "roll_ssh", "format": "qwen4_exp_vllm_2d"},
    )
    with pytest.raises(InvalidTrainingConfigError, match=r"adapter export"):
        executor.load(ref)


def test_load_rejects_a_checkpoint_that_is_not_intact():
    client = FakeClient()
    client.seed_dir("/srv/rise/work/runs/run-1/j-a1/checkpoint-1")
    ref = BigRef(
        uri="ssh://gpu-1/srv/rise/work/runs/run-1/j-a1/checkpoint-1",
        sha256="c" * 64, size_bytes=0, fetch_hint={"type": "roll_ssh"},
    )
    with pytest.raises(CheckpointCorruptError, match=r"is not intact"):
        _executor(client).load(ref)


# ---------------- checkpoint_intact ----------------

def test_checkpoint_intact_is_false_when_the_directory_is_gone():
    ref = BigRef(uri="ssh://gpu-1/srv/rise/work/nope", sha256="d" * 64, size_bytes=0)
    assert _executor(FakeClient()).checkpoint_intact(ref) is False


def test_checkpoint_intact_propagates_transport_errors():
    """传输错误不能变成 False:那会被 decide_resume 判成 T3 永久失败(设计 §5.1)。"""

    class Flaky(FakeClient):
        def hash_tree(self, path, mode):
            raise HelperError("internal", "boom")

    ref = BigRef(uri="ssh://gpu-1/srv/rise/work/x", sha256="e" * 64, size_bytes=0)
    with pytest.raises(HelperError):
        _executor(Flaky()).checkpoint_intact(ref)


# ---------------- preflight 与逐步方法 ----------------

def test_preflight_delegates_to_the_client():
    client = FakeClient()
    _executor(client).preflight()
    assert client.preflight_calls == 1


def test_preflight_raises_compute_busy():
    with pytest.raises(ComputeBusyError):
        _executor(FakeClient(busy=True)).preflight()


def test_step_methods_refuse_to_run():
    executor = _executor(FakeClient())
    with pytest.raises(RuntimeError, match=r"call run_sft_stage"):
        executor.sft_step([_episode()], trained_slot="a")
    with pytest.raises(RuntimeError, match=r"does not support GRPO"):
        executor.grpo_opd_step([], opd_coefficient=0.1, teacher_model_ref=None)


def test_supported_phases_is_sft_only():
    assert RollExecutor.supported_phases == frozenset({"sft"})
    assert RollExecutor.checkpoint_type == "roll_ssh"


# ---------------- run_sft_stage ----------------

def _staged(client: FakeClient, batches, *, first_step=0, prior=()):
    executor = _executor(client)
    executor.load(_base_ref(client))
    return executor, executor.run_sft_stage(
        batches, first_step=first_step, trained_slot="a", prior_batches=list(prior)
    )


def _ckpt_hook(last_step: int):
    """模拟作业写出段尾存点目录。"""
    def hook(job_id, spec):
        out = dict(zip(spec.argv, spec.argv[1:]))["--output-dir"]
        FakeClient.seed_dir.__get__(hook.client)(f"{out}/checkpoint-{last_step - 1}")
    return hook


def test_run_sft_stage_writes_rows_in_step_order_and_returns_roll_steps():
    client = FakeClient(log=metrics_log(0, 2))
    hook = _ckpt_hook(2)
    hook.client = client
    client.job_hook = hook
    executor, results = _staged(client, [[_episode("e0")], [_episode("e1")]])
    assert [r.step for r in results] == [0, 1]
    assert all(r.phase == "sft" and "sft_loss" in r.metrics for r in results)
    # 每集 2 行(被训练角色位 2 句)× 2 步 = 4 行
    train = next(p for p in client.put_calls if p.endswith("train.jsonl"))
    assert len(client.files[train].decode("utf-8").strip().splitlines()) == 4


def test_prior_batches_become_the_file_prefix_and_change_the_job_id():
    """前缀进 train.jsonl 且进 job_id:ROLL 按位置跳过已消费的批次(设计 §5.2)。"""
    first = FakeClient(log=metrics_log(0, 1))
    h1 = _ckpt_hook(1); h1.client = first; first.job_hook = h1
    _staged(first, [[_episode("e0")]])

    second = FakeClient(log=metrics_log(1, 2))
    h2 = _ckpt_hook(2); h2.client = second; second.job_hook = h2
    _staged(second, [[_episode("e1")]], first_step=1, prior=[[_episode("e0")]])

    assert first.run_calls[0][0] != second.run_calls[0][0]
    prefix_rows = second.files[next(p for p in second.put_calls if p.endswith("train.jsonl"))]
    assert prefix_rows.decode("utf-8").splitlines()[0].count("u1") == 1  # 前缀行在最前


def test_resume_adds_the_resume_flag():
    client = FakeClient(log=metrics_log(2, 3))
    hook = _ckpt_hook(3); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e2")]], first_step=2, prior=[[_episode("e0")], [_episode("e1")]])
    argv = client.run_calls[0][1].argv
    assert "--resume" in argv and "--max-steps" in argv
    assert argv[argv.index("--max-steps") + 1] == "3"


def test_metrics_missing_when_a_step_has_no_loss():
    """条数不符一律失败,绝不按位置合成 step(设计 §5.2 第 7 步)。"""
    client = FakeClient(log=metrics_log(0, 1))  # 只报了 1 步,却跑了 2 步
    hook = _ckpt_hook(2); hook.client = client; client.job_hook = hook
    with pytest.raises(MetricsMissingError, match=r"no sft loss for step\(s\) \[1\]"):
        _staged(client, [[_episode("e0")], [_episode("e1")]])


def test_metrics_missing_when_the_loss_key_is_absent():
    client = FakeClient(log='metrics_tag: {"step": 0, "metrics": {"other": 1.0}}\n')
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    with pytest.raises(MetricsMissingError):
        _staged(client, [[_episode("e0")]])


def test_stage_checkpoint_reference_carries_the_roll_hint():
    client = FakeClient(log=metrics_log(0, 1))
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    executor, _ = _staged(client, [[_episode("e0")]])
    ref = executor.save_checkpoint("step-1")
    assert ref.uri.startswith("ssh://gpu-1/srv/rise/work/runs/run-1/")
    assert ref.uri.endswith("/checkpoint-0")
    assert ref.fetch_hint["type"] == "roll_ssh"
    assert ref.fetch_hint["format"] == "roll_lora_legacy"
    assert len(ref.sha256) == 64


def test_save_checkpoint_reclaims_the_loaded_one_when_no_stage_ran():
    """续训时 start == total:一步都没跑,但编排器仍会调 save_checkpoint(设计 §5.1)。"""
    client = FakeClient()
    executor = _executor(client)
    base = _base_ref(client)
    executor.load(base)
    ref = executor.save_checkpoint("final")
    assert ref.uri == base.uri and ref.sha256 == base.sha256


def test_save_checkpoint_refuses_when_the_directory_is_gone():
    client = FakeClient()
    executor = _executor(client)
    executor.load(_base_ref(client))
    client.files.clear()
    with pytest.raises(CheckpointCorruptError, match=r"is gone"):
        executor.save_checkpoint("final")


def test_stage_end_never_cuts_finer():
    assert _executor(FakeClient()).stage_end([], first_step=2, limit=7) == 7
