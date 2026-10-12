"""规划处理器:正常流程、校验违规进入重试、确定性失败记 T3、瞬态上抛、三条重入规则。"""
import pytest

from controlplane.config import write_config
from controlplane.paths import failure_record_file, model_file, planner_decision_file, scheduler_config_file
from controlplane.reconcile import Action
from inference_bridge import BridgeError
from trainplanner.backends import HeuristicBackend, ScriptedDecisionBackend
from trainplanner.templates import install_default_template
from trainscheduler import plan_handler as ph
from trainscheduler.config import install_default_configs
from trainscheduler.plan_handler import decision_id, make_plan_handler, plan_task_id
from ts_fixtures import (
    decision_json,
    env,
    make_store,
    write_decision,
    write_directive,
    write_model,
    write_recipe,
    write_report,
    write_round,
)

DIRECTIVE = "control-plane/runs/{}/directive.yaml"


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    install_default_configs(s)
    install_default_template(s)
    write_round(s, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    return s


def _act(run_id="run-001"):
    return Action(kind="plan_next_round", target_id=run_id, detail={})


def _decision(store, run_id="run-001"):
    return store.read_product(planner_decision_file(run_id))


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD"))


def test_a_new_decision_becomes_an_awaiting_directive_for_the_next_run(store):
    backend = ScriptedDecisionBackend([decision_json()])
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert (d.id, d.type, d.status) == ("planner-run-001", "planner_decision", "executed")
    assert d.payload == {
        "source_run_id": "run-001", "next_run_id": "run-002", "template_version": "v1", "backend": "scripted",
        "decision": {"target_dimension": "tom", "action": "new", "mode": "batch", "source_recipe_id": None,
                     "perturbation": {}, "data_synthesis_hint": ""},
        "rationale": "because the evidence says so", "failure": None,
    }
    assert d.links == ["run-001", "run-002"]
    directive = store.read_product(DIRECTIVE.format("run-002"))
    assert directive.status == "awaiting_confirmation"
    assert directive.payload["planner_decision_ref"] == "planner-run-001"
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-001.safetensors")
    assert [c[1:] for c in backend.calls] == [("run-001", "planner-attempt-1")]


def test_a_candidate_that_got_every_variant_family_right_is_still_planned(store):
    """设计 R21 的回归:真实诊断对全答对的族写 {"": 1.0}。以前装配原样交给计划 4、被拒、决策判 failed。"""
    from ts_fixtures import write_report

    write_report(store, "run-001", {"tom": 0.40, "empathy": 0.70},
                 error_profiles=[{"l2_dimension": "tom.false_belief", "track": "static",
                                  "error_type_distribution": {"": 1.0}}])
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).status == "executed"
    assert store.exists(DIRECTIVE.format("run-002"))


def test_confirm_directives_off_writes_a_pending_directive(store):
    write_config(store, scheduler_config_file(), "scheduler-config", {"confirm_directives": False}, "human")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"


def test_the_next_run_id_skips_hand_named_runs_and_takes_the_largest_number(store):
    write_directive(store, "run-007")
    write_directive(store, "experiment-x")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).payload["next_run_id"] == "run-008"
    assert store.exists(DIRECTIVE.format("run-008"))


def test_a_domain_violation_is_fed_back_and_the_retry_succeeds(store):
    backend = ScriptedDecisionBackend([
        decision_json(action="clone", source_recipe_id="recipe-run-404"),
        decision_json(),
    ])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "executed"
    assert len(backend.calls) == 2
    assert "recipe 'recipe-run-404' does not exist" in backend.calls[1][0]


def test_a_clone_decision_reuses_the_recipe(store):
    write_recipe(store, "run-001")
    backend = ScriptedDecisionBackend([decision_json(action="clone", source_recipe_id="recipe-run-001",
                                                     perturbation={"opd_coefficient": 0.3})])
    make_plan_handler(backend=backend)(store, _act())
    directive = store.read_product(DIRECTIVE.format("run-002"))
    assert directive.payload["recipe_ref"] == "recipe-run-001"
    assert directive.payload["training_config"]["opd_coefficient"] == 0.3
    assert "data_spec" not in directive.payload


def test_the_heuristic_backend_plans_the_lowest_dimension(store):
    make_plan_handler(backend=HeuristicBackend())(store, _act())
    d = _decision(store)
    assert d.payload["backend"] == "heuristic"
    assert d.payload["decision"]["target_dimension"] == "tom"
    assert store.read_product(DIRECTIVE.format("run-002")).payload["target_dimension"] == "tom"


def test_planner_exhaustion_fails_the_decision_with_a_t3_record(store):
    backend = ScriptedDecisionBackend(["not json"] * 3)
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] is None and d.payload["decision"] is None
    assert d.payload["failure"]["category"] == "T3" and d.payload["failure"]["attempts"] == 3
    assert "parse error" in d.payload["failure"]["reason"]
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001")))
    assert rec.payload["task_id"] == "plan-run-001"
    assert (rec.payload["category"], rec.payload["point"], rec.payload["retries"]) == ("T3", "trainscheduler.plan", 2)
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_max_plan_attempts_comes_from_the_scheduler_config(store):
    write_config(store, scheduler_config_file(), "scheduler-config", {"max_plan_attempts": 1}, "human")
    backend = ScriptedDecisionBackend(["nope", decision_json()])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "failed" and len(backend.calls) == 1


