import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, posix } from "node:path";
import { readProduct, writeProduct, Envelope } from "./gittree.js";

// spec §1.3 需审核类合法迁移，与 Python controlplane/states.py 保持一致
const REVIEW_TRANSITIONS: Record<string, string[]> = {
  draft: ["pending_review"],
  pending_review: ["approved", "rejected"],
  approved: ["effective"],
  effective: ["deprecated", "superseded"],
  rejected: [],
  superseded: [],
  deprecated: [],
};

function assertTransition(from: string, to: string): void {
  if (!(REVIEW_TRANSITIONS[from] ?? []).includes(to)) {
    throw new Error(`review: ${from} -> ${to} not allowed`);
  }
}

function findCandidatePath(repoRoot: string, id: string): string {
  // 用 git ls-files 定位含该 id 的候选产物，避免绑定语言 fs 遍历库
  const listed = execFileSync(
    "git",
    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
    { encoding: "utf-8" },
  )
    .split("\n")
    .filter((line) => line.length > 0);
  for (const rel of listed) {
    if (readProduct(repoRoot, rel).id === id) {
      return rel;
    }
  }
  throw new Error(`candidate ${id} not found`);
}

// 诊断报告与候选同在一个 run 目录，布局与 Python controlplane/paths.py 的
// diagnostic_report_file 一致（两种语言只经 git 树里的文件通信，所以文件名在两边各写一次）。
function diagnosticPathFor(candidateRel: string): string {
  return `${posix.dirname(candidateRel)}/diagnostic_report.yaml`;
}

function loadDiagnostic(repoRoot: string, candidateRel: string): Envelope | null {
  const rel = diagnosticPathFor(candidateRel);
  return existsSync(join(repoRoot, rel)) ? readProduct(repoRoot, rel) : null;
}

function fmt(n: unknown): string {
  return typeof n === "number" ? n.toFixed(2) : "?";
}

function summarizeDiagnostic(report: Envelope | null, rel: string): string[] {
  if (report === null) {
    return [`diagnostic: MISSING (${rel}) — approve will refuse until a diagnostic report exists`];
  }
  const p = report.payload;
  if (p.stub === true) {
    // 占位报告没有分数也没有退化检查：醒目标出，别让人把它当真评测
    return [
      `diagnostic: [STUB] ${report.id} — placeholder, NOT a real evaluation; it carries no scores or regression checks`,
      `  candidate_ref=${String(p.candidate_ref)}`,
    ];
  }
  const lines = [
    `diagnostic: ${report.id} benchmark=${String(p.benchmark_version)} tier=${String(p.run_tier)} ` +
      `k=${String(p.sampling_k)} candidate_ref=${String(p.candidate_ref)}`,
  ];
  const dims = Array.isArray(p.dimension_scores) ? (p.dimension_scores as Record<string, unknown>[]) : [];
  const l1 = dims.filter((d) => d.level === "l1");
  if (l1.length === 0) {
    lines.push("  (no l1 scores in this report)");
  }
  for (const d of l1) {
    lines.push(
      `  l1 ${String(d.dimension)} score=${fmt(d.score)} ci=[${fmt(d.ci_low)},${fmt(d.ci_high)}] ` +
        `n=${String(d.sample_size)} track=${String(d.track_provenance)}`,
    );
  }
  // 字段缺失 ≠ 没有退化标记：前者必须说"读不出来"，不能打印成"none"
  const rawFlags = (p.regression_flags as { flags?: unknown } | undefined)?.flags;
  if (!Array.isArray(rawFlags)) {
    lines.push("  regression flags: UNREADABLE (field missing or malformed)");
  } else if (rawFlags.length === 0) {
    lines.push("  regression flags: none");
  } else {
    lines.push(`  regression flags (${rawFlags.length}):`);
    for (const f of rawFlags) {
      lines.push(`    ${JSON.stringify(f)}`);
    }
  }
  lines.push(`  full report: ${rel}`);
  return lines;
}

export function review(repoRoot: string, id: string): string {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  const header = `${env.id} [${env.status}] type=${env.type} links=${env.links.join(",")}`;
  return [header, ...summarizeDiagnostic(loadDiagnostic(repoRoot, rel), diagnosticPathFor(rel))].join("\n");
}

export function approve(repoRoot: string, id: string): void {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  assertTransition(env.status, "approved");
  // 审核门是证据驱动的（spec：诊断报告附于 checkpoint 晋级门）：没有诊断报告就不能批准。
  // 这只保证证据在场，不证明审批人读过——所以 review 才会把证据直接打印出来。
  if (loadDiagnostic(repoRoot, rel) === null) {
    throw new Error(
      `approve: ${id} has no diagnostic report (${diagnosticPathFor(rel)}); ` +
        `the review gate is evidence-driven — wait for the diagnostic to be produced, then run "review ${id}"`,
    );
  }
  env.status = "approved";
  writeProduct(repoRoot, rel, env, `approve ${id}`);
}

export function reject(repoRoot: string, id: string, reason: string): void {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  assertTransition(env.status, "rejected");
  env.status = "rejected";
  env.payload.reject_reason = reason;
  writeProduct(repoRoot, rel, env, `reject ${id}`);
}

export function status(repoRoot: string): string {
  const listed = execFileSync(
    "git",
    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
    { encoding: "utf-8" },
  )
    .split("\n")
    .filter((line) => line.length > 0);
  const lines = listed
    .map((rel) => readProduct(repoRoot, rel))
    .filter((env) => env.status === "pending_review")
    .map((env) => `${env.id} [${env.status}]`);
  return lines.length > 0 ? lines.join("\n") : "no candidates awaiting review";
}
