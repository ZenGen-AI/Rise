"""配方提名:判定的每条不提名原因、候选载荷形状、可复现性报告原样写入、处理器幂等且不改已审核的候选。"""
import copy

import pytest

from controlplane.paths import dataset_manifest_file, nomination_file, recipe_candidate_file
from controlplane.reconcile import Action
from recipestore.admit import has_repro_evidence
from recipestore.validate import validate_reproducibility
from trainscheduler.config import SchedulerConfig
from trainscheduler.nominate import (
    NominationVerdict,
    build_recipe_candidate,
    make_nominate_handler,
    nomination_check,
)
from ts_fixtures import env, make_store, recipe_payload, write_directive, write_round

MOCK_OK = SchedulerConfig(accept_mock_evidence=True)


def _manifest(store, run_id, stage, h):
    store.write_product(dataset_manifest_file(run_id, stage),
                        env(f"dsm-{run_id}-{stage}", "dataset_manifest", "executed",
                            {"id": f"dsm-{run_id}-{stage}", "run_id": run_id, "name": stage,
                             "episode_refs": [], "manifest_hash": h}), "test: manifest")


@pytest.fixture
def store(tmp_path):
    """run-001 晋级为 zengen-v1(基线);run-002 以它为基线,tom 涨了 0.05。"""
    s = make_store(tmp_path)
    write_round(s, "run-001", {"tom": 0.40}, model_n=1)
    write_round(s, "run-002", {"tom": 0.45}, model_n=2, baseline_ref="zengen-v1")
    _manifest(s, "run-002", "sft", "b" * 64)
    _manifest(s, "run-002", "grpo_opd", "c" * 64)
    return s


def _act(run_id="run-002"):
    return Action(kind="nominate_recipe", target_id=run_id, detail={})


def test_a_gain_above_min_gain_with_acceptable_evidence_is_nominated(store):
    v = nomination_check(store, "run-002", config=MOCK_OK)
    assert v.nominate and v.gain == pytest.approx(0.05) and v.baseline_run_id == "run-001"
    assert "min_gain 0.01" in v.reason


def test_mock_evidence_is_refused_by_default(store):
    v = nomination_check(store, "run-002", config=SchedulerConfig())
    assert v == NominationVerdict(False, "diagnostic evidence for run-002 is mock, which is not acceptable", None, None)


def test_the_parameter_overrides_the_config_for_mock_evidence(store):
    assert nomination_check(store, "run-002", config=SchedulerConfig(), accept_mock_evidence=True).nominate
    assert not nomination_check(store, "run-002", config=MOCK_OK, accept_mock_evidence=False).nominate


def test_real_evidence_needs_no_switch(store):
    from ts_fixtures import write_report

    # 计划 3 的 compute_gain 不拿 real 候选减 mock 基线,所以基线 run-002 的报告也要是 real
    write_report(store, "run-002", {"tom": 0.45}, baseline_ref="zengen-v1", backends="real")
    write_round(store, "run-003", {"tom": 0.50}, baseline_ref="zengen-v2", backends="real")
    assert nomination_check(store, "run-003", config=SchedulerConfig()).nominate


def test_real_evidence_against_a_mock_baseline_is_not_comparable(store):
    write_round(store, "run-003", {"tom": 0.50}, baseline_ref="zengen-v2", backends="real")
    v = nomination_check(store, "run-003", config=SchedulerConfig())
    assert not v.nominate and not v.not_yet
    assert v.reason.startswith("no comparable gain:") and v.baseline_run_id == "run-002"


def test_stub_evidence_is_never_nominated(store):
    from ts_fixtures import write_report

    write_report(store, "run-002", stub=True)
    v = nomination_check(store, "run-002", config=MOCK_OK)
    assert not v.nominate and "is stub" in v.reason


def test_no_comparable_baseline_is_not_nominated(store):
    v = nomination_check(store, "run-001", config=MOCK_OK)  # 报告没有 baseline_ref
    assert not v.nominate and v.reason.startswith("no comparable gain:") and v.gain is None


def test_a_gain_below_min_gain_is_not_nominated(store):
    v = nomination_check(store, "run-002", config=SchedulerConfig(accept_mock_evidence=True, min_gain=0.1))
    assert not v.nominate and "below min_gain 0.1" in v.reason and v.gain == pytest.approx(0.05)


