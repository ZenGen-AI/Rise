import pytest

from controlplane.paths import model_file
from recipestore.gain import (
    OK,
    GainResult,
    baseline_run_of,
    compute_gain,
    l1_score,
    read_report_payload,
    regression_flags,
)
from rs_fixtures import envelope, l1, make_store, write_model, write_report


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    # 基线:run-001 产出 zengen-v1,它的报告 tom=0.50
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.50), l1("tom", 0.9, level="l2")])
    return s


def test_gain_is_the_candidate_l1_minus_the_baseline_l1(store):
    write_report(store, "run-002", scores=[l1("tom", 0.62)], baseline_ref="zengen-v1")
    result = compute_gain(store, "run-002", "tom")
    assert result.gain == pytest.approx(0.12)
    assert (result.baseline_run_id, result.reason) == ("run-001", OK)


def test_a_negative_gain_is_reported_as_is(store):
    write_report(store, "run-002", scores=[l1("tom", 0.40)], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "tom").gain == pytest.approx(-0.10)


def test_only_l1_scores_count(store):
    write_report(store, "run-002", scores=[l1("tom", 0.99, level="l2")], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, None, "run-002: report has no l1 score for dimension 'tom'"
    )


def test_no_candidate_report(store):
    assert compute_gain(store, "run-002", "tom") == GainResult(None, None, "no diagnostic report for run-002")


def test_a_stub_candidate_report(store):
    write_report(store, "run-002", stub=True)
    assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 is a stub"


def test_no_baseline_ref(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="")
    assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 has no baseline_ref"


def test_a_baseline_ref_that_is_not_a_model_id(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="latest")
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, None, "baseline_ref 'latest' is not a zengen-vN model id"
    )


def test_no_baseline_model(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v7")
    assert compute_gain(store, "run-002", "tom").reason == "baseline model zengen-v7 not found"


def test_a_baseline_model_whose_links_do_not_name_a_run(store):
    store.write_product(model_file(2), envelope("zengen-v2", "zengen_model", "effective", {}, ["x"]), "t")
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v2")
    assert compute_gain(store, "run-002", "tom").reason == (
        "baseline model zengen-v2 links do not name exactly one producing run"
    )


def test_the_baseline_run_has_no_report(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom") == GainResult(
        None, "run-001", "no diagnostic report for baseline run run-001"
    )


def test_the_baseline_run_has_a_stub_report(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", stub=True)
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom").reason == "diagnostic report for baseline run run-001 is a stub"


def test_the_baseline_report_lacks_the_dimension(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6), l1("emotion", 0.7)], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "emotion") == GainResult(
        None, "run-001", "baseline run run-001: report has no l1 score for dimension 'emotion'"
    )


def test_different_benchmark_versions_are_not_comparable(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1", benchmark_version="bench-v2")
    result = compute_gain(store, "run-002", "tom")
    assert result.gain is None and "not comparable" in result.reason


def test_a_run_cannot_be_its_own_baseline(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-002")
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom") == GainResult(
        None, "run-002", "baseline zengen-v1 was produced by run-002 itself"
    )


def test_two_l1_scores_for_one_dimension_are_refused():
    payload = {"dimension_scores": [l1("tom", 0.1), l1("tom", 0.2)]}
    assert l1_score(payload, "tom") == (None, "report has 2 l1 scores for dimension 'tom'; refusing to pick one")


@pytest.mark.parametrize("score", ["0.5", None, True])
def test_a_non_numeric_score_is_refused(score):
    payload = {"dimension_scores": [dict(l1("tom", 0.1), score=score)]}
    assert l1_score(payload, "tom") == (None, "l1 score for dimension 'tom' is not a number")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_score_is_refused(score):
    payload = {"dimension_scores": [dict(l1("tom", 0.1), score=score)]}
    assert l1_score(payload, "tom") == (None, "l1 score for dimension 'tom' is not a finite number")


def test_a_real_candidate_against_a_mock_baseline_has_no_gain(store):
    # 夹具的基线报告是 mock;backends="real" 的报告判 real(没有标记的报告判 stub,不是 real)
    write_report(store, "run-002", scores=[l1("tom", 0.62)], baseline_ref="zengen-v1", backends="real")
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, "run-001", "baseline run run-001 evidence is mock while run-002 is real; scores are not comparable"
    )


