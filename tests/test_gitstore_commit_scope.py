"""write_product 的提交只带本文件(控制面 CP-12):别的已暂存文件不被卷进提交,也不丢。"""
import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore

A = "control-plane/runs/run-001/directive.yaml"
B = "control-plane/runs/run-002/directive.yaml"


def _env(pid, status="pending"):
    return Envelope(
        id=pid, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
    )


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    s.write_product(A, _env("run-001"), "add run-001")
    return s


def _stage_something_else(store, how):
    """把"别的文件"放进暂存区,返回它的路径。"""
    root = store.repo_root
    if how == "new_file":
        (root / "notes.txt").write_text("hello\n", encoding="utf-8")
        store._git("add", "notes.txt")
        return "notes.txt"
    if how == "modified_tracked":
        (root / A).write_text((root / A).read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
        store._git("add", A)
        return A
    if how == "deleted_tracked":
        store._git("rm", "-q", A)
        return A
    raise AssertionError(how)


def _staged(store):
    return store._git("diff", "--cached", "--name-only").split()


def _commit_count(store):
    return int(store._git("rev-list", "--count", "HEAD"))


def test_the_first_commit_in_a_fresh_repo_still_works(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    s.write_product(A, _env("run-001"), "add run-001")
    assert _commit_count(s) == 1
    assert s._git("show", "--name-only", "--format=", "HEAD").split() == [A]


@pytest.mark.parametrize("how", ["new_file", "modified_tracked", "deleted_tracked"])
def test_another_staged_file_is_not_swept_into_the_commit(store, how):
    other = _stage_something_else(store, how)
    store.write_product(B, _env("run-002"), "add run-002")
    assert store._git("show", "--name-only", "--format=", "HEAD").split() == [B]
    assert store._git("log", "-1", "--format=%s").strip() == "add run-002"
    assert _staged(store) == [other]


@pytest.mark.parametrize("other_staged", [False, True])
def test_identical_content_still_does_not_commit(store, other_staged):
    if other_staged:
        _stage_something_else(store, "new_file")
    before = _commit_count(store)
    store.write_product(A, _env("run-001"), "same again")
    assert _commit_count(store) == before
    assert _staged(store) == (["notes.txt"] if other_staged else [])
