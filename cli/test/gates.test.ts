import { describe, it, expect, beforeEach, vi } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { dump } from "js-yaml";
import { approve, confirm, reject, review, status } from "../src/commands.js";
import { readProduct } from "../src/gittree.js";
import { USAGE, main } from "../src/index.js";

function initRepo(): string {
  const root = mkdtempSync(join(tmpdir(), "cp-"));
  execFileSync("git", ["-C", root, "init"]);
  execFileSync("git", ["-C", root, "config", "user.email", "t@e.com"]);
  execFileSync("git", ["-C", root, "config", "user.name", "t"]);
  return root;
}

// 路径与 Python controlplane/paths.py 一致（见计划「跨语言约定」）：
// recipe_candidate_file → control-plane/recipe_candidates/recipe-<run>.yaml
// proposal_file         → control-plane/skill_memory_proposals/<proposal-id>/proposal.yaml
function seed(root: string, rel: string, env: Record<string, unknown>): string {
  mkdirSync(dirname(join(root, rel)), { recursive: true });
  writeFileSync(join(root, rel), dump(env));
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "commit", "-m", `seed ${rel}`]);
  return rel;
}

function envelope(id: string, type: string, st: string, payload: Record<string, unknown>) {
  return {
    id, type, version: 1, created_at: "2026-10-09T00:00:00Z",
    created_by: { owner: "default", visibility: "shared" },
    status: st, schema_version: 1, links: [], payload,
  };
}

const RECIPE_PAYLOAD = {
  recipe_type: "batch",
  dimension: "tom",
  source_run_id: "run-003",
  eval_anchor: {
    taxonomy_version: "tax-v1", item_pool_version: "pool-v1", benchmark_version: "bench-v1",
    judge_prompt_version: null, scoring_config_version: null,
  },
  result: { gain: 0.04, baseline_ref: "zengen-v1", candidate_ref: "cand-run-003", promoted: true },
  repro: { errors: [], warnings: ["interact runs are only highly similar"] },
  nondeterministic: false,
};

const PROPOSAL_PAYLOAD = {
  round_id: "agent-round-zengen-v1",
  finding: { id: "f-1", pattern: "emotion_high ignored", support: 6, ratio: 0.83, episode_refs: ["e1", "e2"] },
  // definition 是 actio 的 Skill.to_dict()(计划 6「对外契约:技能、记忆、推理经验的字典形状」),键顺序照抄
  increments: [{
    kind: "skill",
    definition: {
      id: "skill-calm-first", version: 1, level: "macro", priority: 0,
      trigger: { signal: "emotion_high" },
      policy: { instructions: ["先回应对方的情绪"] },
      description: "",
    },
  }],
  verification: {
    method: "replay_behavior", episodes: ["e1", "e2"], fired_on_motivating: 0.9, fired_elsewhere: 0.0,
    notes: "verified on the motivating episodes; overfit risk",
  },
};

const RECIPE_REL = "control-plane/recipe_candidates/recipe-run-003.yaml";
const PROPOSAL_ID = "proposal-agent-round-zengen-v1-1";
const PROPOSAL_REL = `control-plane/skill_memory_proposals/${PROPOSAL_ID}/proposal.yaml`;

function seedRecipe(root: string, st = "pending_review", payload: Record<string, unknown> = RECIPE_PAYLOAD) {
  return seed(root, RECIPE_REL, envelope("recipe-run-003", "recipe_candidate", st, payload));
}

function seedProposal(root: string, st = "pending_review", payload: Record<string, unknown> = PROPOSAL_PAYLOAD) {
  return seed(root, PROPOSAL_REL, envelope(PROPOSAL_ID, "skill_memory_proposal", st, payload));
}

function seedDirective(root: string, runId: string, st: string) {
  return seed(root, `control-plane/runs/${runId}/directive.yaml`,
    envelope(runId, "directive", st, { target_dimension: "tom", recipe_ref: "" }));
}

function seedCandidate(root: string, runId: string, st: string) {
  return seed(root, `control-plane/runs/${runId}/candidate_checkpoint.yaml`,
    envelope(`cand-${runId}`, "candidate_checkpoint", st, {}));
}

function head(root: string): string {
  return execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf-8" }).trim();
}

function dirty(root: string): string {
  return execFileSync("git", ["-C", root, "status", "--porcelain"], { encoding: "utf-8" });
}

