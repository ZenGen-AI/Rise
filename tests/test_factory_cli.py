"""命令行的只读子命令:输出与 controlplane.observe 相同,退出码表达缺失,且从不提交。"""
import hashlib
import subprocess

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost as observe_cost, trace as observe_trace
from controlplane.paths import run_dir
from factory.cli import main
from factory_fixtures import held_lock

RUN = "run-001"


def _snapshot(store):
    def git(*a):
        return subprocess.run(["git", "-C", str(store.repo_root), *a],
                              capture_output=True, text=True, check=True).stdout
    return git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=all")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    env = Envelope(id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="executed", schema_version=1,
                   payload={"target_dimension": "tom", "recipe_ref": ""})
    s.write_product(f"{run_dir(RUN)}/directive.yaml", env, "seed directive")
    record_cost(s, RUN, task_id="train", cost_type="gpu", gpu_hours=1.5)
    return s


def test_trace_prints_what_observe_prints(store, capsys):
    assert main(["trace", str(store.repo_root), RUN]) == 0
    assert capsys.readouterr().out == observe_trace(store, RUN) + "\n"


def test_cost_prints_what_observe_prints(store, capsys):
    assert main(["cost", str(store.repo_root), RUN]) == 0
    assert capsys.readouterr().out == observe_cost(store, RUN) + "\n"


def test_trace_of_a_missing_run_exits_1(store, capsys):
    assert main(["trace", str(store.repo_root), "run-404"]) == 1
    assert capsys.readouterr().out == "run run-404: no products found\n"


def test_cost_without_a_run_trace_exits_0(store, capsys):
    assert main(["cost", str(store.repo_root), "run-404"]) == 0
    assert capsys.readouterr().out == "run run-404: no run_trace (nothing recorded yet)\n"


def test_logs_with_task_filter(store, tmp_path, capsys):
    path = tmp_path / "plan-1-1.log"
    path.write_bytes(b"A-body\n")
    ref = BigRef(uri=f"file://{path}", sha256=hashlib.sha256(b"A-body\n").hexdigest(), size_bytes=7)
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=ref)
    assert main(["logs", str(store.repo_root), RUN, "--task", "plan-1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"run {RUN} logs for task plan-1\n") and "A-body" in out


def test_logs_exit_1_when_a_reference_is_unreachable(store, capsys):
    ref = BigRef(uri="file:///compute/logs/x-1.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    assert main(["logs", str(store.repo_root), RUN]) == 1
    assert "[unreachable] file:///compute/logs/x-1.log" in capsys.readouterr().out


def test_the_read_only_commands_never_commit(store, capsys):
    ref = BigRef(uri="file:///nowhere.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    before = _snapshot(store)
    for argv in (["trace", RUN], ["cost", RUN], ["logs", RUN], ["logs", RUN, "--task", "x"],
                 ["trace", "run-404"], ["cost", "run-404"], ["logs", "run-404"]):
        main([argv[0], str(store.repo_root), *argv[1:]])
    capsys.readouterr()
    assert _snapshot(store) == before


def test_entry_script_runs_trace(store):
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_factory.py"
    out = subprocess.run([sys.executable, str(script), "trace", str(store.repo_root), RUN],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == observe_trace(store, RUN) + "\n"


def test_install_defaults_exits_2_while_the_repository_is_locked_and_writes_nothing(store, capsys):
    """install-defaults 一次写十几个提交,与运行中的守护进程会争 index.lock:它和 run、reconcile
    一样是写者,另一个进程持锁时以退出码 2 退出,树里不留任何东西。"""
    before = _snapshot(store)
    with held_lock(store.repo_root):
        code = main(["install-defaults", str(store.repo_root)])
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: repository is locked by another process: ")
    assert _snapshot(store) == before


def test_install_defaults_releases_the_lock_when_it_finishes(store, capsys):
    """装完就放锁:紧接着的写者能拿到锁。"""
    assert main(["install-defaults", str(store.repo_root)]) == 0
    capsys.readouterr()
    with held_lock(store.repo_root):     # 能进来说明锁已经放掉
        pass


def test_the_read_only_commands_work_while_the_repository_is_locked(store, capsys):
    """只读子命令不取锁(设计 CP-12):另一个进程持锁时照常输出,不退出码 2,也不提交。"""
    before = _snapshot(store)
    with held_lock(store.repo_root):
        assert main(["trace", str(store.repo_root), RUN]) == 0
        assert capsys.readouterr().out == observe_trace(store, RUN) + "\n"
        assert main(["cost", str(store.repo_root), RUN]) == 0
        assert capsys.readouterr().out == observe_cost(store, RUN) + "\n"
        assert main(["logs", str(store.repo_root), RUN]) != 2
    assert "locked" not in capsys.readouterr().err
    assert _snapshot(store) == before
