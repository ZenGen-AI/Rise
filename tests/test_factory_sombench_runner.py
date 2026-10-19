"""真实 SoMEval 运行器:不起服务、不起 GPU,一切外部依赖都注入。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from compute.errors import ComputeError
from controlplane.envelope import BigRef, CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file
from diagnostic.datamodel import DiagnosticItem
from factory.eval_config import EvalConfig
from factory.sombench_runner import (
    CandidateUnusableError,
    EvalOutputError,
    EvalSecretLeakError,
    EvalTransientError,
    SomevalRunner,
)

SECRET = "sk-judge-must-not-leak"
EXPORT_SHA = "a" * 64
STAMP = "20261019-120000"


def _items(*ids):
    return [DiagnosticItem(i, "1.1.1", "mcq", "someval", "static", None, None, None) for i in ids]


def _store(tmp_path):
    store = GitStore(str(tmp_path / "repo"))
    store.init()
    return store


def _candidate(store, run_id="run-1", *, fmt="qwen4_exp_vllm_2d", endpoint_id="gpu-1",
               export="/work/exports/abc123"):
    ref = BigRef(
        uri=f"ssh://{endpoint_id}{export}", sha256=EXPORT_SHA, size_bytes=0,
        fetch_hint={"type": "roll_ssh", "format": fmt},
    )
    env = Envelope(
        id=f"cand-{run_id}", type="candidate_checkpoint", version=1, created_at="2026-10-19T00:00:00Z",
        created_by=OwnerRef(), status="pending_review", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id], payload={"checkpoint_ref": ref.to_dict()},
    )
    store.write_product(candidate_checkpoint_file(run_id), env, "candidate")
    return ref


def _config(tmp_path, **over):
    data = dict(
        someval_root=tmp_path / "SoMEval", someval_python=tmp_path / "py",
        work_root=tmp_path / "work", index_path=tmp_path / "idx",
        judge_base_url="https://judge.example/v1", judge_api_key=SECRET,
    )
    data.update(over)
    (tmp_path / "SoMEval" / "datasets" / "SoMBench").mkdir(parents=True, exist_ok=True)
    (tmp_path / "SoMEval" / "datasets" / "SoMBench" / "test-00000-of-00001.parquet").write_bytes(b"pq")
    return EvalConfig(**data)


class FakeEndpoint:
    endpoint_id = "gpu-1"
    base_model_path = "/models/base"


class FakeHandle:
    job_id = "job-1"
    base_url = "http://127.0.0.1:49152"
    api_key = "vllm-key"
    model_names = ("base", "forge")


class FakeServe:
    """替身:记录配置并交出 handle。"""

    def __init__(self, trace, *, fail=False):
        self.trace, self.fail = trace, fail
        self.configs = []

    def __call__(self, client, cfg, **kw):
        self.configs.append((cfg, kw))
        return self._ctx()

    def _ctx(self):
        trace, fail = self.trace, self.fail

        class Ctx:
            def __enter__(self):
                trace.append(("serve_enter",))
                if fail:
                    raise ComputeError("cannot serve")
                return FakeHandle()

            def __exit__(self, *exc):
                trace.append(("serve_exit",))
                return False

        return Ctx()


class FakeRun:
    """替身:第一次(predict)写结果,第二次(metric)什么都不做。"""

    def __init__(self, tmp_path, *, rows, returncode=0, on_run=None):
        self.rows, self.returncode, self.calls, self.on_run = rows, returncode, [], on_run
        self.tmp_path = tmp_path

    def __call__(self, argv, *, cwd, env, capture_output, timeout, check):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        if "--exp-dir" not in argv and self.returncode == 0:
            config = __import__("yaml").safe_load(Path(argv[argv.index("--experiment-config") + 1]).read_text())
            # 真实的 run_eval.py 用自己生成的时间戳覆盖 RUN_TIMESTAMP,替身照此行为
            stamp = f"20261019_1200{len(self.calls):02d}"
            out = Path(config["results_path"]) / "SoMBench" / "forge" / f"exp_{stamp}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "config.json").write_text(json.dumps({
                "dataset": "SoMBench",
                "experiment_config": {"protocol": "cot", "llm_config": {"model_name": "forge"}},
            }), encoding="utf-8")
            (out / "detailed_metrics.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows), encoding="utf-8")
        if self.on_run is not None:          # 在替身写完结果之后调用:测试可以覆盖或加料
            self.on_run(argv, env)
        import subprocess
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"boom")


def _row(sample_id, correct=True, reason=None, extracted="C"):
    return {"sample_id": sample_id, "is_correct": correct, "error_reason": reason,
            "extracted": extracted, "prompt_type": "mcq_single", "repeat": 0}


def _runner(tmp_path, *, rows, store=None, run=None, serve=None, trace=None, **cfg_over):
    store = store or _store(tmp_path)
    trace = trace if trace is not None else []
    return SomevalRunner(
        store=store, client=object(), endpoint=FakeEndpoint(), cfg=_config(tmp_path, **cfg_over),
        serve=serve or FakeServe(trace), build_job=lambda cfg: ("job", cfg),
        tunnel_factory=lambda: object(), probe=lambda url, key: True,
        run=run or FakeRun(tmp_path, rows=rows),
        now=lambda: __import__("datetime").datetime(2026, 10, 19, 12, 0, 0),
        commit_of=lambda root: "6453a83",
    )


def test_a_clean_run_yields_one_outcome_per_item(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    runner = _runner(tmp_path, rows=[_row("i1"), _row("i2", False, "wrong_answer", "A")], store=store)
    out = runner.evaluate_scored(_items("i1", "i2"), "run-1", 1)
    assert out["i1"] == [__import__("diagnostic.datamodel", fromlist=["SampleOutcome"]).SampleOutcome(1, "C", None)]
    assert out["i2"][0].score == 0 and out["i2"][0].error_reason == "wrong_answer"


def test_the_server_gets_the_export_and_the_large_context(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    trace = []
    serve = FakeServe(trace)
    _runner(tmp_path, rows=[_row("i1")], store=store, serve=serve).evaluate_scored(_items("i1"), "run-1", 1)
    cfg, kw = serve.configs[0]
    assert cfg.model_path == "/models/base"
    assert cfg.adapter_name == "forge" and cfg.adapter_path == "/work/exports/abc123"
    assert cfg.max_model_len == 40960
    assert trace == [("serve_enter",), ("serve_exit",)]


def test_two_subprocesses_run_in_the_checkout_with_predict_then_metric(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    run = FakeRun(tmp_path, rows=[_row("i1")])
    runner = _runner(tmp_path, rows=[], store=store, run=run)
    runner.evaluate_scored(_items("i1"), "run-1", 1)
    assert len(run.calls) == 2
    first, second = run.calls
    assert first["cwd"] == str(tmp_path / "SoMEval")
    assert first["argv"][1:3] == ["run_eval.py", "--experiment-config"]
    assert "--exp-dir" not in first["argv"] and second["argv"][-2] == "--exp-dir"
    assert "RUN_TIMESTAMP" not in first["env"]        # run_eval.py 自己生成,传了也会被覆盖
    assert first["env"]["OPENAI_API_KEY"] == SECRET
    assert SECRET not in " ".join(first["argv"]) and SECRET not in json.dumps(first["env"]).replace(SECRET, "")


def test_the_work_directory_is_outside_the_repository(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    runner = _runner(tmp_path, rows=[_row("i1")], store=store)
    runner.evaluate_scored(_items("i1"), "run-1", 1)
    assert (tmp_path / "work" / "run-1" / STAMP).is_dir()
    assert not (tmp_path / "repo" / "work").exists()


def test_a_mock_export_is_refused(tmp_path):
    store = _store(tmp_path)
    _candidate(store, fmt="mock")
    with pytest.raises(CandidateUnusableError, match="adapter export"):
        _runner(tmp_path, rows=[], store=store).evaluate_scored(_items("i1"), "run-1", 1)


def test_an_export_on_another_endpoint_is_refused(tmp_path):
    store = _store(tmp_path)
    _candidate(store, endpoint_id="gpu-2")
    with pytest.raises(CandidateUnusableError, match="another endpoint"):
        _runner(tmp_path, rows=[], store=store).evaluate_scored(_items("i1"), "run-1", 1)


def test_a_missing_candidate_is_refused(tmp_path):
    with pytest.raises(CandidateUnusableError, match="no candidate"):
        _runner(tmp_path, rows=[]).evaluate_scored(_items("i1"), "run-9", 1)


def test_an_item_missing_from_the_results_is_a_deterministic_failure(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    with pytest.raises(EvalOutputError, match="1 of 2"):
        _runner(tmp_path, rows=[_row("i1")], store=store).evaluate_scored(_items("i1", "i2"), "run-1", 1)


def test_a_judge_failure_discards_the_whole_run(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    rows = [_row("i1", False, "judge_error"), _row("i2")]
    with pytest.raises(EvalTransientError, match="judge"):
        _runner(tmp_path, rows=rows, store=store).evaluate_scored(_items("i1", "i2"), "run-1", 1)


def test_too_many_empty_samples_is_transient_and_at_the_threshold_passes(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    rows = [_row("i1", False, "content_none")] + [_row(f"f{n}") for n in range(99)]
    runner = _runner(tmp_path, rows=rows, store=store)
    assert runner.evaluate_scored(_items("i1"), "run-1", 1)["i1"][0].score == 0


def test_a_nonzero_subprocess_is_transient(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    run = FakeRun(tmp_path, rows=[], returncode=3)
    with pytest.raises(EvalTransientError, match="code 3"):
        _runner(tmp_path, rows=[], store=store, run=run).evaluate_scored(_items("i1"), "run-1", 1)


def test_a_mismatched_result_directory_is_deterministic(tmp_path):
    """结果目录里的实验指纹不是本次的:不采信它,按确定性失败处理。"""
    store = _store(tmp_path)
    _candidate(store)
    runner = _runner(tmp_path, rows=[_row("i1")], store=store)
    inner = runner.run

    def rewrite_config(argv, **kw):
        result = inner(argv, **kw)
        if "--exp-dir" not in argv:
            base = tmp_path / "work" / "run-1" / STAMP / "results" / "SoMBench" / "forge"
            (next(base.glob("exp_*")) / "config.json").write_text(json.dumps({
                "experiment_config": {"protocol": "direct", "llm_config": {"model_name": "forge"}}}))
        return result

    runner.run = rewrite_config
    with pytest.raises(EvalOutputError, match="different experiment"):
        runner.evaluate_scored(_items("i1"), "run-1", 1)


def test_a_leaked_credential_deletes_the_work_directory_and_is_not_deferred(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    runner = _runner(tmp_path, rows=[_row("i1")], store=store)
    inner = runner.run

    def run_with_leak(argv, **kw):
        result = inner(argv, **kw)
        if "--exp-dir" in argv:                      # metric 阶段结束:在工作目录里放一个凭证
            (tmp_path / "work" / "run-1" / STAMP / "leak.txt").write_text(SECRET)
        return result

    runner.run = run_with_leak
    with pytest.raises(EvalSecretLeakError, match="leak.txt") as caught:
        runner.evaluate_scored(_items("i1"), "run-1", 1)
    assert SECRET not in str(caught.value)
    assert not (tmp_path / "work" / "run-1" / STAMP).exists()


def test_k_other_than_one_is_refused_before_anything_runs(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    trace = []
    with pytest.raises(Exception, match="repeats at 1"):
        _runner(tmp_path, rows=[], store=store, trace=trace).evaluate_scored(_items("i1"), "run-1", 2)
    assert trace == []


def test_the_someval_stats_are_recorded(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    rows = [_row("i1"), _row("i1", False, "extraction_failed", "")]
    runner = _runner(tmp_path, rows=rows, store=store)
    runner.evaluate_scored(_items("i1"), "run-1", 1)
    result_dir = next((tmp_path / "work" / "run-1" / STAMP / "results" / "SoMBench" / "forge").glob("exp_*"))
    _, stats = runner._read_outcomes(result_dir, _items("i1"))
    assert stats["protocol"] == "cot" and stats["samples"] == 2
    assert stats["extraction_failed"] == 1 and stats["someval_commit"] == "6453a83"


def test_the_transient_failure_rides_the_deferred_channel():
    from controlplane.deferred import DeferredError

    assert issubclass(EvalTransientError, DeferredError)


def test_a_failing_subprocess_never_puts_raw_stderr_in_the_reason(tmp_path):
    """子进程 stderr 可能回显请求头或配置。密钥抹掉,尾巴进属性,完整一份落在 0600 的文件里。"""
    store = _store(tmp_path)
    _candidate(store)
    run = FakeRun(tmp_path, rows=[])
    run.returncode = 3
    original = FakeRun.__call__

    def run_with_secret(argv, **kw):
        import subprocess
        result = original(run, argv, **kw)
        return subprocess.CompletedProcess(argv, 3, b"", f"HTTP 401 key={SECRET} retry later".encode())

    runner = _runner(tmp_path, rows=[], store=store, run=run_with_secret)
    with pytest.raises(EvalTransientError) as caught:
        runner.evaluate_scored(_items("i1"), "run-1", 1)
    message = str(caught.value)
    assert SECRET not in message
    assert "someval-predict.stderr.log" in message and "code 3" in message
    assert SECRET not in caught.value.log_tail and "<redacted>" in caught.value.log_tail
    log = tmp_path / "work" / "run-1" / STAMP / "logs" / "someval-predict.stderr.log"
    assert log.is_file() and SECRET not in log.read_text(encoding="utf-8")
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_the_production_client_carries_the_alias_the_tunnel_needs():
    """生产装配不传 tunnel_factory:运行器从客户端拿别名造隧道。客户端必须带着别名。"""
    from compute.endpoint import ComputeEndpoint
    from factory.compute_config import build_compute_client

    endpoint = ComputeEndpoint(
        endpoint_id="gpu-1", work_root="/work", roll_root="/work/roll", launch_prefix=(),
        base_model_path="/models/base", heldout_jsonl="/work/h.jsonl", sft_template="t",
        min_free_host_mem_gib=1.0, min_free_disk_gib=1.0, poll_seconds=1.0, job_timeout_seconds=60.0,
    )
    client = build_compute_client(endpoint, {"gpu-1": "gpu-181"})
    assert client.alias == "gpu-181"


def test_without_a_tunnel_factory_the_runner_builds_an_ssh_tunnel_from_the_client_alias(tmp_path):
    from compute.tunnel import SshTunnel

    store = _store(tmp_path)
    _candidate(store)
    seen = {}

    class Client:
        alias = "gpu-181"

    def capture_serve(client, cfg, *, tunnel_factory, **kw):
        seen["tunnel"] = tunnel_factory()
        raise ComputeError("stop here")

    runner = SomevalRunner(
        store=store, client=Client(), endpoint=FakeEndpoint(), cfg=_config(tmp_path),
        serve=capture_serve, now=lambda: __import__("datetime").datetime(2026, 10, 19, 12, 0, 0),
        commit_of=lambda root: "x",
    )
    with pytest.raises(ComputeError, match="stop here"):
        runner.evaluate_scored(_items("i1"), "run-1", 1)
    assert isinstance(seen["tunnel"], SshTunnel)


def test_the_serving_key_is_also_scanned_for(tmp_path):
    """服务密钥每次现生成,同样不能落盘:扫描范围包括它。"""
    store = _store(tmp_path)
    _candidate(store)
    runner = _runner(tmp_path, rows=[_row("i1")], store=store)
    inner = runner.run

    def run_with_leak(argv, **kw):
        result = inner(argv, **kw)
        if "--exp-dir" in argv:
            (tmp_path / "work" / "run-1" / STAMP / "dump.txt").write_text(FakeHandle.api_key)
        return result

    runner.run = run_with_leak
    with pytest.raises(EvalSecretLeakError, match="dump.txt") as caught:
        runner.evaluate_scored(_items("i1"), "run-1", 1)
    assert FakeHandle.api_key not in str(caught.value)
