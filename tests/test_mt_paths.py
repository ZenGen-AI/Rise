from controlplane.daemon import read_tree_state
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file, run_dir, training_run_file,
)


def _env(id_, type_, status):
    return Envelope(
        id=id_, type=type_, version=1, created_at="2026-10-03T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
        links=["run-1"], payload={},
    )


def test_training_run_file_is_at_the_top_level_of_the_run_dir():
    path = training_run_file("run-1")
    assert path == "control-plane/runs/run-1/training_run.yaml"
    # 顶层 = 比 run_dir 多恰好一段;list_products 不递归,放深了 trace 就看不到
    assert path.count("/") == run_dir("run-1").count("/") + 1


def test_candidate_checkpoint_file_matches_the_path_the_daemon_reads(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product(
        candidate_checkpoint_file("run-1"),
        _env("cand-run-1", "candidate_checkpoint", "pending_review"),
        "seed candidate",
    )
    views = read_tree_state(store).candidates
    assert [(c.id, c.run_id, c.status) for c in views] == [
        ("cand-run-1", "run-1", "pending_review")
    ]


def test_training_run_is_listed_by_list_products(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product(
        training_run_file("run-1"),
        _env("training-run-1", "training_run", "pending"),
        "seed training run",
    )
    assert training_run_file("run-1") in store.list_products(run_dir("run-1"))
