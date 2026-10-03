import { describe, it, expect, beforeEach } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { dump } from "js-yaml";
import { approve, reject, review, status } from "../src/commands.js";
import { readProduct } from "../src/gittree.js";

function initRepo(): string {
  const root = mkdtempSync(join(tmpdir(), "cp-"));
  execFileSync("git", ["-C", root, "init"]);
  execFileSync("git", ["-C", root, "config", "user.email", "t@e.com"]);
  execFileSync("git", ["-C", root, "config", "user.name", "t"]);
  return root;
}

function writeCandidate(root: string, id: string, status: string): string {
  const rel = `control-plane/runs/${id}/candidate_checkpoint.yaml`;
  const dir = join(root, "control-plane", "runs", id);
  mkdirSync(dir, { recursive: true });
  const env = {
    id: `cand-${id}`,
    type: "candidate_checkpoint",
    version: 1,
    created_at: "2026-09-26T00:00:00Z",
    created_by: { owner: "default", visibility: "shared" },
    status,
    schema_version: 1,
    links: [id],
    payload: {},
  };
  writeFileSync(join(root, rel), dump(env));
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "commit", "-m", "seed"]);
  return rel;
}

// 文件名与 Python controlplane/paths.py 的 diagnostic_report_file 一致：两种语言
// 只经 git 树里的文件通信，所以这个约定在两边各写一次。
function writeDiagnostic(root: string, runId: string, payload: Record<string, unknown>): string {
  const rel = `control-plane/runs/${runId}/diagnostic_report.yaml`;
  const dir = join(root, "control-plane", "runs", runId);
  mkdirSync(dir, { recursive: true });
  const env = {
    id: `diag-${runId}`,
    type: "diagnostic_report",
    version: 1,
    created_at: "2026-09-26T00:00:00Z",
    created_by: { owner: "default", visibility: "shared" },
    status: "executed",
    schema_version: 1,
    links: [runId],
    payload,
  };
  writeFileSync(join(root, rel), dump(env));
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "commit", "-m", "seed diagnostic"]);
  return rel;
}

const STUB_REPORT = {
  candidate_ref: "cand-run-001",
  benchmark_version: "bench-v1",
  dimension_count: 0,
  stub: true,
};

const REAL_REPORT = {
  candidate_ref: "cand-run-001",
  benchmark_version: "bench-v1",
  run_tier: "full",
  sampling_k: 4,
  dimension_scores: [
    {
      dimension: "mental_representation", level: "l1", score: 0.8,
      ci_low: 0.72, ci_high: 0.87, sample_size: 100, track_provenance: "fused",
    },
    {
      dimension: "belief", level: "l2", score: 0.9,
      ci_low: 0.8, ci_high: 0.95, sample_size: 50, track_provenance: "static",
    },
  ],
  regression_flags: { flags: [] },
};

