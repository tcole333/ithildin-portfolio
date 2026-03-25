import { marked } from "marked";
import {
  applyCitations,
  createCitationState,
  renderFootnotes,
  type CitationEntry,
} from "./citations";
import {
  annotateSupportSpans,
  computeSupportMetrics,
  type SupportMap,
  type SupportMetrics,
} from "./supportSpans";

type FindingEvidenceMap = Record<string, string[]>;

type DossierSection = {
  id?: string;
  title?: string;
  content?: string;
  [key: string]: unknown;
};

type ProcessArticleResult = {
  contentHtml: string;
  footnotesHtml: string;
  pageHtml: string;
  citationEntries: CitationEntry[];
  supportMap: SupportMap;
  supportMetrics: SupportMetrics;
};

type SupportPipelineOptions = {
  enableSupportSpans?: boolean;
};

type ProcessDossierResult = {
  leadHtml: string;
  processedSections: Array<DossierSection & { processedContent: string }>;
  legacyOverviewHtml: string;
  legacyFinancialSummaryHtml: string;
  footnotesHtml: string;
  citationEntries: CitationEntry[];
  supportMap: SupportMap;
  supportMetrics: SupportMetrics;
};

type DossierCurationInput = {
  findingEvidenceMap: FindingEvidenceMap;
  lead?: string | null;
  sections?: DossierSection[];
  legacyOverview?: string | null;
  legacyFinancialSummary?: string | null;
  enableSupportSpans?: boolean;
};

