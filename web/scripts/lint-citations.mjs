import { readFileSync, readdirSync, existsSync, writeFileSync } from "node:fs";
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

const strict = args.has("--strict");
const changedFilesMode = args.has("--changed-files");
const strictChangedFiles = args.has("--strict-changed-files");
const baseRef = readArgValue("--base-ref") || process.env.CITATION_LINT_BASE_REF || "";
const headRef = readArgValue("--head-ref") || process.env.CITATION_LINT_HEAD_REF || "HEAD";
const reportFile = readArgValue("--report-file") || process.env.CITATION_LINT_REPORT || "";
const exceptionFile = resolve(cwd, "src", "data", "citation-exceptions.json");

if (args.has("--update-baseline")) {
  process.stderr.write("The citation lint baseline has been removed. Fix the cited refs instead of updating an ignore list.\n");
  process.exit(2);
}

const jiti = createJiti(import.meta.url);
const { applyCitations, createCitationState, extractEvidenceLinks } = jiti(resolve(cwd, "src", "lib", "citations.ts"));
const {
  loadGlobalFindingCatalog,
  loadArticleFindingCatalog,
  loadDossierFindingCatalog,
  mergeFindingCatalogs,
} = jiti(resolve(cwd, "src", "lib", "findingCatalog.ts"));

/** @typedef {{severity: 'error'|'warning', code: string, file: string, location: string, subject: string, message: string}} LintIssue */
/** @typedef {{code: string, file: string, location: string, subject: string, reason: string, expires_on: string}} CitationException */

/** @type {LintIssue[]} */
const issues = [];
const seenIssueKeys = new Set();

/** @type {CitationException[]} */
const citationExceptions = existsSync(exceptionFile)
  ? JSON.parse(readFileSync(exceptionFile, "utf-8"))
  : [];

function issueKey(issue) {
  return `${issue.code}|${issue.file}|${issue.location}|${issue.subject}`;
}

function addIssue(severity, code, file, location, subject, message) {
  const issue = { severity, code, file, location, subject, message };
  const key = issueKey(issue);
  if (seenIssueKeys.has(key)) return;
  seenIssueKeys.add(key);
  issues.push(issue);
}

function normalizeRef(ref) {
  return String(ref || "").trim();
}

function lintCitationEntries(file, location, entries) {
  for (const entry of entries || []) {
    const label = String(entry.label || "");
    const url = typeof entry.url === "string" ? entry.url : "";
    const sourceRecordUrl = typeof entry.sourceRecordUrl === "string" ? entry.sourceRecordUrl : "";
    const sources = Array.isArray(entry.sources) ? entry.sources : [];
    const availabilityStatus = typeof entry.availabilityStatus === "string" ? entry.availabilityStatus : "";
    const metadataComplete = entry.metadataComplete !== false;

    if (/^https?:$/i.test(label)) {
      addIssue(
        "error",
        "CITE_SPLIT_URL_SCHEME",
        file,
        location,
        label,
        `Citation label is an URL scheme fragment (${label}) and likely came from URL splitting.`,
      );
    }

    if (!url && /%[0-9a-f]{2}/i.test(label)) {
      addIssue(
        "error",
        "CITE_SPLIT_URL_FRAGMENT",
        file,
        location,
        label,
        "Citation label looks like a URL path fragment but has no URL.",
      );
    }

    if (!url && /^www\./i.test(label)) {
      addIssue(
        "warning",
        "CITE_BARE_DOMAIN",
        file,
        location,
        label,
        "Citation label looks like a bare domain without an attached URL.",
      );
    }

    if (entry.kind === "source" && !url) {
      addIssue(
        "error",
        "CITE_MISSING_TARGET",
        file,
        location,
        label,
        "Public source citation is missing a navigable target.",
      );
    }

    if (entry.kind === "source" && !sourceRecordUrl) {
      addIssue(
        "error",
        "CITE_MISSING_SOURCE_RECORD",
        file,
        location,
        label,
        "Public source citation is missing its source-record URL.",
      );
    }

    if (entry.kind === "source" && availabilityStatus === "metadata_only" && !metadataComplete) {
      addIssue(
        "error",
        "CITE_INCOMPLETE_SOURCE_RECORD",
        file,
        location,
        label,
        "Metadata-only citation is missing required source-record fields.",
      );
    }

    if (entry.kind === "source" && url.startsWith("#registry-")) {
      addIssue(
        "error",
        "CITE_DEAD_REGISTRY_HASH",
        file,
        location,
        label,
        "Public source citation resolved to a dead in-page registry hash instead of a source target.",
      );
    }

    if (entry.kind === "finding" && sources.length === 0) {
      addIssue(
        "error",
        "FINDING_NO_SOURCES",
        file,
        location,
        label,
        "Finding citation does not resolve to any publish-valid sources.",
      );
    }

    if (
      entry.kind === "finding"
      && sources.length > 0
      && !sources.some((source) => source.publishValid !== false && (source.openUrl || source.metadataComplete !== false))
    ) {
      addIssue(
        "error",
        "FINDING_NO_NAVIGABLE_SOURCE",
        file,
        location,
        label,
        "Finding citation has sources, but none expose a navigable public endpoint.",
      );
    }
  }
}

