"""提案打包器(闭环集成设计 §4.2、§8.3):验证通过的草案写成 skill_memory_proposal 产物。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from controlplane.envelope import Envelope, OwnerRef, bump
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, proposal_file
from mindagent.analysis import BehaviorFinding
from mindagent.proposals import ProposalDraft
from mindagent.verify import Verification

PROPOSAL_TYPE = "skill_memory_proposal"


@dataclass(frozen=True)
class Thresholds:
    min_fired_on_motivating: float
    max_fired_elsewhere: float


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_id_for(round_id: str, n: int) -> str:
    return f"proposal-{round_id}-{n}"


def screen(
    drafts: list[ProposalDraft] | tuple[ProposalDraft, ...],
    verifications: list[Verification] | tuple[Verification, ...],
    thresholds: Thresholds,
) -> tuple[list[int], list[dict]]:
    """返回 (通过的草案下标, dropped 列表)。dropped 每项 {finding_id, reason, fired_on_motivating, fired_elsewhere}。"""
    if len(drafts) != len(verifications):
        raise ValueError(f"{len(drafts)} drafts but {len(verifications)} verifications")
    kept: list[int] = []
    dropped: list[dict] = []
    for i, (draft, v) in enumerate(zip(drafts, verifications)):
        reasons = []
        if v.fired_on_motivating < thresholds.min_fired_on_motivating:
            reasons.append(
                f"fired_on_motivating {v.fired_on_motivating:.3f} < {thresholds.min_fired_on_motivating}"
            )
        if v.fired_elsewhere > thresholds.max_fired_elsewhere:
            reasons.append(f"fired_elsewhere {v.fired_elsewhere:.3f} > {thresholds.max_fired_elsewhere}")
        if reasons:
            dropped.append({
                "finding_id": draft.finding_id, "reason": "; ".join(reasons),
                "fired_on_motivating": v.fired_on_motivating, "fired_elsewhere": v.fired_elsewhere,
            })
        else:
            kept.append(i)
    return kept, dropped


def proposal_payload(
    round_id: str, finding: BehaviorFinding, draft: ProposalDraft, verification: Verification,
    *, run_id: str, bound_model_id: str,
) -> dict:
    return {
        "round_id": round_id,
        "bound_model_id": bound_model_id,
        "run_id": run_id,
        "generator": draft.generator,
        "finding": {
            "id": finding.id,
            "pattern": finding.pattern,
            "support": finding.support,
            "ratio": finding.ratio,
            "episode_refs": list(finding.episode_refs),
        },
        "increments": [{"kind": inc["kind"], "definition": dict(inc["definition"])} for inc in draft.increments],
        "verification": verification.to_dict(),
    }


def package(
    store: GitStore,
    round_id: str,
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    drafts: list[ProposalDraft] | tuple[ProposalDraft, ...],
    verifications: list[Verification] | tuple[Verification, ...],
    *,
    thresholds: Thresholds,
    run_id: str,
    bound_model_id: str,
) -> list[Envelope]:
    """通过筛选的草案按顺序编号 proposal-<round_id>-<n>(n 从 1 起),写成 pending_review,返回写下
    (或已在树里)的信封。重入:同 id 的提案已不是 pending_review(人已审过)就原样保留;仍是
    pending_review 且载荷相同就不重写;载荷变了就就地改写,信封 `version` 在旧版本上加一(`controlplane.envelope.bump`),
    不重置为 1。"""
    by_id = {f.id: f for f in findings}
    kept, _ = screen(drafts, verifications, thresholds)
    out: list[Envelope] = []
    for n, i in enumerate(kept, start=1):
        draft, verification = drafts[i], verifications[i]
        pid = proposal_id_for(round_id, n)
        rel = proposal_file(pid)
        payload = proposal_payload(
            round_id, by_id[draft.finding_id], draft, verification,
            run_id=run_id, bound_model_id=bound_model_id,
        )
        existing = None
        if store.exists(rel):
            existing = store.read_product(rel)
            if existing.status != "pending_review" or existing.payload == payload:
                out.append(existing)
                continue
        env = Envelope(
            id=pid, type=PROPOSAL_TYPE, version=1, created_at=_now(), created_by=OwnerRef(),
            status="pending_review", schema_version=1, links=[round_id, draft.finding_id],
            payload=payload,
        )
        env = bump(env, existing)                    # 改写已有产物:版本号在旧版本上加一,读取方看得出它改过
        store.write_product(rel, env, f"mindagent: proposal {pid}")
        out.append(env)
    return out


def stale_proposals(store: GitStore, round_id: str, kept: int) -> list[dict]:
    """本轮重入后编号超出 kept 的旧提案(上一次运行留下、这次筛选没再产出的)。只报告仍为
    pending_review 的那些,原样留在树里不删不改,由轮次处理器写进 dropped 提醒审核人;
    已审过的不报告(人已经看过)。按编号排序。"""
    prefix = f"proposal-{round_id}-"
    root = store.repo_root / CONTROL_PLANE_ROOT / "skill_memory_proposals"
    if not root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for sub in root.iterdir():
        tail = sub.name[len(prefix):] if sub.name.startswith(prefix) else ""
        if tail.isdecimal() and int(tail) > kept and store.exists(proposal_file(sub.name)):
            found.append((int(tail), sub.name))
    out = []
    for _, pid in sorted(found):
        env = store.read_product(proposal_file(pid))
        if env.status == "pending_review":
            out.append({
                "proposal_id": pid,
                "reason": "stale: left by an earlier run of this round, not produced again; do not approve",
            })
    return out
