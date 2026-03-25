import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { contentPath, investigationDbCandidates } from "./projectPaths";

export type FindingEvidenceMap = Record<string, string[]>;

export type RawFindingEvidenceDetail = {
  evidence_type: string;
  evidence_ref: string;
  source_quote?: string;
  source_page?: string;
  assessment?: string;
};

export type RawFindingDetail = {
  id: string;
  summary: string;
  finding_type: string;
  confidence: string;
  claim_type: string;
  verification_status: string;
  date_of_event?: string;
  evidence: RawFindingEvidenceDetail[];
};

export type RawFindingDetailMap = Record<string, RawFindingDetail>;

export type FindingCatalog = {
  detailMap: RawFindingDetailMap;
  evidenceMap: FindingEvidenceMap;
};

type CatalogOptions = {
  findingIds?: string[];
  includeDbFallback?: boolean;
};

let cachedContentCatalog: FindingCatalog | null = null;
let cachedDbCatalog: FindingCatalog | null = null;

function articlesDir(): string {
  return contentPath("articles");
}

function dossiersDir(): string {
  return contentPath("dossiers");
}

function isUsableDb(path: string): boolean {
  if (!existsSync(path)) return false;
  try {
    return statSync(path).size > 0;
  } catch {
    return false;
  }
}

function normalizeEvidenceItem(evidence: any): RawFindingEvidenceDetail | null {
  const evidenceRef = typeof evidence?.evidence_ref === "string" ? evidence.evidence_ref.trim() : "";
  if (!evidenceRef) return null;
  return {
    evidence_type: evidence?.evidence_type || "unknown",
    evidence_ref: evidenceRef,
    source_quote: evidence?.source_quote || undefined,
    source_page: evidence?.source_page || undefined,
    assessment: evidence?.assessment || undefined,
  };
}

function normalizeFindingRecord(finding: any): RawFindingDetail | null {
  const id = String(finding?.id || "").trim();
  if (!id) return null;
  return {
    id,
    summary: finding?.summary || "",
    finding_type: finding?.finding_type || "unknown",
    confidence: finding?.confidence || "medium",
    claim_type: finding?.claim_type || "inference",
    verification_status: finding?.verification_status || "unverified",
    date_of_event: finding?.date_of_event || undefined,
    evidence: Array.isArray(finding?.evidence)
      ? finding.evidence.map(normalizeEvidenceItem).filter((item): item is RawFindingEvidenceDetail => Boolean(item))
      : [],
  };
}

