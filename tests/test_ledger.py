import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost, sum_costs, trace
from controlplane.paths import failure_record_file, run_dir, run_trace_file


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _records(store, run_id):
    return store.read_product(run_trace_file(run_id)).payload["cost_records"]


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


def _commits(store):
    # 新建的 store 还没有提交,rev-parse HEAD 会失败;用提交数比较"没有新提交"
    return store._git("rev-list", "--all", "--count").strip()


def test_the_first_cost_creates_the_run_trace(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=1200, calls=1)
    env = store.read_product(run_trace_file("run-1"))
    assert (env.id, env.type, env.status, env.links) == ("trace-run-1", "run_trace", "executed", ["run-1"])
    [rec] = env.payload["cost_records"]
    assert {k: rec[k] for k in ("run_id", "task_id", "cost_type", "tokens", "calls")} == {
        "run_id": "run-1", "task_id": "plan-1", "cost_type": "external_inference",
        "tokens": 1200, "calls": 1,
    }
    assert isinstance(rec["recorded_at"], str) and rec["recorded_at"].endswith("Z")


def test_unknown_values_are_omitted_not_null(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", calls=1)
    [rec] = _records(store, "run-1")
    for key in ("gpu_hours", "tokens", "usd"):
        assert key not in rec
    assert sum_costs([rec]) == {"gpu_hours": 0, "tokens": 0}  # 写 null 会在这里抛 TypeError


def test_records_append_in_order(store):
    record_cost(store, "run-1", task_id="train", cost_type="gpu", gpu_hours=1.5)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    assert [(r["task_id"], r["cost_type"]) for r in _records(store, "run-1")] == [
        ("train", "gpu"), ("plan-1", "external_inference"),
    ]


def test_same_task_and_cost_type_is_recorded_once(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=10)
    head = _head(store)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=99)
    assert [r["tokens"] for r in _records(store, "run-1")] == [10]  # 先到者为准
    assert _head(store) == head  # 没有新提交


def test_same_task_with_another_cost_type_is_a_separate_record(store):
    record_cost(store, "run-1", task_id="t", cost_type="gpu", gpu_hours=1.0)
    record_cost(store, "run-1", task_id="t", cost_type="external_inference", tokens=5)
    assert len(_records(store, "run-1")) == 2


def test_other_run_trace_fields_are_preserved(store):
    seeded = Envelope(
        id="trace-run-1", type="run_trace", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, links=["run-1"],
        payload={"wall_clock_s": 120, "log_refs": [], "cost_records": [
            {"run_id": "run-1", "task_id": "task-1", "cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
        ]},
    )
    store.write_product(run_trace_file("run-1"), seeded, "seed")
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    env = store.read_product(run_trace_file("run-1"))
    assert env.status == "effective" and env.payload["wall_clock_s"] == 120
    assert [r["task_id"] for r in env.payload["cost_records"]] == ["task-1", "plan-1"]


def test_observe_cost_prints_and_sums_the_written_records(store):
    record_cost(store, "run-1", task_id="train", cost_type="gpu", gpu_hours=1.5)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    record_cost(store, "run-1", task_id="plan-2", cost_type="external_inference", calls=2)
    out = cost(store, "run-1")
    assert "train      gpu                  gpu_hours=1.5 tokens=0" in out
    assert "plan-1     external_inference   gpu_hours=0.0 tokens=3200" in out
    assert "plan-2     external_inference   gpu_hours=0.0 tokens=0" in out
    assert out.endswith("total: gpu_hours=1.5 tokens=3200")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_id": "", "cost_type": "gpu"},
        {"task_id": "a/b", "cost_type": "gpu"},
        {"task_id": "t", "cost_type": ""},
        {"task_id": "t", "cost_type": "gpu", "gpu_hours": -1.0},
        {"task_id": "t", "cost_type": "gpu", "tokens": True},
        {"task_id": "t", "cost_type": "gpu", "calls": 1.5},
        {"task_id": "t", "cost_type": "gpu", "gpu_hours": float("nan")},
        {"task_id": "t", "cost_type": "gpu", "usd": float("inf")},
    ],
)
def test_bad_cost_input_is_rejected_before_anything_is_written(store, kwargs):
    with pytest.raises(ValueError):
        record_cost(store, "run-1", **kwargs)
    assert not store.exists(run_trace_file("run-1"))


