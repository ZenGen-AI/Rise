import pytest

from controlplane.evidence import GRADES, diagnostic_grade, evidence_acceptable
from simulator.stubs import diagnostic_evaluator_stub

_SCORES = [{"dimension": "tom", "level": "l1", "score": 0.6}]


def test_the_stub_report_is_graded_stub():
    assert diagnostic_grade(diagnostic_evaluator_stub("run-1", "cand-run-1").payload) == "stub"


@pytest.mark.parametrize(
    ("payload", "grade"),
    [
        ({"stub": True, "dimension_scores": _SCORES, "backends": "real"}, "stub"),  # stub 优先
        ({"backends": "mock"}, "mock"),
        ({"backends": "mock", "dimension_scores": _SCORES}, "mock"),
        ({"backends": "mock", "dimension_scores": []}, "mock"),
        ({"stub": False, "backends": "mock", "dimension_scores": _SCORES}, "mock"),
        ({"dimension_scores": _SCORES}, "stub"),  # 现有 run_diagnostic 报告:没有 backends 标记,失败关闭
        ({"backends": "real", "dimension_scores": _SCORES}, "real"),
        ({"stub": False, "backends": "real", "dimension_scores": _SCORES}, "real"),
        ({"stub": False, "dimension_scores": _SCORES}, "stub"),
        ({}, "stub"),
        ({"dimension_scores": []}, "stub"),
        ({"backends": "real", "dimension_scores": []}, "stub"),
        ({"backends": "real"}, "stub"),
        ({"backends": "unknown", "dimension_scores": _SCORES}, "stub"),
        ({"backends": None, "dimension_scores": _SCORES}, "stub"),
        ({"dimension_scores": "lots"}, "stub"),
        ({"stub": "yes", "dimension_scores": _SCORES}, "stub"),  # stub 字段取真值判定
    ],
)
def test_grade_rules(payload, grade):
    assert diagnostic_grade(payload) == grade


def test_grade_does_not_mutate_the_payload():
    payload = {"backends": "mock", "dimension_scores": list(_SCORES)}
    snapshot = {"backends": "mock", "dimension_scores": list(_SCORES)}
    diagnostic_grade(payload)
    assert payload == snapshot


@pytest.mark.parametrize("bad", [None, [], "stub"])
def test_a_non_mapping_payload_is_a_type_error(bad):
    with pytest.raises(TypeError):
        diagnostic_grade(bad)


@pytest.mark.parametrize(
    ("grade", "accept_mock", "ok"),
    [
        ("real", False, True), ("real", True, True),
        ("mock", False, False), ("mock", True, True),
        ("stub", False, False), ("stub", True, False),
    ],
)
def test_acceptability(grade, accept_mock, ok):
    assert evidence_acceptable(grade, accept_mock) is ok


@pytest.mark.parametrize("grade", ["real", "mock", "stub"])
@pytest.mark.parametrize("bad", ["false", "true", "", 0, 1, None])
def test_a_non_bool_accept_mock_is_a_type_error_not_a_silent_yes(grade, bad):
    # 手改 YAML 写出 accept_mock_evidence: "false" 是非空字符串,bool() 会把它当成真,放行 mock 证据。
    with pytest.raises(TypeError, match="accept_mock"):
        evidence_acceptable(grade, bad)


def test_an_unknown_grade_is_a_value_error():
    with pytest.raises(ValueError, match="bogus"):
        evidence_acceptable("bogus", True)


def test_grades_constant():
    assert GRADES == ("stub", "mock", "real")
