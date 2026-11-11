"""诊断资产(分类表与题库)的信封读写,以及 write_report / run_diagnostic 的 backends 标记(R17)。"""
import shutil

import pytest
import yaml

from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.assets import (
    ITEM_POOL_TYPE,
    TAXONOMY_TYPE,
    DiagnosticAssetError,
    read_item_pool,
    read_taxonomy,
    write_item_pool,
    write_taxonomy,
)
from diagnostic.datamodel import (
    ConsistencySignals,
    DiagnosticItem,
    DiagnosticReport,
    DiagnosticTaxonomy,
    RegressionFlags,
    VariantFamily,
)
from diagnostic.orchestrator import DiagnosticConfig, run_diagnostic
from diagnostic.paths import item_pool_file, taxonomy_file
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.store_adapter import read_report, write_report
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


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _report_obj():
    return DiagnosticReport("diag-1", "cand-1", "zengen-v3", "bench-v1", "full", 4,
                            [], [], RegressionFlags([]), ConsistencySignals({}, 0.0), ["run-1"])


# ---- 资产 ----

def test_assets_round_trip_as_envelopes_at_the_versioned_paths(store):
    assert write_taxonomy(store, TAX) == taxonomy_file("tax-v1")
    assert write_item_pool(store, POOL) == item_pool_file("pool-v1")
    assert store.read_product(taxonomy_file("tax-v1")).type == TAXONOMY_TYPE == "diagnostic_taxonomy"
    assert store.read_product(item_pool_file("pool-v1")).type == ITEM_POOL_TYPE == "diagnostic_item_pool"
    assert store.read_product(taxonomy_file("tax-v1")).status == "effective"
    assert read_taxonomy(store, "tax-v1") == TAX
    assert read_item_pool(store, "pool-v1").to_dict() == POOL.to_dict()


def test_rewriting_the_same_asset_is_a_no_op(store):
    write_taxonomy(store, TAX)
    head = store._git("rev-parse", "HEAD")
    write_taxonomy(store, TAX)
    assert store._git("rev-parse", "HEAD") == head


def test_a_missing_asset_fails_loudly(store):
    with pytest.raises(DiagnosticAssetError, match="taxonomy_tax-v9"):
        read_taxonomy(store, "tax-v9")
    with pytest.raises(DiagnosticAssetError, match="item_pool_pool-v9"):
        read_item_pool(store, "pool-v9")


def test_a_wrong_product_type_at_the_asset_path_is_refused(store):
    env = Envelope(id="x", type="config", version=1, created_at="t", created_by=OwnerRef(),
                   status="effective", schema_version=1, payload=TAX.to_dict())
    store.write_product(taxonomy_file("tax-v1"), env, "seed wrong type")
    with pytest.raises(DiagnosticAssetError, match="not 'diagnostic_taxonomy'"):
        read_taxonomy(store, "tax-v1")


def test_a_version_mismatch_inside_the_asset_is_refused(store):
    write_taxonomy(store, DiagnosticTaxonomy("tax-v2", [], {}, {}, []))
    shutil.copy(store.repo_root / taxonomy_file("tax-v2"), store.repo_root / taxonomy_file("tax-v1"))
    with pytest.raises(DiagnosticAssetError, match="'tax-v2', not 'tax-v1'"):
        read_taxonomy(store, "tax-v1")


# ---- write_report 的 backends 参数 ----

def test_write_report_requires_the_backends_marker(store):
    with pytest.raises(TypeError, match="backends"):
        write_report(store, "run-1", _report_obj())  # 失败关闭:没有标记就没有真证据
    assert not store.exists(diagnostic_report_file("run-1"))


@pytest.mark.parametrize("backends", ["mock", "real"])
def test_write_report_records_the_backends_marker_and_still_reads_back(store, backends):
    write_report(store, "run-1", _report_obj(), backends=backends)
    assert store.read_product(diagnostic_report_file("run-1")).payload["backends"] == backends
    assert read_report(store, "run-1") == _report_obj()  # from_dict 忽略多出的键


def test_write_report_rejects_an_unknown_marker(store):
    with pytest.raises(ValueError, match="backends"):
        write_report(store, "run-1", _report_obj(), backends="fake")
    assert not store.exists(diagnostic_report_file("run-1"))


def test_the_backends_marker_lands_in_the_same_commit_as_the_report(store):
    write_report(store, "run-1", _report_obj(), backends="mock")
    assert store._git("log", "--format=%s").splitlines()[0] == "diagnostic_report: diag-1 for run-1"
    assert len(store._git("log", "--format=%s").splitlines()) == 1


@pytest.mark.parametrize("backends", ["mock", "real"])
def test_the_written_report_carries_the_marker_under_the_key_the_readers_use(store, backends):
    """controlplane.evidence 与 cli/src/commands.ts 各自写死键名 backends:钉落盘的报告,而不是常量。"""
    write_report(store, "run-1", _report_obj(), backends=backends)
    on_disk = yaml.safe_load((store.repo_root / diagnostic_report_file("run-1")).read_text())
    assert on_disk["payload"]["backends"] == backends


@pytest.mark.parametrize("backends", ["mock", "real"])
def test_run_diagnostic_passes_the_marker_through(store, backends):
    someval = MockSoMEvalRunner(fixed={"i1": ["B"] * 4, "i1v": ["C"] * 4})
    cfg = DiagnosticConfig("run-1", "cand-1", "cand-model", "none", "full", 4, "bench-v1")
    run_diagnostic(cfg, TAX, POOL, someval, MockJudgeBackend("external-judge", {}), store,
                   baseline_scores=[], dynamic_specs=[], backends=backends)
    payload = store.read_product(diagnostic_report_file("run-1")).payload
    assert payload["backends"] == backends
    assert diagnostic_grade(payload) == backends


def test_run_diagnostic_rejects_an_unknown_marker_and_writes_nothing(store):
    someval = MockSoMEvalRunner(fixed={"i1": ["B"] * 4, "i1v": ["C"] * 4})
    cfg = DiagnosticConfig("run-1", "cand-1", "cand-model", "none", "full", 4, "bench-v1")
    with pytest.raises(ValueError, match="backends"):
        run_diagnostic(cfg, TAX, POOL, someval, MockJudgeBackend("external-judge", {}), store,
                       baseline_scores=[], dynamic_specs=[], backends="fake")
    assert not store.exists(diagnostic_report_file("run-1"))


def test_run_diagnostic_requires_the_backends_marker(store):
    someval = MockSoMEvalRunner(fixed={"i1": ["B"] * 4, "i1v": ["C"] * 4})
    cfg = DiagnosticConfig("run-1", "cand-1", "cand-model", "none", "full", 4, "bench-v1")
    with pytest.raises(TypeError, match="backends"):
        run_diagnostic(cfg, TAX, POOL, someval, MockJudgeBackend("external-judge", {}), store,
                       baseline_scores=[], dynamic_specs=[])
    assert not store.exists(diagnostic_report_file("run-1"))


def test_the_existing_stub_report_still_grades_stub():
    assert diagnostic_grade(diagnostic_evaluator_stub("run-001", "cand-run-001").payload) == "stub"
