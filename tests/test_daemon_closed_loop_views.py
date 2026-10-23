"""read_tree_state 的闭环视图(闭环集成设计 §3.1、§3.3):enabled 为空时与现在逐字节相同,
非空时才读 runs / models / gates。"""
import pytest

import logging

from controlplane.daemon import read_tree_state, reconcile_once, run_daemon
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    agent_round_file,
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    nomination_file,
    planner_decision_file,
    proposal_file,
    recipe_candidate_file,
    recipe_track_note_file,
    run_dir,
)
from controlplane.reconcile import (
    CLOSED_LOOP_ACTIONS, UNREADABLE_STATUS, GateView, ModelView, RunView, TreeState,
)

ALL = frozenset(CLOSED_LOOP_ACTIONS)


def _env(id_, type_, status, payload=None, links=None):
    return Envelope(
        id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(),
        status=status, schema_version=1, links=links or [], payload=payload or {},
    )


def _put(store, rel, env):
    store.write_product(rel, env, f"seed {rel}")


def _directive(store, run_id, status="pending", recipe_ref=""):
    _put(store, f"{run_dir(run_id)}/directive.yaml",
         _env(run_id, "directive", status, {"target_dimension": "tom", "recipe_ref": recipe_ref}))


def _candidate(store, run_id, status):
    _put(store, candidate_checkpoint_file(run_id), _env(f"cand-{run_id}", "candidate_checkpoint", status, links=[run_id]))


def _report(store, run_id, payload):
    _put(store, diagnostic_report_file(run_id), _env(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]))


REAL = {"backends": "real", "dimension_scores": [{"dimension": "tom", "level": "l1", "score": 0.5}],
        "regression_flags": {"flags": []}}  # 失败关闭(DX-12):只有带 backends: real 才判 real
MOCK = {**REAL, "backends": "mock"}
STUB = {"candidate_ref": "cand", "benchmark_version": "bench-v1", "dimension_count": 0, "stub": True}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _seed_everything(store):
    _directive(store, "run-001", "executed", "recipe-run-000")
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", REAL)
    _put(store, planner_decision_file("run-001"), _env("pd-run-001", "planner_decision", "pending", {"next_run_id": "run-002"}))
    _put(store, nomination_file("run-001"), _env("nomination-run-001", "nomination_note", "executed"))
    _put(store, recipe_track_note_file("run-001"), _env("track-run-001", "recipe_track_note", "executed"))
    _directive(store, "run-002", "awaiting_confirmation")
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", {"promoted_from": "cand-run-001"},
                                    ["cand-run-001", "run-001"]))
    _put(store, agent_round_file("zengen-v1"), _env("agent-round-zengen-v1", "agent_round", "executed"))
    _put(store, recipe_candidate_file("run-001"), _env("recipe-run-001", "recipe_candidate", "approved"))
    _put(store, proposal_file("proposal-agent-round-zengen-v1-1"),
         _env("proposal-agent-round-zengen-v1-1", "skill_memory_proposal", "pending_review"))


def test_with_nothing_enabled_the_state_equals_the_legacy_shape(store):
    _seed_everything(store)
    state = read_tree_state(store)
    assert state == TreeState(
        directives=state.directives, candidates=state.candidates, model_count=1, max_model_version=1,
    )
    assert (state.runs, state.models, state.gates) == ([], [], [])
    assert read_tree_state(store, enabled=frozenset(), accept_mock_evidence=True) == state


def test_with_nothing_enabled_the_legacy_fields_match_what_the_full_read_sees(store):
    _seed_everything(store)
    legacy, full = read_tree_state(store), read_tree_state(store, enabled=ALL)
    assert (legacy.directives, legacy.candidates, legacy.model_count) == (
        full.directives, full.candidates, full.model_count)


