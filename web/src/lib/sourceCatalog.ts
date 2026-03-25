import { existsSync, readFileSync, readdirSync } from "node:fs";
import { basename, resolve } from "node:path";
import {
  applyCitations,
  createCitationState,
  extractEvidenceSourceRecords,
  type CitationEntry,
  type CitationLink,
  type SourceRecord,
} from "./citations";
import { contentPath } from "./projectPaths";

export type SourceOccurrence = {
  routeType: "article" | "dossier";
  slug: string;
  title: string;
  context: "inline_citation" | "finding_evidence";
  findingId?: string;
  evidenceType?: string;
  sourceQuote?: string;
  sourcePage?: string;
  assessment?: string;
};

export type CatalogSourceRecord = SourceRecord & {
  occurrences: SourceOccurrence[];
};

type CatalogMap = Record<string, CatalogSourceRecord>;

let cachedCatalog: CatalogMap | null = null;

function buildFindingEvidenceMap(items: Array<{ id?: string | number; evidence?: Array<{ evidence_ref?: string | null }> }> = []): Record<string, string[]> {
  const map: Record<string, string[]> = {};
  for (const item of items) {
    const id = String(item?.id || "").trim();
    if (!id) continue;
    map[id] = [];
    for (const evidence of item?.evidence || []) {
      const ref = typeof evidence?.evidence_ref === "string" ? evidence.evidence_ref.trim() : "";
      if (ref) {
        map[id].push(ref);
      }
    }
  }
  return map;
}

