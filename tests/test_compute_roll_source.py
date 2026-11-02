"""ROLL 来源:rise HEAD 里的 gitlink 定版本,打包前核对 submodule 工作树。全部在临时目录里建真 git 仓库。"""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from compute.errors import ComputeError
from compute.roll_source import ROLL_PATHS, ROLL_SUBMODULE, pinned_roll_commit, roll_archive

GIT_ENV = ("-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "protocol.file.allow=always")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ENV, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """一个假的 ROLL 仓库(有训练用不到的 data/ 与 docs_roll/),和一个把它挂成 submodule 的 rise 仓库。"""
    roll = tmp_path / "roll-upstream"
    for rel, body in (("roll/__init__.py", "x"), ("mcore_adapter/src/mcore_adapter/__init__.py", "y"),
                      ("scripts/qwen38/run_sft_validation.py", "z"), ("setup.py", "s"), ("pyproject.toml", "p"),
                      ("data/big.jsonl", "d" * 1000), ("docs_roll/a.md", "doc")):
        (roll / rel).parent.mkdir(parents=True, exist_ok=True)
        (roll / rel).write_text(body)
    _git(roll, "init", "-q")
    _git(roll, "add", ".")
    _git(roll, "commit", "-qm", "roll")
    commit = _git(roll, "rev-parse", "HEAD")
    rise = tmp_path / "rise"
    rise.mkdir()
    _git(rise, "init", "-q")
    _git(rise, "submodule", "add", "-q", str(roll), ROLL_SUBMODULE)
    _git(rise, "commit", "-qm", "add roll")
    return rise, roll, commit


def test_the_pinned_commit_is_the_gitlink_in_the_rise_head(repos):
    rise, _, commit = repos
    assert pinned_roll_commit(rise) == commit


def test_a_staged_but_uncommitted_gitlink_does_not_count(repos):
    """记进训练记录的版本必须能从 rise 的提交里找回来,所以只认 HEAD。"""
    rise, roll, old = repos
    (roll / "roll" / "__init__.py").write_text("changed")
    _git(roll, "commit", "-qam", "next")
    _git(rise / ROLL_SUBMODULE, "pull", "-q")
    _git(rise, "add", ROLL_SUBMODULE)
    assert pinned_roll_commit(rise) == old


def test_a_repo_without_the_submodule_is_refused(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "c")
    with pytest.raises(ComputeError, match="not a submodule"):
        pinned_roll_commit(tmp_path)


def test_the_archive_holds_only_the_paths_training_needs(repos):
    rise, _, commit = repos
    with tarfile.open(fileobj=io.BytesIO(roll_archive(rise, commit))) as tar:
        names = {m.name.rstrip("/") for m in tar.getmembers()}
    tops = {name.split("/", 1)[0] for name in names}
    assert tops == set(ROLL_PATHS)
    assert "scripts/qwen38/run_sft_validation.py" in names
    assert not any(n.startswith(("data", "docs_roll")) for n in names)


def test_an_uninitialised_submodule_is_refused_with_the_init_command(repos):
    """没初始化时 submodule 目录是空的,rev-parse 会落到 rise 自己的仓库上:不能把 rise 的 HEAD 当成 ROLL 的。"""
    rise, _, commit = repos
    _git(rise, "submodule", "deinit", "-f", "--", ROLL_SUBMODULE)
    assert not any((rise / ROLL_SUBMODULE).iterdir())
    with pytest.raises(ComputeError, match="git submodule update --init"):
        roll_archive(rise, commit)


def test_a_checkout_moved_off_the_pinned_commit_is_refused(repos):
    rise, roll, commit = repos
    (roll / "roll" / "__init__.py").write_text("changed")
    _git(roll, "commit", "-qam", "next")
    _git(rise / ROLL_SUBMODULE, "pull", "-q")
    with pytest.raises(ComputeError, match="not at the pinned commit"):
        roll_archive(rise, commit)


def test_local_changes_in_the_checkout_are_refused(repos):
    rise, _, commit = repos
    (rise / ROLL_SUBMODULE / "roll" / "__init__.py").write_text("edited in place")
    with pytest.raises(ComputeError, match="local changes"):
        roll_archive(rise, commit)
