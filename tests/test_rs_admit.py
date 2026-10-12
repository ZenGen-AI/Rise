import pytest

from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    failure_record_file,
    recipe_candidate_file,
    recipe_file,
    recipe_lineage_file,
    recipe_track_file,
    run_dir,
)
from controlplane.reconcile import Action
from recipestore.admit import handle_admit_recipe, has_repro_evidence
from recipestore.library import find_recipes, get_recipe, read_track_entries
from rs_fixtures import batch_payload, make_store, write_directive, write_recipe_candidate

R = "recipe-run-003"
ACTION = Action(kind="admit_recipe", target_id=R, detail={})
WRITTEN = [recipe_file("run-003"), recipe_lineage_file("run-003"), recipe_track_file("run-003"),
           recipe_candidate_file("run-003")]


def _tree(store):
    return {p: store.read_product(p).to_dict() for p in WRITTEN if store.exists(p)}


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe_candidate(s, "run-003", "approved")
    return s


def test_admitting_writes_the_recipe_lineage_and_an_empty_track_record(store):
    handle_admit_recipe(store, ACTION)
    recipe = get_recipe(store, R)
    cand = store.read_product(recipe_candidate_file("run-003"))
    assert (recipe.type, recipe.status) == ("recipe", "effective")
    assert recipe.payload == cand.payload  # 候选与配方共用同一形状
    assert recipe.links == cand.links
    lineage = store.read_product(recipe_lineage_file("run-003"))
    assert lineage.payload == {
        "recipe_id": R, "source_run_id": "run-003", "admitted_from": R,
        "events": [{"event": "admitted", "run_id": "run-003"}],
    }
    assert read_track_entries(store, R) == []
    assert cand.status == "effective"
    assert [v.id for v in find_recipes(store)] == [R]


def test_admitting_twice_changes_nothing(store):
    handle_admit_recipe(store, ACTION)
    before, commits = _tree(store), _commits(store)
    handle_admit_recipe(store, ACTION)
    assert _tree(store) == before and _commits(store) == commits


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4])
def test_a_crash_at_any_step_converges_on_rerun(tmp_path, monkeypatch, crash_at):
    clean = make_store(tmp_path / "clean")
    write_recipe_candidate(clean, "run-003", "approved")
    handle_admit_recipe(clean, ACTION)

    s = make_store(tmp_path / "crashy")
    write_recipe_candidate(s, "run-003", "approved")
    real = s.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == crash_at:
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(s, "write_product", flaky)
    with pytest.raises(OSError):
        handle_admit_recipe(s, ACTION)
    monkeypatch.setattr(s, "write_product", real)
    assert calls[-1] == WRITTEN[crash_at - 1]
    if crash_at < 4:
        assert s.read_product(recipe_candidate_file("run-003")).status == "approved"
    handle_admit_recipe(s, ACTION)
    assert _tree(s) == _tree(clean)


@pytest.mark.parametrize("status", ["pending_review", "rejected", "draft"])
def test_a_candidate_that_is_not_approved_is_left_alone(tmp_path, status):
    s = make_store(tmp_path)
    write_recipe_candidate(s, "run-003", status)
    handle_admit_recipe(s, ACTION)
    assert s.read_product(recipe_candidate_file("run-003")).status == status
    assert not s.exists(recipe_file("run-003"))


def test_an_existing_recipe_is_not_overwritten(store):
    handle_admit_recipe(store, ACTION)
    # 人为退回 approved,模拟"配方已写、候选还没置 effective"之后配方又被改过
    from recipestore.track import deprecate
    deprecate(store, R, reason="r", run_id="run-009")
    cand = store.read_product(recipe_candidate_file("run-003"))
    cand.status = "approved"
    store.write_product(recipe_candidate_file("run-003"), cand, "t")
    handle_admit_recipe(store, ACTION)
    assert get_recipe(store, R).status == "deprecated"
    assert store.read_product(recipe_candidate_file("run-003")).status == "effective"


def test_an_approved_candidate_without_repro_is_not_admitted_and_the_failure_is_recorded(tmp_path):
    s = make_store(tmp_path)
    payload = batch_payload()
    del payload["repro"]
    write_recipe_candidate(s, "run-003", "approved", payload)
    write_directive(s, "run-003")  # 来源 run 的目录在场,失败记录才写得进去
    handle_admit_recipe(s, ACTION)
    assert not s.exists(recipe_file("run-003"))
    assert s.read_product(recipe_candidate_file("run-003")).status == "approved"
    failure = s.read_product(failure_record_file("run-003", "admit_recipe"))
    assert (failure.payload["category"], failure.payload["point"]) == ("T2", "recipestore.admit")
    assert "carries no repro evidence" in failure.payload["reason"]
    commits = _commits(s)
    handle_admit_recipe(s, ACTION)  # 每轮重来也不堆提交
    assert _commits(s) == commits


@pytest.mark.parametrize("repro", [None, {}, {"errors": []}, {"errors": "x", "warnings": []}, "ok"])
def test_has_repro_evidence_needs_both_lists(repro):
    assert not has_repro_evidence({"repro": repro})


def test_has_repro_evidence_accepts_errors_too():
    # 有错误也算"证据在场":校验结果给人看,不硬拒绝
    assert has_repro_evidence({"repro": {"errors": ["x"], "warnings": []}})


def _runs(store):
    return sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())


@pytest.mark.parametrize("target", ["run-003", "recipe-", "recipe-a/b", "recipe-.."])
def test_a_malformed_target_is_a_no_op(store, target):
    # 计划 8 的 GateView 用信封 id;手写候选的 id 可能不是 recipe-<run>。抛错会卡死守护进程(Ruling 20)
    commits, runs = _commits(store), _runs(store)
    handle_admit_recipe(store, Action(kind="admit_recipe", target_id=target, detail={}))
    assert (_commits(store), _runs(store)) == (commits, runs)
    assert store.read_product(recipe_candidate_file("run-003")).status == "approved"


def test_a_missing_candidate_records_one_failure_and_does_not_raise(store):
    write_directive(store, "run-404")
    action = Action(kind="admit_recipe", target_id="recipe-run-404", detail={})
    handle_admit_recipe(store, action)
    failure = store.read_product(failure_record_file("run-404", "admit_recipe"))
    assert (failure.payload["category"], failure.payload["point"]) == ("T2", "recipestore.admit")
    assert failure.payload["reason"] == "recipe candidate recipe-run-404 not found"
    commits = _commits(store)
    handle_admit_recipe(store, action)  # 每 tick 重来也不堆提交
    assert _commits(store) == commits


def test_a_missing_candidate_without_a_run_dir_leaves_no_trace(store):
    commits = _commits(store)
    handle_admit_recipe(store, Action(kind="admit_recipe", target_id="recipe-zz", detail={}))
    assert _commits(store) == commits
    assert not (store.repo_root / run_dir("zz")).exists()  # 多出的 runs/zz 会挡住 run id 分配
