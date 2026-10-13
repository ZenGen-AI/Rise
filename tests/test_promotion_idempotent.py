"""晋级幂等(控制面 CP-7):新版本号取 models/ 下现有最大版本加一;晋级中途崩溃后重放,复用已登记的模型。"""
import re

import pytest

from controlplane import models
from controlplane.daemon import apply_action, read_tree_state, reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    run_dir,
)
from controlplane.reconcile import CLOSED_LOOP_ACTIONS, Action, CandidateView, TreeState, reconcile


def _env(env_id, env_type, status, payload=None, links=None):
    return Envelope(
        id=env_id, type=env_type, version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
        links=links or [], payload=payload or {},
    )


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _seed_model(store, version):
    store.write_product(
        model_file(version),
        _env(f"zengen-v{version}", "zengen_model", "effective", {"promoted_from": f"cand-old-{version}"}),
        f"seed: zengen-v{version}",
    )


def _seed_approved(store, run_id):
    """一个已批准、诊断报告就位、等待晋级的候选(候选 id 为 cand-<run_id>)。"""
    store.write_product(
        f"{run_dir(run_id)}/directive.yaml",
        _env(run_id, "directive", "executed", {"target_dimension": "tom", "recipe_ref": ""}),
        f"seed: directive {run_id}",
    )
    store.write_product(
        candidate_checkpoint_file(run_id),
        _env(f"cand-{run_id}", "candidate_checkpoint", "approved",
             {"checkpoint_ref": {"uri": f"s3://ckpt/{run_id}", "sha256": "ab" * 32}}),
        f"seed: candidate {run_id}",
    )
    store.write_product(
        diagnostic_report_file(run_id),
        _env(f"diag-{run_id}", "diagnostic_report", "executed", {"backends": "mock"}),
        f"seed: diagnostic {run_id}",
    )


def _versions(store):
    found = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = re.search(r"zengen-v(\d+)\.yaml$", rel)
        if match:
            found.append(int(match.group(1)))
    return sorted(found)


def _promotions(actions):
    return [a for a in actions if a.kind == "promote_candidate"]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], 0),
        (["zengen-v1"], 1),
        (["zengen-v9", "zengen-v10"], 10),
        (["zengen-v2", "zengen-v7", "zengen-v4"], 7),
        (["notes", "zengen-v3"], 3),
        (["notes", "zengen-v-final"], 0),
    ],
)
def test_max_model_version_reads_the_version_numbers_in_the_file_names(store, names, expected):
    models_dir = store.repo_root / CONTROL_PLANE_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (models_dir / f"{name}.yaml").write_text("id: x\n", encoding="utf-8")
    assert models.max_model_version(store) == expected


def test_tree_state_max_model_version_defaults_to_none():
    assert TreeState().max_model_version is None


@pytest.mark.parametrize(
    ("model_count", "max_version", "expected"),
    [(2, None, 3), (1, 5, 6), (3, 0, 1), (0, None, 1)],
)
def test_reconcile_starts_numbering_from_the_max_version_when_it_is_known(model_count, max_version, expected):
    state = TreeState(
        candidates=[CandidateView(id="cand-run-001", status="approved", run_id="run-001", has_diagnostic=True)],
        model_count=model_count,
        max_model_version=max_version,
    )
    (action,) = reconcile(state)
    assert action.detail["model_version"] == expected


@pytest.mark.parametrize("enabled", [frozenset(), frozenset(CLOSED_LOOP_ACTIONS)])
def test_read_tree_state_fills_max_model_version_on_both_return_paths(store, enabled):
    assert read_tree_state(store, enabled=enabled).max_model_version == 0
    _seed_model(store, 2)
    _seed_model(store, 5)
    state = read_tree_state(store, enabled=enabled)
    assert (state.model_count, state.max_model_version) == (2, 5)


@pytest.mark.parametrize(
    ("existing", "expected"),
    [([], 1), ([1], 2), ([1, 2, 3], 4), ([1, 3], 4), ([2], 3), ([5], 6)],
)
def test_the_new_version_is_the_existing_maximum_plus_one(store, existing, expected):
    for version in existing:
        _seed_model(store, version)
    _seed_approved(store, "run-001")
    (action,) = _promotions(reconcile_once(store))
    assert action.detail["model_version"] == expected
    assert _versions(store) == sorted([*existing, expected])
    assert store.read_product(model_file(expected)).payload["promoted_from"] == "cand-run-001"