describe("generalized gate lookup", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("finds a recipe candidate and a nested proposal by id", () => {
    seedRecipe(root);
    seedProposal(root);
    expect(review(root, "recipe-run-003")).toContain("recipe-run-003 [pending_review] type=recipe_candidate");
    expect(review(root, PROPOSAL_ID)).toContain(`${PROPOSAL_ID} [pending_review] type=skill_memory_proposal`);
  });

  it("still says not found for an unknown id", () => {
    seedRecipe(root);
    expect(() => review(root, "nope")).toThrow(/candidate nope not found/);
  });

  it("refuses an id that two products share instead of guessing", () => {
    seedRecipe(root);
    seed(root, "control-plane/runs/run-003/candidate_checkpoint.yaml",
      envelope("recipe-run-003", "candidate_checkpoint", "pending_review", {}));
    expect(() => approve(root, "recipe-run-003")).toThrow(/ambiguous/);
  });

  it("ignores untracked files, like the original lookup", () => {
    seedCandidate(root, "run-001", "pending_review");
    mkdirSync(dirname(join(root, RECIPE_REL)), { recursive: true });
    writeFileSync(join(root, RECIPE_REL), dump(envelope("recipe-run-003", "recipe_candidate", "pending_review", RECIPE_PAYLOAD)));
    expect(() => review(root, "recipe-run-003")).toThrow(/not found/);
  });
});

describe("review prints evidence per kind", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("recipe candidate: anchor, result and repro", () => {
    seedRecipe(root);
    const out = review(root, "recipe-run-003");
    expect(out).toContain("recipe: type=batch dimension=tom source_run=run-003 nondeterministic=false");
    expect(out).toContain("eval_anchor: taxonomy_version=tax-v1 item_pool_version=pool-v1 benchmark_version=bench-v1 judge_prompt_version=null");
    expect(out).toContain("result: gain=0.04 baseline_ref=zengen-v1 candidate_ref=cand-run-003 promoted=true");
    expect(out).toContain("repro errors: none");
    expect(out).toContain("repro warnings (1):");
    expect(out).toContain("interact runs are only highly similar");
    expect(out).toContain(`full record: ${RECIPE_REL}`);
  });

  it("recipe candidate: a null gain is printed as null, not as unreadable", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, result: { ...RECIPE_PAYLOAD.result, gain: null } });
    expect(review(root, "recipe-run-003")).toContain("result: gain=null");
  });

  it("recipe candidate: missing or malformed fields say UNREADABLE", () => {
    const { repro: _r, eval_anchor: _a, result: _res, ...rest } = RECIPE_PAYLOAD;
    seedRecipe(root, "pending_review", rest);
    const out = review(root, "recipe-run-003");
    expect(out).toContain("eval_anchor: UNREADABLE");
    expect(out).toContain("result: UNREADABLE");
    expect(out).toContain("repro: UNREADABLE");
    expect(out).not.toContain("repro errors: none");
  });

  it("recipe candidate: repro lists that are not arrays say UNREADABLE", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: "oops", warnings: [] } });
    const out = review(root, "recipe-run-003");
    expect(out).toContain("repro errors: UNREADABLE");
    expect(out).toContain("repro warnings: none");
  });

  it("proposal: finding, skill definitions and verification", () => {
    seedProposal(root);
    const out = review(root, PROPOSAL_ID);
    expect(out).toContain("proposal: round=agent-round-zengen-v1");
    expect(out).toContain("finding: f-1 pattern=emotion_high ignored support=6 ratio=0.83 episodes=2");
    expect(out).toContain("increments (1):");
    expect(out).toContain(
      'skill {"id":"skill-calm-first","version":1,"level":"macro","priority":0,' +
        '"trigger":{"signal":"emotion_high"},"policy":{"instructions":["先回应对方的情绪"]},"description":""}',
    );
    expect(out).toContain("verification: method=replay_behavior fired_on_motivating=0.90 fired_elsewhere=0.00 episodes=2");
    expect(out).toContain("notes: verified on the motivating episodes; overfit risk");
  });

  it("proposal: missing or malformed fields say UNREADABLE", () => {
    seedProposal(root, "pending_review", { round_id: "r", finding: "oops", increments: [{ kind: "skill" }] });
    const out = review(root, PROPOSAL_ID);
    expect(out).toContain("finding: UNREADABLE");
    expect(out).toContain("increments (1):");
    expect(out).toContain('UNREADABLE (field missing or malformed): {"kind":"skill"}');
    expect(out).toContain("verification: UNREADABLE");
  });

  it("proposal: an empty increments list says none", () => {
    seedProposal(root, "pending_review", { ...PROPOSAL_PAYLOAD, increments: [] });
    expect(review(root, PROPOSAL_ID)).toContain("increments: none");
  });

  it("candidate: a mock-backend report is flagged loudly", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      candidate_ref: "cand-run-001", benchmark_version: "bench-v1", run_tier: "full", sampling_k: 4, backends: "mock",
      dimension_scores: [{ dimension: "tom", level: "l1", score: 0.5, ci_low: 0.4, ci_high: 0.6, sample_size: 8, track_provenance: "static" }],
      regression_flags: { flags: [] },
    }));
    const out = review(root, "cand-run-001");
    expect(out).toContain("diagnostic: [MOCK BACKENDS] diag-run-001");
    expect(out).toContain("not model quality");
    expect(out).toContain("l1 tom score=0.50");
  });

  it("candidate: a real-backend report carries no mock marker", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      backends: "real", dimension_scores: [], regression_flags: { flags: [] },
    }));
    const out = review(root, "cand-run-001");
    expect(out).not.toContain("MOCK");
    expect(out).not.toContain("backends: UNREADABLE");
  });

  it("candidate: an unknown backends value says UNREADABLE", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      backends: "fake", dimension_scores: [], regression_flags: { flags: [] },
    }));
    expect(review(root, "cand-run-001")).toContain('backends: UNREADABLE (expected "mock" or "real", got "fake")');
  });
});

