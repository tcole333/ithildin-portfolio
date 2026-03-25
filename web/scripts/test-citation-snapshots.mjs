import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { createJiti } from "jiti";
import { contentRoot } from "./lib/project-paths.mjs";

const args = new Set(process.argv.slice(2));
const update = args.has("--update");

const cwd = process.cwd();
const snapshotPath = resolve(cwd, "scripts", "citation-snapshots.json");

const jiti = createJiti(import.meta.url);
const { applyCitations, createCitationState, renderFootnotes } = jiti(resolve(cwd, "src", "lib", "citations.ts"));
const {
  processArticleEvidenceContent,
  processDossierCurationEvidence,
  buildDossierFindingEvidenceMap,
} = jiti(resolve(cwd, "src", "lib", "contentEvidencePipeline.ts"));

function normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function normalizeCitationNumbering(value) {
  return normalizeText(value)
    .replace(/href="#fn-\d+"/g, 'href="#fn-N"')
    .replace(/\/sources\/[a-z0-9-]+/gi, "/sources/source-record")
    .replace(/data-support-span-id="[^"]+"/g, 'data-support-span-id="support-N"')
    .replace(/data-citation-number="\d+"/g, 'data-citation-number="N"')
    .replace(/id="fn-\d+"/g, 'id="fn-N"')
    .replace(/aria-label="Source \d+:/g, 'aria-label="Source N:')
    .replace(/<span class="citation-index">\d+\.<\/span>/g, '<span class="citation-index">N.</span>')
    .replace(/>(\d+)<\/a>/g, ">N</a>");
}

function extractFirstMatch(value, pattern) {
  const match = String(value || "").match(pattern);
  return match ? normalizeCitationNumbering(match[0]) : "";
}

function extractFootnoteEntry(footnotesHtml, needle) {
  const entries = String(footnotesHtml || "").match(/<li id="fn-\d+">[\s\S]*?<\/li>/g) || [];
  const entry = entries.find((item) => item.includes(needle));
  return normalizeCitationNumbering(entry || "");
}

async function buildArticleSnapshot() {
  const articlePath = resolve(contentRoot, "articles", "port-watch-procurement-network.mdx");
  const raw = readFileSync(articlePath, "utf-8");
  const body = raw.replace(/^---\n[\s\S]*?\n---\n*/, "");

  const state = createCitationState();
  const { markdown } = applyCitations(body, {}, state);
  const footnotesHtml = renderFootnotes(state.entries);
  const articleEvidence = await processArticleEvidenceContent(body, {});

  return {
    markdown_link_line: extractFirstMatch(
      markdown,
      /In this case, a public registry filing[\s\S]*?PORT-WATCH-CALL-NOTES-2025-02-14[\s\S]*?<\/sup>/,
    ),
    metadata_only_footnote: extractFootnoteEntry(footnotesHtml, "PORT-WATCH-CALL-NOTES-2025-02-14"),
    support_span_line: extractFirstMatch(
      articleEvidence.contentHtml,
      /<span class="support-span support-span--supported"[^>]*>A reporting memo[\s\S]*?<\/span>/,
    ),
    has_unsupported_spans: articleEvidence.supportMap.spans.some((span) => span.supported === false),
    has_split_fragments: state.entries.some((entry) =>
      ["https:", "en.wikipedia.org", "wiki", "Israel%E2%80%93Qatar_relations"].includes(String(entry.label || "")),
    ),
  };
}

function buildDossierSnapshot() {
  const dossierPath = resolve(contentRoot, "dossiers", "harbor-ledger-holdings.json");
  const dossier = JSON.parse(readFileSync(dossierPath, "utf-8"));

  const lead = dossier.curation?.lead || "";
  const sections = Array.isArray(dossier.curation?.sections) ? dossier.curation.sections : [];
  const overview = dossier.curation?.overview || "";
  const financialSummary = dossier.curation?.financial_summary || "";
  const hasCurationProse =
    Boolean(String(lead).trim()) ||
    sections.some((section) => Boolean(String(section?.content || "").trim())) ||
    Boolean(String(overview).trim()) ||
    Boolean(String(financialSummary).trim());

  const findingEvidenceMap = buildDossierFindingEvidenceMap(dossier);
  const dossierEvidence = processDossierCurationEvidence({
    findingEvidenceMap,
    lead,
    sections,
    legacyOverview: overview,
    legacyFinancialSummary: financialSummary,
  });
  const footnotesHtml = dossierEvidence.footnotesHtml;
  const findingFootnote = extractFootnoteEntry(footnotesHtml, "Finding #");
  const urls = Array.from(findingFootnote.matchAll(/href="([^"]+)"/g), (match) => normalizeCitationNumbering(match[1]));
  const supportLine = extractFirstMatch(
    `${dossierEvidence.leadHtml}\n${dossierEvidence.processedSections.map((section) => section.processedContent).join("\n")}\n${dossierEvidence.legacyOverviewHtml}\n${dossierEvidence.legacyFinancialSummaryHtml}`,
    /<span class="support-span support-span--supported"[^>]*>[\s\S]*?<\/span>/,
  );

  return {
    has_curation_prose: hasCurationProse,
    has_footnotes: Boolean(String(footnotesHtml).trim()),
    finding_footnote: findingFootnote,
    finding_urls: urls,
    support_span_line: supportLine,
    has_source_data_attrs: /data-source-key=/.test(findingFootnote) && /data-parent-citation-key=/.test(findingFootnote),
  };
}

async function buildSnapshotPayload() {
  return {
    article_port_watch_procurement_network: await buildArticleSnapshot(),
    dossier_harbor_ledger_holdings: buildDossierSnapshot(),
  };
}

const current = await buildSnapshotPayload();

assert.ok(
  current.article_port_watch_procurement_network.markdown_link_line,
  "Expected Port Watch article citation line to be present.",
);
assert.ok(
  current.article_port_watch_procurement_network.metadata_only_footnote,
  "Expected Port Watch metadata-only footnote to be present.",
);
assert.ok(
  current.article_port_watch_procurement_network.support_span_line,
  "Expected Port Watch support span sample to be present.",
);
assert.equal(
  current.article_port_watch_procurement_network.has_unsupported_spans,
  true,
  "Expected unsupported article spans.",
);
assert.equal(
  current.article_port_watch_procurement_network.has_split_fragments,
  false,
  "Split URL citation fragments detected.",
);

if (current.dossier_harbor_ledger_holdings.has_curation_prose) {
  assert.ok(
    current.dossier_harbor_ledger_holdings.support_span_line,
    "Expected Harbor Ledger support span sample to be present when curation prose exists.",
  );
  if (current.dossier_harbor_ledger_holdings.has_footnotes) {
    assert.ok(
      current.dossier_harbor_ledger_holdings.finding_footnote,
      "Expected Harbor Ledger finding footnote to be present when footnotes are rendered.",
    );
    assert.equal(
      current.dossier_harbor_ledger_holdings.has_source_data_attrs,
      true,
      "Expected finding footnote source data attributes.",
    );
  }
}

if (update || !existsSync(snapshotPath)) {
  writeFileSync(snapshotPath, `${JSON.stringify(current, null, 2)}\n`, "utf-8");
  process.stdout.write(`Updated citation snapshot fixture at ${snapshotPath}\n`);
  process.exit(0);
}

const expected = JSON.parse(readFileSync(snapshotPath, "utf-8"));
assert.deepEqual(current, expected);
process.stdout.write("Citation snapshot regression checks passed.\n");
