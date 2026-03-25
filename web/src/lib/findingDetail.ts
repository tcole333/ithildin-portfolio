import {
  extractEvidenceLinks,
  extractEvidenceSourceRecords,
  type CitationEntry,
  type CitationLink,
  type SourceRecord,
} from "./citations";
import {
  catalogFromFindingItems,
  loadArticleFindingCatalog,
  loadDossierFindingCatalog,
  loadGlobalFindingCatalog,
  mergeFindingCatalogs,
  type RawFindingDetail,
  type RawFindingEvidenceDetail,
} from "./findingCatalog";

export type FindingEvidenceDetail = {
  evidence_type: string;
  evidence_ref: string;
  source_quote?: string;
  source_page?: string;
  assessment?: string;
  resolved_links: CitationLink[];
  resolved_sources: SourceRecord[];
};

export type FindingDetail = {
  id: string;
  summary: string;
  finding_type: string;
  confidence: string;
  claim_type: string;
  verification_status: string;
  date_of_event?: string;
  evidence: FindingEvidenceDetail[];
};

export type FindingDetailMap = Record<string, FindingDetail>;

export function extractCitedFindingIds(entries: CitationEntry[]): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const entry of entries) {
    if (!entry.key.startsWith("finding:")) continue;
    const id = entry.key.slice("finding:".length);
    if (seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

function resolveFindingEvidence(
  evidence: RawFindingEvidenceDetail[],
  findingEvidenceMap: Record<string, string[]>,
): FindingEvidenceDetail[] {
  return evidence.map((item) => ({
    evidence_type: item.evidence_type || "unknown",
    evidence_ref: item.evidence_ref,
    source_quote: item.source_quote || undefined,
    source_page: item.source_page || undefined,
    assessment: item.assessment || undefined,
    resolved_links: extractEvidenceLinks(item.evidence_ref, { findingEvidenceMap }),
    resolved_sources: extractEvidenceSourceRecords(item.evidence_ref, { findingEvidenceMap }),
  }));
}

function resolveFindingDetails(rawMap: Record<string, RawFindingDetail>): FindingDetailMap {
  const findingEvidenceMap = Object.fromEntries(
    Object.entries(rawMap).map(([id, detail]) => [
      id,
      (detail.evidence || []).map((item) => String(item.evidence_ref || "").trim()).filter(Boolean),
    ]),
  );

  const detailMap: FindingDetailMap = {};
  for (const [id, detail] of Object.entries(rawMap)) {
    detailMap[id] = {
      id,
      summary: detail.summary || "",
      finding_type: detail.finding_type || "unknown",
      confidence: detail.confidence || "medium",
      claim_type: detail.claim_type || "inference",
      verification_status: detail.verification_status || "unverified",
      date_of_event: detail.date_of_event || undefined,
      evidence: resolveFindingEvidence(detail.evidence || [], findingEvidenceMap),
    };
  }

  return detailMap;
}

function filterResolvedDetailMap(detailMap: FindingDetailMap, findingIds: string[]): FindingDetailMap {
  const idSet = new Set(findingIds);
  const filtered: FindingDetailMap = {};
  for (const [id, detail] of Object.entries(detailMap)) {
    if (!idSet.has(id)) continue;
    filtered[id] = detail;
  }
  return filtered;
}

export function loadFindingDetails(findingIds: string[], slug?: string): FindingDetailMap {
  if (findingIds.length === 0) return {};
  const globalCatalog = loadGlobalFindingCatalog({ findingIds, includeDbFallback: true });
  const articleCatalog = slug ? loadArticleFindingCatalog(slug) : catalogFromFindingItems([]);
  const mergedCatalog = mergeFindingCatalogs(globalCatalog, articleCatalog);
  return filterResolvedDetailMap(resolveFindingDetails(mergedCatalog.detailMap), findingIds);
}

export function buildDossierFindingDetailMap(
  dossier: any,
  citedFindingIds: string[],
): FindingDetailMap {
  if (!citedFindingIds.length) return {};
  const globalCatalog = loadGlobalFindingCatalog({ findingIds: citedFindingIds, includeDbFallback: true });
  const dossierCatalog = loadDossierFindingCatalog(dossier);
  const mergedCatalog = mergeFindingCatalogs(globalCatalog, dossierCatalog);
  return filterResolvedDetailMap(resolveFindingDetails(mergedCatalog.detailMap), citedFindingIds);
}