describe("approve requires per-kind evidence", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("approves a recipe candidate that carries repro", () => {
    seedRecipe(root);
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
    expect(execFileSync("git", ["-C", root, "log", "-1", "--format=%s"], { encoding: "utf-8" }).trim()).toBe("approve recipe-run-003");
  });

  it("approves a proposal that carries verification", () => {
    seedProposal(root);
    approve(root, PROPOSAL_ID);
    expect(readProduct(root, PROPOSAL_REL).status).toBe("approved");
  });

  it("approves a recipe candidate whose repro has empty errors and an array of warnings", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: [], warnings: [] } });
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
  });

  it("approves a recipe candidate even when repro.errors is non-empty (the gate only checks presence)", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: ["unanchored ref"], warnings: [] } });
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
  });

  // 与 Python recipestore/admit.py has_repro_evidence 逐条一致：缺 warnings、errors 是字典、
  // repro 是 {} 或任意不带两个列表的对象，都算缺证据
  for (const repro of [undefined, null, {}, [], "ok", { x: 1 }, { errors: [] }, { errors: {}, warnings: [] },
    { errors: [], warnings: null }, { errors: [], warnings: "w" }]) {
    it(`refuses a recipe candidate whose repro is ${JSON.stringify(repro) ?? "missing"} and changes nothing`, () => {
      const { repro: _drop, ...rest } = RECIPE_PAYLOAD;
      seedRecipe(root, "pending_review", repro === undefined ? rest : { ...rest, repro });
      const before = head(root);
      expect(() => approve(root, "recipe-run-003")).toThrow(/no reproducibility check/);
      expect(readProduct(root, RECIPE_REL).status).toBe("pending_review");
      expect(head(root)).toBe(before);
      expect(dirty(root)).toBe("");
    });
  }

  for (const verification of [undefined, null, {}, [], 1]) {
    it(`refuses a proposal whose verification is ${JSON.stringify(verification) ?? "missing"} and changes nothing`, () => {
      const { verification: _drop, ...rest } = PROPOSAL_PAYLOAD;
      seedProposal(root, "pending_review", verification === undefined ? rest : { ...rest, verification });
      const before = head(root);
      expect(() => approve(root, PROPOSAL_ID)).toThrow(/no verification result/);
      expect(readProduct(root, PROPOSAL_REL).status).toBe("pending_review");
      expect(head(root)).toBe(before);
      expect(dirty(root)).toBe("");
    });
  }

  it("the transition check comes first for every kind", () => {
    seedRecipe(root, "effective");
    seedProposal(root, "rejected");
    expect(() => approve(root, "recipe-run-003")).toThrow(/not allowed/);
    expect(() => approve(root, PROPOSAL_ID)).toThrow(/not allowed/);
  });

  it("reject works for the new kinds and keeps the reason", () => {
    seedRecipe(root);
    seedProposal(root, "pending_review", { round_id: "r" });  // 驳回不需要证据
    reject(root, "recipe-run-003", "gain is noise");
    reject(root, PROPOSAL_ID, "overfit");
    expect(readProduct(root, RECIPE_REL).payload.reject_reason).toBe("gain is noise");
    expect(readProduct(root, PROPOSAL_REL).status).toBe("rejected");
  });
});

