from controlplane.gitstore import GitStore
from diagnostic.orchestrator import run_diagnostic, DiagnosticConfig
from diagnostic.taxonomy_loader import ItemPool
from diagnostic.datamodel import DiagnosticTaxonomy, DiagnosticItem, VariantFamily
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.scorer import MockJudgeBackend
from diagnostic.store_adapter import read_report


def _setup():
    tax = DiagnosticTaxonomy(
        "tax-v1", ["mental_representation"],
        {"mental_representation": ["belief"]},
        {"belief": ["false_belief_transfer"]},
        ["format_compliance"],
    )
    items = [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
        DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1"),
    ]
    pool = ItemPool("pool-v1", items, [VariantFamily("vf1", "i1", ["i1v"])])
    someval = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "B", "B"], "i1v": ["C", "C", "C", "C"]})
    judge = MockJudgeBackend(model_id="external-judge", fixed={})
    return tax, pool, someval, judge


def test_run_diagnostic_end_to_end_writes_report(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    tax, pool, someval, judge = _setup()
    cfg = DiagnosticConfig(
        run_id="run-1", candidate_ref="cand-1", candidate_model_id="cand-model",
        baseline_ref="zengen-v3", tier="full", sampling_k=4,
        benchmark_version="bench-v1",
    )
    report = run_diagnostic(cfg, tax, pool, someval, judge, store,
                            baseline_scores=[], dynamic_specs=[], backends="mock")
    # 报告已落盘,可读回
    got = read_report(store, "run-1")
    assert got.id == report.id
    assert got.benchmark_version == "bench-v1"
    # 至少产出 L1 维度分,且带轨道来源标注
    assert any(d.level == "l1" for d in got.dimension_scores)
    assert all(d.track_provenance in ("static", "dynamic", "fused")
               for d in got.dimension_scores)


def test_run_diagnostic_static_only_track_provenance(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    tax, pool, someval, judge = _setup()
    cfg = DiagnosticConfig("run-2", "cand-1", "cand-model", "zengen-v3", "quick", 4, "bench-v1")
    report = run_diagnostic(cfg, tax, pool, someval, judge, store,
                            baseline_scores=[], dynamic_specs=[], backends="mock")
    # 无动态 specs → 纯静态轨,轨道来源都是 static
    assert all(d.track_provenance == "static" for d in report.dimension_scores)


def test_diagnostic_evaluator_stub_shape():
    from simulator.stubs import diagnostic_evaluator_stub
    env = diagnostic_evaluator_stub("run-1", "cand-1")
    assert env.type == "diagnostic_report"
    assert env.status == "executed"
    assert "run-1" in env.links
    assert env.payload["candidate_ref"] == "cand-1"
