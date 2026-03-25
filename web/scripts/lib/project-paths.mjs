import { resolve } from "node:path";

const cwd = process.cwd();

function configuredPath(envName, fallback) {
  const value = process.env[envName];
  return value ? resolve(value) : fallback;
}

export const webRoot = cwd;
export const repoRoot = resolve(cwd, "..");
export const contentRoot = configuredPath("ITHILDIN_CONTENT_ROOT", resolve(repoRoot, "content"));
export const articlesDir = resolve(contentRoot, "articles");
export const dossiersDir = resolve(contentRoot, "dossiers");