export function buildFindingEvidenceMapFromItems(
  items: Array<{ id?: string | number; evidence?: Array<{ evidence_ref?: string | null }> }> = [],
): FindingEvidenceMap {
  const map: FindingEvidenceMap = {};
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

export function buildRawFindingDetailMapFromItems(items: any[] = []): RawFindingDetailMap {
  const detailMap: RawFindingDetailMap = {};
  for (const finding of items) {
    const normalized = normalizeFindingRecord(finding);
    if (!normalized) continue;
    detailMap[normalized.id] = normalized;
  }
  return detailMap;
}

export function catalogFromFindingItems(items: any[] = []): FindingCatalog {
  const detailMap = buildRawFindingDetailMapFromItems(items);
  return {
    detailMap,
    evidenceMap: buildFindingEvidenceMapFromItems(Object.values(detailMap)),
  };
}

function mergeDetailMaps(...maps: RawFindingDetailMap[]): RawFindingDetailMap {
  const merged: RawFindingDetailMap = {};
  for (const map of maps) {
    for (const [id, detail] of Object.entries(map || {})) {
      merged[id] = detail;
    }
  }
  return merged;
}

export function mergeFindingCatalogs(...catalogs: Array<FindingCatalog | null | undefined>): FindingCatalog {
  const detailMaps = catalogs.filter(Boolean).map((catalog) => catalog!.detailMap);
  const detailMap = mergeDetailMaps(...detailMaps);
  return {
    detailMap,
    evidenceMap: buildFindingEvidenceMapFromItems(Object.values(detailMap)),
  };
}

function filterCatalog(catalog: FindingCatalog, findingIds?: string[]): FindingCatalog {
  if (!findingIds || findingIds.length === 0) return catalog;
  const idSet = new Set(findingIds.map((id) => String(id).trim()).filter(Boolean));
  const detailMap: RawFindingDetailMap = {};
  for (const id of idSet) {
    if (catalog.detailMap[id]) {
      detailMap[id] = catalog.detailMap[id];
    }
  }
  return {
    detailMap,
    evidenceMap: buildFindingEvidenceMapFromItems(Object.values(detailMap)),
  };
}

function scanArticleFindings(detailMap: RawFindingDetailMap): void {
  const dir = articlesDir();
  if (!existsSync(dir)) return;
  const files = readdirSync(dir).filter((file) => file.endsWith("-findings.json"));
  for (const fileName of files) {
    const raw = JSON.parse(readFileSync(resolve(dir, fileName), "utf-8")) as Record<string, any>;
    Object.assign(detailMap, buildRawFindingDetailMapFromItems(Object.values(raw)));
  }
}

function scanDossierFindings(detailMap: RawFindingDetailMap): void {
  const dir = dossiersDir();
  if (!existsSync(dir)) return;
  const files = readdirSync(dir).filter((file) => file.endsWith(".json") && !file.startsWith("_"));
  for (const fileName of files) {
    const dossier = JSON.parse(readFileSync(resolve(dir, fileName), "utf-8"));
    Object.assign(detailMap, buildRawFindingDetailMapFromItems(dossier?.findings || []));
  }
}

function queryFindingRowsFromDb(dbPath: string, findingIds?: string[]): any[] {
  const whereClause = findingIds && findingIds.length > 0
    ? `WHERE f.id IN (${findingIds.map((id) => `'${String(id).replace(/'/g, "''")}'`).join(",")})`
    : "";
  const sql = `
    SELECT f.id, f.summary, f.finding_type, f.confidence, f.claim_type,
           f.verification_status, f.date_of_event,
           fe.evidence_type, fe.evidence_ref, fe.source_quote,
           fe.source_page, fe.assessment
    FROM findings f
    LEFT JOIN finding_evidence fe ON fe.finding_id = f.id
    ${whereClause}
    ORDER BY f.id;
  `;

  try {
    const output = execFileSync("sqlite3", [dbPath, ".mode json", sql], {
      encoding: "utf-8",
    }).trim();
    if (!output) return [];
    return JSON.parse(output) as any[];
  } catch {
    return [];
  }
}

function buildCatalogFromDbRows(rows: any[]): FindingCatalog {
  const detailMap: RawFindingDetailMap = {};
  for (const row of rows) {
    const id = String(row.id);
    if (!detailMap[id]) {
      detailMap[id] = {
        id,
        summary: row.summary || "",
        finding_type: row.finding_type || "unknown",
        confidence: row.confidence || "medium",
        claim_type: row.claim_type || "inference",
        verification_status: row.verification_status || "unverified",
        date_of_event: row.date_of_event || undefined,
        evidence: [],
      };
    }
    const normalized = normalizeEvidenceItem(row);
    if (normalized) {
      detailMap[id].evidence.push(normalized);
    }
  }
  return {
    detailMap,
    evidenceMap: buildFindingEvidenceMapFromItems(Object.values(detailMap)),
  };
}

function loadDbFindingCatalog(findingIds?: string[]): FindingCatalog {
  const moduleDir = dirname(fileURLToPath(import.meta.url));
  const dbPaths = Array.from(
    new Set([
      ...investigationDbCandidates(),
      resolve(moduleDir, "..", "..", "..", "investigation.db"),
    ]),
  );

  if (!findingIds || findingIds.length === 0) {
    if (cachedDbCatalog) return cachedDbCatalog;
    for (const dbPath of dbPaths) {
      if (!isUsableDb(dbPath)) continue;
      const catalog = buildCatalogFromDbRows(queryFindingRowsFromDb(dbPath));
      cachedDbCatalog = catalog;
      return cachedDbCatalog;
    }
    cachedDbCatalog = { detailMap: {}, evidenceMap: {} };
    return cachedDbCatalog;
  }

  for (const dbPath of dbPaths) {
    if (!isUsableDb(dbPath)) continue;
    return buildCatalogFromDbRows(queryFindingRowsFromDb(dbPath, findingIds));
  }

  return { detailMap: {}, evidenceMap: {} };
}