def test_repeated_violations_exhaust_the_shared_budget(store):
    backend = ScriptedDecisionBackend([decision_json(mode="interact")] * 3)
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "failed"
    assert "allow_interact" in _decision(store).payload["failure"]["reason"]


def test_stub_evidence_fails_the_decision_without_calling_the_backend(store):
    write_report(store, "run-001", stub=True)
    backend = ScriptedDecisionBackend([])
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert d.status == "failed" and "graded stub" in d.payload["failure"]["reason"]
    assert d.payload["failure"]["attempts"] == 0 and backend.calls == []
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


def test_a_resolve_error_after_the_decision_was_recorded_fails_it(store):
    store.write_product(model_file(2), env("zengen-v2", "zengen_model", "effective", {"promoted_from": "x"}), "t")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] == "run-002"  # 已分配,留痕
    assert "no checkpoint_ref" in d.payload["failure"]["reason"]
    assert not store.exists(DIRECTIVE.format("run-002"))
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_any_bridge_error_fails_the_decision_as_t3_and_does_not_propagate(store, category):
    """R18:T1 表示桥接已用尽自己的重试,与 T2 一样升级为 T3,不留 pending、不上抛。"""
    from controlplane.envelope import BigRef

    log = BigRef("file:///logs/plan-run-001-1.log", "d" * 64, 10)
    backend = ScriptedDecisionBackend([BridgeError(category, "cli timed out", attempts=3, log_ref=log)])
    make_plan_handler(backend=backend)(store, _act())  # 不抛
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] is None and d.payload["decision"] is None
    assert d.payload["failure"] == {
        "category": "T3",
        "reason": f"bridge {category} after 3 bridge attempt(s): cli timed out",
        "attempts": 1,
    }
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001"))).payload
    assert (rec["category"], rec["point"], rec["retries"]) == ("T3", "trainscheduler.plan", 0)
    assert rec["log_refs"] == [log.to_dict()]
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_a_bridge_error_on_a_retry_counts_the_planner_attempts(store):
    backend = ScriptedDecisionBackend(["not json", BridgeError("T1", "down")])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).payload["failure"]["attempts"] == 2
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001"))).payload
    assert rec["retries"] == 1 and rec["log_refs"] == []


def test_a_failed_decision_is_not_replanned_after_a_bridge_error(store):
    make_plan_handler(backend=ScriptedDecisionBackend([BridgeError("T1", "down")]))(store, _act())
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 再调就会 AssertionError
    assert _commits(store) == n


def test_an_os_error_from_the_backend_propagates_and_writes_nothing(store):
    backend = ScriptedDecisionBackend([OSError("broken pipe")])
    with pytest.raises(OSError, match="broken pipe"):
        make_plan_handler(backend=backend)(store, _act())
    assert not store.exists(planner_decision_file("run-001"))
    assert not store.exists(failure_record_file("run-001", "plan-run-001"))


