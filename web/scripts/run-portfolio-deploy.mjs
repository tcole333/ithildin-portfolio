import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, "..");
const repoRoot = resolve(webRoot, "..");

const env = {
  ...process.env,
  ITHILDIN_CONTENT_ROOT: resolve(repoRoot, "examples/portfolio-demo/content"),
  ITHILDIN_INVESTIGATION_DB: resolve(repoRoot, "examples/portfolio-demo/investigation.db"),
  ITHILDIN_REGISTRY_DB: resolve(repoRoot, "examples/portfolio-demo/registry.db"),
  ITHILDIN_DOJ_DB: resolve(repoRoot, "examples/portfolio-demo/doj_documents.db"),
  PUBLIC_ENABLE_EVIDENCE_MODE: "true",
};

const preview = process.argv.includes("--preview");
const deployBranch = preview ? "preview" : "main";

const commands = [
  ["npm", ["run", "lint:citations"]],
  ["npm", ["run", "test:citations"]],
  ["npm", ["run", "test:citations:snapshots"]],
  ["npm", ["run", "build"]],
  ["npm", ["run", "test:citations:build"]],
  [
    "npx",
    [
      "wrangler",
      "pages",
      "deploy",
      "dist/",
      "--project-name=ithildin-portfolio",
      `--branch=${deployBranch}`,
      "--commit-dirty=true",
    ],
  ],
];

for (const [command, args] of commands) {
  const result = spawnSync(command, args, {
    cwd: webRoot,
    env,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
