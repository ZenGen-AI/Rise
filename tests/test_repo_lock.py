"""仓库文件锁(控制面 CP-12):同一棵树同一时刻只许一个守护进程写。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import controlplane
from controlplane.daemon import apply_action, reconcile_once, run_daemon, stub_dispatcher
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.lock import RepoLockedError, acquire_repo_lock
from controlplane.paths import candidate_checkpoint_file, run_dir
from controlplane.reconcile import Action

DIRECTIVE = f"{run_dir('run-001')}/directive.yaml"


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _lock_path(root):
    return str(Path(root).absolute() / ".git" / "rismforge.lock")


def _put_directive(store):
    env = Envelope(
        id="run-001", type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(DIRECTIVE, env, "human: write directive")


@pytest.mark.parametrize("as_type", [str, Path])
def test_a_second_acquire_fails_and_the_message_is_the_lock_file_path(store, as_type):
    root = as_type(store.repo_root)
    with acquire_repo_lock(root):
        with pytest.raises(RepoLockedError) as exc:
            with acquire_repo_lock(root):
                pytest.fail("the second acquire must not succeed")
    assert str(exc.value) == _lock_path(store.repo_root)


def test_a_relative_repo_root_is_reported_as_an_absolute_lock_path(store, monkeypatch):
    monkeypatch.chdir(store.repo_root)
    with acquire_repo_lock("."):
        with pytest.raises(RepoLockedError) as exc:
            with acquire_repo_lock("."):
                pytest.fail("the second acquire must not succeed")
    message = str(exc.value)
    assert os.path.isabs(message)
    assert Path(message).samefile(store.repo_root / ".git" / "rismforge.lock")


@pytest.mark.parametrize("body_raises", [False, True])
def test_the_lock_is_released_when_the_with_block_exits(store, body_raises):
    if body_raises:
        with pytest.raises(ValueError):
            with acquire_repo_lock(store.repo_root):
                raise ValueError("boom")
    else:
        with acquire_repo_lock(store.repo_root):
            pass
    with acquire_repo_lock(store.repo_root):
        pass


def test_the_lock_file_lives_under_dot_git_and_git_does_not_track_it(store):
    _put_directive(store)
    with acquire_repo_lock(store.repo_root):
        assert (store.repo_root / ".git" / "rismforge.lock").is_file()
        assert "rismforge.lock" not in store._git("ls-files")
        assert store._git("status", "--porcelain") == ""


def _worktree(store, tmp_path):
    """给 store 加一个 git worktree:它的 .git 是一个文件,内容是 `gitdir: <路径>`。"""
    _put_directive(store)                       # worktree add 需要至少一个提交
    wt = tmp_path.parent / f"{tmp_path.name}-wt"
    subprocess.run(["git", "-C", str(store.repo_root), "worktree", "add", "-q", "--detach", str(wt)],
                   check=True, capture_output=True)
    assert (wt / ".git").is_file()
    return wt


def test_a_worktree_takes_its_lock_in_its_own_git_directory(store, tmp_path):
    """worktree 的 .git 是文件,不能当目录拼路径(原先会抛 NotADirectoryError)。锁放进这个
    工作树自己的 git 目录,与 git 的 index.lock 同一粒度:同一工作树互斥,两个工作树互不干扰。"""
    wt = _worktree(store, tmp_path)
    with acquire_repo_lock(wt):
        gitdir = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                                     check=True, capture_output=True, text=True).stdout.strip())
        assert (gitdir / "rismforge.lock").exists()
        with pytest.raises(RepoLockedError, match="rismforge.lock"):
            with acquire_repo_lock(wt):
                pass
        with acquire_repo_lock(store.repo_root):     # 主工作树是另一棵树,照常能取
            pass


def test_a_relative_gitdir_in_the_dot_git_file_is_resolved_against_it(store, tmp_path):
    """`gitdir:` 后面也可以是相对路径(git worktree repair 之后、或手写):相对 .git 文件所在目录。"""
    wt = _worktree(store, tmp_path)
    absolute = Path((wt / ".git").read_text().split(":", 1)[1].strip())
    (wt / ".git").write_text(f"gitdir: {os.path.relpath(absolute, wt)}\n")
    with acquire_repo_lock(wt):
        assert (absolute / "rismforge.lock").exists()


def test_run_daemon_refuses_to_start_while_the_lock_is_held_and_writes_nothing(store):
    _put_directive(store)
    commits = store._git("rev-list", "--count", "HEAD")
    with acquire_repo_lock(store.repo_root):
        with pytest.raises(RepoLockedError) as exc:
            run_daemon(store, 0, max_ticks=3)
    assert str(exc.value) == _lock_path(store.repo_root)
    assert store.read_product(DIRECTIVE).status == "pending"
    assert not store.exists(candidate_checkpoint_file("run-001"))
    assert store._git("rev-list", "--count", "HEAD") == commits
    assert store._git("status", "--porcelain") == ""


@pytest.mark.parametrize("tick_raises", [False, True])
def test_run_daemon_holds_the_lock_while_ticking_and_releases_it_afterwards(store, tick_raises):
    _put_directive(store)
    seen = []

    def dispatcher(s, directive):
        try:
            with acquire_repo_lock(s.repo_root):
                seen.append("acquired")
        except RepoLockedError:
            seen.append("locked")
        if tick_raises:
            raise RuntimeError("tick failed")
        stub_dispatcher(s, directive)

    if tick_raises:
        with pytest.raises(RuntimeError, match="tick failed"):
            run_daemon(store, 0, max_ticks=1, dispatcher=dispatcher)
    else:
        run_daemon(store, 0, max_ticks=1, dispatcher=dispatcher)
    assert seen == ["locked"]
    with acquire_repo_lock(store.repo_root):
        pass


def test_on_locked_runs_once_while_the_lock_is_held_and_before_the_first_tick(store):
    _put_directive(store)
    seen = []

    def on_locked():
        try:
            with acquire_repo_lock(store.repo_root):
                seen.append("free")
        except RepoLockedError:
            seen.append("locked")

    def dispatcher(s, directive):
        seen.append("tick")
        stub_dispatcher(s, directive)

    run_daemon(store, 0, max_ticks=2, dispatcher=dispatcher, on_locked=on_locked)
    assert seen[0] == "locked" and seen.count("locked") == 1
    assert "tick" in seen[1:]


def test_on_locked_is_not_called_when_the_lock_is_taken(store):
    called = []
    with acquire_repo_lock(store.repo_root):
        with pytest.raises(RepoLockedError):
            run_daemon(store, 0, max_ticks=1, on_locked=lambda: called.append(1))
    assert called == []


def test_an_on_locked_failure_stops_the_daemon_before_any_tick_and_releases_the_lock(store):
    _put_directive(store)
    commits = store._git("rev-list", "--count", "HEAD")

    def boom():
        raise OSError("sweep failed")

    with pytest.raises(OSError, match="sweep failed"):
        run_daemon(store, 0, max_ticks=1, on_locked=boom)
    assert store.read_product(DIRECTIVE).status == "pending"
    assert store._git("rev-list", "--count", "HEAD") == commits
    with acquire_repo_lock(store.repo_root):
        pass


@pytest.mark.parametrize("entry", ["reconcile_once", "apply_action"])
def test_reconcile_once_and_apply_action_do_not_take_the_lock(store, entry):
    _put_directive(store)
    with acquire_repo_lock(store.repo_root):
        if entry == "reconcile_once":
            assert [a.kind for a in reconcile_once(store)] == ["dispatch_directive"]
        else:
            apply_action(store, Action(kind="dispatch_directive", target_id="run-001", detail={}))
    assert store.read_product(DIRECTIVE).status == "executed"


_HOLDER = (
    "import sys\n"
    "from controlplane.lock import acquire_repo_lock\n"
    "with acquire_repo_lock(sys.argv[1]):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.read()\n"
)


def test_a_lock_held_by_another_process_blocks_us_and_dies_with_that_process(store):
    src = str(Path(controlplane.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    with subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(store.repo_root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    ) as proc:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(RepoLockedError):
            with acquire_repo_lock(store.repo_root):
                pytest.fail("the other process still holds the lock")
        proc.kill()  # 持锁进程崩溃:内核随进程收回 flock,不留死锁
        proc.wait()
    with acquire_repo_lock(store.repo_root):
        pass
