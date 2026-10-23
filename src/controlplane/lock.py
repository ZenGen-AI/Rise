"""仓库文件锁(控制面 CP-12):同一棵 git 树同一时刻只许一个守护进程写。

锁文件放在这个工作树的 git 目录下:普通仓库是 <repo_root>/.git/rismforge.lock;worktree 与子模块的
.git 是一个写着 `gitdir: <路径>` 的文件,锁放进它指向的目录。git 不追踪它,也不会出现在 status 里。
锁按工作树隔离,与 git 的 index.lock 同一粒度。
POSIX 用 fcntl.flock(LOCK_EX | LOCK_NB),Windows 用 msvcrt.locking(Windows 未验证)。两者都是
内核级锁:持锁进程退出或崩溃,系统随进程收回,不会留下死锁。锁文件本身不删(删了会和并发的
取锁竞争)。
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - Windows 未验证
    import msvcrt

    _BUSY: type[OSError] = OSError  # msvcrt.locking 失败一律是 OSError

    def _try_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    _BUSY = BlockingIOError  # flock(LOCK_NB) 被占用时抛它;别的 OSError(如 ENOLCK)不是"被锁"

    def _try_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class RepoLockedError(Exception):
    """另一个进程已持有这棵树的写锁。str(exc) 恰好是锁文件的绝对路径。"""


def _git_dir(repo_root: Path) -> Path:
    """这个工作树的 git 目录。.git 是目录就用它;是文件(worktree、子模块)就按 git 的格式读
    `gitdir: <路径>`,相对路径相对 .git 文件所在目录。不起 git 子进程:取锁在入口处,要快且确定。"""
    dot_git = repo_root / ".git"
    if not dot_git.is_file():
        return dot_git            # 目录,或不存在(不存在时由下面的 open 报 FileNotFoundError,与原先一致)
    first = dot_git.read_text(encoding="utf-8").splitlines()[0] if dot_git.stat().st_size else ""
    prefix, sep, target = first.partition(":")
    if not sep or prefix.strip() != "gitdir" or not target.strip():
        raise FileNotFoundError(f"{dot_git} is a file but not a 'gitdir: <path>' pointer")
    return (repo_root / target.strip()).resolve()


@contextmanager
def acquire_repo_lock(repo_root: str | Path) -> Iterator[None]:
    """取仓库写锁;被占用时立刻抛 RepoLockedError(不等待),退出上下文时释放。"""
    lock_path = _git_dir(Path(repo_root).absolute()) / "rismforge.lock"
    fh = open(lock_path, "a+b")
    try:
        try:
            _try_lock(fh)
        except _BUSY as exc:
            raise RepoLockedError(str(lock_path)) from exc
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()
