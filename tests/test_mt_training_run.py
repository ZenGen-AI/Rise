import dataclasses
import json
import subprocess

import pytest
import yaml

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.observe import trace
from controlplane.paths import run_dir, training_run_file
from controlplane.states import InvalidTransitionError
from mindtrainer.datamodel import StepResult, TrainingRun
from mindtrainer.resume import NeedsHumanError
from mindtrainer.store_adapter import read_training_run, write_training_run


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _ref(ch):
    return BigRef(f"mock://ckpt/{ch * 64}@2", ch * 64, 0, {"type": "mock", "tag": "t"})


def _run(**kw):
    base = dict(run_id="run-1", config_hash="c" * 64)
    base.update(kw)
    return TrainingRun(**base)


_POLICY_BACKEND = {
    "trained": {"ref": "file:///ckpt/base", "kind": "base", "endpoint": "gpu-1"},
    "opponents": {"b": {"driver": "vllm", "source": "zengen-v1"}},
    "real_inference_with_mock_training": True,
}

# 加字段之前 training_run 载荷的全部键(顺序也算):policy_backend 为 None 时必须一个都不多
_LEGACY_KEYS = [
    "run_id", "config_hash", "checkpoint_step", "last_checkpoint_ref", "resume_attempts",
    "curve_summary", "metric_stream_refs", "failure",
]


def _commits(root):
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout)


# ---------------- StepResult ----------------

def test_step_result_is_frozen():
    result = StepResult(0, "sft", {"sft_loss": 1.0})
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.step = 1


# ---------------- TrainingRun ----------------

def test_defaults_describe_a_fresh_run():
    run = _run()
    assert (run.checkpoint_step, run.last_checkpoint_ref, run.resume_attempts) == (0, None, 0)
    assert (run.curve_summary, run.metric_stream_refs, run.status, run.failure) == ({}, [], "pending", None)


def test_round_trip_keeps_refs_and_progress():
    run = _run(
        checkpoint_step=4, last_checkpoint_ref=_ref("a"), resume_attempts=1,
        curve_summary={"final_mean_reward": 0.5}, metric_stream_refs=[_ref("b")],
    )
    assert TrainingRun.from_dict(run.to_dict()) == run


def test_status_belongs_to_the_envelope_not_the_payload():
    assert "status" not in _run(status="executed").to_dict()
    assert TrainingRun.from_dict(_run().to_dict(), status="executed").status == "executed"


def test_the_metric_stream_key_is_the_one_trace_reads():
    assert "metric_stream_refs" in _run(metric_stream_refs=[_ref("a")]).to_dict()


def test_to_dict_is_plain_data_and_does_not_alias():
    run = _run(curve_summary={"curve": {"mean": 0.5}}, failure={"category": "T2"})
    d = run.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d
    d["curve_summary"]["curve"]["mean"] = 99
    d["failure"]["category"] = "X"
    assert run.curve_summary["curve"]["mean"] == 0.5 and run.failure["category"] == "T2"


# ---------------- policy_backend ----------------

def test_policy_backend_defaults_to_none():
    assert _run().policy_backend is None


def test_round_trip_keeps_policy_backend():
    run = _run(policy_backend=_POLICY_BACKEND)
    assert TrainingRun.from_dict(run.to_dict()) == run
    assert TrainingRun.from_dict(run.to_dict()).policy_backend == _POLICY_BACKEND


def test_the_key_is_written_only_when_there_is_a_backend():
    assert list(_run().to_dict()) == _LEGACY_KEYS   # 与加字段之前逐键相同,既有记录的序列化不变
    assert "policy_backend" not in _run(policy_backend=None).to_dict()
    assert _run(policy_backend=_POLICY_BACKEND).to_dict()["policy_backend"] == _POLICY_BACKEND


def test_an_empty_backend_dict_is_still_written_because_it_is_not_none():
    assert _run(policy_backend={}).to_dict()["policy_backend"] == {}


def test_a_payload_without_the_key_reads_back_as_none():
    payload = _run().to_dict()
    assert "policy_backend" not in payload
    assert TrainingRun.from_dict(payload).policy_backend is None


def test_policy_backend_to_dict_is_plain_data_and_does_not_alias():
    run = _run(policy_backend=_POLICY_BACKEND)
    d = run.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d
    d["policy_backend"]["trained"]["kind"] = "adapter"
    assert run.policy_backend["trained"]["kind"] == "base"
    back = TrainingRun.from_dict(run.to_dict())
    back.policy_backend["opponents"]["b"]["driver"] = "scripted"
    assert run.policy_backend["opponents"]["b"]["driver"] == "vllm"


# ---------------- 存储适配 ----------------

def test_write_then_read_round_trips_with_the_status(store):
    run = _run(checkpoint_step=2, last_checkpoint_ref=_ref("a"), status="pending")
    write_training_run(store, run)
    assert read_training_run(store, "run-1") == run


def test_write_then_read_round_trips_policy_backend(store):
    run = _run(policy_backend=_POLICY_BACKEND)
    path = write_training_run(store, run)
    assert store.read_product(path).payload["policy_backend"] == _POLICY_BACKEND
    assert read_training_run(store, "run-1") == run


def test_a_record_without_a_backend_has_no_key_in_the_file_and_replays_without_a_commit(store, tmp_path):
    path = write_training_run(store, _run(checkpoint_step=2))
    assert list(store.read_product(path).payload) == _LEGACY_KEYS
    assert "policy_backend" not in (tmp_path / path).read_text()
    before = _commits(tmp_path)
    write_training_run(store, read_training_run(store, "run-1"))
    assert _commits(tmp_path) == before


