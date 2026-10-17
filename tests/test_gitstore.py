import pytest

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


def test_write_product_commits_a_file_that_is_already_on_disk_but_untracked(tmp_path):
    """崩溃恢复重放会留下"已落盘、未进索引"的半程状态:写入要认出它并提交,而不是
    把它当成已跟踪文件去 commit(那会以 pathspec 错失败)。"""
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    rel = "control-plane/runs/run-001/directive.yaml"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("half: written\n", encoding="utf-8")  # 直接落盘,不经 git

    store.write_product(rel, _env("run-001", "pending"), "recover")

    assert subprocess.run(["git", "-C", str(tmp_path), "ls-files", rel],
                          capture_output=True, text=True, check=True).stdout.strip() == rel
    assert store.read_product(rel) == _env("run-001", "pending")


def test_write_product_does_not_sweep_in_other_staged_files(tmp_path):
    """控制面 CP-12:只提交本文件。别的文件已在暂存区时,不被卷进这次提交,也不丢。"""
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    rel = "control-plane/runs/run-001/directive.yaml"
    store.write_product(rel, _env("run-001", "pending"), "first")

    other = tmp_path / "control-plane" / "runs" / "run-001" / "unrelated.yaml"
    other.write_text("x: 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", str(other.relative_to(tmp_path))],
                   check=True, capture_output=True)

    store.write_product(rel, _env("run-001", "executed"), "second")

    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    committed = subprocess.run(["git", "-C", str(tmp_path), "show", "--name-only", "--format=", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.split()
    assert staged == ["control-plane/runs/run-001/unrelated.yaml"]  # 还在暂存区,没丢
    assert committed == [rel]                                        # 没被卷进来


def test_write_product_raises_on_a_real_git_failure(tmp_path):
    """幂等的 "nothing to commit" 是 no-op,但真故障仍要抛:否则写不进去也没人知道。"""
    import subprocess

    root = tmp_path / "cp"
    store = GitStore(str(root))
    store.init()
    with pytest.raises(subprocess.CalledProcessError):
        store.write_product("../outside.yaml", _env("run-001", "pending"), "nope")


@pytest.mark.parametrize("other_staged", [False, True])
def test_write_product_is_idempotent_on_identical_rewrite(tmp_path, other_staged):
    """崩溃恢复重放:逐字节相同的重复写入不得抛,也不得产生空提交。

    `other_staged` 这一维是必要的:`commit -- <path>` 做的是部分提交(按 HEAD 加 pathspec
    造临时索引),所以暂存区里有别的文件时,git 的提示文本会变,靠文本分辨 no-op 的实现
    会在这一格翻车。参见 test_gitstore_commit_scope.py 的同名维度。
    """
    import subprocess

    store = GitStore(str(tmp_path))
    store.init()
    rel = "control-plane/runs/run-001/directive.yaml"
    env = _env("run-001", "pending")
    store.write_product(rel, env, "add directive")

    if other_staged:
        (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
        store._git("add", "notes.txt")

    def count():
        return subprocess.run(["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    before = count()
    store.write_product(rel, env, "add directive")  # 不得抛
    assert count() == before                        # 未产生空提交
    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert staged == (["notes.txt"] if other_staged else [])  # 别人的暂存没被动


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