def test_run_views_project_every_marker(store):
    _seed_everything(store)
    state = read_tree_state(store, enabled=ALL, accept_mock_evidence=True)
    assert state.enabled == ALL and state.accept_mock_evidence is True
    assert state.runs == [
        RunView(run_id="run-001", directive_status="executed", recipe_ref="recipe-run-000",
                candidate_status="effective", has_diagnostic=True, diagnostic_grade="real",
                planner_decision_status="pending", has_nomination_note=True, has_track_note=True),
        RunView(run_id="run-002", directive_status="awaiting_confirmation", recipe_ref="",
                candidate_status=None, has_diagnostic=False, diagnostic_grade=None,
                planner_decision_status=None, has_nomination_note=False, has_track_note=False),
    ]


@pytest.mark.parametrize("payload,grade", [(REAL, "real"), (MOCK, "mock"), (STUB, "stub")])
def test_the_grade_comes_from_controlplane_evidence(store, payload, grade):
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", payload)
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == grade


def test_a_non_dict_report_payload_grades_stub(store, monkeypatch):
    # 判不出来按最弱算:载荷不是 dict 时不抛 TypeError,记为 stub
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", REAL)
    real_read = store.read_product

    def read(rel):
        env = real_read(rel)
        if rel == diagnostic_report_file("run-001"):
            env.payload = ["not", "a", "mapping"]
        return env

    monkeypatch.setattr(store, "read_product", read)
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == "stub"


def test_an_unreadable_report_file_grades_stub(store):
    _candidate(store, "run-001", "effective")
    path = store.repo_root / diagnostic_report_file("run-001")
    path.write_text("id: diag-run-001\npayload: [1, 2]\n", encoding="utf-8")  # 信封校验失败
    view = read_tree_state(store, enabled=ALL).runs[0]
    assert (view.has_diagnostic, view.diagnostic_grade) == (True, "stub")


def test_a_daemon_stub_report_is_graded_stub(store):
    _directive(store, "run-001")
    reconcile_once(store)  # 派发:桩候选
    reconcile_once(store)  # 桩诊断
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == "stub"


def test_a_run_dir_without_a_directive_still_has_a_view(store):
    _candidate(store, "run-001", "rejected")
    view = read_tree_state(store, enabled=ALL).runs[0]
    assert (view.directive_status, view.recipe_ref, view.candidate_status) == (None, "", "rejected")


def test_a_non_string_recipe_ref_reads_as_no_recipe(store):
    _put(store, f"{run_dir('run-001')}/directive.yaml",
         _env("run-001", "directive", "executed", {"target_dimension": "tom", "recipe_ref": None}))
    assert read_tree_state(store, enabled=ALL).runs[0].recipe_ref == ""


def test_model_views_take_the_run_id_from_the_promotion_links(store):
    _seed_everything(store)
    _put(store, model_file(10), _env("zengen-v10", "zengen_model", "deprecated", links=["cand-run-010", "run-010"]))
    _put(store, model_file(2), _env("zengen-v2", "zengen_model", "effective", links=[]))
    state = read_tree_state(store, enabled=ALL)
    assert sorted(state.models, key=lambda m: m.version) == [
        ModelView("zengen-v1", 1, "run-001", "effective", "executed"),
        ModelView("zengen-v2", 2, "", "effective", None),
        ModelView("zengen-v10", 10, "run-010", "deprecated", None),
    ]


@pytest.mark.parametrize("round_status", ["pending", "executed", "failed"])
def test_the_model_view_carries_the_round_envelope_status(store, round_status):
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", links=["cand-run-001", "run-001"]))
    _put(store, agent_round_file("zengen-v1"), _env("agent-round-zengen-v1", "agent_round", round_status))
    (view,) = read_tree_state(store, enabled=ALL).models
    assert (view.agent_round_status, view.has_agent_round) == (round_status, True)