def test_a_negative_gain_is_not_nominated(store):
    write_round(store, "run-003", {"tom": 0.30}, baseline_ref="zengen-v2")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and v.gain == pytest.approx(-0.15)


def test_regression_flags_block_nomination(store):
    write_round(store, "run-003", {"tom": 0.60}, baseline_ref="zengen-v2",
                flags=[{"dimension": "empathy", "kind": "social_regression", "drop": 0.1}])
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and "regression flags ['social_regression']" in v.reason


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected"])
def test_only_effective_candidates_are_nominated(store, status):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status=status)
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and f"is {status}" in v.reason


def test_a_directive_without_a_training_config_is_not_nominated(store):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    from controlplane.paths import run_dir

    d = store.read_product(f"{run_dir('run-003')}/directive.yaml")
    del d.payload["training_config"]
    store.write_product(f"{run_dir('run-003')}/directive.yaml", d, "test")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and "not a batch or interact training directive" in v.reason


def test_missing_pieces_are_reasons_not_exceptions(tmp_path):
    s = make_store(tmp_path)
    assert "has no candidate" in nomination_check(s, "run-009", config=MOCK_OK).reason
    write_directive(s, "run-009")
    from ts_fixtures import write_candidate

    write_candidate(s, "run-009")
    assert "has no diagnostic report" in nomination_check(s, "run-009", config=MOCK_OK).reason


def test_the_candidate_payload_has_the_recipe_shape(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK, repo_commit="0385004")
    assert (c.id, c.type, c.status) == ("recipe-run-002", "recipe_candidate", "pending_review")
    assert c.links == ["run-002", "cand-run-002"]
    p = c.payload
    assert set(p) == {"recipe_type", "dimension", "source_run_id", "eval_anchor", "result", "repro",
                      "nondeterministic", "training_config", "stage_manifests", "code_version"}
    assert (p["recipe_type"], p["dimension"], p["source_run_id"], p["nondeterministic"]) == (
        "batch", "tom", "run-002", False)
    assert p["eval_anchor"] == {"taxonomy_version": None, "item_pool_version": None, "benchmark_version": "bench-v1",
                                "judge_prompt_version": None, "scoring_config_version": None}
    assert p["result"] == {"gain": pytest.approx(0.05), "baseline_ref": "zengen-v1",
                           "candidate_ref": "cand-run-002", "promoted": True}
    assert p["stage_manifests"] == {"grpo_opd": {"id": "dsm-run-002-grpo_opd", "manifest_hash": "c" * 64},
                                    "sft": {"id": "dsm-run-002-sft", "manifest_hash": "b" * 64}}
    assert p["code_version"] == {"repo_commit": "0385004"}
    assert p["training_config"] == store.read_product("control-plane/runs/run-002/directive.yaml").payload[
        "training_config"]


def test_repro_is_the_validator_output_verbatim(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK, repo_commit="0385004")
    without = {k: v for k, v in c.payload.items() if k != "repro"}
    assert c.payload["repro"] == validate_reproducibility(without).to_dict()
    # 分类表与题库版本诊断报告里没有,如实记成错误交给审核门③
    assert c.payload["repro"]["errors"] == ["eval_anchor.taxonomy_version: missing",
                                            "eval_anchor.item_pool_version: missing"]
    assert has_repro_evidence(c.payload)


def test_an_unknown_commit_and_a_missing_manifest_are_repro_errors(store):
    from controlplane.paths import run_dir
    import os

    os.remove(store.repo_root / dataset_manifest_file("run-002", "sft"))
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert c.payload["code_version"] == {"repo_commit": None}
    assert c.payload["stage_manifests"]["sft"] == {"id": "dsm-run-002-sft", "manifest_hash": None}
    errors = c.payload["repro"]["errors"]
    assert "code_version.repo_commit: must be a 7-40 char lowercase hex commit id" in errors
    assert "stage_manifests.sft.manifest_hash: must be a 64-char lowercase hex digest" in errors


def test_an_interact_run_becomes_an_interact_candidate(store):
    cfg = recipe_payload("run-003", recipe_type="interact")["training_config"]
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    write_directive(store, "run-003", training_config=cfg)
    p = build_recipe_candidate(store, "run-003", config=MOCK_OK, repo_commit="0385004").payload
    assert (p["recipe_type"], p["nondeterministic"]) == ("interact", True)
    assert p["scenario_ids"] == ["scen-1", "scen-2"] and p["bindings"] == cfg["bindings"]
    assert "stage_manifests" not in p
    assert any("nondeterministic" in w for w in p["repro"]["warnings"])