def test_deleting_a_middle_version_leaves_a_hole_and_never_repeats_a_number(store):
    for n in (1, 2, 3):
        _seed_approved(store, f"run-00{n}")
        reconcile_once(store)
    assert _versions(store) == [1, 2, 3]
    store._git("rm", "-q", model_file(2))
    store._git("commit", "-q", "-m", "human: drop zengen-v2")
    _seed_approved(store, "run-004")
    reconcile_once(store)
    assert _versions(store) == [1, 3, 4]
    assert store.read_product(model_file(3)).payload["promoted_from"] == "cand-run-003"
    assert store.read_product(model_file(4)).payload["promoted_from"] == "cand-run-004"


@pytest.mark.parametrize(
    ("existing", "expected"),
    [([], [1, 2]), ([1], [2, 3]), ([1, 3], [4, 5])],
)
def test_two_candidates_in_one_tick_get_different_versions(store, existing, expected):
    for version in existing:
        _seed_model(store, version)
    _seed_approved(store, "run-001")
    _seed_approved(store, "run-002")
    actions = _promotions(reconcile_once(store))
    assert [a.detail["model_version"] for a in actions] == expected
    assert _versions(store) == sorted([*existing, *expected])
    promoted = {store.read_product(model_file(v)).payload["promoted_from"] for v in expected}
    assert promoted == {"cand-run-001", "cand-run-002"}


class _CrashWhenCandidateTurnsEffective(GitStore):
    """模拟"模型已登记、候选还没置 effective"时进程崩溃:写候选 effective 这一步抛错。"""

    def write_product(self, rel_path, env, message):
        if rel_path.endswith("candidate_checkpoint.yaml") and env.status == "effective":
            raise RuntimeError("simulated crash after the model was written")
        super().write_product(rel_path, env, message)


def _crash_after_the_model_was_written(store):
    crashing = _CrashWhenCandidateTurnsEffective(str(store.repo_root))
    with pytest.raises(RuntimeError, match="simulated crash"):
        reconcile_once(crashing)


def test_replaying_a_promotion_that_crashed_after_the_model_write_reuses_that_model(store):
    _seed_approved(store, "run-001")
    _crash_after_the_model_was_written(store)
    assert _versions(store) == [1]
    assert store.read_product(candidate_checkpoint_file("run-001")).status == "approved"
    assert not store.exists(f"{run_dir('run-001')}/outcome.yaml")
    model_commits = store._git("log", "--format=%s", "--", model_file(1)).splitlines()

    reconcile_once(store)  # 重放

    assert _versions(store) == [1]
    assert store.read_product(candidate_checkpoint_file("run-001")).status == "effective"
    outcome = store.read_product(f"{run_dir('run-001')}/outcome.yaml")
    assert outcome.payload["promoted_model"] == "zengen-v1"
    assert outcome.links == ["cand-run-001", "zengen-v1"]
    assert store._git("log", "--format=%s", "--", model_file(1)).splitlines() == model_commits
    assert reconcile_once(store) == []


def test_apply_action_reuses_the_recorded_model_even_when_the_action_carries_another_version(store):
    _seed_approved(store, "run-001")
    _crash_after_the_model_was_written(store)
    stale = Action(
        kind="promote_candidate", target_id="cand-run-001",
        detail={"run_id": "run-001", "model_version": 9},
    )
    apply_action(store, stale)
    assert _versions(store) == [1]
    assert not store.exists(model_file(9))
    assert store.read_product(candidate_checkpoint_file("run-001")).status == "effective"
    outcome = store.read_product(f"{run_dir('run-001')}/outcome.yaml")
    assert outcome.payload["promoted_model"] == "zengen-v1"


class _CrashBetweenAddAndCommitOfTheModel(GitStore):
    """模拟 write_product 崩在 git add 之后、git commit 之前:模型文件留在暂存区,没有提交。"""

    def _git(self, *args):
        if args[:1] == ("commit",) and model_file(1) in args:
            raise RuntimeError("simulated crash between git add and git commit")
        return super()._git(*args)


def test_replaying_a_promotion_that_crashed_between_add_and_commit_commits_the_staged_model(store):
    _seed_approved(store, "run-001")
    with pytest.raises(RuntimeError, match="simulated crash"):
        reconcile_once(_CrashBetweenAddAndCommitOfTheModel(str(store.repo_root)))
    assert store._git("diff", "--cached", "--name-only").split() == [model_file(1)]
    assert store._git("log", "--format=%s", "--", model_file(1)) == ""

    reconcile_once(store)  # 重放

    assert _versions(store) == [1]
    assert store._git("status", "--porcelain") == ""
    assert store._git("log", "--format=%s", "--", model_file(1)).splitlines() == ["reconcile: promote zengen-v1"]
    assert store.read_product(candidate_checkpoint_file("run-001")).status == "effective"
    assert store.exists(f"{run_dir('run-001')}/outcome.yaml")
