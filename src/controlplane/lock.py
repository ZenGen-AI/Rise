"""仓库文件锁(控制面 CP-12):同一棵 git 树同一时刻只许一个守护进程写。

锁文件是 <repo_root>/.git/rismforge.lock:放在 .git 下,git 不追踪它,也不会出现在 status 里。
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


@contextmanager
def acquire_repo_lock(repo_root: str | Path) -> Iterator[None]:
    """取仓库写锁;被占用时立刻抛 RepoLockedError(不等待),退出上下文时释放。"""
    lock_path = Path(repo_root).absolute() / ".git" / "rismforge.lock"
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
