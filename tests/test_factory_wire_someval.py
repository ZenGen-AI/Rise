"""`--evaluator someval` 与污染准入检查的装配规则(设计 90 的 FC-16、不变量 8 到 10)。"""
from __future__ import annotations

import os

import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import compute_endpoints_file
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.contamination import build_index
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy
from diagnostic.taxonomy_loader import ItemPool
from factory.errors import FactoryConfigError
from factory.sombench import file_sha256, sombench_parquet, sombench_version
from factory.wire import FactoryOptions, assemble

ENDPOINT = {
    "endpoint_id": "gpu-1", "work_root": "/work", "roll_root": "/work/roll", "launch_prefix": [],
    "base_model_path": "/models/base", "heldout_jsonl": "/work/heldout.jsonl", "sft_template": "t",
    "min_free_host_mem_gib": 1.0, "min_free_disk_gib": 1.0, "poll_seconds": 1.0,
    "job_timeout_seconds": 60.0,
}


def _repo(tmp_path):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    store.write_product(compute_endpoints_file(), Envelope(
        id="cfg", type="config", version=1, created_at="2026-10-19T00:00:00Z", created_by=OwnerRef(),
        status="effective", schema_version=1, payload={"endpoints": [ENDPOINT]}), "endpoints")
    return store


def _env(tmp_path, *, index_digest=None, with_eval=True):
    root = tmp_path / "SoMEval"
    parquet = sombench_parquet(root)
    parquet.parent.mkdir(parents=True, exist_ok=True)
    parquet.write_bytes(b"stub parquet")
    digest = file_sha256(parquet)
    index = tmp_path / "sombench.idx"
    index.write_bytes(build_index(["这是评测题面里的一段话，训练数据里不该出现同样的这一段文字。"], index_digest or digest))
    hosts = tmp_path / "hosts.yaml"
    hosts.write_text("gpu-1: gpu-181\n", encoding="utf-8")
    env = {"RISMFORGE_COMPUTE_HOSTS": str(hosts)}
    if with_eval:
        config = tmp_path / "eval.yaml"
        config.write_text(
            f"someval_root: {root}\nsomeval_python: {root}/.venv/bin/python\n"
            f"work_root: {tmp_path / 'work'}\nindex_path: {index}\n"
            "judge_base_url: https://judge.example/v1\njudge_api_key: sk-test\n",
            encoding="utf-8")
        os.chmod(config, 0o600)
        env["RISMFORGE_EVAL_CONFIG"] = str(config)
    return env, digest


def _install_sombench_assets(store, digest, *, tier="full", k=1):
    version = sombench_version(digest)
    taxonomy = DiagnosticTaxonomy(version, ["1"], {"1": ["1.1"]}, {"1.1": ["1.1.1"]}, [])
    pool = ItemPool(version, [DiagnosticItem("a|base|Q1", "1.1.1", "mcq", "someval", "static",
                                             None, None, None)], [])
    write_taxonomy(store, taxonomy)
    write_item_pool(store, pool)
    store.write_product("control-plane/config/evaluator.yaml", Envelope(
        id="evaluator", type="config", version=1, created_at="2026-10-19T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        payload={"taxonomy_version": version, "item_pool_version": version,
                 "benchmark_version": "bench-sombench-v1", "tier": tier, "sampling_k": k}),
        "evaluator")


def test_someval_needs_the_roll_executor(tmp_path):
    store = _repo(tmp_path)
    with pytest.raises(FactoryConfigError, match="needs --executor roll"):
        assemble(store, FactoryOptions(evaluator="someval"), env={})


def test_someval_cannot_be_combined_with_enable(tmp_path):
    store = _repo(tmp_path)
    env, _ = _env(tmp_path)
    with pytest.raises(FactoryConfigError, match="cannot be combined with --enable"):
        assemble(store, FactoryOptions(evaluator="someval", executor="roll", switches=("plan",)), env=env)


def test_a_real_training_run_without_the_eval_config_is_refused(tmp_path):
    store = _repo(tmp_path)
    env, _ = _env(tmp_path, with_eval=False)
    with pytest.raises(FactoryConfigError, match="RISMFORGE_EVAL_CONFIG"):
        assemble(store, FactoryOptions(executor="roll"), env=env)


def test_an_index_built_from_another_dataset_is_refused(tmp_path):
    store = _repo(tmp_path)
    env, _ = _env(tmp_path, index_digest="0" * 64)
    with pytest.raises(ValueError, match="different dataset"):
        assemble(store, FactoryOptions(executor="roll"), env=env)


def test_the_roll_executor_gets_a_working_gate(tmp_path):
    store = _repo(tmp_path)
    env, _ = _env(tmp_path)
    executor = assemble(store, FactoryOptions(executor="roll"), env=env).executor_factory("run-1")
    assert executor.row_gate is not None
    assert executor.row_gate(b'{"instruction": "x", "output": "y"}\n') == (0, 0)


def test_the_gate_catches_the_indexed_text(tmp_path):
    store = _repo(tmp_path)
    env, _ = _env(tmp_path)
    executor = assemble(store, FactoryOptions(executor="roll"), env=env).executor_factory("run-1")
    bad = '{"instruction": "无关", "output": "这是评测题面里的一段话，训练数据里不该出现同样的这一段文字。"}\n'.encode()
    assert executor.row_gate(bad)[1] > 0


def test_someval_builds_an_evaluator_with_real_backends(tmp_path):
    store = _repo(tmp_path)
    env, digest = _env(tmp_path)
    _install_sombench_assets(store, digest)
    asm = assemble(store, FactoryOptions(executor="roll", evaluator="someval"), env=env)
    assert callable(asm.evaluator)