function escapeRegex(value: string): string {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function extractWrappedFragment(combined: string, id: string): string {
  const escaped = escapeRegex(id);
  const pattern = new RegExp(
    `<!--SUPPORT_FRAGMENT:${escaped}:start-->([\\s\\S]*?)<!--SUPPORT_FRAGMENT:${escaped}:end-->`,
  );
  const match = combined.match(pattern);
  return match ? match[1] : "";
}

function wrapFragment(id: string, html: string): string {
  return `<!--SUPPORT_FRAGMENT:${id}:start-->${html}<!--SUPPORT_FRAGMENT:${id}:end-->`;
}

function emptySupportState(): { supportMap: SupportMap; supportMetrics: SupportMetrics } {
  const supportMap: SupportMap = {
    version: 1,
    spans: [],
    nodes: {},
    citation_to_nodes: {},
    orphan_citations: [],
  };
  return {
    supportMap,
    supportMetrics: computeSupportMetrics(supportMap),
  };
}

export function safeJsonForScript(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

export function buildDossierFindingEvidenceMap(dossier: any): FindingEvidenceMap {
  const map: FindingEvidenceMap = {};
  for (const finding of dossier?.findings || []) {
    const id = String(finding?.id);
    map[id] = [];
    for (const ev of finding?.evidence || []) {
      if (typeof ev?.evidence_ref === "string" && ev.evidence_ref.trim()) {
        map[id].push(ev.evidence_ref.trim());
      }
    }
  }
  return map;
}

export async function processArticleEvidenceContent(
  rawContent: string,
  findingEvidenceMap: FindingEvidenceMap = {},
  options: SupportPipelineOptions = {},
): Promise<ProcessArticleResult> {
  const enableSupportSpans = options.enableSupportSpans !== false;
  const citationState = createCitationState();
  const { markdown: contentWithCitations } = applyCitations(
    rawContent,
    { findingEvidenceMap },
    citationState,
  );
  const renderedHtml = String(await marked.parse(String(contentWithCitations)));
  let contentHtml = renderedHtml;
  let { supportMap, supportMetrics } = emptySupportState();

  if (enableSupportSpans) {
    const annotated = annotateSupportSpans(renderedHtml, citationState.entries, { spanIdPrefix: "article-support" });
    contentHtml = annotated.html;
    supportMap = annotated.supportMap;
    supportMetrics = annotated.metrics;
  }

  const footnotesHtml = renderFootnotes(citationState.entries);
  return {
    contentHtml,
    footnotesHtml,
    pageHtml: `${contentHtml}${footnotesHtml}`,
    citationEntries: citationState.entries,
    supportMap,
    supportMetrics,
  };
}

export function processDossierCurationEvidence(input: DossierCurationInput): ProcessDossierResult {
  const enableSupportSpans = input.enableSupportSpans !== false;
  const findingEvidenceMap = input.findingEvidenceMap || {};
  const citationState = createCitationState();

  const rawLead = typeof input.lead === "string" ? input.lead : "";
  const rawSections = Array.isArray(input.sections) ? input.sections : [];
  const rawLegacyOverview = typeof input.legacyOverview === "string" ? input.legacyOverview : "";
  const rawLegacyFinancialSummary =
    typeof input.legacyFinancialSummary === "string" ? input.legacyFinancialSummary : "";

  const leadHtml = rawLead
    ? String(applyCitations(rawLead, { findingEvidenceMap }, citationState).markdown)
    : "";

  const processedSections = rawSections.map((section) => {
    const content = typeof section?.content === "string" ? section.content : "";
    const processedContent = content
      ? String(applyCitations(content, { findingEvidenceMap }, citationState).markdown)
      : "";
    return {
      ...section,
      processedContent,
    };
  });

  const legacyOverviewHtml = rawLegacyOverview
    ? String(applyCitations(rawLegacyOverview, { findingEvidenceMap }, citationState).markdown)
    : "";

  const legacyFinancialSummaryHtml = rawLegacyFinancialSummary
    ? String(applyCitations(rawLegacyFinancialSummary, { findingEvidenceMap }, citationState).markdown)
    : "";

  const fragmentIdsBySection: string[] = [];
  const wrappedFragments: string[] = [];
  if (leadHtml.trim()) {
    wrappedFragments.push(wrapFragment("lead", leadHtml));
  }
  processedSections.forEach((section, index) => {
    if (!section.processedContent.trim()) return;
    const fragmentId = `section-${index}`;
    fragmentIdsBySection[index] = fragmentId;
    wrappedFragments.push(wrapFragment(fragmentId, section.processedContent));
  });
  if (legacyOverviewHtml.trim()) {
    wrappedFragments.push(wrapFragment("legacy-overview", legacyOverviewHtml));
  }
  if (legacyFinancialSummaryHtml.trim()) {
    wrappedFragments.push(wrapFragment("legacy-financial-summary", legacyFinancialSummaryHtml));
  }

  let { supportMap, supportMetrics } = emptySupportState();
  let combined = wrappedFragments.join("\n");

  if (enableSupportSpans && combined.trim()) {
    const annotated = annotateSupportSpans(combined, citationState.entries, { spanIdPrefix: "dossier-support" });
    combined = annotated.html;
    supportMap = annotated.supportMap;
    supportMetrics = annotated.metrics;
  }

  const annotatedLeadHtml = leadHtml.trim()
    ? extractWrappedFragment(combined, "lead") || leadHtml
    : leadHtml;

  const annotatedSections = processedSections.map((section, index) => {
    const fragmentId = fragmentIdsBySection[index];
    if (!fragmentId) return section;
    const annotatedContent = extractWrappedFragment(combined, fragmentId) || section.processedContent;
    return {
      ...section,
      processedContent: annotatedContent,
    };
  });

  const annotatedLegacyOverview = legacyOverviewHtml.trim()
    ? extractWrappedFragment(combined, "legacy-overview") || legacyOverviewHtml
    : legacyOverviewHtml;

  const annotatedLegacyFinancialSummary = legacyFinancialSummaryHtml.trim()
    ? extractWrappedFragment(combined, "legacy-financial-summary") || legacyFinancialSummaryHtml
    : legacyFinancialSummaryHtml;

  return {
    leadHtml: annotatedLeadHtml,
    processedSections: annotatedSections,
    legacyOverviewHtml: annotatedLegacyOverview,
    legacyFinancialSummaryHtml: annotatedLegacyFinancialSummary,
    footnotesHtml: renderFootnotes(citationState.entries),
    citationEntries: citationState.entries,
    supportMap,
    supportMetrics,
  };
}
