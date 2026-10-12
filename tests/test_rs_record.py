import pytest

from controlplane.paths import recipe_file, recipe_lineage_file, recipe_track_file, recipe_track_note_file, run_dir
from controlplane.reconcile import Action
from recipestore.library import get_recipe, read_track_entries
from recipestore.record import handle_record_track, used_as_for
from rs_fixtures import (
    l1,
    make_store,
    write_candidate_checkpoint,
    write_directive,
    write_model,
    write_planner_decision,
    write_report,
)
from rs_recipes import entry, put_recipe

R = "recipe-run-003"
FLAG = {"dimension": "tom", "kind": "social_regression", "drop": 0.2}
WRITE_ORDER = [
    recipe_track_file("run-003"),
    recipe_lineage_file("run-003"),
    recipe_file("run-003"),
    recipe_track_note_file("run-004"),
]


def _act(run_id="run-004"):
    return Action(kind="record_track", target_id=run_id, detail={})


def _note(store, run_id="run-004"):
    return store.read_product(recipe_track_note_file(run_id))


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


@pytest.fixture
def store(tmp_path):
    """run-001 产出基线 zengen-v1(tom=0.5);配方 recipe-run-003 已收录;
    run-004 用该配方 clone,候选已晋级,报告 tom=0.6 对比 zengen-v1。"""
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    put_recipe(s, "run-003", entries=[])
    write_planner_decision(s, "run-003", "run-004", "clone")
    write_directive(s, "run-004", recipe_ref=R)
    write_candidate_checkpoint(s, "run-004", "effective")
    write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    return s


def test_a_promoted_run_is_recorded_with_its_gain(store):
    handle_record_track(store, _act())
    [e] = read_track_entries(store, R)
    assert e == {"run_id": "run-004", "gain": pytest.approx(0.1), "promoted": True,
                 "regressed": False, "used_as": "clone"}
    note = _note(store)
    assert (note.type, note.status) == ("recipe_track_note", "executed")
    assert note.payload["result"] == "recorded"
    assert (note.payload["verdict"], note.payload["deprecated"], note.payload["reason"]) == ("keep", False, "ok")
    assert get_recipe(store, R).status == "effective"


def test_a_rejected_run_is_recorded_as_not_promoted(store):
    write_candidate_checkpoint(store, "run-004", "rejected")
    handle_record_track(store, _act())
    assert read_track_entries(store, R)[0]["promoted"] is False


def test_a_regression_deprecates_the_recipe_and_says_why(store):
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    handle_record_track(store, _act())
    [e] = read_track_entries(store, R)
    assert e["regressed"] is True and e["gain"] == pytest.approx(-0.2)
    assert get_recipe(store, R).status == "deprecated"
    assert _note(store).payload["verdict"] == "deprecate" and _note(store).payload["deprecated"] is True
    events = store.read_product(recipe_lineage_file("run-003")).payload["events"]
    assert events == [{"event": "deprecated", "run_id": "run-004", "reason": "latest track entry (run-004) regressed"}]


def test_an_earlier_regression_followed_by_a_clean_run_keeps_the_recipe(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    put_recipe(s, "run-003", entries=[entry("run-002", -0.1, regressed=True)])
    write_directive(s, "run-004", recipe_ref=R)
    write_candidate_checkpoint(s, "run-004", "effective")
    write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    handle_record_track(s, _act())
    assert get_recipe(s, R).status == "effective"


def test_a_missing_baseline_still_records_the_entry_with_no_gain(store):
    write_report(store, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v9")
    handle_record_track(store, _act())
    assert read_track_entries(store, R)[0]["gain"] is None
    assert _note(store).payload["reason"] == "baseline model zengen-v9 not found"


def test_a_directive_without_a_recipe_ref_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="")
    handle_record_track(store, _act())
    assert _note(store).payload == {"recipe_id": "", "result": "skipped",
                                    "reason": "directive run-004 has no recipe_ref",
                                    "entry": None, "verdict": None, "deprecated": False}
    assert read_track_entries(store, R) == []


def test_a_missing_recipe_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="recipe-run-404")
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "recipe recipe-run-404 not found"
    assert _note(store).links == ["run-004", "recipe-run-404"]


def test_a_malformed_recipe_ref_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="latest")
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "recipe latest not found"


def test_a_missing_directive_writes_nothing_and_creates_no_run_dir(tmp_path):
    # 写标记会新建只有 recipe_track.yaml 的 runs/run-004/,它会参与"最大 run"判定(Ruling 15)
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    commits = _commits(s)
    handle_record_track(s, _act())
    assert _commits(s) == commits
    assert not (s.repo_root / run_dir("run-004")).exists()


