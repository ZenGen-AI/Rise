"""诊断证据等级(闭环集成设计 §3.1、R17)。全系统只有这一处实现:
reconcile 扩展、提名、规划处理器都调这里,不各自判断。"""
from __future__ import annotations

GRADES = ("stub", "mock", "real")


def diagnostic_grade(payload: dict) -> str:
    """给一份诊断报告的载荷定等级。

    - payload["stub"] 为真 → "stub"(占位报告,优先于其余字段)
    - payload["backends"] == "mock" → "mock"
    - backends == "real" 且 dimension_scores 是非空列表 → "real"
    - 其余(没有 backends 标记、取值不认识、没有分数、空报告)→ "stub":失败关闭,判不出来就按最弱的证据算
    """
    if not isinstance(payload, dict):
        raise TypeError(f"diagnostic payload must be a mapping, got {type(payload).__name__}")
    if payload.get("stub"):
        return "stub"
    if payload.get("backends") == "mock":
        return "mock"
    scores = payload.get("dimension_scores")
    has_scores = isinstance(scores, list) and len(scores) > 0
    if has_scores and payload.get("backends") == "real":
        return "real"
    return "stub"


def evidence_acceptable(grade: str, accept_mock: bool) -> bool:
    """real 恒可接受;mock 仅当 accept_mock 为真;stub 恒不可接受。"""
    if grade not in GRADES:
        raise ValueError(f"unknown evidence grade {grade!r}; expected one of {GRADES}")
    # 严格要求 bool:手改 YAML 写成 "false"(非空字符串)时,bool() 会把它当成真而放行 mock 证据,
    # 与 DX-12「默认拒绝 mock」的保证相悖。宁可抛错,也不静默放行。
    if not isinstance(accept_mock, bool):
        raise TypeError(f"accept_mock must be a bool, got {type(accept_mock).__name__}")
    if grade == "real":
        return True
    if grade == "mock":
        return accept_mock
    return False