export function loadContentFindingCatalog(): FindingCatalog {
  if (cachedContentCatalog) return cachedContentCatalog;
  const detailMap: RawFindingDetailMap = {};
  scanArticleFindings(detailMap);
  scanDossierFindings(detailMap);
  cachedContentCatalog = {
    detailMap,
    evidenceMap: buildFindingEvidenceMapFromItems(Object.values(detailMap)),
  };
  return cachedContentCatalog;
}

export function loadGlobalFindingCatalog(options: CatalogOptions = {}): FindingCatalog {
  const contentCatalog = filterCatalog(loadContentFindingCatalog(), options.findingIds);
  if (!options.includeDbFallback) {
    return contentCatalog;
  }

  const requestedIds = options.findingIds?.map((id) => String(id).trim()).filter(Boolean) || [];
  if (requestedIds.length > 0) {
    const missingIds = requestedIds.filter((id) => !contentCatalog.detailMap[id]);
    if (missingIds.length === 0) {
      return contentCatalog;
    }
    return mergeFindingCatalogs(loadDbFindingCatalog(missingIds), contentCatalog);
  }

  return mergeFindingCatalogs(loadDbFindingCatalog(), contentCatalog);
}

export function loadArticleFindingCatalog(slug: string): FindingCatalog {
  const filePath = resolve(articlesDir(), `${slug}-findings.json`);
  if (!existsSync(filePath)) {
    return { detailMap: {}, evidenceMap: {} };
  }
  const raw = JSON.parse(readFileSync(filePath, "utf-8")) as Record<string, any>;
  return catalogFromFindingItems(Object.values(raw));
}

export function loadDossierFindingCatalog(dossier: any): FindingCatalog {
  return catalogFromFindingItems(dossier?.findings || []);
}

export function loadFindingEvidenceMap(options: CatalogOptions = {}): FindingEvidenceMap {
  return loadGlobalFindingCatalog(options).evidenceMap;
}

export function loadFindingDetailMap(options: CatalogOptions = {}): RawFindingDetailMap {
  return loadGlobalFindingCatalog(options).detailMap;
}

export function findArticleFindingsFile(slug: string): string | null {
  const candidates = [
    resolve(articlesDir(), `${slug}-findings.json`),
    resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "content", "articles", `${slug}-findings.json`),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

export function listContentFindingIds(): string[] {
  return Object.keys(loadContentFindingCatalog().detailMap);
}

export function findFindingOwners(findingId: string): Array<{ routeType: "article" | "dossier"; slug: string }> {
  const owners: Array<{ routeType: "article" | "dossier"; slug: string }> = [];
  const normalizedId = String(findingId).trim();
  if (!normalizedId) return owners;

  const articleDir = articlesDir();
  if (existsSync(articleDir)) {
    const files = readdirSync(articleDir).filter((file) => file.endsWith("-findings.json"));
    for (const fileName of files) {
      const raw = JSON.parse(readFileSync(resolve(articleDir, fileName), "utf-8")) as Record<string, any>;
      if (raw[normalizedId]) {
        owners.push({ routeType: "article", slug: basename(fileName, "-findings.json") });
      }
    }
  }

  const dossierDir = dossiersDir();
  if (existsSync(dossierDir)) {
    const files = readdirSync(dossierDir).filter((file) => file.endsWith(".json") && !file.startsWith("_"));
    for (const fileName of files) {
      const dossier = JSON.parse(readFileSync(resolve(dossierDir, fileName), "utf-8"));
      if ((dossier?.findings || []).some((finding: any) => String(finding?.id) === normalizedId)) {
        owners.push({ routeType: "dossier", slug: basename(fileName, ".json") });
      }
    }
  }

  return owners;
}