def test_a_finished_record_cannot_have_its_backend_rewritten(store):
    write_training_run(store, _run(status="executed", policy_backend=_POLICY_BACKEND))
    other = dict(_POLICY_BACKEND, real_inference_with_mock_training=False)
    with pytest.raises(ValueError, match="finished record"):
        write_training_run(store, _run(status="executed", policy_backend=other))
    assert read_training_run(store, "run-1").policy_backend == _POLICY_BACKEND


def test_reading_a_run_that_was_never_written_is_none(store):
    assert read_training_run(store, "run-404") is None


def test_the_record_sits_at_the_top_of_the_run_dir_where_trace_can_see_it(store):
    path = write_training_run(store, _run())
    assert path == training_run_file("run-1")
    assert path in store.list_products(run_dir("run-1"))
    env = store.read_product(path)
    assert (env.type, env.id, env.links) == ("training_run", "trainrun-run-1", ["run-1"])


def test_trace_reports_metric_streams_through_the_expected_key(store, tmp_path):
    present = tmp_path / "metrics.jsonl"
    present.write_text("{}\n")
    refs = [
        BigRef(present.as_uri(), "d" * 64, 3),
        BigRef((tmp_path / "gone.jsonl").as_uri(), "e" * 64, 3),
    ]
    write_training_run(store, _run(metric_stream_refs=refs))
    out = trace(store, "run-1")
    assert "training_run" in out
    assert f"metric_ref [ok] {present.as_uri()}" in out
    assert "metric_ref [unreachable]" in out


def test_rewriting_identical_content_adds_no_commit(store, tmp_path):
    write_training_run(store, _run(checkpoint_step=2))
    before = _commits(tmp_path)
    write_training_run(store, _run(checkpoint_step=2))
    assert _commits(tmp_path) == before


def test_progress_updates_while_pending_are_allowed(store):
    write_training_run(store, _run(checkpoint_step=0))
    write_training_run(store, _run(checkpoint_step=3, last_checkpoint_ref=_ref("a")))
    assert read_training_run(store, "run-1").checkpoint_step == 3


def test_pending_can_become_executed_or_failed(store):
    write_training_run(store, _run())
    write_training_run(store, _run(status="executed"))
    assert read_training_run(store, "run-1").status == "executed"
    write_training_run(store, _run(run_id="run-2"))
    write_training_run(store, _run(run_id="run-2", status="failed", failure={"category": "T2", "point": "grouping", "detail": "x"}))
    assert read_training_run(store, "run-2").failure["category"] == "T2"


@pytest.mark.parametrize("final,then", [("executed", "pending"), ("executed", "failed"), ("failed", "executed"), ("failed", "pending")])
def test_terminal_states_cannot_be_reopened_or_swapped(store, final, then):
    failure = {"category": "T2"}
    write_training_run(store, _run())
    write_training_run(store, _run(status=final, failure=failure if final == "failed" else None))
    with pytest.raises(InvalidTransitionError):
        write_training_run(store, _run(status=then, failure=failure if then == "failed" else None))


def test_a_failed_run_must_say_why(store):
    write_training_run(store, _run())
    with pytest.raises(ValueError, match="failure"):
        write_training_run(store, _run(status="failed"))
    with pytest.raises(ValueError, match="failure"):
        write_training_run(store, _run(status="failed", failure={"detail": "no category"}))


# ---------------- 终审修复 C2:终态记录不可被不同内容悄悄改写 ----------------

_FAILED = dict(status="failed", failure={"category": "T2", "point": "step 1", "detail": "A"})
_EXECUTED = dict(status="executed", curve_summary={"final_mean_reward": 0.5})


@pytest.mark.parametrize("terminal", [_FAILED, _EXECUTED], ids=["failed", "executed"])
def test_a_finished_record_accepts_an_identical_replay_without_a_new_commit(store, tmp_path, terminal):
    write_training_run(store, _run(**terminal))
    before = _commits(tmp_path)
    write_training_run(store, _run(**terminal))
    assert _commits(tmp_path) == before


@pytest.mark.parametrize(
    "terminal,changed",
    [
        (_FAILED, dict(_FAILED, failure={"category": "T2", "point": "step 1", "detail": "B"})),
        (_EXECUTED, dict(_EXECUTED, curve_summary={"final_mean_reward": 0.9})),
    ],
    ids=["failed", "executed"],
)
def test_a_finished_record_cannot_be_rewritten_with_different_content(store, terminal, changed):
    write_training_run(store, _run(**terminal))
    with pytest.raises(ValueError, match="finished record"):
        write_training_run(store, _run(**changed))
    assert read_training_run(store, "run-1") == _run(**terminal)


@pytest.mark.parametrize("key", ["last_checkpoint_ref", "metric_stream_refs"])
def test_a_record_with_an_unusable_ref_needs_a_human(store, key):
    """记录在而读不出(手改进树的坏引用,I8):NeedsHumanError 在派发器的确定性元组里,指令 failed;
    InvalidBigRefError 不在,会让指令保持 pending、每轮重派。"""
    write_training_run(store, _run(last_checkpoint_ref=_ref("a"), metric_stream_refs=[_ref("b")]))
    env = store.read_product(training_run_file("run-1"))
    bad = {**_ref("a").to_dict(), "sha256": "../../etc"}
    env.payload[key] = [bad] if key == "metric_stream_refs" else bad
    store.write_product(training_run_file("run-1"), env, "human: spoil a ref")
    with pytest.raises(NeedsHumanError, match="run-1"):
        read_training_run(store, "run-1")
