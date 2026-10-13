import { describe, it, expect, beforeEach } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { Envelope, readProduct, writeProduct } from "../src/gittree.js";

const A = "control-plane/runs/run-001/directive.yaml";
const B = "control-plane/runs/run-002/directive.yaml";

function git(root: string, ...args: string[]): string {
  return execFileSync("git", ["-C", root, ...args], { encoding: "utf-8" });
}

function env(id: string, status: string): Envelope {
  return {
    id,
    type: "directive",
    version: 1,
    created_at: "2026-09-26T00:00:00Z",
    created_by: { owner: "", visibility: "private" },
    status,
    schema_version: 1,
    links: [],
    payload: {},
  };
}

describe("writeProduct commit scope", () => {
  let root: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "gt-"));
    git(root, "init");
    git(root, "config", "user.email", "t@e.com");
    git(root, "config", "user.name", "t");
    mkdirSync(join(root, dirname(A)), { recursive: true });
    mkdirSync(join(root, dirname(B)), { recursive: true });
    writeProduct(root, A, env("run-001", "pending"), "add run-001");
  });

  it("commits the written file with the given message and the content reads back", () => {
    writeProduct(root, B, env("run-002", "pending"), "add run-002");
    expect(git(root, "log", "-1", "--format=%s").trim()).toBe("add run-002");
    expect(readProduct(root, B).status).toBe("pending");
  });

  it.each(["new_file", "modified_tracked", "deleted_tracked"])(
    "does not sweep another staged file into the commit (%s)",
    (how) => {
      let other: string;
      if (how === "new_file") {
        writeFileSync(join(root, "notes.txt"), "hello\n");
        git(root, "add", "notes.txt");
        other = "notes.txt";
      } else if (how === "modified_tracked") {
        writeFileSync(join(root, A), readFileSync(join(root, A), "utf-8") + "# edited by hand\n");
        git(root, "add", A);
        other = A;
      } else {
        git(root, "rm", "-q", A);
        other = A;
      }
      writeProduct(root, B, env("run-002", "pending"), "add run-002");
      expect(git(root, "show", "--name-only", "--format=", "HEAD").trim().split("\n")).toEqual([B]);
      expect(git(root, "diff", "--cached", "--name-only").trim().split("\n")).toEqual([other]);
    },
  );
});
