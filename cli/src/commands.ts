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

// 非审核类合法迁移，与 Python controlplane/states.py 的 NONREVIEW_TRANSITIONS 保持一致
// （awaiting_confirmation 由闭环集成设计 §3.4 引入）。命令行只用到 confirm 这一条。
const NONREVIEW_TRANSITIONS: Record<string, string[]> = {
  pending: ["executed", "failed"],
  awaiting_confirmation: ["pending", "failed"],
  executed: ["consumed"],
  consumed: [],
  failed: [],
};

function assertTransition(from: string, to: string): void {
  if (!(REVIEW_TRANSITIONS[from] ?? []).includes(to)) {
    throw new Error(`review: ${from} -> ${to} not allowed`);
  }
}

// ---- 跨语言约定：路径模式。两种语言只经 git 树里的文件通信，所以每个模式在两侧各写一次 ----
// candidate_checkpoint  ↔ Python controlplane/paths.py candidate_checkpoint_file
// recipe_candidate      ↔ Python controlplane/paths.py recipe_candidate_file（daemon._read_gate_views 扫同一目录）
// skill_memory_proposal ↔ Python controlplane/paths.py proposal_file（daemon._read_gate_views 逐个子目录读 proposal.yaml）
export type GateKind = "candidate_checkpoint" | "recipe_candidate" | "skill_memory_proposal";

const GATE_PATTERNS: ReadonlyArray<{ kind: GateKind; pattern: string }> = [
  { kind: "candidate_checkpoint", pattern: "control-plane/runs/*/candidate_checkpoint.yaml" },
  { kind: "recipe_candidate", pattern: "control-plane/recipe_candidates/*.yaml" },
  { kind: "skill_memory_proposal", pattern: "control-plane/skill_memory_proposals/*/proposal.yaml" },
];

// 指令 ↔ Python controlplane/daemon.py read_tree_state 读的 f"{run_dir(run_id)}/directive.yaml"
const DIRECTIVE_PATTERN = "control-plane/runs/*/directive.yaml";

function lsFiles(repoRoot: string, pattern: string): string[] {
  // 用 git ls-files 定位产物，避免绑定语言 fs 遍历库
  return execFileSync("git", ["-C", repoRoot, "ls-files", pattern], { encoding: "utf-8" })
    .split("\n")
    .filter((line) => line.length > 0);
}

interface Located {
  kind: GateKind;
  rel: string;
  env: Envelope;
}

