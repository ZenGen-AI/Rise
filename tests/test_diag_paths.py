from controlplane.paths import (
    SUBDIRS, run_dir, diagnostic_report_file, taxonomy_file, item_pool_file,
)


def test_diagnostics_subdir_registered():
    assert "diagnostics" in SUBDIRS


def test_diagnostic_report_lives_under_run_dir():
    p = diagnostic_report_file("run-001")
    assert p == f"{run_dir('run-001')}/diagnostic_report.yaml"


def test_taxonomy_file_is_versioned_under_diagnostics():
    assert taxonomy_file("tax-v1") == "control-plane/diagnostics/taxonomy_tax-v1.yaml"


def test_item_pool_file_is_versioned_under_diagnostics():
    assert item_pool_file("pool-v1") == "control-plane/diagnostics/item_pool_pool-v1.yaml"