def test_the_assets_must_be_versioned_for_this_dataset(tmp_path):
    store = _repo(tmp_path)
    env, digest = _env(tmp_path)
    _install_sombench_assets(store, "f" * 64)
    with pytest.raises(FactoryConfigError, match="import-sombench"):
        assemble(store, FactoryOptions(executor="roll", evaluator="someval"), env=env)


def test_the_quick_tier_is_refused(tmp_path):
    store = _repo(tmp_path)
    env, digest = _env(tmp_path)
    _install_sombench_assets(store, digest, tier="quick")
    with pytest.raises(FactoryConfigError, match="tier: full"):
        assemble(store, FactoryOptions(executor="roll", evaluator="someval"), env=env)


def test_k_must_be_one(tmp_path):
    store = _repo(tmp_path)
    env, digest = _env(tmp_path)
    _install_sombench_assets(store, digest, k=4)
    with pytest.raises(FactoryConfigError, match="sampling_k: 1"):
        assemble(store, FactoryOptions(executor="roll", evaluator="someval"), env=env)


def test_the_default_path_is_unchanged(tmp_path):
    store = _repo(tmp_path)
    asm = assemble(store, FactoryOptions(), env={})
    assert asm.evaluator is None and asm.executor_factory is None


# ---------------- 守护进程真实路径:reconcile_once 产生 request_diagnostic,真实评估器接住 ----------------

def _pending_candidate(store, run_id="run-1", *, fmt="qwen4_exp_vllm_2d"):
    from controlplane.envelope import CURRENT_SCHEMA_VERSION, BigRef
    from controlplane.paths import candidate_checkpoint_file

    ref = BigRef(uri=f"ssh://gpu-1/work/exports/{run_id}", sha256="a" * 64, size_bytes=0,
                 fetch_hint={"type": "roll_ssh", "format": fmt})
    store.write_product(candidate_checkpoint_file(run_id), Envelope(
        id=f"cand-{run_id}", type="candidate_checkpoint", version=1, created_at="2026-10-19T00:00:00Z",
        created_by=OwnerRef(), status="pending_review", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id], payload={"checkpoint_ref": ref.to_dict()}), "candidate")


class _ScriptedRunner:
    """运行器替身:记录 bind 与候选 id,按脚本返回或抛错。"""

    def __init__(self, behaviour):
        self.behaviour, self.bound, self.asked = behaviour, None, None

    def bind(self, run_id):
        self.bound = run_id

    def evaluate(self, items, candidate_ref, k):
        raise AssertionError("not used")

    def evaluate_scored(self, items, candidate_ref, k):
        from diagnostic.datamodel import SampleOutcome

        self.asked = candidate_ref
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return {item.id: [SampleOutcome(1, "A", None)] for item in items}

    def last_stats(self):
        return {"protocol": "cot", "samples": 1}


def _evaluator_with(store, digest, runner):
    from factory.evaluation import _someval_evaluator, read_evaluator_config

    _install_sombench_assets(store, digest)
    return _someval_evaluator(store=store, runner=runner, cfg=read_evaluator_config(store))


def test_the_daemon_path_binds_the_run_and_passes_the_candidate_id(tmp_path):
    from controlplane.daemon import reconcile_once

    store = _repo(tmp_path)
    _, digest = _env(tmp_path)
    _pending_candidate(store)
    runner = _ScriptedRunner(None)
    kinds = [a.kind for a in reconcile_once(store, evaluator=_evaluator_with(store, digest, runner))]
    assert kinds == ["request_diagnostic"]
    assert runner.bound == "run-1" and runner.asked == "cand-run-1"
    payload = store.read_product("control-plane/runs/run-1/diagnostic_report.yaml").payload
    assert payload["backends"] == "real" and payload["someval"] == {"protocol": "cot", "samples": 1}
    # 有了报告,下一轮就只等人审
    assert [a.kind for a in reconcile_once(store, evaluator=_evaluator_with(store, digest, runner))] == ["await_review"]


def test_a_deterministic_failure_writes_a_stub_report_and_the_action_completes(tmp_path):
    from controlplane.daemon import reconcile_once
    from controlplane.evidence import diagnostic_grade
    from factory.sombench_runner import CandidateUnusableError

    store = _repo(tmp_path)
    _, digest = _env(tmp_path)
    _pending_candidate(store)
    runner = _ScriptedRunner(CandidateUnusableError("the candidate is not a real adapter export"))
    reconcile_once(store, evaluator=_evaluator_with(store, digest, runner))
    payload = store.read_product("control-plane/runs/run-1/diagnostic_report.yaml").payload
    assert payload["stub"] is True and diagnostic_grade(payload) == "stub"
    assert "not a real adapter export" in payload["failure_reason"]
    # 不再触发评测
    assert [a.kind for a in reconcile_once(store, evaluator=_evaluator_with(store, digest, runner))] == ["await_review"]


def test_a_transient_failure_writes_nothing_and_is_retried(tmp_path):
    from controlplane.daemon import DeferralBook, reconcile_once
    from factory.sombench_runner import EvalTransientError

    store = _repo(tmp_path)
    _, digest = _env(tmp_path)
    _pending_candidate(store)
    runner = _ScriptedRunner(EvalTransientError("judge down"))
    book = DeferralBook()
    reconcile_once(store, evaluator=_evaluator_with(store, digest, runner), deferrals=book)
    assert not store.exists("control-plane/runs/run-1/diagnostic_report.yaml")
