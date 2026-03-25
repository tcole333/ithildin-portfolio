import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { createJiti } from "jiti";
import { articlesDir, dossiersDir, repoRoot } from "./lib/project-paths.mjs";

const cwd = process.cwd();

const argv = process.argv.slice(2);
const args = new Set(argv.filter((arg) => arg.startsWith("--")));

function readArgValue(flag) {
  const index = argv.indexOf(flag);
  if (index === -1) return "";
  const candidate = argv[index + 1];
  if (!candidate || candidate.startsWith("--")) return "";
  return candidate;
}

const changedFilesMode = args.has("--changed-files");
const baseRef = readArgValue("--base-ref") || process.env.SUPPORT_COVERAGE_BASE_REF || "";
const headRef = readArgValue("--head-ref") || process.env.SUPPORT_COVERAGE_HEAD_REF || "HEAD";

function runGit(argsList) {
  try {
    return execFileSync("git", argsList, { cwd: repoRoot, encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

function normalizeRepoPath(path) {
  const normalized = String(path || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/^\.\//, "");
  if (normalized.startsWith("site/")) {
    return normalized.slice("site/".length);
  }
  return normalized;
}

function isCoverageTrackedContent(path) {
  return /^content\/articles\/.+\.mdx$/.test(path) || /^content\/dossiers\/[^/]+\.json$/.test(path);
}

function getChangedContentFiles() {
  if (!changedFilesMode) return null;

  /** @type {string[]} */
  const candidates = [];
  if (baseRef) {
    const rangeOutput = runGit(["diff", "--name-only", "--diff-filter=ACMR", `${baseRef}...${headRef}`]);
    if (rangeOutput) {
      candidates.push(...rangeOutput.split("\n"));
    }
  } else {
    const unstaged = runGit(["diff", "--name-only", "--diff-filter=ACMR"]);
    const staged = runGit(["diff", "--name-only", "--diff-filter=ACMR", "--cached"]);
    if (unstaged) candidates.push(...unstaged.split("\n"));
    if (staged) candidates.push(...staged.split("\n"));
  }

  const out = new Set();
  for (const rawPath of candidates) {
    const file = normalizeRepoPath(rawPath);
    if (isCoverageTrackedContent(file)) out.add(file);
  }
  return out;
}

const jiti = createJiti(import.meta.url);
const { loadFindingEvidenceMap } = jiti(resolve(cwd, "src", "lib", "findingEvidence.ts"));
const {
  processArticleEvidenceContent,
  processDossierCurationEvidence,
  buildDossierFindingEvidenceMap,
} = jiti(resolve(cwd, "src", "lib", "contentEvidencePipeline.ts"));

function metricTotals(metricsByFile) {
  const totalSentences = metricsByFile.reduce((acc, item) => acc + item.total_sentences, 0);
  const supportedSentences = metricsByFile.reduce((acc, item) => acc + item.supported_sentences, 0);
  const unsupportedSentences = metricsByFile.reduce((acc, item) => acc + item.unsupported_sentences, 0);
  const supportedSentencePct = totalSentences > 0
    ? Number(((supportedSentences / totalSentences) * 100).toFixed(1))
    : 0;

  const orphanCitationKeys = Array.from(
    new Set(metricsByFile.flatMap((item) => item.orphan_citations || [])),
  );

  return {
    total_sentences: totalSentences,
    supported_sentences: supportedSentences,
    unsupported_sentences: unsupportedSentences,
    supported_sentence_pct: supportedSentencePct,
    orphan_citations_count: orphanCitationKeys.length,
    orphan_citations: orphanCitationKeys,
  };
}

async function gatherArticleMetrics(fileScope, findingEvidenceMap) {
  if (!existsSync(articlesDir)) return [];
  const files = readdirSync(articlesDir).filter((name) => name.endsWith(".mdx"));
  const out = [];

  for (const fileName of files) {
    const abs = resolve(articlesDir, fileName);
    const rel = abs.replace(`${repoRoot}/`, "");
    if (fileScope && !fileScope.has(rel)) continue;

    const raw = readFileSync(abs, "utf-8");
    const body = raw.replace(/^---\n[\s\S]*?\n---\n*/, "");
    const result = await processArticleEvidenceContent(body, findingEvidenceMap);

    out.push({
      file: rel,
      content_type: "article",
      ...result.supportMetrics,
    });
  }
  return out;
}

function gatherDossierMetrics(fileScope) {
  if (!existsSync(dossiersDir)) return [];
  const files = readdirSync(dossiersDir).filter((name) => name.endsWith(".json") && !name.startsWith("_"));
  const out = [];

  for (const fileName of files) {
    const abs = resolve(dossiersDir, fileName);
    const rel = abs.replace(`${repoRoot}/`, "");
    if (fileScope && !fileScope.has(rel)) continue;

    const dossier = JSON.parse(readFileSync(abs, "utf-8"));
    const findingEvidenceMap = buildDossierFindingEvidenceMap(dossier);
    const result = processDossierCurationEvidence({
      findingEvidenceMap,
      lead: dossier.curation?.lead || "",
      sections: Array.isArray(dossier.curation?.sections) ? dossier.curation.sections : [],
      legacyOverview: dossier.curation?.overview || "",
      legacyFinancialSummary: dossier.curation?.financial_summary || "",
    });

    out.push({
      file: rel,
      content_type: "dossier",
      ...result.supportMetrics,
    });
  }

  return out;
}

async function main() {
  const changedContentFiles = getChangedContentFiles();
  const findingEvidenceMap = loadFindingEvidenceMap();

  const articleMetrics = await gatherArticleMetrics(changedContentFiles, findingEvidenceMap);
  const dossierMetrics = gatherDossierMetrics(changedContentFiles);
  const files = [...articleMetrics, ...dossierMetrics];
  const totals = metricTotals(files);

  const payload = {
    generated_at: new Date().toISOString(),
    scope: changedContentFiles ? "changed" : "full",
    file_count: files.length,
    files,
    totals,
  };

  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`support coverage report failed: ${error?.message || String(error)}\n`);
  process.exit(1);
});
