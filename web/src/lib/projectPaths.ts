import { resolve } from "node:path";

function resolveConfiguredPath(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  return resolve(value);
}

export function contentRoot(): string {
  return resolveConfiguredPath(
    process.env.ITHILDIN_CONTENT_ROOT,
    resolve(process.cwd(), "..", "content"),
  );
}

export function contentPath(...parts: string[]): string {
  return resolve(contentRoot(), ...parts);
}

export function investigationDbCandidates(): string[] {
  const candidates = [
    process.env.ITHILDIN_INVESTIGATION_DB,
    process.env.INVESTIGATION_DB_PATH,
    resolve(process.cwd(), "investigation.db"),
    resolve(process.cwd(), "..", "investigation.db"),
  ].filter((value): value is string => Boolean(value));
  return Array.from(new Set(candidates.map((value) => resolve(value))));
}
