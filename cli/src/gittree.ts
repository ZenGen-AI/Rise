import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { load, dump } from "js-yaml";

export interface OwnerRef {
  owner: string;
  visibility: string;
}

export interface Envelope {
  id: string;
  type: string;
  version: number;
  created_at: string;
  created_by: OwnerRef;
  status: string;
  schema_version: number;
  links: string[];
  payload: Record<string, unknown>;
}

export function readProduct(repoRoot: string, relPath: string): Envelope {
  const raw = readFileSync(join(repoRoot, relPath), "utf-8");
  return load(raw) as Envelope;
}

export function writeProduct(
  repoRoot: string,
  relPath: string,
  env: Envelope,
  message: string,
): void {
  writeFileSync(join(repoRoot, relPath), dump(env, { sortKeys: false }));
  execFileSync("git", ["-C", repoRoot, "add", relPath]);
  // 只提交本文件:别的文件若已在暂存区,不被卷进这次提交,也不丢
  execFileSync("git", ["-C", repoRoot, "commit", "-m", message, "--", relPath]);
}