describe("gate cli", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("approve flips pending_review to approved and commits", () => {
    const rel = writeCandidate(root, "run-001", "pending_review");
    writeDiagnostic(root, "run-001", REAL_REPORT);
    approve(root, "cand-run-001");
    expect(readProduct(root, rel).status).toBe("approved");
    const log = execFileSync("git", ["-C", root, "log", "--oneline"], { encoding: "utf-8" });
    expect(log).toContain("approve cand-run-001");
  });

  it("reject flips pending_review to rejected preserving reason", () => {
    const rel = writeCandidate(root, "run-001", "pending_review");
    reject(root, "cand-run-001", "reward hacking");
    const env = readProduct(root, rel);
    expect(env.status).toBe("rejected");
    expect(env.payload.reject_reason).toBe("reward hacking");
  });

  it("approve refuses illegal transition from effective", () => {
    writeCandidate(root, "run-001", "effective");
    expect(() => approve(root, "cand-run-001")).toThrow(/not allowed/);
  });

  it("status lists candidates awaiting review", () => {
    writeCandidate(root, "run-001", "pending_review");
    expect(status(root)).toContain("cand-run-001");
  });

  describe("evidence-driven gate", () => {
    it("approve refuses without a diagnostic report and changes nothing", () => {
      const rel = writeCandidate(root, "run-001", "pending_review");
      expect(() => approve(root, "cand-run-001")).toThrow(/no diagnostic report/);
      expect(readProduct(root, rel).status).toBe("pending_review");
      const log = execFileSync("git", ["-C", root, "log", "--oneline"], { encoding: "utf-8" });
      expect(log).not.toContain("approve cand-run-001");
    });

    it("a diagnostic belonging to another run does not satisfy the gate", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-002", REAL_REPORT);
      expect(() => approve(root, "cand-run-001")).toThrow(/no diagnostic report/);
    });

    it("the transition check still comes first for a non-reviewable candidate", () => {
      writeCandidate(root, "run-001", "effective");
      expect(() => approve(root, "cand-run-001")).toThrow(/not allowed/);
    });

    it("a stub diagnostic satisfies presence — the skeleton daemon writes stubs — but review flags it", () => {
      // 这只保证证据在场，不证明它是真评测：[STUB] 标记就是让审批人看见这一点。
      const rel = writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", STUB_REPORT);
      approve(root, "cand-run-001");
      expect(readProduct(root, rel).status).toBe("approved");
    });
  });

  describe("review prints the evidence", () => {
    it("still prints the candidate header line", () => {
      writeCandidate(root, "run-001", "pending_review");
      expect(review(root, "cand-run-001")).toContain("cand-run-001 [pending_review]");
    });

    it("says MISSING and names the expected path when there is no diagnostic", () => {
      writeCandidate(root, "run-001", "pending_review");
      const out = review(root, "cand-run-001");
      expect(out).toContain("diagnostic: MISSING");
      expect(out).toContain("control-plane/runs/run-001/diagnostic_report.yaml");
    });

    it("marks a stub report loudly and prints no scores for it", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", STUB_REPORT);
      const out = review(root, "cand-run-001");
      expect(out).toContain("[STUB]");
      expect(out).toContain("NOT a real evaluation");
      expect(out).not.toContain("l1 ");
    });

    it("prints l1 scores with confidence interval, sample size and track provenance", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", REAL_REPORT);
      const out = review(root, "cand-run-001");
      expect(out).toContain("benchmark=bench-v1");
      expect(out).toContain("l1 mental_representation score=0.80 ci=[0.72,0.87] n=100 track=fused");
      expect(out).toContain("regression flags: none");
      expect(out).toContain("control-plane/runs/run-001/diagnostic_report.yaml");
    });

    it("prints only l1 rows; lower levels stay in the full report", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", REAL_REPORT);
      expect(review(root, "cand-run-001")).not.toContain("belief");
    });

    it("lists regression flags with their evidence", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", {
        ...REAL_REPORT,
        regression_flags: {
          flags: [{ dimension: "empathy", kind: "social_regression", drop: 0.25, beyond_ci: true }],
        },
      });
      const out = review(root, "cand-run-001");
      expect(out).toContain("regression flags (1):");
      expect(out).toContain("social_regression");
      expect(out).toContain("empathy");
      expect(out).not.toContain("regression flags: none");
    });

    it("says UNREADABLE when regression_flags is missing instead of claiming none", () => {
      writeCandidate(root, "run-001", "pending_review");
      const { regression_flags: _drop, ...withoutFlags } = REAL_REPORT;
      writeDiagnostic(root, "run-001", withoutFlags);
      const out = review(root, "cand-run-001");
      expect(out).toContain("regression flags: UNREADABLE");
      expect(out).not.toContain("regression flags: none");
    });

    it("says UNREADABLE when regression_flags is malformed", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", { ...REAL_REPORT, regression_flags: { flags: "oops" } });
      expect(review(root, "cand-run-001")).toContain("regression flags: UNREADABLE");
    });

    it("says so when the report has no l1 scores", () => {
      writeCandidate(root, "run-001", "pending_review");
      writeDiagnostic(root, "run-001", { ...REAL_REPORT, dimension_scores: [] });
      expect(review(root, "cand-run-001")).toContain("(no l1 scores in this report)");
    });
  });
});