function lintEvidenceRef(file, findingId, ref) {
  const value = normalizeRef(ref);
  const loc = `finding:${findingId}`;

  if (!value) {
    addIssue("warning", "EVIDENCE_EMPTY", file, loc, "", "Evidence reference is empty.");
    return;
  }

  if (/^https?:\/\//i.test(value)) {
    try {
      // eslint-disable-next-line no-new
      new URL(value);
    } catch {
      addIssue("error", "EVIDENCE_BAD_URL", file, loc, value, "Evidence reference URL is malformed.");
    }
    return;
  }

  if (/^EFTA/i.test(value) && !/^EFTA\d{6,}$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_EFTA", file, loc, value, "EFTA reference must be EFTA followed by at least 6 digits.");
    return;
  }

  if (/^HOUSE_OVERSIGHT/i.test(value) && !/^HOUSE_OVERSIGHT_\d+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_HOUSE", file, loc, value, "HOUSE_OVERSIGHT reference must be HOUSE_OVERSIGHT_<digits>.");
    return;
  }

  if (/^SEC:/i.test(value) && !/^SEC:\d{10}-\d{2}-\d{6}$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_SEC", file, loc, value, "SEC reference must be SEC:##########-##-######.");
    return;
  }

  if (/^EDGAR:/i.test(value) && !/^EDGAR:\d{10}-\d{2}-\d{6}$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_EDGAR", file, loc, value, "EDGAR reference must be EDGAR:##########-##-######.");
    return;
  }

  if (/^990:/i.test(value) && !/^990:\d{9}$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_990", file, loc, value, "IRS 990 reference must be 990:<9-digit EIN>.");
    return;
  }

  if (/^ACRIS:/i.test(value) && !/^ACRIS:\d{13,16}$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_ACRIS", file, loc, value, "ACRIS reference must be ACRIS:<13-16 digits>.");
    return;
  }

  if (/^CL:/i.test(value) && !/^CL:\d+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_CL", file, loc, value, "CourtListener reference must be CL:<docket id>.");
    return;
  }

  if (/^FARA:/i.test(value) && !/^FARA:\d+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_FARA", file, loc, value, "FARA reference must be FARA:<digits>.");
    return;
  }

  if (/^USVI:/i.test(value) && !/^USVI:[A-Za-z0-9]+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_USVI", file, loc, value, "USVI reference must be USVI:<entity id>.");
    return;
  }

  if (/^REG:/i.test(value) && !/^REG:[A-Z]{2}:[A-Za-z0-9]+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_REG", file, loc, value, "Registry reference must be REG:<CC>:<entity id>.");
    return;
  }

  if (/^FL[-_]?SunBiz/i.test(value) && !/^FL[-_]?SunBiz[:\s]+[A-Za-z0-9]+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_FL_SUNBIZ", file, loc, value, "FL-SunBiz reference must include a valid entity id.");
    return;
  }

  if (/^NM[-_]?SoS/i.test(value) && !/^NM[-_]?SoS[:\s]+[A-Za-z0-9]+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_NM_SOS", file, loc, value, "NM-SoS reference must include a valid entity id.");
    return;
  }

  if (/^NY[-_]?SoS/i.test(value) && !/^NY[-_]?SoS[:\s]+[A-Za-z0-9]+$/i.test(value)) {
    addIssue("error", "EVIDENCE_BAD_NY_SOS", file, loc, value, "NY-SoS reference must include a valid entity id.");
    return;
  }

  if (/^FEC:/i.test(value)) {
    const isCommittee = /^FEC:C\d{8}$/i.test(value);
    const isScheduleA = /^FEC:C\d{8}\/schedule_a$/i.test(value);
    const isCommitteeYear = /^FEC:C\d{8}-(\d{4})$/i.test(value);
    const isAlias = /^FEC:[A-Za-z0-9_]+$/i.test(value);

    if (!(isCommittee || isScheduleA || isCommitteeYear || isAlias)) {
      addIssue(
        "error",
        "EVIDENCE_BAD_FEC",
        file,
        loc,
        value,
        "FEC reference must be one of: FEC:C########, FEC:C########/schedule_a, FEC:C########-YYYY, or FEC:<alias>.",
      );
      return;
    }

    if (isCommitteeYear) return;

    if (isAlias && !value.includes("C")) {
      addIssue(
        "warning",
        "FEC_ALIAS",
        file,
        loc,
        value,
        "FEC alias token is not a canonical committee id; check source provenance.",
      );
    }
  }
}