def test_a_stub_report_writes_a_skip_note(store):
    write_report(store, "run-004", stub=True)
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "diagnostic report for run-004 is a stub"
    assert read_track_entries(store, R) == []


@pytest.mark.parametrize("setup", ["no_candidate", "pending_review", "approved", "no_report"])
def test_a_premature_action_writes_nothing(tmp_path, setup):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    write_directive(s, "run-004", recipe_ref=R)
    if setup in ("pending_review", "approved"):
        write_candidate_checkpoint(s, "run-004", setup)
        write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    elif setup == "no_report":
        write_candidate_checkpoint(s, "run-004", "effective")
    commits = _commits(s)
    handle_record_track(s, _act())
    assert _commits(s) == commits
    assert not s.exists(recipe_track_note_file("run-004"))


def test_running_twice_changes_nothing(store):
    handle_record_track(store, _act())
    commits = _commits(store)
    handle_record_track(store, _act())
    assert _commits(store) == commits
    assert len(read_track_entries(store, R)) == 1


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4])
def test_a_crash_at_any_step_converges_on_rerun(store, monkeypatch, crash_at):
    # 带退化:写入顺序是 track_record → lineage → recipe(deprecated) → note,共 4 次
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    real = store.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == crash_at:
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        handle_record_track(store, _act())
    monkeypatch.setattr(store, "write_product", real)
    assert not store.exists(recipe_track_note_file("run-004"))
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert get_recipe(store, R).status == "deprecated"
    assert len(store.read_product(recipe_lineage_file("run-003")).payload["events"]) == 1
    assert calls == WRITE_ORDER[:crash_at]
    assert _note(store).payload["verdict"] == "deprecate"
    assert _note(store).payload["deprecated"] is True


def test_a_crash_before_the_note_converges_on_rerun(store, monkeypatch):
    real = store.write_product

    def flaky(rel, env, msg):
        if rel == recipe_track_note_file("run-004"):
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        handle_record_track(store, _act())
    monkeypatch.setattr(store, "write_product", real)
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert _note(store).payload["result"] == "recorded"


def test_a_recipe_already_deprecated_still_gets_the_entry(store):
    from recipestore.track import deprecate
    deprecate(store, R, reason="manual", run_id="run-002")
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert _note(store).payload["deprecated"] is False


def test_used_as_comes_from_the_planner_decision(store):
    assert used_as_for(store, "run-004") == "clone"
    write_planner_decision(store, "run-003", "run-004", "replay")
    assert used_as_for(store, "run-004") == "replay"


def test_used_as_defaults_to_clone_without_a_decision(tmp_path):
    s = make_store(tmp_path)
    assert used_as_for(s, "run-004") == "clone"
    write_planner_decision(s, "run-003", "run-004", "new")
    assert used_as_for(s, "run-004") == "clone"


def test_used_as_follows_the_directive_when_two_decisions_name_the_same_run(tmp_path):
    # 计划 5 Ruling 14:失败决策分配过的 run id 会被再分配,两份决策的 next_run_id 相同
    s = make_store(tmp_path)
    write_planner_decision(s, "run-002", "run-004", "clone", status="failed")
    write_planner_decision(s, "run-003", "run-004", "replay")
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="planner-run-003")
    assert used_as_for(s, "run-004") == "replay"
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="planner-run-002")
    assert used_as_for(s, "run-004") == "clone"  # 指令点名哪份就读哪份,不看状态
    write_planner_decision(s, "run-002", "run-004", "replay", status="failed")
    assert used_as_for(s, "run-004") == "replay"


def test_without_a_decision_ref_only_an_executed_decision_counts(tmp_path):
    s = make_store(tmp_path)
    write_planner_decision(s, "run-002", "run-004", "replay", status="failed")
    write_directive(s, "run-004", recipe_ref=R)  # 人工指令,没有 planner_decision_ref
    assert used_as_for(s, "run-004") == "clone"
    write_planner_decision(s, "run-003", "run-004", "replay")
    assert used_as_for(s, "run-004") == "replay"


def test_a_decision_ref_that_names_no_decision_falls_back_to_the_scan(tmp_path):
    s = make_store(tmp_path)
    write_planner_decision(s, "run-003", "run-004", "replay")
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="decision-run-003")  # 形状不对
    assert used_as_for(s, "run-004") == "replay"
