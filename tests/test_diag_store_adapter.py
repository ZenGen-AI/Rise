from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.datamodel import DiagnosticReport, RegressionFlags, ConsistencySignals
from diagnostic.store_adapter import write_report, read_report


def _report():
    return DiagnosticReport(
        "diag-1", "cand-1", "zengen-v3", "bench-v1", "full", 4,
        [], [], RegressionFlags([]), ConsistencySignals({}, 0.0), ["run-1"])


def test_write_then_read_report_round_trips(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report(), backends="mock")
    got = read_report(store, "run-1")
    assert got == _report()


def test_report_written_at_spec_path(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report(), backends="mock")
    assert store.exists(diagnostic_report_file("run-1"))


def test_write_is_idempotent(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report(), backends="mock")
    write_report(store, "run-1", _report(), backends="mock")   # 逐字节相同,不应报错/重复提交
    assert read_report(store, "run-1") == _report()


def test_read_goes_through_migration(tmp_path):
    # 写入的 envelope 必须能被 migration.load_product 正常读回(校验通过)
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report(), backends="mock")
    env = store.read_product(diagnostic_report_file("run-1"))
    assert env.type == "diagnostic_report"
    assert env.payload["candidate_ref"] == "cand-1"