describe("confirm", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("moves awaiting_confirmation to pending and commits", () => {
    const rel = seedDirective(root, "run-004", "awaiting_confirmation");
    confirm(root, "run-004");
    const env = readProduct(root, rel);
    expect(env.status).toBe("pending");
    expect(env.payload.target_dimension).toBe("tom");
    expect(execFileSync("git", ["-C", root, "log", "-1", "--format=%s"], { encoding: "utf-8" }).trim()).toBe("confirm run-004");
  });

  for (const st of ["pending", "executed", "failed", "consumed"]) {
    it(`refuses a ${st} directive and changes nothing`, () => {
      const rel = seedDirective(root, "run-004", st);
      const before = head(root);
      expect(() => confirm(root, "run-004")).toThrow(/only an awaiting_confirmation directive/);
      expect(readProduct(root, rel).status).toBe(st);
      expect(head(root)).toBe(before);
    });
  }

  it("says not found for an unknown directive", () => {
    seedDirective(root, "run-004", "awaiting_confirmation");
    expect(() => confirm(root, "run-999")).toThrow(/directive run-999 not found/);
  });

  it("reports ambiguity instead of confirming the first of several matches", () => {
    seedDirective(root, "run-004", "awaiting_confirmation");
    // 同一信封 id 出现在第二个 run 目录(人手复制目录时会这样)
    const other = seed(root, "control-plane/runs/run-005/directive.yaml",
      envelope("run-004", "directive", "awaiting_confirmation", { target_dimension: "tom", recipe_ref: "" }));
    const before = head(root);
    expect(() => confirm(root, "run-004")).toThrow(/directive run-004 is ambiguous/);
    expect(readProduct(root, other).status).toBe("awaiting_confirmation");
    expect(head(root)).toBe(before);
  });
});

describe("status", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("lists all three gate kinds awaiting review and directives awaiting confirmation", () => {
    seedCandidate(root, "run-001", "pending_review");
    seedCandidate(root, "run-002", "effective");
    seedRecipe(root);
    seedProposal(root);
    seedDirective(root, "run-004", "awaiting_confirmation");
    seedDirective(root, "run-005", "pending");
    expect(status(root)).toBe([
      "awaiting review:",
      "  cand-run-001 [pending_review] candidate_checkpoint",
      "  recipe-run-003 [pending_review] recipe_candidate",
      `  ${PROPOSAL_ID} [pending_review] skill_memory_proposal`,
      "awaiting confirmation (run: confirm <directive-id>):",
      "  run-004 [awaiting_confirmation] directive",
    ].join("\n"));
  });

  it("says so when nothing waits", () => {
    seedCandidate(root, "run-001", "effective");
    seedDirective(root, "run-001", "executed");
    expect(status(root)).toBe("nothing awaiting review or confirmation");
  });
});

describe("index dispatch", () => {
  it("usage names every command", () => {
    expect(USAGE).toBe("usage: rise <review|approve|reject|confirm|status> [id] [reason]");
  });

  it("confirm is routed through main", () => {
    const root = initRepo();
    const rel = seedDirective(root, "run-004", "awaiting_confirmation");
    // vitest 的 worker 里不能 chdir;main 只经 process.cwd() 取仓库根,桩掉它即可
    const cwd = vi.spyOn(process, "cwd").mockReturnValue(root);
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    try {
      main(["confirm", "run-004"]);
      expect(log).toHaveBeenCalledWith("confirmed run-004");
    } finally {
      cwd.mockRestore();
      log.mockRestore();
    }
    expect(readProduct(root, rel).status).toBe("pending");
  });
});
