from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore


def _env(pid: str, status: str) -> Envelope:
    return Envelope(
        id=pid,
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status=status,
        schema_version=1,
    )


def test_init_creates_subdirs(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    assert (tmp_path / "control-plane" / "runs").is_dir()
    assert (tmp_path / ".git").is_dir()


def test_write_then_read_round_trip(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    env = _env("run-001", "pending")
    store.write_product("control-plane/runs/run-001/directive.yaml", env, "add directive")
    got = store.read_product("control-plane/runs/run-001/directive.yaml")
    assert got == env


def test_write_commits(tmp_path):
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    store.write_product("control-plane/runs/run-001/directive.yaml", _env("run-001", "pending"), "add directive")
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "add directive" in log


def test_write_product_idempotent_on_identical_rewrite(tmp_path):
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    env = _env("run-001", "pending")
    store.write_product("control-plane/runs/run-001/directive.yaml", env, "add directive")
    count_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # 崩溃恢复重放：内容逐字节相同的重复写入不应抛出异常
    store.write_product("control-plane/runs/run-001/directive.yaml", env, "add directive")

    count_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert count_after == count_before  # 未产生空提交


def test_list_products(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product("control-plane/runs/run-001/directive.yaml", _env("run-001", "pending"), "m1")
    store.write_product("control-plane/runs/run-001/outcome.yaml", _env("run-001", "pending"), "m2")
    found = store.list_products("control-plane/runs/run-001")
    assert set(found) == {
        "control-plane/runs/run-001/directive.yaml",
        "control-plane/runs/run-001/outcome.yaml",
    }
