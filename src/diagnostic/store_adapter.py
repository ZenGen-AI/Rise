from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.datamodel import DiagnosticReport

_NOW = "2026-09-27T00:00:00Z"


def write_report(store: GitStore, run_id: str, report: DiagnosticReport) -> None:
    env = Envelope(
        id=report.id,
        type="diagnostic_report",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="executed",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=report.links,
        payload=report.to_dict(),
    )
    store.write_product(
        diagnostic_report_file(run_id), env, message=f"diagnostic_report: {report.id} for {run_id}"
    )


def read_report(store: GitStore, run_id: str) -> DiagnosticReport:
    env = store.read_product(diagnostic_report_file(run_id))
    return DiagnosticReport.from_dict(env.payload)