function parseFrontmatterTitle(raw: string, fallback: string): string {
  const match = raw.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return fallback;
  for (const line of match[1].split("\n")) {
    const titleMatch = line.match(/^title:\s*(.+)$/);
    if (titleMatch) {
      return titleMatch[1].trim().replace(/^["']|["']$/g, "");
    }
  }
  return fallback;
}

function createSourceRecordFromLink(link: CitationLink): SourceRecord | null {
  if (!link.sourceId) return null;
  const kind = link.sourceKind || (link.openUrl ? "external" : "record_only");
  return {
    id: link.sourceId,
    label: link.label,
    title: link.label,
    kind,
    availabilityStatus: link.availabilityStatus || (link.openUrl ? "public_artifact" : "metadata_only"),
    canonicalRef: link.key,
    artifactUrl: link.artifactUrl || link.openUrl || undefined,
    externalUrl: kind === "external" ? (link.openUrl || link.url) : undefined,
    hostedAssetUrl: kind === "hosted_copy" ? (link.openUrl || link.url) : undefined,
    recordUrl: link.sourceRecordUrl || link.url || `/sources/${encodeURIComponent(link.sourceId)}`,
    sourceType: "source_record",
    accessNote: kind === "record_only"
      ? "Held locally by Ithildin. No public artifact URL is currently available."
      : "Public source artifact available.",
    metadataComplete: link.metadataComplete ?? true,
    publishValid: link.publishValid ?? true,
  };
}

function mergeRecord(target: CatalogSourceRecord | undefined, source: SourceRecord): CatalogSourceRecord {
  if (!target) {
    return { ...source, occurrences: [] };
  }
  return {
    ...target,
    label: source.label || target.label,
    title: source.title || target.title,
    kind: source.kind === "record_only" ? target.kind : source.kind,
    availabilityStatus: source.availabilityStatus || target.availabilityStatus,
    canonicalRef: source.canonicalRef || target.canonicalRef,
    artifactUrl: source.artifactUrl || target.artifactUrl,
    externalUrl: source.externalUrl || target.externalUrl,
    hostedAssetUrl: source.hostedAssetUrl || target.hostedAssetUrl,
    recordUrl: source.recordUrl || target.recordUrl,
    sourceType: source.sourceType || target.sourceType,
    publisherOrOrigin: source.publisherOrOrigin || target.publisherOrOrigin,
    publicationOrCaptureDate: source.publicationOrCaptureDate || target.publicationOrCaptureDate,
    pageOrLocator: source.pageOrLocator || target.pageOrLocator,
    excerptOrQuote: source.excerptOrQuote || target.excerptOrQuote,
    accessNote: source.accessNote || target.accessNote,
    integrity: source.integrity || target.integrity,
    metadataComplete: source.metadataComplete ?? target.metadataComplete,
    publishValid: source.publishValid ?? target.publishValid,
    occurrences: target.occurrences,
  };
}

function addOccurrence(
  catalog: CatalogMap,
  record: SourceRecord,
  occurrence: SourceOccurrence,
): void {
  const existing = mergeRecord(catalog[record.id], record);
  existing.occurrences.push(occurrence);
  catalog[record.id] = existing;
}

function collectSourceEntries(markdown: string): CitationEntry[] {
  const state = createCitationState();
  applyCitations(markdown, {}, state);
  return state.entries.filter((entry) => entry.kind === "source");
}

function scanArticles(catalog: CatalogMap): void {
  const articlesDir = contentPath("articles");
  if (!existsSync(articlesDir)) return;

  const articleFiles = readdirSync(articlesDir).filter((file) => file.endsWith(".mdx"));
  for (const fileName of articleFiles) {
    const slug = basename(fileName, ".mdx");
    const raw = readFileSync(resolve(articlesDir, fileName), "utf-8");
    const title = parseFrontmatterTitle(raw, slug);
    const body = raw.replace(/^---\n[\s\S]*?\n---\n*/, "");
    const entries = collectSourceEntries(body);
    for (const entry of entries) {
      const record = createSourceRecordFromLink(entry);
      if (!record || !record.publishValid) continue;
      addOccurrence(catalog, record, {
        routeType: "article",
        slug,
        title,
        context: "inline_citation",
      });
    }
  }

  const findingsFiles = readdirSync(articlesDir).filter((file) => file.endsWith("-findings.json"));
  for (const fileName of findingsFiles) {
    const articleSlug = fileName.replace(/-findings\.json$/, "");
    const raw = JSON.parse(readFileSync(resolve(articlesDir, fileName), "utf-8")) as Record<string, any>;
    const findingEvidenceMap = buildFindingEvidenceMap(
      Object.entries(raw).map(([id, detail]) => ({ id, evidence: detail?.evidence || [] })),
    );
    for (const [findingId, detail] of Object.entries(raw)) {
      for (const ev of detail?.evidence || []) {
        const records = extractEvidenceSourceRecords(ev.evidence_ref || "", { findingEvidenceMap });
        for (const record of records) {
          addOccurrence(catalog, {
            ...record,
            pageOrLocator: record.pageOrLocator || ev.source_page || undefined,
            excerptOrQuote: record.excerptOrQuote || ev.source_quote || undefined,
          }, {
            routeType: "article",
            slug: articleSlug,
            title: articleSlug,
            context: "finding_evidence",
            findingId,
            evidenceType: ev.evidence_type || undefined,
            sourceQuote: ev.source_quote || undefined,
            sourcePage: ev.source_page || undefined,
            assessment: ev.assessment || undefined,
          });
        }
      }
    }
  }
}

function scanDossiers(catalog: CatalogMap): void {
  const dossiersDir = contentPath("dossiers");
  if (!existsSync(dossiersDir)) return;

  const dossierFiles = readdirSync(dossiersDir).filter((file) => file.endsWith(".json") && !file.startsWith("_"));
  for (const fileName of dossierFiles) {
    const slug = basename(fileName, ".json");
    const dossier = JSON.parse(readFileSync(resolve(dossiersDir, fileName), "utf-8"));
    const title = dossier?.name || slug;
    const findingEvidenceMap = buildFindingEvidenceMap(dossier?.findings || []);

    const proseSections = [
      typeof dossier?.curation?.lead === "string" ? dossier.curation.lead : "",
      typeof dossier?.curation?.overview === "string" ? dossier.curation.overview : "",
      typeof dossier?.curation?.financial_summary === "string" ? dossier.curation.financial_summary : "",
      ...(Array.isArray(dossier?.curation?.sections) ? dossier.curation.sections.map((section: any) => section?.content || "") : []),
    ].filter(Boolean);

    for (const prose of proseSections) {
      const entries = collectSourceEntries(String(prose));
      for (const entry of entries) {
        const record = createSourceRecordFromLink(entry);
        if (!record || !record.publishValid) continue;
        addOccurrence(catalog, record, {
          routeType: "dossier",
          slug,
          title,
          context: "inline_citation",
        });
      }
    }

    for (const finding of dossier?.findings || []) {
      for (const ev of finding?.evidence || []) {
        const records = extractEvidenceSourceRecords(ev.evidence_ref || "", { findingEvidenceMap });
        for (const record of records) {
          addOccurrence(catalog, {
            ...record,
            pageOrLocator: record.pageOrLocator || ev.source_page || undefined,
            excerptOrQuote: record.excerptOrQuote || ev.source_quote || undefined,
          }, {
            routeType: "dossier",
            slug,
            title,
            context: "finding_evidence",
            findingId: String(finding.id),
            evidenceType: ev.evidence_type || undefined,
            sourceQuote: ev.source_quote || undefined,
            sourcePage: ev.source_page || undefined,
            assessment: ev.assessment || undefined,
          });
        }
      }
    }
  }
}

export function loadPublicSourceCatalog(): CatalogMap {
  if (cachedCatalog) return cachedCatalog;
  const catalog: CatalogMap = {};
  scanArticles(catalog);
  scanDossiers(catalog);
  cachedCatalog = catalog;
  return cachedCatalog;
}

export function listPublicSourceRecords(): CatalogSourceRecord[] {
  return Object.values(loadPublicSourceCatalog()).sort((left, right) => left.title.localeCompare(right.title));
}

export function getPublicSourceRecord(sourceId: string): CatalogSourceRecord | null {
  return loadPublicSourceCatalog()[sourceId] || null;
}