function lintResolvedEvidenceLinks(file, findingId, ref, findingEvidenceMap = undefined) {
  const value = normalizeRef(ref);
  if (!value) return;
  const links = extractEvidenceLinks(value, findingEvidenceMap ? { findingEvidenceMap } : undefined);
  if (links.length === 0) {
    addIssue(
      "error",
      "EVIDENCE_NO_PUBLIC_SOURCE",
      file,
      `finding:${findingId}`,
      value,
      "Evidence reference does not resolve to any publish-valid public source or source record.",
    );
    return;
  }

  if (links.some((link) => String(link.url || "").startsWith("#registry-"))) {
    addIssue(
      "error",
      "EVIDENCE_DEAD_REGISTRY_HASH",
      file,
      `finding:${findingId}`,
      value,
      "Evidence reference resolved to a dead registry hash instead of a source target.",
    );
  }
}

function lintResidualCitationTokens(file, location, markdown) {
  const rendered = String(markdown || "");
  const matches = rendered.matchAll(/\[([^\]]+)\]/g);
  for (const match of matches) {
    const inner = String(match[1] || "").trim();
    if (!inner) continue;
    if (typeof match.index === "number" && rendered[match.index + match[0].length] === "(") continue;
    if (/^\//.test(inner)) continue;
    if (!(/[-_:]/.test(inner) || /\d/.test(inner))) continue;
    addIssue(
      "error",
      "RAW_CITATION_TOKEN",
      file,
      location,
      inner,
      "Citation-like bracket token remained in rendered markdown instead of becoming a public citation.",
    );
  }
}

