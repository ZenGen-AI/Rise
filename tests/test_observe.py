from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from controlplane.observe import trace, cost, sum_costs


def _seed_run(store: GitStore, run_id: str) -> None:
    directive = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", directive, "seed directive")
    run_trace = Envelope(
        id=f"trace-{run_id}", type="run_trace", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        links=[run_id],
        payload={
            "wall_clock_s": 120,
            "log_refs": [{"uri": "file:///compute/logs/run-001/task-1.log", "sha256": "a" * 64, "size_bytes": 10, "fetch_hint": None}],
            "cost_records": [
                {"run_id": run_id, "task_id": "task-1", "cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
                {"run_id": run_id, "task_id": "task-2", "cost_type": "external_inference", "gpu_hours": 0.5, "tokens": 3200},
            ],
        },
    )
    store.write_product(f"{run_dir(run_id)}/run_trace.yaml", run_trace, "seed run trace")


def test_sum_costs_is_pure_aggregation():
    records = [
        {"cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
        {"cost_type": "gpu", "gpu_hours": 0.5, "tokens": 0},
        {"cost_type": "external_inference", "gpu_hours": 0.0, "tokens": 3200},
    ]
    assert sum_costs(records) == {"gpu_hours": 2.0, "tokens": 3200}


def test_trace_lists_products_and_links(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = trace(store, "run-001")
    assert "directive" in out
    assert "executed" in out
    assert "run_trace" in out


def test_trace_reports_unreachable_reference(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = trace(store, "run-001")
    # 引用指向计算层文件，本地不可达时必须明说，而非静默省略
    assert "unreachable" in out
    assert "file:///compute/logs/run-001/task-1.log" in out


def test_cost_prints_detail_and_summary(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    _seed_run(store, "run-001")
    out = cost(store, "run-001")
    assert "task-1" in out
    assert "1.5" in out
    assert "3200" in out
    assert "gpu_hours=2.0" in out


def test_cost_on_run_without_trace(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    out = cost(store, "run-404")
    assert "no run_trace" in out