def test_record_failure_writes_the_product_and_returns_its_path(store):
    ref = BigRef(uri="file:///nowhere/plan-1.log", sha256="b" * 64, size_bytes=7)
    path = record_failure(
        store, "run-1", task_id="plan-1", category="T3", point="trainscheduler.plan",
        retries=2, reason="planner output unparsable after 3 attempts", log_ref=ref,
    )
    assert path == failure_record_file("run-1", "plan-1")
    env = store.read_product(path)
    assert (env.id, env.type, env.status, env.links) == (
        "failure-run-1-plan-1", "failure_record", "executed", ["run-1"],
    )
    p = dict(env.payload)
    assert isinstance(p.pop("recorded_at"), str)
    assert p == {
        "run_id": "run-1", "task_id": "plan-1", "category": "T3",
        "point": "trainscheduler.plan", "retries": 2,
        "reason": "planner output unparsable after 3 attempts",
        "log_refs": [ref.to_dict()],
    }


def test_record_failure_without_a_log_has_an_empty_log_ref_list(store):
    path = record_failure(store, "run-1", task_id="t", category="T2", point="p", retries=0, reason="r")
    assert store.read_product(path).payload["log_refs"] == []


def test_trace_shows_the_failure_and_its_log_reachability(store):
    ref = BigRef(uri="file:///nowhere/plan-1.log", sha256="b" * 64, size_bytes=7)
    record_failure(store, "run-1", task_id="plan-1", category="T3", point="p", retries=1, reason="r", log_ref=ref)
    out = trace(store, "run-1")
    assert "failure_record" in out
    assert "log_ref [unreachable] file:///nowhere/plan-1.log" in out


def test_record_failure_is_write_once(store):
    first = record_failure(store, "run-1", task_id="t", category="T2", point="p", retries=0, reason="first")
    head = _head(store)
    second = record_failure(store, "run-1", task_id="t", category="T3", point="p", retries=5, reason="second")
    assert first == second
    assert store.read_product(first).payload["reason"] == "first"
    assert _head(store) == head


@pytest.mark.parametrize(
    "kwargs",
    [
        {"category": "T4", "retries": 0, "task_id": "t"},
        {"category": "T1", "retries": -1, "task_id": "t"},
        {"category": "T1", "retries": 0, "task_id": "x/y"},
        {"category": "T1", "retries": 0, "task_id": ".."},
    ],
)
def test_bad_failure_input_is_rejected(store, kwargs):
    before = _commits(store)
    with pytest.raises(ValueError):
        record_failure(store, "run-1", point="p", reason="r", **kwargs)
    assert _commits(store) == before
    assert not (store.repo_root / run_dir("run-1")).exists()


_BAD_RUN_IDS = ["", "../x", "a/b", ".", ".."]


def _runs_tree(store):
    return sorted(str(p.relative_to(store.repo_root)) for p in (store.repo_root / "control-plane").rglob("*"))


@pytest.mark.parametrize("run_id", _BAD_RUN_IDS)
def test_record_cost_rejects_a_run_id_that_is_not_one_path_segment(store, run_id):
    commits, before = _commits(store), _runs_tree(store)
    with pytest.raises(ValueError, match="run_id"):
        record_cost(store, run_id, task_id="t", cost_type="gpu", gpu_hours=1.0)
    assert _commits(store) == commits and _runs_tree(store) == before


@pytest.mark.parametrize("run_id", _BAD_RUN_IDS)
def test_record_failure_rejects_a_run_id_that_is_not_one_path_segment(store, run_id):
    commits, before = _commits(store), _runs_tree(store)
    with pytest.raises(ValueError, match="run_id"):
        record_failure(store, run_id, task_id="t", category="T2", point="p", retries=0, reason="r")
    assert _commits(store) == commits and _runs_tree(store) == before
