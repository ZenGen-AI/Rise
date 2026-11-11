"""真实诊断评估器(闭环集成设计 §10.2、R17):backends 标记、基线解析、注入调和循环。"""
import ast
from pathlib import Path

import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, run_dir
from diagnostic.assets import DiagnosticAssetError, write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.evaluator import NO_BASELINE, make_diagnostic_evaluator, resolve_baseline
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.taxonomy_loader import ItemPool
from simulator.stubs import diagnostic_evaluator_stub

TAX = DiagnosticTaxonomy(
    "tax-v1", ["mental_representation"], {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]}, ["format_compliance"],
)
POOL = ItemPool(
    "pool-v1",
    [DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
     DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1")],
    [VariantFamily("vf1", "i1", ["i1v"])],
)


def _someval(right=4):
    wrong = 4 - right
    return MockSoMEvalRunner(fixed={"i1": ["B"] * right + ["X"] * wrong, "i1v": ["C"] * right + ["X"] * wrong})


def _judge():
    return MockJudgeBackend(model_id="external-judge", fixed={})


def _evaluator(right=4, **kw):
    return make_diagnostic_evaluator(
        taxonomy_version="tax-v1", item_pool_version="pool-v1", someval_runner=_someval(right),
        judge_backend=_judge(), benchmark_version="bench-v1", **kw,
    )


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


@pytest.fixture
def seeded(store):
    write_taxonomy(store, TAX)
    write_item_pool(store, POOL)
    return store


# ---- 评估器 ----

def test_the_evaluator_writes_a_mock_graded_report(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    payload = seeded.read_product(diagnostic_report_file("run-001")).payload
    assert payload["backends"] == "mock"
    assert payload["candidate_ref"] == "cand-run-001"
    assert payload["baseline_ref"] == NO_BASELINE == "none"
    assert payload["regression_flags"] == {"flags": []}
    assert any(d["level"] == "l1" for d in payload["dimension_scores"])
    assert diagnostic_grade(payload) == "mock"


def test_a_stub_report_from_the_daemon_and_a_mock_report_from_the_evaluator_grade_differently(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    seeded.write_product(diagnostic_report_file("run-002"), diagnostic_evaluator_stub("run-002", "cand-run-002"), "stub")
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-001")).payload) == "mock"
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-002")).payload) == "stub"


def test_a_real_marker_grades_real(seeded):
    _evaluator(backends="real")(seeded, "run-001", "cand-run-001")
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-001")).payload) == "real"


def test_the_marker_is_validated_when_the_evaluator_is_built():
    with pytest.raises(ValueError, match="backends"):
        _evaluator(backends="pretend")


def test_the_evaluator_fails_loudly_without_assets(store):
    with pytest.raises(DiagnosticAssetError):
        _evaluator()(store, "run-001", "cand-run-001")
    assert not store.exists(diagnostic_report_file("run-001"))


def _promote(store, n, run_id, status="effective", links=None):
    env = Envelope(id=f"zengen-v{n}", type="zengen_model", version=1, created_at="t",
                   created_by=OwnerRef(), status=status, schema_version=1,
                   links=links if links is not None else [f"cand-{run_id}", run_id],
                   payload={"promoted_from": f"cand-{run_id}"})
    store.write_product(model_file(n), env, f"seed zengen-v{n}")


def test_the_baseline_is_the_latest_promoted_models_run(seeded):
    _evaluator(right=4)(seeded, "run-001", "cand-run-001")
    _promote(seeded, 1, "run-001")
    _evaluator(right=0)(seeded, "run-002", "cand-run-002")  # 全错:相对基线明显退化
    payload = seeded.read_product(diagnostic_report_file("run-002")).payload
    assert payload["baseline_ref"] == "zengen-v1"
    assert payload["regression_flags"]["flags"], "a disjoint-CI drop against the baseline must be flagged"
    assert all(f["baseline_version"] == "zengen-v1" for f in payload["regression_flags"]["flags"])


@pytest.mark.parametrize("case", ["no_model", "not_effective", "no_links", "same_run", "no_report",
                                  "stub_report", "other_benchmark"])
def test_no_comparable_baseline_reads_as_none(seeded, case):
    if case == "no_model":
        pass
    elif case == "not_effective":
        _evaluator()(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001", status="deprecated")
    elif case == "no_links":
        _evaluator()(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001", links=[])
    elif case == "same_run":
        _evaluator()(seeded, "run-002", "cand-run-002")
        _promote(seeded, 1, "run-002")
    elif case == "no_report":
        _promote(seeded, 1, "run-001")
    elif case == "stub_report":
        seeded.write_product(diagnostic_report_file("run-001"), diagnostic_evaluator_stub("run-001", "c"), "stub")
        _promote(seeded, 1, "run-001")
    elif case == "other_benchmark":
        make_diagnostic_evaluator(taxonomy_version="tax-v1", item_pool_version="pool-v1",
                                  someval_runner=_someval(), judge_backend=_judge(),
                                  benchmark_version="bench-v0")(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends="mock") == (NO_BASELINE, [])


@pytest.mark.parametrize("payload", [
    {"backends": "mock", "benchmark_version": "bench-v1"},                           # 只有标记,没有分数
    {"backends": "mock", "benchmark_version": "bench-v1", "dimension_scores": []},   # 分数为空
    {"backends": "mock", "benchmark_version": "bench-v1", "dimension_scores": {}},   # 分数不是列表
])
def test_a_baseline_report_without_scores_reads_as_none(seeded, payload):
    # diagnostic_grade 对 backends:"mock" 不看分数就返回 mock;这里不能因 KeyError 让守护进程退出
    env = Envelope(id="diag-run-001", type="diagnostic_report", version=1, created_at="t",
                   created_by=OwnerRef(), status="executed", schema_version=1, links=["run-001"], payload=payload)
    seeded.write_product(diagnostic_report_file("run-001"), env, "seed odd baseline")
    _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends="mock") == (NO_BASELINE, [])
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends="real") == (NO_BASELINE, [])


@pytest.mark.parametrize("baseline,current", [("real", "mock"), ("mock", "real")])
def test_a_baseline_of_another_evidence_grade_reads_as_none(seeded, baseline, current):
    _evaluator(backends=baseline)(seeded, "run-001", "cand-run-001")
    _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends=current) == (NO_BASELINE, [])
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends=baseline)[0] == "zengen-v1"
    _evaluator(right=0, backends=current)(seeded, "run-002", "cand-run-002")  # 全错,但不与另一种等级比
    payload = seeded.read_product(diagnostic_report_file("run-002")).payload
    assert (payload["baseline_ref"], payload["regression_flags"]) == (NO_BASELINE, {"flags": []})


def test_the_highest_effective_version_wins_numerically(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    _evaluator()(seeded, "run-009", "cand-run-009")
    _promote(seeded, 9, "run-009")
    _promote(seeded, 10, "run-001")
    assert resolve_baseline(seeded, "run-011", "bench-v1", backends="mock")[0] == "zengen-v10"


def test_a_deprecated_top_version_falls_back_to_the_highest_effective_one(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    _evaluator()(seeded, "run-002", "cand-run-002")
    _promote(seeded, 1, "run-001")
    _promote(seeded, 2, "run-002", status="deprecated")
    ref, scores = resolve_baseline(seeded, "run-003", "bench-v1", backends="mock")
    assert ref == "zengen-v1"
    assert scores, "the fallback baseline must carry run-001's scores"


def test_injected_into_the_loop_it_replaces_the_stub_diagnostic(seeded):
    env = Envelope(id="run-001", type="directive", version=1, created_at="t", created_by=OwnerRef(),
                   status="pending", schema_version=1, payload={"target_dimension": "tom", "recipe_ref": ""})
    seeded.write_product(f"{run_dir('run-001')}/directive.yaml", env, "human: directive")
    ev = _evaluator()
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["dispatch_directive"]
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["request_diagnostic"]
    payload = seeded.read_product(diagnostic_report_file("run-001")).payload
    assert "stub" not in payload and payload["backends"] == "mock"
    assert payload["candidate_ref"] == seeded.read_product(candidate_checkpoint_file("run-001")).id
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["await_review"]


def test_diagnostic_never_imports_mindtrainer():
    root = Path(__file__).resolve().parents[1] / "src" / "diagnostic"
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n == "mindtrainer" or n.startswith("mindtrainer.") for n in names), path.name
