from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.datamodel import DiagnosticReport

_NOW = "2026-09-27T00:00:00Z"

# 诊断报告载荷里的后端标记(闭环集成设计 R17)。controlplane.evidence.diagnostic_grade 读它定等级;
# cli/src/commands.ts 的 summarizeDiagnostic 读它打印 [MOCK BACKENDS]。两侧各写一次。
BACKENDS_KEY = "backends"
BACKENDS_VALUES = ("mock", "real")


def write_report(
    store: GitStore, run_id: str, report: DiagnosticReport, *, backends: str,
    extra: dict | None = None,
) -> None:
    """backends("mock" 或 "real")必填,写进载荷的 backends 键:没有标记的报告按失败关闭判 stub。
    标记与报告在同一次写入里落盘,不存在"报告已写、标记未写"的窗口。"""
    if backends not in BACKENDS_VALUES:
        raise ValueError(f"backends must be one of {BACKENDS_VALUES}, got {backends!r}")
    payload = report.to_dict()
    payload[BACKENDS_KEY] = backends
    if extra:
        payload.update(extra)
    env = Envelope(
        id=report.id,
        type="diagnostic_report",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=report.links,
        payload=payload,
    )
    store.write_product(
        diagnostic_report_file(run_id), env, message=f"diagnostic_report: {report.id} for {run_id}"
    )


def read_report(store: GitStore, run_id: str) -> DiagnosticReport:
    env = store.read_product(diagnostic_report_file(run_id))
    return DiagnosticReport.from_dict(env.payload)


def write_failure_report(
    store: GitStore, run_id: str, *, candidate_ref: str, benchmark_version: str, failure_reason: str
) -> None:
    """确定性失败的评测:写一份 stub 报告,而不是让动作一直失败(设计 21 的失败处理表)。

    diagnostic_grade 对 stub 失败关闭,所以它不会驱动规划与提名;审核门 review 标 [STUB]。
    failure_reason 只能是不含题面、凭证的简短说明,调用方负责。"""
    env = Envelope(
        id=run_id,
        type="diagnostic_report",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id],
        payload={
            "candidate_ref": candidate_ref,
            "benchmark_version": benchmark_version,
            "stub": True,
            "failure_reason": failure_reason,
        },
    )
    store.write_product(
        diagnostic_report_file(run_id), env, message=f"diagnostic_report: evaluation of {candidate_ref} failed"
    )