def test_building_a_candidate_for_a_non_nominable_run_raises(store):
    with pytest.raises(ValueError, match="not nominable"):
        build_recipe_candidate(store, "run-002", config=SchedulerConfig())


def test_the_handler_writes_the_candidate_and_a_nominated_note(store):
    make_nominate_handler(config_loader=lambda s: MOCK_OK, repo_commit="0385004")(store, _act())
    cand = store.read_product(recipe_candidate_file("run-002"))
    assert cand.status == "pending_review"
    note = store.read_product(nomination_file("run-002"))
    assert (note.id, note.type, note.status) == ("nomination-run-002", "nomination_note", "executed")
    assert note.payload["result"] == "nominated" and note.payload["recipe_candidate_id"] == "recipe-run-002"
    assert note.payload["gain"] == pytest.approx(0.05) and note.payload["baseline_run_id"] == "run-001"
    assert note.links == ["run-002", "recipe-run-002"]


def test_the_handler_writes_a_skipped_note_and_no_candidate(store):
    make_nominate_handler()(store, _act())  # 默认配置:mock 证据不可接受
    note = store.read_product(nomination_file("run-002"))
    assert note.payload["result"] == "skipped" and "mock" in note.payload["reason"]
    assert note.payload["recipe_candidate_id"] is None and note.links == ["run-002"]
    assert not store.exists(recipe_candidate_file("run-002"))


@pytest.mark.parametrize("status", ["pending_review", "approved"])
def test_a_not_yet_terminal_candidate_writes_no_note_and_is_nominated_later(store, status):
    """候选还没到 effective:不写标记(标记是幂等键),等它晋级后照常提名。"""
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status=status)
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and v.not_yet
    n = store._git("rev-list", "--count", "HEAD")
    handler(store, _act("run-003"))
    assert store._git("rev-list", "--count", "HEAD") == n
    assert not store.exists(nomination_file("run-003"))
    from ts_fixtures import write_candidate

    write_candidate(store, "run-003", "effective")
    handler(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "nominated"


def test_a_missing_report_writes_no_note_until_the_report_arrives(store):
    from ts_fixtures import write_candidate, write_report

    write_directive(store, "run-003")
    write_candidate(store, "run-003")
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    handler(store, _act("run-003"))
    assert not store.exists(nomination_file("run-003"))
    write_report(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    handler(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "nominated"


def test_a_rejected_candidate_is_a_permanent_skip(store):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status="rejected")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and not v.not_yet
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "skipped"


def test_the_handler_argument_overrides_the_config(store):
    make_nominate_handler(accept_mock_evidence=True)(store, _act())
    assert store.read_product(nomination_file("run-002")).payload["result"] == "nominated"


def test_the_note_is_the_idempotency_key(store):
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    handler(store, _act())
    n = store._git("rev-list", "--count", "HEAD")
    handler(store, _act())
    assert store._git("rev-list", "--count", "HEAD") == n


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected", "effective"])
def test_an_existing_candidate_is_never_rewritten(store, status):
    """崩在"候选已写、标记未写"之后,或人已经审核过:只补标记,候选原样保留。"""
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act())
    cand = store.read_product(recipe_candidate_file("run-002"))
    cand.status = status
    cand.payload["review_note"] = "looked at it"
    store.write_product(recipe_candidate_file("run-002"), cand, "human: review")
    import os

    os.remove(store.repo_root / nomination_file("run-002"))
    before = store.read_product(recipe_candidate_file("run-002")).to_dict()
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act())
    assert store.read_product(recipe_candidate_file("run-002")).to_dict() == before
    note = store.read_product(nomination_file("run-002"))
    assert note.payload["result"] == "nominated" and f"({status})" in note.payload["reason"]


def test_nomination_check_and_build_only_read(store):
    head = store._git("rev-parse", "HEAD")
    nomination_check(store, "run-002", config=MOCK_OK)
    build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert store._git("rev-parse", "HEAD") == head


def test_build_does_not_alias_the_directive(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    c.payload["training_config"]["sft_steps"] = 99
    again = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert again.payload["training_config"]["sft_steps"] == 2
    assert copy.deepcopy(again.payload) == again.payload