@pytest.mark.parametrize("text", [
    "<<<<<<< HEAD\nstatus: pending\n=======\nstatus: executed\n>>>>>>> other\n",  # 合并冲突标记
    "id: agent-round-zengen-v1\nstatus: pending\n",                                 # 信封缺必填字段
])
def test_an_unreadable_round_file_reads_as_no_round(store, text):
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", links=["cand-run-001", "run-001"]))
    path = store.repo_root / agent_round_file("zengen-v1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    (view,) = read_tree_state(store, enabled=ALL).models
    assert (view.agent_round_status, view.has_agent_round) == (None, False)


def test_the_daemon_promotion_writes_links_the_model_view_understands(store):
    _directive(store, "run-001")
    reconcile_once(store)
    reconcile_once(store)
    cand = store.read_product(candidate_checkpoint_file("run-001"))
    cand.status = "approved"
    store.write_product(candidate_checkpoint_file("run-001"), cand, "human: approve")
    reconcile_once(store)
    assert read_tree_state(store, enabled=ALL).models == [ModelView("zengen-v1", 1, "run-001", "effective", None)]


def test_gate_views_cover_recipe_candidates_and_nested_proposals(store):
    _seed_everything(store)
    _put(store, proposal_file("proposal-agent-round-zengen-v1-2"),
         _env("proposal-agent-round-zengen-v1-2", "skill_memory_proposal", "approved"))
    # 不是 proposal.yaml 的文件、空子目录都不算
    _put(store, "control-plane/skill_memory_proposals/proposal-x/notes.yaml", _env("n", "note", "executed"))
    (store.repo_root / "control-plane/skill_memory_proposals/empty").mkdir()
    assert read_tree_state(store, enabled=ALL).gates == [
        GateView("recipe-run-001", "recipe_candidate", "approved"),
        GateView("proposal-agent-round-zengen-v1-1", "skill_memory_proposal", "pending_review"),
        GateView("proposal-agent-round-zengen-v1-2", "skill_memory_proposal", "approved"),
    ]


def test_missing_new_directories_read_as_empty(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    import shutil
    for sub in ("recipe_candidates", "skill_memory_proposals", "agent_rounds"):
        shutil.rmtree(store.repo_root / "control-plane" / sub, ignore_errors=True)
    state = read_tree_state(store, enabled=ALL)
    assert (state.runs, state.models, state.gates) == ([], [], [])


# ---- CP-17:读不出的已提交产物记为 unreadable,不让守护进程退出 ----

CONFLICT = "<<<<<<< HEAD\nstatus: pending\n=======\nstatus: executed\n>>>>>>> other\n"


def _spoil(store, rel, text=CONFLICT):
    """像合并冲突或手改那样,把一个已提交的产物写成读不出的样子(不经 write_product)。"""
    path = store.repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def test_an_unreadable_directive_is_projected_not_raised(store):
    rel = _spoil(store, f"{run_dir('run-001')}/directive.yaml")
    state = read_tree_state(store, enabled=ALL)
    (d,) = state.directives
    assert (d.id, d.status) == ("run-001", UNREADABLE_STATUS)
    assert state.runs[0].directive_status == UNREADABLE_STATUS
    assert state.unreadable == (rel,)


def test_an_unreadable_candidate_is_projected_not_raised(store):
    rel = _spoil(store, candidate_checkpoint_file("run-001"), "id: cand-run-001\npayload: [1]\n")
    state = read_tree_state(store, enabled=ALL)
    (c,) = state.candidates
    assert (c.id, c.status, c.run_id) == ("cand-run-001", UNREADABLE_STATUS, "run-001")
    assert state.runs[0].candidate_status == UNREADABLE_STATUS
    assert state.unreadable == (rel,)


def test_an_unreadable_planner_decision_is_not_read_as_missing(store):
    """决策「为空」正是 plan_next_round 的触发条件:读不出不能当成没有。"""
    _candidate(store, "run-001", "effective")
    rel = _spoil(store, planner_decision_file("run-001"))
    state = read_tree_state(store, enabled=ALL)
    assert state.runs[0].planner_decision_status == UNREADABLE_STATUS
    assert state.unreadable == (rel,)


def test_an_unreadable_model_is_projected_from_its_file_name(store):
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", links=["cand-run-001", "run-001"]))
    rel = _spoil(store, model_file(2))
    state = read_tree_state(store, enabled=ALL)
    assert sorted(state.models, key=lambda m: m.version)[1] == ModelView("zengen-v2", 2, "", UNREADABLE_STATUS, None)
    assert state.unreadable == (rel,)


def test_an_unreadable_model_is_reported_even_without_closed_loop_actions(store):
    """晋级的暂停规则要用:enabled 为空时也要读模型文件。"""
    rel = _spoil(store, model_file(1))
    state = read_tree_state(store)
    assert (state.models, state.unreadable) == ([], (rel,))


def test_unreadable_gates_take_their_id_from_the_path(store):
    recipe = _spoil(store, recipe_candidate_file("run-001"))
    proposal = _spoil(store, proposal_file("prop-7"))
    state = read_tree_state(store, enabled=ALL)
    assert sorted(state.gates, key=lambda g: g.id) == [
        GateView("prop-7", "skill_memory_proposal", UNREADABLE_STATUS),
        GateView("recipe-run-001", "recipe_candidate", UNREADABLE_STATUS),
    ]
    assert state.unreadable == tuple(sorted((recipe, proposal)))


def test_a_healthy_tree_has_nothing_unreadable(store):
    _seed_everything(store)
    assert read_tree_state(store, enabled=ALL).unreadable == ()
    assert read_tree_state(store).unreadable == ()


def test_an_os_error_still_propagates(store, monkeypatch):
    """OSError 是瞬态(磁盘、权限):不吞,守护进程照旧退出。"""
    _directive(store, "run-001")

    def broken(rel):
        raise PermissionError(rel)

    monkeypatch.setattr(store, "read_product", broken)
    with pytest.raises(PermissionError):
        read_tree_state(store)



def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "controlplane.daemon" and r.levelno == logging.WARNING]


def test_the_daemon_survives_a_corrupt_file_and_reports_it_once(store, caplog):
    """坏文件在树里:守护进程跑多个 tick 不退出;同一路径只记一次警告;别的指令照常派发。"""
    rel = _spoil(store, f"{run_dir('run-001')}/directive.yaml")
    _directive(store, "run-002")
    caplog.set_level(logging.WARNING, logger="controlplane.daemon")
    run_daemon(store, interval_s=0, max_ticks=3)
    assert store.read_product(f"{run_dir('run-002')}/directive.yaml").status == "executed"
    reported = [m for m in _warnings(caplog) if rel in m]
    assert len(reported) == 1, reported
    assert "Error" in reported[0], reported          # 带上异常类名,人能看出是哪种坏法


def test_a_one_shot_tick_reports_every_unreadable_path(store, caplog):
    first = _spoil(store, f"{run_dir('run-001')}/directive.yaml")
    second = _spoil(store, model_file(1))
    caplog.set_level(logging.WARNING, logger="controlplane.daemon")
    reconcile_once(store)
    messages = _warnings(caplog)
    assert any(first in m for m in messages) and any(second in m for m in messages)


def test_a_path_is_reported_again_after_it_was_fixed_and_broke_again(store, caplog):
    rel = f"{run_dir('run-001')}/directive.yaml"
    _spoil(store, rel)
    reported: set[str] = set()
    caplog.set_level(logging.WARNING, logger="controlplane.daemon")
    reconcile_once(store, reported=reported)
    reconcile_once(store, reported=reported)                   # 仍坏:不重复记
    _directive(store, "run-001", status="executed")           # 修好
    reconcile_once(store, reported=reported)
    assert reported == set()
    _spoil(store, rel)                                        # 又坏
    reconcile_once(store, reported=reported)
    assert len([m for m in _warnings(caplog) if rel in m]) == 2


def test_the_closed_loop_resumes_once_the_file_is_fixed(store):
    """暂停只持续到人修好文件:读得出之后同一棵树照常产生闭环动作。"""
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", REAL)
    rel = _spoil(store, recipe_candidate_file("run-000"))
    assert read_tree_state(store, enabled=ALL).unreadable == (rel,)
    from controlplane.reconcile import reconcile
    assert not [a for a in reconcile(read_tree_state(store, enabled=ALL)) if a.kind in CLOSED_LOOP_ACTIONS]
    (store.repo_root / rel).unlink()
    assert [a.kind for a in reconcile(read_tree_state(store, enabled=ALL)) if a.kind in CLOSED_LOOP_ACTIONS]