def test_any_other_exception_propagates(store):
    backend = ScriptedDecisionBackend([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        make_plan_handler(backend=backend)(store, _act())
    assert not store.exists(planner_decision_file("run-001"))


def test_a_broken_scheduler_config_propagates_instead_of_failing_the_run(store):
    from trainscheduler.errors import SchedulerConfigError

    write_config(store, scheduler_config_file(), "scheduler-config", {"min_gain": "x"}, "human")
    with pytest.raises(SchedulerConfigError):
        make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert not store.exists(planner_decision_file("run-001"))


def test_reentry_a_pending_decision_without_directive_is_resumed_without_replanning(store):
    write_decision(store, "run-001", "run-005", status="pending")
    backend = ScriptedDecisionBackend([])  # 一调就抛 AssertionError
    make_plan_handler(backend=backend)(store, _act())
    assert backend.calls == []
    assert _decision(store).status == "executed"
    assert store.read_product(DIRECTIVE.format("run-005")).payload["planner_decision_ref"] == "planner-run-001"
    assert not store.exists(DIRECTIVE.format("run-002"))  # 没有重新分配


def test_hand_named_and_unpadded_run_dirs_do_not_block_planning(store):
    """数值序(设计 §3.2):zz、test-x 不参与比较,run-9 按编号 9 参与;新 run 取 run-010。"""
    for name in ("zz", "test-x", "run-9"):
        write_directive(store, name)
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).status == "executed"
    assert _decision(store).payload["next_run_id"] == "run-010"
    assert store.exists(DIRECTIVE.format("run-010"))


def test_a_hand_made_dir_with_the_allocated_number_fails_the_decision(store, monkeypatch):
    """防御性分支:正常路径走不到。让编号计算"看漏"一个人手建的 run-002,分配器拒绝,决策判 T3 留痕。"""
    import trainscheduler.run_ids as rid

    write_directive(store, "run-002", status="pending")
    monkeypatch.setattr(rid, "next_run_id", lambda existing: "run-002")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    d = _decision(store)
    assert d.status == "failed" and d.payload["next_run_id"] is None
    assert d.payload["decision"]["action"] == "new"  # 规划结果留痕
    assert "not numerically greater than existing run dir(s) ['run-002']" in d.payload["failure"]["reason"]
    assert d.payload["failure"]["attempts"] == 1
    assert store.exists(failure_record_file("run-001", "plan-run-001"))
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"  # 人手建的那条原样保留


@pytest.mark.parametrize("bad", [None, "", "../escape", "run-x"])
def test_reentry_a_with_a_broken_next_run_id_fails_instead_of_writing_a_bogus_directive(store, bad):
    write_decision(store, "run-001", bad, status="pending")
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    d = _decision(store)
    assert d.status == "failed" and "no usable next_run_id" in d.payload["failure"]["reason"]
    assert not (store.repo_root / "control-plane/runs/None").exists()
    assert not store.exists(DIRECTIVE.format("run-x"))
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda p: p.update(decision=None), "recorded decision is not a mapping"),
        (lambda p: p["decision"].update(action="teleport"), "recorded decision is malformed: action must be one of"),
        (lambda p: p["decision"].pop("mode"), "recorded decision is malformed: missing keys"),
        (lambda p: p.update(rationale=""), "recorded decision is malformed: rationale must be a non-empty string"),
    ],
)
def test_reentry_a_with_a_corrupted_decision_payload_fails_deterministically(store, mutate, fragment):
    write_decision(store, "run-001", "run-002", status="pending")
    env_ = _decision(store)
    mutate(env_.payload)
    store.write_product(planner_decision_file("run-001"), env_, "human: corrupt")
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 不抛
    d = _decision(store)
    assert d.status == "failed" and fragment in d.payload["failure"]["reason"]
    assert not store.exists(DIRECTIVE.format("run-002"))
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 下一个 tick 是空操作
    assert _commits(store) == n


def test_reentry_a_crash_while_writing_the_directive_resumes_with_the_same_run_id(store, monkeypatch):
    real = ph.construct_directive
    calls = []

    def flaky(*args, **kwargs):
        calls.append(kwargs["new_run_id"])
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(*args, **kwargs)

    monkeypatch.setattr(ph, "construct_directive", flaky)
    backend = ScriptedDecisionBackend([decision_json()])
    handler = make_plan_handler(backend=backend)
    with pytest.raises(OSError):
        handler(store, _act())
    # 非桥接的瞬态异常:决策保持 pending,不写失败记录(R18 只升级 BridgeError)
    assert _decision(store).status == "pending" and _decision(store).payload["next_run_id"] == "run-002"
    assert not store.exists(failure_record_file("run-001", "plan-run-001"))
    handler(store, _act())
    assert calls == ["run-002", "run-002"]
    assert len(backend.calls) == 1
    assert _decision(store).status == "executed"


def test_reentry_b_a_pending_decision_whose_directive_exists_is_only_marked(store):
    write_decision(store, "run-001", "run-002", status="pending")
    write_directive(store, "run-002", status="awaiting_confirmation")
    before = store.read_product(DIRECTIVE.format("run-002")).to_dict()
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert _decision(store).status == "executed"
    assert store.read_product(DIRECTIVE.format("run-002")).to_dict() == before
    assert _commits(store) == n + 1


@pytest.mark.parametrize("status", ["executed", "failed"])
def test_reentry_c_a_finished_decision_is_a_no_op(store, status):
    write_decision(store, "run-001", "run-002" if status == "executed" else None, status=status)
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert _commits(store) == n
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_running_twice_plans_once(store):
    backend = ScriptedDecisionBackend([decision_json()])
    handler = make_plan_handler(backend=backend)
    handler(store, _act())
    n = _commits(store)
    handler(store, _act())
    assert _commits(store) == n and len(backend.calls) == 1


def test_loaders_are_injectable(store):
    from trainscheduler.config import DEFAULT_SCHEDULER_CONFIG, SchedulerConfig, load_training_defaults
    from trainplanner.templates import load_template

    seen = []

    def config_loader(s):
        seen.append("config")
        return SchedulerConfig(confirm_directives=False)

    def template_loader(s, version):
        seen.append(("template", version))
        return load_template(s, version)

    def defaults_loader(s):
        seen.append("defaults")
        return load_training_defaults(s)

    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]), template_loader=template_loader,
                      config_loader=config_loader, defaults_loader=defaults_loader)(store, _act())
    assert seen == ["config", "defaults", ("template", DEFAULT_SCHEDULER_CONFIG.template_version)]
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"


def test_decision_id_and_task_id():
    assert (decision_id("run-003"), plan_task_id("run-003")) == ("planner-run-003", "plan-run-003")


def test_a_promoted_model_from_a_later_run_is_the_base(store):
    write_round(store, "run-002", {"tom": 0.5}, model_n=2, baseline_ref="zengen-v1")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act("run-002"))
    directive = store.read_product(DIRECTIVE.format("run-003"))
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-002.safetensors")
