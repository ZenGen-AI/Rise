"""ROLL4ZenGen 的来源:本仓库的 git submodule `third_party/ROLL4ZenGen`,钉在一个提交上。

版本以 rise 仓库 HEAD 里的 gitlink 为准,不以 submodule 的工作树为准:工作树可以被人切走,
gitlink 只能随 rise 的提交改,所以"这次训练用的是哪个 ROLL"跟着 rise 的代码版本走。
打包前核对工作树的 HEAD 等于 gitlink 且没有改动,否则部署上去的就不是记录里写的那个版本。

只打包训练与导出用得到的路径(ROLL_PATHS):训练入口只 import `roll.*` 与 `scripts.qwen38.*`,
导出只 import `mcore_adapter`。`data/` 104 MB、文档与图片 30 MB,远端都用不到。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from compute.errors import ComputeError

ROLL_SUBMODULE = "third_party/ROLL4ZenGen"
ROLL_PATHS = ("roll", "mcore_adapter", "scripts", "setup.py", "pyproject.toml")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_INIT_HINT = f"run `git submodule update --init {ROLL_SUBMODULE}` in the rise checkout"


def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, check=False)
    except OSError:
        raise ComputeError("git is not available to read the ROLL submodule") from None


def pinned_roll_commit(repo_root: Path) -> str:
    """rise HEAD 里 ROLL submodule 的 gitlink(40 位提交)。不是 submodule 或读不到就抛 ComputeError。

    读的是 HEAD 而不是暂存区:暂存了没提交的 gitlink 不算数,记进训练记录的版本必须能从 rise 的提交里找回来。"""
    out = _git("-C", str(repo_root), "ls-tree", "HEAD", ROLL_SUBMODULE)
    fields = out.stdout.decode("utf-8", "replace").split()
    # ls-tree 的一行是 "<mode> <type> <object>\t<path>";submodule 的 type 是 commit
    if out.returncode != 0 or len(fields) < 3 or fields[1] != "commit" or not _COMMIT.match(fields[2]):
        raise ComputeError(f"{ROLL_SUBMODULE} is not a submodule in the rise HEAD")
    return fields[2]


def roll_archive(repo_root: Path, commit: str) -> bytes:
    """钉住提交的 tar 包,只含 ROLL_PATHS。工作树没初始化、HEAD 不符或有改动时抛 ComputeError。"""
    sub = Path(repo_root) / ROLL_SUBMODULE
    head = _git("-C", str(sub), "rev-parse", "HEAD")
    actual = head.stdout.decode("utf-8", "replace").strip()
    # 没初始化时 sub 是空目录,rev-parse 会落到 rise 自己的仓库上,返回 rise 的 HEAD;所以只认等于钉住的值
    if head.returncode != 0 or actual != commit:
        raise ComputeError(f"the {ROLL_SUBMODULE} checkout is not at the pinned commit {commit[:12]}; {_INIT_HINT}")
    status = _git("-C", str(sub), "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0 or status.stdout.strip():
        raise ComputeError(f"the {ROLL_SUBMODULE} checkout has local changes; the deployed ROLL must be the pinned commit")
    archive = _git("-C", str(sub), "archive", "--format=tar", commit, "--", *ROLL_PATHS)
    if archive.returncode != 0 or not archive.stdout:
        raise ComputeError(f"git archive of {ROLL_SUBMODULE} at {commit[:12]} failed")
    return archive.stdout