function findGateProduct(repoRoot: string, id: string): Located {
  const hits: Located[] = [];
  for (const { kind, pattern } of GATE_PATTERNS) {
    for (const rel of lsFiles(repoRoot, pattern)) {
      const env = readProduct(repoRoot, rel);
      if (env.id === id) {
        hits.push({ kind, rel, env });
      }
    }
  }
  if (hits.length === 0) {
    throw new Error(`candidate ${id} not found`);
  }
  if (hits.length > 1) {
    throw new Error(`id ${id} is ambiguous: ${hits.map((h) => h.rel).join(", ")}`);
  }
  return hits[0];
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

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// 审核门③"证据在场"规则 ↔ Python recipestore/admit.py 的 has_repro_evidence（计划 3 任务 45）。
// 两侧逐条一致：repro 是对象，且 errors 与 warnings 都是数组（可为空）；errors 非空仍允许批准。
export function hasReproEvidence(payload: Record<string, unknown>): boolean {
  const repro = payload.repro;
  return isRecord(repro) && Array.isArray(repro.errors) && Array.isArray(repro.warnings);
}

function isNonEmptyRecord(v: unknown): v is Record<string, unknown> {
  return isRecord(v) && Object.keys(v).length > 0;
}

const UNREADABLE = "UNREADABLE (field missing or malformed)";

function summarizeDiagnostic(report: Envelope | null, rel: string): string[] {
  if (report === null) {
    return [`diagnostic: MISSING (${rel}) — approve will refuse until a diagnostic report exists`];
  }
  const p = report.payload;
  // stub 标记 ↔ Python simulator/stubs.py 的 diagnostic_evaluator_stub；等级判定 ↔ controlplane/evidence.py diagnostic_grade
  if (p.stub === true) {
    // 占位报告没有分数也没有退化检查：醒目标出，别让人把它当真评测
    return [
      `diagnostic: [STUB] ${report.id} — placeholder, NOT a real evaluation; it carries no scores or regression checks`,
      `  candidate_ref=${String(p.candidate_ref)}`,
    ];
  }
  // 后端标记（闭环集成设计 R17）↔ Python diagnostic/store_adapter.py 的 BACKENDS_KEY，
  // 等级判定 ↔ controlplane/evidence.py diagnostic_grade
  const mock = p.backends === "mock";
  const lines = [
    `diagnostic: ${mock ? "[MOCK BACKENDS] " : ""}${report.id} benchmark=${String(p.benchmark_version)} ` +
      `tier=${String(p.run_tier)} k=${String(p.sampling_k)} candidate_ref=${String(p.candidate_ref)}`,
  ];
  if (mock) {
    lines.push("  [MOCK BACKENDS] scores come from mock SoMEval/judge backends — they show the pipeline ran, not model quality");
  } else if ("backends" in p && p.backends !== "real") {
    lines.push(`  backends: UNREADABLE (expected "mock" or "real", got ${JSON.stringify(p.backends)})`);
  }
  // 载荷键 ↔ Python diagnostic/datamodel.py 的 DiagnosticReport.to_dict
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

function listLines(label: string, v: unknown): string[] {
  if (!Array.isArray(v)) {
    return [`  ${label}: ${UNREADABLE}`];
  }
  if (v.length === 0) {
    return [`  ${label}: none`];
  }
  return [`  ${label} (${v.length}):`, ...v.map((x) => `    ${typeof x === "string" ? x : JSON.stringify(x)}`)];
}

// 配方候选载荷键 ↔ 闭环集成设计 §4.2「配方」；Python 写方是 trainscheduler.build_recipe_candidate（计划 5），
// 校验结果 repro 来自 recipestore.validate_reproducibility（计划 3）。
function summarizeRecipeCandidate(env: Envelope, rel: string): string[] {
  const p = env.payload;
  const lines = [
    `recipe: type=${String(p.recipe_type)} dimension=${String(p.dimension)} ` +
      `source_run=${String(p.source_run_id)} nondeterministic=${String(p.nondeterministic)}`,
  ];
  const anchor = p.eval_anchor;
  if (!isRecord(anchor)) {
    lines.push(`  eval_anchor: ${UNREADABLE}`);
  } else {
    // 值为 null 是"暂无"（设计 §4.2 规定暂无的填 null），照实打印，不当成读不出来
    const parts = Object.entries(anchor).map(([k, v]) => `${k}=${v === null ? "null" : String(v)}`);
    lines.push(`  eval_anchor: ${parts.length > 0 ? parts.join(" ") : "(empty)"}`);
  }
  const result = p.result;
  if (!isRecord(result)) {
    lines.push(`  result: ${UNREADABLE}`);
  } else {
    lines.push(
      `  result: gain=${result.gain === null ? "null" : fmt(result.gain)} baseline_ref=${String(result.baseline_ref)} ` +
        `candidate_ref=${String(result.candidate_ref)} promoted=${String(result.promoted)}`,
    );
  }
  const repro = p.repro;
  if (!isRecord(repro)) {
    lines.push(`  repro: ${UNREADABLE} — approve will refuse until the reproducibility check is recorded`);
  } else {
    lines.push(...listLines("repro errors", repro.errors), ...listLines("repro warnings", repro.warnings));
  }
  lines.push(`  full record: ${rel}`);
  return lines;
}

// 技能提案载荷键 ↔ 闭环集成设计 §4.2「技能提案」；Python 写方是 mindagent.package（计划 7）。
function summarizeProposal(env: Envelope, rel: string): string[] {
  const p = env.payload;
  const lines: string[] = [`proposal: round=${String(p.round_id)}`];
  const finding = p.finding;
  if (!isRecord(finding)) {
    lines.push(`  finding: ${UNREADABLE}`);
  } else {
    const refs = finding.episode_refs;
    lines.push(
      `  finding: ${String(finding.id)} pattern=${String(finding.pattern)} support=${String(finding.support)} ` +
        `ratio=${fmt(finding.ratio)} episodes=${Array.isArray(refs) ? refs.length : "UNREADABLE"}`,
    );
  }
  const increments = p.increments;
  if (!Array.isArray(increments)) {
    lines.push(`  increments: ${UNREADABLE}`);
  } else if (increments.length === 0) {
    lines.push("  increments: none");
  } else {
    lines.push(`  increments (${increments.length}):`);
    for (const inc of increments) {
      if (!isRecord(inc) || !isRecord(inc.definition)) {
        lines.push(`    ${UNREADABLE}: ${JSON.stringify(inc)}`);
      } else {
        lines.push(`    ${String(inc.kind)} ${JSON.stringify(inc.definition)}`);
      }
    }
  }
  const v = p.verification;
  if (!isNonEmptyRecord(v)) {
    lines.push(`  verification: ${UNREADABLE} — approve will refuse until the proposal is verified`);
  } else {
    lines.push(
      `  verification: method=${String(v.method)} fired_on_motivating=${fmt(v.fired_on_motivating)} ` +
        `fired_elsewhere=${fmt(v.fired_elsewhere)} episodes=${Array.isArray(v.episodes) ? v.episodes.length : "UNREADABLE"}`,
    );
    if (typeof v.notes === "string" && v.notes.length > 0) {
      lines.push(`  notes: ${v.notes}`);
    }
  }
  lines.push(`  full record: ${rel}`);
  return lines;
}

export function review(repoRoot: string, id: string): string {
  const { kind, rel, env } = findGateProduct(repoRoot, id);
  const header = `${env.id} [${env.status}] type=${env.type} links=${env.links.join(",")}`;
  if (kind === "recipe_candidate") {
    return [header, ...summarizeRecipeCandidate(env, rel)].join("\n");
  }
  if (kind === "skill_memory_proposal") {
    return [header, ...summarizeProposal(env, rel)].join("\n");
  }
  return [header, ...summarizeDiagnostic(loadDiagnostic(repoRoot, rel), diagnosticPathFor(rel))].join("\n");
}

export function approve(repoRoot: string, id: string): void {
  const { kind, rel, env } = findGateProduct(repoRoot, id);
  assertTransition(env.status, "approved");
  // 审核门是证据驱动的：证据不在场就不能批准，且什么都不写、不提交。
  // 这只保证证据在场，不证明审批人读过——所以 review 才会把证据直接打印出来。
  if (kind === "candidate_checkpoint" && loadDiagnostic(repoRoot, rel) === null) {
    throw new Error(
      `approve: ${id} has no diagnostic report (${diagnosticPathFor(rel)}); ` +
        `the review gate is evidence-driven — wait for the diagnostic to be produced, then run "review ${id}"`,
    );
  }
  if (kind === "recipe_candidate" && !hasReproEvidence(env.payload)) {
    throw new Error(
      `approve: ${id} has no reproducibility check (payload.repro must be an object whose errors and warnings are both lists); ` +
        `run "review ${id}" to see what the record carries`,
    );
  }
  if (kind === "skill_memory_proposal" && !isNonEmptyRecord(env.payload.verification)) {
    throw new Error(
      `approve: ${id} has no verification result (payload.verification missing, empty or malformed); ` +
        `run "review ${id}" to see what the proposal carries`,
    );
  }
  env.status = "approved";
  writeProduct(repoRoot, rel, env, `approve ${id}`);
}

export function reject(repoRoot: string, id: string, reason: string): void {
  const { rel, env } = findGateProduct(repoRoot, id);
  assertTransition(env.status, "rejected");
  env.status = "rejected";
  // reject_reason ↔ Python 读方是计划 5 的上下文装配（PlanningContext.candidate.reject_reason）
  env.payload.reject_reason = reason;
  writeProduct(repoRoot, rel, env, `reject ${id}`);
}

export function confirm(repoRoot: string, id: string): void {
  // 闭环集成设计 §3.4：调度器写的指令先是 awaiting_confirmation，人确认后才变 pending、被派发
  // 与 findGateProduct 同一规则：收集全部命中，多处命中报歧义，不取第一个
  const hits = lsFiles(repoRoot, DIRECTIVE_PATTERN).filter((r) => readProduct(repoRoot, r).id === id);
  if (hits.length === 0) {
    throw new Error(`directive ${id} not found`);
  }
  if (hits.length > 1) {
    throw new Error(`directive ${id} is ambiguous: ${hits.join(", ")}`);
  }
  const rel = hits[0];
  const env = readProduct(repoRoot, rel);
  if (!(NONREVIEW_TRANSITIONS[env.status] ?? []).includes("pending")) {
    throw new Error(`confirm: ${id} is ${env.status}; only an awaiting_confirmation directive can be confirmed`);
  }
  env.status = "pending";
  writeProduct(repoRoot, rel, env, `confirm ${id}`);
}

export function status(repoRoot: string): string {
  const review: string[] = [];
  for (const { kind, pattern } of GATE_PATTERNS) {
    for (const rel of lsFiles(repoRoot, pattern)) {
      const env = readProduct(repoRoot, rel);
      if (env.status === "pending_review") {
        review.push(`  ${env.id} [${env.status}] ${kind}`);
      }
    }
  }
  const confirmation = lsFiles(repoRoot, DIRECTIVE_PATTERN)
    .map((rel) => readProduct(repoRoot, rel))
    .filter((env) => env.status === "awaiting_confirmation")
    .map((env) => `  ${env.id} [${env.status}] directive`);
  if (review.length === 0 && confirmation.length === 0) {
    return "nothing awaiting review or confirmation";
  }
  const lines: string[] = [];
  if (review.length > 0) {
    lines.push("awaiting review:", ...review);
  }
  if (confirmation.length > 0) {
    lines.push("awaiting confirmation (run: confirm <directive-id>):", ...confirmation);
  }
  return lines.join("\n");
}
