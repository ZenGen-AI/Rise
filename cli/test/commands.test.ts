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

describe("gate cli", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("approve flips pending_review to approved and commits", () => {
    const rel = writeCandidate(root, "run-001", "pending_review");
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

  it("review prints candidate summary", () => {
    writeCandidate(root, "run-001", "pending_review");
    expect(review(root, "cand-run-001")).toContain("pending_review");
  });

  it("status lists candidates awaiting review", () => {
    writeCandidate(root, "run-001", "pending_review");
    expect(status(root)).toContain("cand-run-001");
  });
});