function runGit(args) {
  try {
    return execFileSync("git", args, { cwd: repoRoot, encoding: "utf-8" }).trim();
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

function isCitationTrackedContent(path) {
  return /^content\/articles\/.+\.mdx$/.test(path) || /^content\/dossiers\/[^/]+\.json$/.test(path);
}

function getChangedContentFiles() {
  if (!changedFilesMode && !strictChangedFiles) {
    return null;
  }

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

    if (!unstaged && !staged && process.env.CI === "true") {
      const recentRange = runGit(["diff", "--name-only", "--diff-filter=ACMR", "HEAD~1...HEAD"]);
      if (recentRange) {
        candidates.push(...recentRange.split("\n"));
      }
    }
  }

  const out = new Set();
  for (const rawPath of candidates) {
    const file = normalizeRepoPath(rawPath);
    if (isCitationTrackedContent(file)) {
      out.add(file);
    }
  }
  return out;
}

function lintArticles(fileScope = null) {
  if (!existsSync(articlesDir)) return;

  const globalFindingCatalog = loadGlobalFindingCatalog({ includeDbFallback: true });
  const files = readdirSync(articlesDir).filter((f) => f.endsWith(".mdx"));
  for (const fileName of files) {
    const abs = resolve(articlesDir, fileName);
    const rel = abs.replace(`${repoRoot}/`, "");
    if (fileScope && !fileScope.has(rel)) continue;
    const raw = readFileSync(abs, "utf-8");
    const body = raw.replace(/^---\n[\s\S]*?\n---\n*/, "");
    const slug = fileName.replace(/\.mdx$/, "");
    const findingEvidenceMap = mergeFindingCatalogs(
      globalFindingCatalog,
      loadArticleFindingCatalog(slug),
    ).evidenceMap;
    const state = createCitationState();

    const findingsPath = resolve(articlesDir, `${fileName.replace(/\.mdx$/, "")}-findings.json`);
    if (existsSync(findingsPath)) {
      const findings = JSON.parse(readFileSync(findingsPath, "utf-8"));
      const rendered = applyCitations(body, { findingEvidenceMap }, state);
      lintCitationEntries(rel, "article:body", state.entries);
      lintResidualCitationTokens(rel, "article:body", rendered.markdown);
      for (const [findingId, detail] of Object.entries(findings)) {
        for (const ev of detail?.evidence || []) {
          lintResolvedEvidenceLinks(rel, String(findingId), ev.evidence_ref, findingEvidenceMap);
        }
      }
    } else {
      const rendered = applyCitations(body, { findingEvidenceMap }, state);
      lintCitationEntries(rel, "article:body", state.entries);
      lintResidualCitationTokens(rel, "article:body", rendered.markdown);
    }
  }
}

function buildDossierFindingEvidenceMap(dossier) {
  return mergeFindingCatalogs(
    loadGlobalFindingCatalog({ includeDbFallback: true }),
    loadDossierFindingCatalog(dossier),
  ).evidenceMap;
}

function lintDossiers(fileScope = null) {
  if (!existsSync(dossiersDir)) return;

  const files = readdirSync(dossiersDir).filter((f) => f.endsWith(".json") && !f.startsWith("_"));
  for (const fileName of files) {
    const abs = resolve(dossiersDir, fileName);
    const rel = abs.replace(`${repoRoot}/`, "");
    if (fileScope && !fileScope.has(rel)) continue;
    const dossier = JSON.parse(readFileSync(abs, "utf-8"));
    const findingEvidenceMap = buildDossierFindingEvidenceMap(dossier);

    for (const finding of dossier.findings || []) {
      for (const ev of finding.evidence || []) {
        lintEvidenceRef(rel, String(finding.id), ev.evidence_ref);
        lintResolvedEvidenceLinks(rel, String(finding.id), ev.evidence_ref, findingEvidenceMap);
      }
    }

    const state = createCitationState();

    const lead = dossier.curation?.lead;
    if (typeof lead === "string" && lead.trim()) {
      const rendered = applyCitations(lead, { findingEvidenceMap }, state);
      lintResidualCitationTokens(rel, "dossier:lead", rendered.markdown);
    }

    const sections = Array.isArray(dossier.curation?.sections) ? dossier.curation.sections : [];
    for (const [index, section] of sections.entries()) {
      const content = typeof section?.content === "string" ? section.content : "";
      if (!content.trim()) continue;
      const rendered = applyCitations(content, { findingEvidenceMap }, state);
      lintCitationEntries(rel, `section:${index}:${section?.title || "untitled"}`, state.entries);
      lintResidualCitationTokens(rel, `section:${index}:${section?.title || "untitled"}`, rendered.markdown);
    }

    const overview = dossier.curation?.overview;
    if (typeof overview === "string" && overview.trim()) {
      const rendered = applyCitations(overview, { findingEvidenceMap }, state);
      lintResidualCitationTokens(rel, "dossier:overview", rendered.markdown);
    }

    const finSummary = dossier.curation?.financial_summary;
    if (typeof finSummary === "string" && finSummary.trim()) {
      const rendered = applyCitations(finSummary, { findingEvidenceMap }, state);
      lintResidualCitationTokens(rel, "dossier:financial_summary", rendered.markdown);
    }

    lintCitationEntries(rel, "dossier:curation", state.entries);
  }
}

const changedContentFiles = getChangedContentFiles();
if (changedContentFiles && changedContentFiles.size === 0) {
  process.stdout.write("Citation lint found no changed article/dossier files to check.\n");
}

lintArticles(changedContentFiles);
lintDossiers(changedContentFiles);

const today = new Date().toISOString().slice(0, 10);
const matchedExceptionIndexes = new Set();

function matchesException(issue, exception) {
  return issue.code === exception.code
    && issue.file === exception.file
    && issue.location === exception.location
    && issue.subject === exception.subject;
}

const filteredIssues = issues.filter((issue) => {
  const matchIndex = citationExceptions.findIndex((exception) => {
    if (!matchesException(issue, exception)) return false;
    return exception.expires_on >= today;
  });

  if (matchIndex === -1) return true;
  matchedExceptionIndexes.add(matchIndex);
  return false;
});

/** @type {LintIssue[]} */
const exceptionDiagnostics = [];
for (const [index, exception] of citationExceptions.entries()) {
  if (exception.expires_on < today) {
    exceptionDiagnostics.push({
      severity: "error",
      code: "CITATION_EXCEPTION_EXPIRED",
      file: exception.file,
      location: exception.location,
      subject: exception.subject,
      message: `Citation exception expired on ${exception.expires_on}: ${exception.reason}`,
    });
    continue;
  }

  if (!matchedExceptionIndexes.has(index)) {
    exceptionDiagnostics.push({
      severity: "error",
      code: "CITATION_EXCEPTION_STALE",
      file: exception.file,
      location: exception.location,
      subject: exception.subject,
      message: `Citation exception no longer matches an active issue: ${exception.reason}`,
    });
  }
}

const finalIssues = [...filteredIssues, ...exceptionDiagnostics];
const errors = finalIssues.filter((i) => i.severity === "error");
const warnings = finalIssues.filter((i) => i.severity === "warning");

const scopeLabel = changedContentFiles
  ? `changed scope (${changedContentFiles.size} file(s))`
  : "full scope";

process.stdout.write(
  `Citation lint scanned ${finalIssues.length} unique issue(s) in ${scopeLabel}: ${errors.length} error(s), ${warnings.length} warning(s).\n`,
);

function printIssueBucket(label, bucket, headingPrefix = "Current") {
  if (bucket.length === 0) return;
  process.stdout.write(`\n${headingPrefix} ${label}s (${bucket.length}):\n`);
  for (const issue of bucket.slice(0, 200)) {
    process.stdout.write(`- [${issue.code}] ${issue.file} (${issue.location}) ${issue.subject} :: ${issue.message}\n`);
  }
  if (bucket.length > 200) {
    process.stdout.write(`- ... ${bucket.length - 200} more ${label}(s)\n`);
  }
}

if (finalIssues.length > 0) {
  printIssueBucket("error", errors);
  printIssueBucket("warning", warnings);
}

if (reportFile) {
  writeFileSync(reportFile, JSON.stringify({
    generated_at: new Date().toISOString(),
    scope: scopeLabel,
    errors: errors.length,
    warnings: warnings.length,
    issues: finalIssues,
    exceptions: citationExceptions,
    matched_exceptions: Array.from(matchedExceptionIndexes),
  }, null, 2));
}

if (strictChangedFiles && finalIssues.length > 0) {
  process.stdout.write(
    "\nStrict changed-file mode failed: citation issues are not allowed in modified article/dossier files.\n",
  );
  printIssueBucket("error", errors, "Changed-file");
  printIssueBucket("warning", warnings, "Changed-file");
  process.exit(1);
}

if (errors.length > 0) {
  process.exit(1);
}

if (strict && warnings.length > 0) {
  process.exit(1);
}

process.exit(0);
