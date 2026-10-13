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
    store: GitStore, run_id: str, report: DiagnosticReport, *, backends: str
) -> None:
    """backends("mock" 或 "real")必填,写进载荷的 backends 键:没有标记的报告按失败关闭判 stub。
    标记与报告在同一次写入里落盘,不存在"报告已写、标记未写"的窗口。"""
    if backends not in BACKENDS_VALUES:
        raise ValueError(f"backends must be one of {BACKENDS_VALUES}, got {backends!r}")
    payload = report.to_dict()
    payload[BACKENDS_KEY] = backends
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
