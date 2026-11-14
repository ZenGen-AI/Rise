"""RollExecutor:load 的三种引用、存点回认、阶段作业、指标对账、逐步方法拒绝。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint, training_artifacts
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


def _ckpt_hook(last_step: int, *, first_step: int = 0, losses=None):
    """模拟作业结束时留在输出目录里的文件:布局照 B1 在 .181 上的实测(见 training_artifacts)。

    log_history 照 2026-11-14 在 171 上的实测:续训作业接过之前的 log_history 再往后追加,
    system/step 是全局步号,所以存点里是 0..last_step-1 全部的步,存点目录叫 checkpoint-{last_step-1}。
    losses 默认给这些步每步一个数。first_step 只为调用处读得清楚,不影响产物。"""
    del first_step
    def hook(job_id, spec):
        out = dict(zip(spec.argv, spec.argv[1:]))["--output-dir"]
        hook.client.files.update(training_artifacts(out, last_step, losses=losses))
    return hook


def test_run_sft_stage_writes_rows_in_step_order_and_returns_roll_steps():
    client = FakeClient()
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
    first = FakeClient()
    h1 = _ckpt_hook(1); h1.client = first; first.job_hook = h1
    _staged(first, [[_episode("e0")]])

    second = FakeClient()
    h2 = _ckpt_hook(2, first_step=1); h2.client = second; second.job_hook = h2
    _staged(second, [[_episode("e1")]], first_step=1, prior=[[_episode("e0")]])

    assert first.run_calls[0][0] != second.run_calls[0][0]
    prefix_rows = second.files[next(p for p in second.put_calls if p.endswith("train.jsonl"))]
    assert prefix_rows.decode("utf-8").splitlines()[0].count("u1") == 1  # 前缀行在最前


def test_the_launch_command_points_at_the_real_training_entry():
    """入口在 ROLL4ZenGen 的 scripts/qwen38/ 下,解释器是 python3(容器里没有 python)。
    此前这里写的是 {roll_root}/run_sft_validation.py,路径漏了两层,到真机上才会发现。"""
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e0")]], first_step=0, prior=[])
    argv = client.run_calls[0][1].argv
    assert argv[0] == "python3"
    assert argv[1] == "/srv/roll/scripts/qwen38/run_sft_validation.py"


def test_resume_adds_the_resume_flag():
    client = FakeClient()
    hook = _ckpt_hook(3, first_step=2); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e2")]], first_step=2, prior=[[_episode("e0")], [_episode("e1")]])
    argv = client.run_calls[0][1].argv
    assert "--resume" in argv and "--max-steps" in argv
    assert argv[argv.index("--max-steps") + 1] == "3"


def test_a_later_stage_in_the_same_process_resumes_from_the_previous_stage():
    """同一进程内的第二段必须从上一段的存点续训,不能回落到 load() 的起点基座。

    2026-11-14 在 171 上撞到过:load() 只在编排器开头收一次起点基座,而 run_sft_stage 的
    --resume 取的就是它,于是第二段让 ROLL 去基座目录里找 pipeline/worker_state_pipeline.json,
    报 FileNotFoundError 退出。第一段成功、第二段必挂,单段的测试看不出来。"""
    client = FakeClient()
    hook = _ckpt_hook(2); hook.client = client; client.job_hook = hook
    executor, _ = _staged(client, [[_episode("e0")], [_episode("e1")]])   # 第一段:[0, 2)
    stage_one_ckpt = executor._stage_ref
    assert stage_one_ckpt is not None

    hook2 = _ckpt_hook(4, first_step=2); hook2.client = client; client.job_hook = hook2
    results = executor.run_sft_stage(
        [[_episode("e2")], [_episode("e3")]],
        first_step=2, trained_slot="a",
        prior_batches=[[_episode("e0")], [_episode("e1")]],
    )
    argv = client.run_calls[1][1].argv
    assert argv[argv.index("--resume") + 1] == stage_one_ckpt.uri[len("ssh://gpu-1"):]
    assert argv[argv.index("--resume") + 1] != fake_endpoint().base_model_path
    assert [r.step for r in results] == [2, 3]


def test_a_resumed_stage_reads_its_own_steps_from_the_cumulative_history():
    """续训作业的 log_history 带着之前各段的步,system/step 是全局步号(171 上实测)。
    第二段要取第 2、3 步自己的 loss,不能把第 0、1 步的 loss 当成它们。"""
    client = FakeClient()
    losses = [4.59, 4.21, 3.87, 3.07]   # 171 上 run-s1-2 第二段存点里的四条,保留两位
    hook = _ckpt_hook(4, first_step=2, losses=losses); hook.client = client; client.job_hook = hook
    _, results = _staged(client, [[_episode("e2")], [_episode("e3")]], first_step=2,
                         prior=[[_episode("e0")], [_episode("e1")]])
    assert [(r.step, r.metrics["sft_loss"]) for r in results] == [(2, 3.87), (3, 3.07)]


def test_metrics_missing_when_the_history_is_shorter_than_the_steps_run():
    """条数不符一律失败,绝不按位置合成 step(设计 §5.2 第 7 步)。"""
    client = FakeClient()
    hook = _ckpt_hook(2, losses=[2.5]); hook.client = client; client.job_hook = hook  # 只记了 1 步,却跑了 2 步
    with pytest.raises(MetricsMissingError, match=r"no sft loss for step\(s\) \[1\]"):
        _staged(client, [[_episode("e0")], [_episode("e1")]])


def test_metrics_missing_when_the_loss_key_is_absent():
    import json

    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client

    def broken(job_id, spec):
        hook(job_id, spec)
        state = next(k for k in client.files if k.endswith("worker_state_pipeline.json"))
        client.files[state] = json.dumps({"log_history": [{"system/step": 0, "other": 1.0}]}).encode("utf-8")

    client.job_hook = broken
    with pytest.raises(MetricsMissingError):
        _staged(client, [[_episode("e0")]])


def test_metrics_missing_when_the_job_left_no_completion_marker():
    """训练脚本没写 completion.json:存点位置不可知,只能失败,不能去猜带时间戳的目录名。"""
    client = FakeClient()
    with pytest.raises(MetricsMissingError, match=r"completion\.json"):
        _staged(client, [[_episode("e0")]])


@pytest.mark.parametrize("reported", [
    "/etc/passwd",                                        # 作业目录之外的绝对路径
    "/srv/rise/work/runs/run-1/elsewhere/checkpoint-0",   # 前缀像、但不在本作业目录之下
    "{out}/../../../etc/checkpoint-0",                    # 用 .. 绕出去
])
def test_a_completion_record_pointing_outside_the_job_directory_is_refused(reported):
    """completion.json 是远端写的,它报的存点位置会被拿去做 hash_tree 和进 BigRef。
    越界的位置不能被采信:否则一个被改过的记录就能让我们对任意路径取摘要、并把它登记成存点。"""
    import json

    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client

    def lying(job_id, spec):
        hook(job_id, spec)
        out = dict(zip(spec.argv, spec.argv[1:]))["--output-dir"]
        client.files[f"{out}/completion.json"] = json.dumps(
            {"checkpoint": reported.format(out=out)}).encode("utf-8")

    client.job_hook = lying
    with pytest.raises(MetricsMissingError, match=r"outside the job directory"):
        _staged(client, [[_episode("e0")]])


def test_metrics_come_from_the_checkpoint_not_from_the_log_tail():
    """指标行在混杂日志里离末尾很远(B1 实测第 0 步距末尾 40 行,而取的尾巴只有 48 行,刚好踩线),
    步数一多会系统性丢掉早期步。所以数据源是存点里的 log_history,日志里有什么都不影响。"""
    client = FakeClient(log="x" * 10 + "\nmetrics_tag: not json at all\n")
    hook = _ckpt_hook(2, losses=[2.64, 2.45]); hook.client = client; client.job_hook = hook
    _, results = _staged(client, [[_episode("e0")], [_episode("e1")]])
    assert [(r.step, r.metrics["sft_loss"]) for r in results] == [(0, 2.64), (1, 2.45)]


def test_steps_are_matched_by_system_step_not_by_position():
    """对账按 system/step 取,不按下标:乱序、重复、多出来的步都不能被按位置收下。"""
    import json

    client = FakeClient()
    hook = _ckpt_hook(2); hook.client = client

    def shuffled(job_id, spec):
        hook(job_id, spec)
        state = next(k for k in client.files if k.endswith("worker_state_pipeline.json"))
        client.files[state] = json.dumps({"log_history": [
            {"system/step": 1, "sft_train/loss@sum": 2.45},
            {"system/step": 0, "sft_train/loss@sum": 2.64},
            {"system/step": 7, "sft_train/loss@sum": 9.99},  # 多出来的,不属于本段
        ]}).encode("utf-8")

    client.job_hook = shuffled
    _, results = _staged(client, [[_episode("e0")], [_episode("e1")]])
    assert [(r.step, r.metrics["sft_loss"]) for r in results] == [(0, 2.64), (1, 2.45)]


def test_stage_checkpoint_reference_carries_the_roll_hint():
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    executor, _ = _staged(client, [[_episode("e0")]])
    ref = executor.save_checkpoint("step-1")
    assert ref.uri.startswith("ssh://gpu-1/srv/rise/work/runs/run-1/")
    assert ref.uri.endswith("/checkpoint-0")
    assert ref.fetch_hint["type"] == "roll_ssh"
    assert ref.fetch_hint["format"] == "roll_lora_legacy"
    assert len(ref.sha256) == 64
    assert "roll_commit" not in ref.fetch_hint       # 没绑运行环境:形状与 v3 之前相同


RECORD = {"roll_commit": "8b" * 20, "roll_env": "abcdef012345", "image_id": "sha256:" + "1" * 64,
          "dependencies": {"Megatron-LM": "16819e70677d", "fla": "2ef8d23a54d1"}}


def _bound(client: FakeClient, *, record=RECORD) -> RollExecutor:
    executor = _executor(client)
    derived = fake_endpoint(roll_root="/srv/rise/work/deps/roll-8b8b8b8b8b8b",
                            sft_template="/srv/rise/work/deps/roll-8b8b8b8b8b8b/scripts/qwen38/configs/sft_lora.yaml",
                            launch_prefix=("docker", "exec", "-i", "forge-roll-abcdef012345"))
    executor.bind_runtime(client, derived, record)
    return executor


def test_a_bound_runtime_runs_the_deployed_roll_and_records_its_version():
    """部署好的运行环境:训练入口在部署目录里,存点 fetch_hint 带版本两项,backend_record 给训练记录。"""
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    executor = _bound(client)
    executor.load(_base_ref(client))
    executor.run_sft_stage([[_episode("e0")]], first_step=0, trained_slot="a", prior_batches=[])
    argv = client.run_calls[0][1].argv
    assert argv[1] == "/srv/rise/work/deps/roll-8b8b8b8b8b8b/scripts/qwen38/run_sft_validation.py"
    assert dict(zip(argv, argv[1:]))["--config"].endswith("/scripts/qwen38/configs/sft_lora.yaml")
    ref = executor.save_checkpoint("step-1")
    assert (ref.fetch_hint["roll_commit"], ref.fetch_hint["roll_env"]) == ("8b" * 20, "abcdef012345")
    assert executor.backend_record == {"executor": "roll", "endpoint": "gpu-1", **RECORD}


def test_another_roll_environment_is_another_job():
    """运行环境进 job_id:换了 ROLL 版本不会接上旧版本的成功作业。"""
    ids = []
    for env in ("abcdef012345", "999999999999"):
        client = FakeClient()
        hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
        executor = _bound(client, record={**RECORD, "roll_env": env})
        executor.load(_base_ref(client))
        executor.run_sft_stage([[_episode("e0")]], first_step=0, trained_slot="a", prior_batches=[])
        ids.append(client.run_calls[0][0])
    assert ids[0] != ids[1]


def test_the_job_runs_in_its_own_directory_not_in_the_roll_tree():
    """sft_lora.yaml 的 logging_dir 是相对路径;cwd 在 ROLL 树里会把日志写进只读的部署目录。"""
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e0")]])
    job_id, spec, *_ = client.run_calls[0]
    assert spec.cwd == f"/srv/rise/work/jobs/{job_id}"


def test_the_checkpoint_reference_follows_the_location_the_script_reported():
    """存点在 <out_dir>/checkpoints/<时间戳>/checkpoint-N:时间戳是 ROLL 框架无条件追加的,
    事先猜不到。B1 实测此前假定的 <out_dir>/checkpoint-N 是错的,引用会指向一个不存在的目录。"""
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    executor, _ = _staged(client, [[_episode("e0")]])
    ref = executor.save_checkpoint("step-1")
    assert "/checkpoints/20261018-010530/checkpoint-0" in ref.uri


def test_the_launch_command_pins_the_checkpoint_dir_and_a_short_ray_temp_dir():
    """B1 的第一次尝试死在 ROLL_RAY_TEMP_DIR 太长(Unix 套接字 107 字节上限,目录最多 37 字符);
    work_root 本身可以很长,所以 Ray 临时目录不能落在它下面。"""
    client = FakeClient()
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e0")]])
    spec = client.run_calls[0][1]
    assert len(spec.env["ROLL_RAY_TEMP_DIR"]) <= 37
    assert not spec.env["ROLL_RAY_TEMP_DIR"].startswith("/srv/rise/work")
    assert spec.env["ROLL_RAY_TEMP_DIR"] != "/tmp/ray"  # 要带上本作业的标识,否则两个作业会共用一个 Ray 会话


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