def test_a_real_baseline_is_comparable_to_a_real_or_a_mock_candidate(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.50)], backends="real")
    write_report(s, "run-002", scores=[l1("tom", 0.60)], baseline_ref="zengen-v1", backends="real")
    write_report(s, "run-003", scores=[l1("tom", 0.70)], baseline_ref="zengen-v1", backends="mock")
    assert compute_gain(s, "run-002", "tom").gain == pytest.approx(0.10)
    assert compute_gain(s, "run-003", "tom").gain == pytest.approx(0.20)


def test_the_grade_comes_from_controlplane_evidence(store):
    # 没有 stub 键,但 backends 取值不认识、或没有标记:diagnostic_grade 判 stub(失败关闭),这里不另写一份判断
    for marker in ("Real", None):
        write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1", backends=marker)
        assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 is a stub"


def test_a_report_without_a_score_list_is_refused():
    assert l1_score({}, "tom") == (None, "report has no dimension_scores list")


def test_baseline_run_of_reads_the_links_the_daemon_writes(store):
    assert baseline_run_of(store, "zengen-v1") == ("run-001", OK)


def test_regression_flags_reads_the_flag_list(store):
    flag = {"dimension": "tom", "kind": "social_regression", "drop": 0.2}
    write_report(store, "run-002", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[flag])
    assert regression_flags(read_report_payload(store, "run-002")) == [flag]
    assert regression_flags(read_report_payload(store, "run-001")) == []


def test_a_stub_report_has_no_regression_flags(store):
    write_report(store, "run-002", stub=True)
    assert regression_flags(read_report_payload(store, "run-002")) == []


def test_read_report_payload_is_none_when_the_report_is_absent(store):
    assert read_report_payload(store, "run-404") is None


def test_a_report_written_by_the_diagnostic_package_is_read_correctly(tmp_path):
    # 本包不导入 diagnostic;这条测试用 diagnostic 自己的写法造报告,钉住字段名没有漂移。
    from diagnostic.datamodel import ConsistencySignals, DimensionScore, RegressionFlags
    from diagnostic.report import build_report
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.paths import diagnostic_report_file

    s = make_store(tmp_path)
    write_model(s, 1, "run-001")

    def _report(run_id, score, baseline_ref, flags):
        return build_report(
            report_id=run_id, candidate_ref=f"cand-{run_id}", baseline_ref=baseline_ref,
            benchmark_version="bench-v1", run_tier="quick", sampling_k=1,
            dimension_scores=[DimensionScore("tom", "l1", score, score - 0.1, score + 0.1, 30, "static")],
            error_profiles=[], regression_flags=RegressionFlags(flags=flags),
            consistency_signals=ConsistencySignals({}, 0.0), run_id=run_id,
        )

    def _write(run_id, report):
        """报告载荷取 DiagnosticReport.to_dict() 并带 backends: real(失败关闭:没有标记的报告判 stub)。
        不调 diagnostic.store_adapter.write_report:它的 backends 必填参数由计划 8 加入,本计划执行时
        还没有;计划 8 之后的 write_report 写出的载荷同样是 to_dict() 加这一键,这里钉住的字段名一致。"""
        s.write_product(
            diagnostic_report_file(run_id),
            Envelope(id=report.id, type="diagnostic_report", version=1, created_at="2026-09-27T00:00:00Z",
                     created_by=OwnerRef(), status="executed", schema_version=1, links=report.links,
                     payload={**report.to_dict(), "backends": "real"}),
            f"test: report {run_id}",
        )

    _write("run-001", _report("run-001", 0.5, "", []))
    _write("run-002", _report("run-002", 0.75, "zengen-v1", [{"kind": "g_probe_failure"}]))
    assert compute_gain(s, "run-002", "tom") == GainResult(0.25, "run-001", OK)
    assert regression_flags(read_report_payload(s, "run-002")) == [{"kind": "g_probe_failure"}]
