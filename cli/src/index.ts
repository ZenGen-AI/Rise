import { review, approve, reject, confirm, status } from "./commands.js";

export const CLI_NAME = "rismforge";

export const USAGE = `usage: ${CLI_NAME} <review|approve|reject|confirm|status> [id] [reason]`;

export function main(argv: string[]): void {
  const [cmd, ...rest] = argv;
  const repoRoot = process.cwd();
  switch (cmd) {
    case "review":
      console.log(review(repoRoot, rest[0]));
      break;
    case "approve":
      approve(repoRoot, rest[0]);
      console.log(`approved ${rest[0]}`);
      break;
    case "reject":
      reject(repoRoot, rest[0], rest.slice(1).join(" ") || "no reason");
      console.log(`rejected ${rest[0]}`);
      break;
    case "confirm":
      confirm(repoRoot, rest[0]);
      console.log(`confirmed ${rest[0]}`);
      break;
    case "status":
      console.log(status(repoRoot));
      break;
    default:
      console.error(USAGE);
      process.exitCode = 1;
  }
}

if (process.argv[1] && process.argv[1].endsWith("index.js")) {
  main(process.argv.slice(2));
}
