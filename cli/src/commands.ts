import { execFileSync } from "node:child_process";
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

export function review(repoRoot: string, id: string): string {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  return `${env.id} [${env.status}] type=${env.type} links=${env.links.join(",")}`;
}

export function approve(repoRoot: string, id: string): void {
  const rel = findCandidatePath(repoRoot, id);
  const env = readProduct(repoRoot, rel);
  assertTransition(env.status, "approved");
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
