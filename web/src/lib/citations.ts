import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkStringify from "remark-stringify";
import jmailOverridesData from "../data/jmail-overrides.json";
import clOverridesData from "../data/cl-overrides.json";
import sourceUrlOverridesData from "../data/source-urls.json";
import manualSourceRecordsData from "../data/source-records.json";
import { loadFindingEvidenceMap } from "./findingEvidence";

export type CitationLink = {
  key: string;
  label: string;
  url?: string;
  openUrl?: string;
  artifactUrl?: string;
  sourceRecordUrl?: string;
  sourceId?: string;
  sourceKind?: SourceKind;
  availabilityStatus?: SourceAvailabilityStatus;
  metadataComplete?: boolean;
  publishValid?: boolean;
};

export type CitationEntry = CitationLink & {
  number: number;
  kind: "source" | "finding";
  targetKind: "artifact" | "source_record" | "finding_popover";
  sourceId?: string;
  sources?: CitationLink[];
};

export type SourceKind = "external" | "hosted_copy" | "record_only" | "private_internal";
export type SourceAvailabilityStatus = "public_artifact" | "hosted_copy" | "metadata_only" | "restricted";

export type SourceIntegrity = {
  sha256?: string;
  fileSize?: number;
  internalArtifactId?: string;
};

export type SourceRecord = {
  id: string;
  label: string;
  title: string;
  kind: SourceKind;
  availabilityStatus: SourceAvailabilityStatus;
  canonicalRef: string;
  artifactUrl?: string;
  externalUrl?: string;
  hostedAssetUrl?: string;
  recordUrl: string;
  sourceType: string;
  publisherOrOrigin?: string;
  publicationOrCaptureDate?: string;
  pageOrLocator?: string;
  excerptOrQuote?: string;
  accessNote: string;
  integrity?: SourceIntegrity;
  metadataComplete: boolean;
  publishValid: boolean;
};

type CitationOptions = {
  findingEvidenceMap?: Record<string, string[]>;
};

type EvidenceExtractionOptions = {
  findingEvidenceMap?: Record<string, string[]>;
  seenFindingIds?: Set<string>;
};

export type CitationState = {
  entries: CitationEntry[];
  index: Map<string, number>;
};

export type HealthTier = "tier1" | "tier2" | "tier3" | "tier4" | "label-only";

type RawCitationResolution = Omit<CitationEntry, "number" | "kind" | "targetKind"> & {
  kind?: CitationEntry["kind"];
  targetKind?: CitationEntry["targetKind"];
};

type CitationTypeDef = {
  id: string;
  tokenPattern: string;
  healthTier: HealthTier;
  resolve(token: string, options: CitationOptions): RawCitationResolution | null;
  extract(raw: string): CitationLink[];
  stripPattern?: RegExp | false;
};

type ManualSourceRecord = {
  title?: string;
  kind?: SourceKind;
  availability_status?: SourceAvailabilityStatus;
  source_type?: string;
  publisher_or_origin?: string;
  publication_or_capture_date?: string;
  page_or_locator?: string;
  excerpt_or_quote?: string;
  access_note?: string;
  artifact_url?: string;
  integrity?: SourceIntegrity;
  publish_valid?: boolean;
  external_url?: string;
  hosted_asset_url?: string;
};

const URL_RE = /https?:\/\/[^\s\]]+/gi;

const JMAIL_BASE = "https://jmail.world/thread";

function buildSecEdgarUrl(accession: string): string {
  const dashless = accession.replace(/-/g, "");
  return `https://www.sec.gov/Archives/edgar/data/${dashless.slice(0, 10)}/${accession}-index.htm`;
}

function build990Url(ein: string): string {
  return `https://projects.propublica.org/nonprofits/organizations/${ein}`;
}

function buildAcrisUrl(docId: string): string {
  return `https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentDetail?doc_id=${docId}`;
}

const clOverrides: Record<string, string> = clOverridesData;
const sourceUrlOverrides: Record<string, string> = sourceUrlOverridesData;
const manualSourceRecords = manualSourceRecordsData as Record<string, ManualSourceRecord>;

function buildCourtListenerUrl(docketId: string): string {
  if (clOverrides[docketId]) return clOverrides[docketId];
  return `https://www.courtlistener.com/docket/${docketId}/`;
}

function buildDs10Url(): string {
  return "/financials";
}

function buildLdaUrl(registrant: string): string {
  return `https://lda.senate.gov/filings/public/filing/search/?registrant=${encodeURIComponent(registrant)}&filing_type=`;
}

function buildOpenSanctionsUrl(entityId: string): string {
  return `https://www.opensanctions.org/entities/${entityId}/`;
}

function buildDocumentCloudUrl(docId: string): string {
  return `https://www.documentcloud.org/documents/${docId}`;
}

function buildOffshoreAlertUrl(slug: string): string {
  return `https://www.offshorealert.com/${slug}/`;
}

function buildMuckRockUrl(requestId: string): string {
  return `https://www.muckrock.com/foi/${requestId}/`;
}

function buildLittleSisUrl(entityId: string): string {
  return `https://littlesis.org/entities/${entityId}`;
}

function buildIcijUrl(nodeId: string): string {
  return `https://offshoreleaks.icij.org/nodes/${nodeId}`;
}

function buildFecCommitteeUrl(committeeId: string): string {
  return `https://www.fec.gov/data/committee/${committeeId}/`;
}

function buildFecSearchUrl(query: string): string {
  return `https://www.fec.gov/data/search/?q=${encodeURIComponent(query)}`;
}

function normalizeFecCycle(year: number): number {
  return year % 2 === 0 ? year : year + 1;
}

function buildFecReceiptsUrl(committeeId: string, year?: string): string {
  const params = new URLSearchParams({ committee_id: committeeId });
  if (year && /^\d{4}$/.test(year)) {
    const parsed = Number.parseInt(year, 10);
    if (Number.isFinite(parsed)) {
      params.set("two_year_transaction_period", String(normalizeFecCycle(parsed)));
    }
  }
  return `https://www.fec.gov/data/receipts/?${params.toString()}`;
}

function buildFaraUrl(_regNum: string): string {
  return "https://efile.fara.gov/docs/";
}

function buildRegistryUrl(jurisdiction: string, entityId: string): string {
  const builders: Record<string, (id: string) => string> = {
    FL: () => "https://search.sunbiz.org/Inquiry/CorporationSearch/ByDocumentNumber",
    NY: () => "https://appext20.dos.ny.gov/corp_public/CORPSEARCH.ENTITY_SEARCH_ENTRY",
    NM: () => "https://portal.sos.state.nm.us/BFS/online/CorporationBusinessSearch",
    DE: () => "https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx",
    USVI: () => "https://www.ltg.gov.vi/division-of-corporations/",
    UK: (id) => `https://find-and-update.company-information.service.gov.uk/company/${id}`,
  };
  const normalizedJurisdiction = normalizeRegistryJurisdiction(jurisdiction);
  const builder = builders[normalizedJurisdiction];
  return builder ? builder(entityId) : `#registry-${jurisdiction}-${entityId}`;
}

function cleanToken(value: string): string {
  return value
    .replace(/^[\s[(]+/, "")
    .replace(/[\s\])]+$/, "")
    .replace(/\s+/g, " ")
    .replace(/[.,;]+$/, "")
    .trim();
}

function cleanUrl(value: string): string {
  return value.replace(/[),.;]+$/, "");
}

const jmailOverrides: Record<string, string> = jmailOverridesData;

function buildJmailUrl(id: string): string {
  if (jmailOverrides[id]) return jmailOverrides[id];
  return `${JMAIL_BASE}/${id}?view=inbox`;
}

function isExternalUrl(url?: string): boolean {
  return Boolean(url && /^https?:\/\//i.test(url));
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function uniqueInOrder(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    if (seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-");
}

function hashString(value: string): string {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash << 5) - hash + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

function normalizeRegistryJurisdiction(jurisdiction: string): string {
  const normalized = jurisdiction.toUpperCase();
  return normalized === "VI" ? "USVI" : normalized;
}

function buildSourceRecordPath(sourceId: string): string {
  return `/sources/${encodeURIComponent(sourceId)}`;
}

function createSourceId(canonicalRef: string, _label: string): string {
  const base = slugify(canonicalRef) || "source";
  return `${base.slice(0, 56)}-${hashString(canonicalRef).slice(0, 8)}`;
}

function guessSourceType(value: string): string {
  const token = cleanToken(value);
  if (!token) return "source_record";
  if (/^https?:\/\//i.test(token)) return "web_page";
  if (/^EFTA\d+/i.test(token)) return "released_correspondence";
  if (/^HOUSE_OVERSIGHT_/i.test(token)) return "government_document";
  if (/^(SEC|EDGAR):/i.test(token)) return "securities_filing";
  if (/^990:/i.test(token)) return "tax_filing";
  if (/^ACRIS:/i.test(token)) return "property_record";
  if (/^(CL|CourtListener)/i.test(token)) return "court_record";
  if (/^FEC:/i.test(token)) return "campaign_finance_record";
  if (/^FARA:/i.test(token)) return "lobbying_record";
  if (/^(USVI:|REG:|FL-SunBiz|NM-SoS|NY-SoS)/i.test(token)) return "corporate_registry";
  if (/^DOCUMENTCLOUD:/i.test(token)) return "document_cloud_record";
  if (/^MUCKROCK:/i.test(token)) return "foia_record";
  if (/^OffshoreAlert:/i.test(token)) return "news_archive";
  if (/^LittleSis/i.test(token)) return "entity_database";
  if (/^ICIJ/i.test(token)) return "leaks_database";
  if (/^KPMG:/i.test(token)) return "forensic_report";
  if (/^DS10/i.test(token)) return "dataset";
  if (/^(DFS|SAR|DECHERT|DOJ|F\d+)/i.test(token)) return "local_document";
  return "source_record";
}

function parsePageLocator(token: string): string | undefined {
  const match = token.match(/(?:^|[-_:])p(?:age)?[-_:]?(\d+)$/i);
  if (!match) return undefined;
  return `p. ${match[1]}`;
}

function prettifySourceTitle(value: string): string {
  const cleaned = cleanToken(value);
  if (!cleaned) return "Source Record";
  return cleaned
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b([a-z])/g, (_, char: string) => char.toUpperCase());
}

type InternalReferenceKind = "finding" | "connection" | "entity" | "lead" | "thread";

type InternalReference = {
  kind: InternalReferenceKind;
  id: string;
};

const INTERNAL_REFERENCE_LABELS: Record<InternalReferenceKind, string> = {
  finding: "Finding",
  connection: "Connection",
  entity: "Entity",
  lead: "Lead",
  thread: "Thread",
};

const LOOSE_SOURCE_LABEL_ALLOWLIST = new Set(["bisbase"]);

function isPrivateInternalReference(token: string): boolean {
  return /^(?:Connection|Entity|Lead|Hypothesis|Thread)\s*#\s*\d+$/i.test(token)
    || /^Finding\s*#\s*\d+$/i.test(token)
    || /^unified:/i.test(token);
}

function isInternalReferenceKind(value: string): value is InternalReferenceKind {
  return ["finding", "connection", "entity", "lead", "thread"].includes(value.toLowerCase());
}

function normalizeInternalReferenceKindToken(value: string): InternalReferenceKind | null {
  const normalized = String(value || "").trim().toLowerCase().replace(/s$/, "");
  return isInternalReferenceKind(normalized) ? normalized : null;
}

function parseInternalReferenceGroup(group: string): InternalReference[] | null {
  const normalized = cleanToken(group);
  if (!normalized) return null;

  const parts = normalized
    .split(";")
    .flatMap(part => part.split(","))
    .flatMap(part => part.split(/\s+and\s+/i))
    .map(cleanToken)
    .filter(Boolean);

  if (!parts.length) return null;

  const refs: InternalReference[] = [];
  let currentKind: InternalReferenceKind | null = null;

  for (const part of parts) {
    const typedMatch = part.match(/^([A-Za-z]+)\s*#\s*(\d+)$/);
    if (typedMatch) {
      const kind = normalizeInternalReferenceKindToken(typedMatch[1]);
      if (!kind) return null;
      currentKind = kind;
      refs.push({ kind, id: typedMatch[2] });
      continue;
    }

    const shorthandMatch = part.match(/^#\s*(\d+)$/);
    if (shorthandMatch && currentKind) {
      refs.push({ kind: currentKind, id: shorthandMatch[1] });
      continue;
    }

    return null;
  }

  return refs.length ? refs : null;
}

function extractReferencedFindingIds(raw: string): string[] {
  const refs = parseInternalReferenceGroup(raw);
  if (refs) {
    return uniqueInOrder(refs.filter((ref) => ref.kind === "finding").map((ref) => ref.id));
  }

  const findingMatches = raw.matchAll(/Finding\s*#\s*(\d+)/gi);
  const shorthandMatches = raw.matchAll(/\bF(\d+)\b/g);
  const colonMatches = raw.matchAll(/\bfindings?\s*:\s*(\d+)\b/gi);
  const bareMatches = raw.matchAll(/\bfindings?\s+(\d+)\b/gi);
  return uniqueInOrder([
    ...Array.from(findingMatches, (match) => String(match[1] || "").trim()).filter(Boolean),
    ...Array.from(shorthandMatches, (match) => String(match[1] || "").trim()).filter(Boolean),
    ...Array.from(colonMatches, (match) => String(match[1] || "").trim()).filter(Boolean),
    ...Array.from(bareMatches, (match) => String(match[1] || "").trim()).filter(Boolean),
  ]);
}

function isInlineCitationToken(token: string): boolean {
  const cleaned = cleanToken(token);
  if (!cleaned) return false;
  if (CITE_TOKEN_RE.test(cleaned)) return true;
  if (isPrivateInternalReference(cleaned)) return false;
  return /^[-A-Za-z0-9_:.\/]+$/.test(cleaned) && (/\d/.test(cleaned) || /[-_:]/.test(cleaned));
}

function isMeaningfulFallbackSourceToken(token: string, hasStructuredCandidates: boolean): boolean {
  const cleaned = cleanToken(token);
  if (!cleaned || isPrivateInternalReference(cleaned)) return false;
  if (/^\d{4}$/.test(cleaned)) return false;
  if (/^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?$/i.test(cleaned)) {
    return false;
  }
  if (isInlineCitationToken(cleaned)) return true;
  if (!hasStructuredCandidates) return cleaned.length >= 3;
  return cleaned.length >= 12 && cleaned.split(/\s+/).length >= 2;
}

function sourceFingerprint(value?: string): string {
  if (!value) return "";
  return value
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/[^a-z0-9]+/g, "");
}

function canonicalizeCandidateRef(candidate: CitationLink): string {
  const preferred = [
    candidate.label,
    candidate.key,
    candidate.url,
    candidate.openUrl,
  ].map((value) => cleanToken(String(value || ""))).filter(Boolean);

  for (const value of preferred) {
    if (/^https?:\/\//i.test(value)) {
      return cleanUrl(value);
    }
    if (/^reg:/i.test(value)) {
      return value.replace(/^reg:([a-z]{2,4}):/i, (_, jurisdiction: string) => `reg:${normalizeRegistryJurisdiction(jurisdiction)}:`);
    }
    if (/^[a-z]+:/i.test(value)) {
      const tokenMatch = value.match(/^([A-Za-z]+):(.*)$/);
      if (tokenMatch) {
        return `${tokenMatch[1].toLowerCase()}:${tokenMatch[2]}`;
      }
      return value;
    }
    if (isInlineCitationToken(value)) {
      return value;
    }
  }

  return preferred[0] || "unknown";
}

function normalizeManualSourceLookupKey(value: string): string {
  const cleaned = cleanToken(value);
  if (!cleaned) return "";
  const known = resolveKnownSourceToken(cleaned);
  if (known?.key && !/^https?:\/\//i.test(known.key)) return known.key;
  if (/^reg:/i.test(cleaned)) {
    return cleaned.replace(/^reg:([a-z]{2,4}):/i, (_, jurisdiction: string) => `reg:${normalizeRegistryJurisdiction(jurisdiction)}:`);
  }
  const prefixMatch = cleaned.match(/^([A-Za-z]+):(.*)$/);
  if (prefixMatch) {
    return `${prefixMatch[1].toLowerCase()}:${prefixMatch[2]}`;
  }
  return cleaned;
}

function mergeManualSourceRecord(canonicalRef: string, rawToken: string): ManualSourceRecord | null {
  const lookupKeys = uniqueInOrder([
    canonicalRef,
    rawToken,
    normalizeManualSourceLookupKey(canonicalRef),
    normalizeManualSourceLookupKey(rawToken),
  ]).filter(Boolean);

  for (const key of lookupKeys) {
    const manual = manualSourceRecords[key];
    if (manual) return manual;
  }
  return null;
}

function availabilityStatusFromKind(kind: SourceKind): SourceAvailabilityStatus {
  if (kind === "external") return "public_artifact";
  if (kind === "hosted_copy") return "hosted_copy";
  if (kind === "private_internal") return "restricted";
  return "metadata_only";
}

function isMetadataComplete(
  record: Pick<
    SourceRecord,
    | "title"
    | "canonicalRef"
    | "sourceType"
    | "publisherOrOrigin"
    | "publicationOrCaptureDate"
    | "pageOrLocator"
    | "excerptOrQuote"
    | "accessNote"
  >,
): boolean {
  const hasCoreMetadata = Boolean(
    String(record.title || "").trim()
      && String(record.canonicalRef || "").trim()
      && String(record.sourceType || "").trim()
      && String(record.accessNote || "").trim(),
  );
  const hasProvenance = Boolean(
    String(record.publisherOrOrigin || "").trim()
      || String(record.publicationOrCaptureDate || "").trim(),
  );
  const hasLocatorContext = Boolean(
    String(record.pageOrLocator || "").trim()
      || String(record.excerptOrQuote || "").trim(),
  );
  return Boolean(
    hasCoreMetadata
      && hasProvenance
      && hasLocatorContext,
  );
}

function buildSourceRecord(candidate: CitationLink, rawToken: string, hint?: Partial<SourceRecord>): SourceRecord {
  const canonicalRef = hint?.canonicalRef || canonicalizeCandidateRef(candidate);
  const manual = mergeManualSourceRecord(canonicalRef, cleanToken(rawToken));
  const label = candidate.label || cleanToken(rawToken) || hint?.label || canonicalRef;
  const sourceId = hint?.id || createSourceId(canonicalRef, label);
  const recordUrl = hint?.recordUrl || buildSourceRecordPath(sourceId);
  const externalUrl = manual?.external_url || hint?.externalUrl || (isExternalUrl(candidate.artifactUrl || candidate.openUrl || candidate.url) ? cleanUrl(candidate.artifactUrl || candidate.openUrl || candidate.url || "") : undefined);
  const hostedAssetUrl = manual?.hosted_asset_url || hint?.hostedAssetUrl || ((candidate.url || "").startsWith("/") ? candidate.url : undefined);
  const artifactUrl = manual?.artifact_url || hint?.artifactUrl || hostedAssetUrl || externalUrl;

  let kind: SourceKind = hint?.kind || manual?.kind || "record_only";
  if (!manual?.kind && !hint?.kind) {
    if (hostedAssetUrl) {
      kind = "hosted_copy";
    } else if (externalUrl) {
      kind = "external";
    } else if (isPrivateInternalReference(rawToken)) {
      kind = "private_internal";
    } else {
      kind = "record_only";
    }
  }

  const publishValid = manual?.publish_valid ?? hint?.publishValid ?? kind !== "private_internal";
  const availabilityStatus = manual?.availability_status || hint?.availabilityStatus || availabilityStatusFromKind(kind);
  const record: SourceRecord = {
    id: sourceId,
    label,
    title: manual?.title || hint?.title || prettifySourceTitle(label),
    kind,
    availabilityStatus,
    canonicalRef,
    artifactUrl,
    externalUrl,
    hostedAssetUrl,
    recordUrl,
    sourceType: manual?.source_type || hint?.sourceType || guessSourceType(rawToken || canonicalRef),
    publisherOrOrigin: manual?.publisher_or_origin || hint?.publisherOrOrigin,
    publicationOrCaptureDate: manual?.publication_or_capture_date || hint?.publicationOrCaptureDate,
    pageOrLocator: manual?.page_or_locator || hint?.pageOrLocator || parsePageLocator(rawToken || canonicalRef),
    excerptOrQuote: manual?.excerpt_or_quote || hint?.excerptOrQuote,
    accessNote: manual?.access_note
      || hint?.accessNote
      || (availabilityStatus === "metadata_only"
        ? "Held locally by Ithildin. No public artifact URL is currently available."
        : availabilityStatus === "restricted"
          ? "Internal reference. This item does not meet the public source-disclosure bar."
          : "Public source artifact available."),
    integrity: manual?.integrity || hint?.integrity,
    metadataComplete: true,
    publishValid,
  };
  record.metadataComplete = availabilityStatus !== "metadata_only" || isMetadataComplete(record);
  return record;
}

export function getSourcePrimaryUrl(record: SourceRecord): string {
  if (record.artifactUrl) return record.artifactUrl;
  return record.recordUrl;
}

export function resolveSourceRecord(rawToken: string): SourceRecord | null {
  const token = cleanToken(rawToken);
  if (!token || isPrivateInternalReference(token)) return null;

  const known = resolveKnownSourceToken(token);
  if (known) {
    return buildSourceRecord(known, token, {
      label: known.label,
    });
  }

  const overrideUrl = sourceUrlOverrides[token];
  if (overrideUrl) {
    return buildSourceRecord({ key: token, label: token, url: overrideUrl }, token);
  }

  return buildSourceRecord({ key: token, label: token }, token);
}

function getCitationKey(candidate: CitationLink, rawToken: string, record: SourceRecord): string {
  const known = resolveKnownSourceToken(rawToken);
  if (known?.key && !/^https?:\/\//i.test(known.key)) return known.key;
  if (candidate.key && !/^https?:\/\//i.test(candidate.key)) return candidate.key;
  return record.canonicalRef;
}

function sourceRecordToCitationLink(record: SourceRecord, labelOverride?: string, keyOverride?: string): CitationLink {
  const primaryUrl = getSourcePrimaryUrl(record);
  return {
    key: keyOverride || record.canonicalRef,
    label: labelOverride || record.label,
    url: primaryUrl,
    openUrl: record.artifactUrl,
    artifactUrl: record.artifactUrl,
    sourceRecordUrl: record.recordUrl,
    sourceId: record.id,
    sourceKind: record.kind,
    availabilityStatus: record.availabilityStatus,
    metadataComplete: record.metadataComplete,
    publishValid: record.publishValid,
  };
}

function createSourceCitationEntry(record: SourceRecord, labelOverride?: string): RawCitationResolution {
  const link = sourceRecordToCitationLink(record, labelOverride, record.canonicalRef);
  return {
    ...link,
    kind: "source",
    targetKind: record.availabilityStatus === "metadata_only" || record.availabilityStatus === "restricted"
      ? "source_record"
      : "artifact",
  };
}

function resolveFecToken(token: string): RawCitationResolution | null {
  const match = cleanToken(token).match(/FEC:([A-Za-z0-9_/-]+)/i);
  if (!match) return null;

  const rawBody = match[1];
  const body = rawBody.trim();
  const committeeMatch = body.match(/^(C\d{8})(?:([/-])(.*))?$/i);

  if (!committeeMatch) {
    return {
      key: `fec:query:${body.toLowerCase()}`,
      label: `FEC:${body}`,
      url: buildFecSearchUrl(body),
    };
  }

  const committeeId = committeeMatch[1].toUpperCase();
  const separator = committeeMatch[2] || "";
  const suffix = committeeMatch[3] || "";
  const normalizedSuffix = suffix.toLowerCase();

  if (normalizedSuffix === "schedule_a") {
    return {
      key: `fec:${committeeId}:schedule_a`,
      label: `FEC:${committeeId}/schedule_a`,
      url: buildFecReceiptsUrl(committeeId),
    };
  }

  if (separator === "-" && /^\d{4}$/.test(suffix)) {
    return {
      key: `fec:${committeeId}:${suffix}`,
      label: `FEC:${committeeId}-${suffix}`,
      url: buildFecReceiptsUrl(committeeId, suffix),
    };
  }

  return {
    key: suffix ? `fec:${committeeId}:${normalizedSuffix}` : `fec:${committeeId}`,
    label: `FEC:${committeeId}${separator}${suffix}`,
    url: buildFecCommitteeUrl(committeeId),
  };
}

function resolveFlSunBizToken(token: string): RawCitationResolution | null {
  const match = cleanToken(token).match(/FL[-_]?SunBiz[:\s]+([A-Za-z0-9]+)/i);
  if (!match) return null;

  const entityId = match[1].toUpperCase();
  return {
    key: `reg:FL:${entityId}`,
    label: `FL-SunBiz:${entityId}`,
    url: buildRegistryUrl("FL", entityId),
  };
}

function resolveNmSosToken(token: string): RawCitationResolution | null {
  const match = cleanToken(token).match(/NM[-_]?SoS[:\s]+([A-Za-z0-9]+)/i);
  if (!match) return null;

  const entityId = match[1];
  return {
    key: `reg:NM:${entityId}`,
    label: `NM-SoS:${entityId}`,
    url: buildRegistryUrl("NM", entityId),
  };
}

function resolveNySosToken(token: string): RawCitationResolution | null {
  const match = cleanToken(token).match(/NY[-_]?SoS[:\s]+([A-Za-z0-9]+)/i);
  if (!match) return null;

  const entityId = match[1];
  return {
    key: `reg:NY:${entityId}`,
    label: `NY-SoS:${entityId}`,
    url: buildRegistryUrl("NY", entityId),
  };
}

// ---------------------------------------------------------------------------
// Citation Type Registry
// ---------------------------------------------------------------------------

const CITATION_REGISTRY: CitationTypeDef[] = [
  {
    id: "finding",
    tokenPattern: "Finding\\s*#\\s*\\d+",
    healthTier: "label-only",
    resolve(token, options) {
      const findingMatch = token.match(/Finding\s*#\s*(\d+)/i);
      if (!findingMatch) return null;

      const findingId = findingMatch[1];
      const rawRefs = options.findingEvidenceMap?.[findingId] || [];
      const sources: CitationLink[] = [];
      const seen = new Set<string>();

      for (const ref of rawRefs) {
        for (const link of extractEvidenceLinks(ref, { findingEvidenceMap: options.findingEvidenceMap })) {
          if (seen.has(link.key)) continue;
          seen.add(link.key);
          sources.push(link);
        }
      }

      return {
        key: `finding:${findingId}`,
        label: `Finding #${findingId}`,
        kind: "finding",
        targetKind: "finding_popover",
        sources: sources.length ? sources : undefined,
      };
    },
    extract() { return []; },
    stripPattern: false,
  },
  {
    id: "efta",
    tokenPattern: "EFTA\\d{6,}",
    healthTier: "tier4",
    resolve(token) {
      const eftaMatches = token.match(/EFTA\d{6,}/gi);
      if (!eftaMatches || eftaMatches.length === 0) return null;
      const first = eftaMatches[0].toUpperCase();
      let label = first;
      if (eftaMatches.length > 1 && token.includes("-")) {
        label = `${first}-${eftaMatches[1].toUpperCase()}`;
      }
      return { key: `efta:${label}`, label, url: buildJmailUrl(first) };
    },
    extract(raw) {
      return (raw.match(/EFTA\d{6,}/gi) || []).map(id => {
        const normalized = id.toUpperCase();
        const url = buildJmailUrl(normalized);
        return { key: url, label: normalized, url };
      });
    },
  },
  {
    id: "house_oversight",
    tokenPattern: "HOUSE_OVERSIGHT_\\d+",
    healthTier: "tier4",
    resolve(token) {
      const houseMatches = token.match(/HOUSE_OVERSIGHT_\d+/gi);
      if (!houseMatches || houseMatches.length === 0) return null;
      const id = houseMatches[0].toUpperCase();
      return { key: `house:${id}`, label: id, url: buildJmailUrl(id) };
    },
    extract(raw) {
      return (raw.match(/HOUSE_OVERSIGHT_\d+/gi) || []).map(id => {
        const normalized = id.toUpperCase();
        const url = buildJmailUrl(normalized);
        return { key: url, label: normalized, url };
      });
    },
  },
  {
    id: "sec",
    tokenPattern: "SEC:\\d{10}-\\d{2}-\\d{6}",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/SEC:(\d{10}-\d{2}-\d{6})/i);
      if (!match) return null;
      const accession = match[1];
      return { key: `sec:${accession}`, label: `SEC ${accession}`, url: buildSecEdgarUrl(accession) };
    },
    extract(raw) {
      return (raw.match(/SEC:\d{10}-\d{2}-\d{6}/gi) || []).map(ref => {
        const acc = ref.replace(/SEC:/i, "");
        const url = buildSecEdgarUrl(acc);
        return { key: url, label: `SEC:${acc}`, url };
      });
    },
  },
  {
    id: "edgar",
    tokenPattern: "EDGAR:\\d{10}-\\d{2}-\\d{6}",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/EDGAR:(\d{10}-\d{2}-\d{6})/i);
      if (!match) return null;
      const accession = match[1];
      return { key: `sec:${accession}`, label: `EDGAR ${accession}`, url: buildSecEdgarUrl(accession) };
    },
    extract(raw) {
      return (raw.match(/EDGAR:\d{10}-\d{2}-\d{6}/gi) || []).map(ref => {
        const acc = ref.replace(/EDGAR:/i, "");
        const url = buildSecEdgarUrl(acc);
        return { key: url, label: `EDGAR:${acc}`, url };
      });
    },
  },
  {
    id: "irs990",
    tokenPattern: "990:\\d{9}",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/990:(\d{9})/i);
      if (!match) return null;
      const ein = match[1];
      return { key: `990:${ein}`, label: `990 EIN ${ein}`, url: build990Url(ein) };
    },
    extract(raw) {
      return (raw.match(/990:\d{9}/gi) || []).map(ref => {
        const ein = ref.replace(/990:/i, "");
        const url = build990Url(ein);
        return { key: url, label: `990:${ein}`, url };
      });
    },
  },
  {
    id: "acris",
    tokenPattern: "ACRIS:\\d{13,16}",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/ACRIS:(\d{13,16})/i);
      if (!match) return null;
      const docId = match[1];
      return { key: `acris:${docId}`, label: `ACRIS ${docId}`, url: buildAcrisUrl(docId) };
    },
    extract(raw) {
      return (raw.match(/ACRIS:\d{13,16}/gi) || []).map(ref => {
        const docId = ref.replace(/ACRIS:/i, "");
        const url = buildAcrisUrl(docId);
        return { key: url, label: `ACRIS:${docId}`, url };
      });
    },
  },
  {
    id: "cl",
    tokenPattern: "CL:\\d+",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/CL:(\d+)/i);
      if (!match) return null;
      const docketId = match[1];
      return { key: `cl:${docketId}`, label: `CourtListener ${docketId}`, url: buildCourtListenerUrl(docketId) };
    },
    extract(raw) {
      return (raw.match(/CL:\d+/gi) || []).map(ref => {
        const docketId = ref.replace(/CL:/i, "");
        const url = buildCourtListenerUrl(docketId);
        return { key: url, label: `CL:${docketId}`, url };
      });
    },
  },
  {
    id: "fec",
    tokenPattern: "FEC:[A-Za-z0-9_/-]+",
    healthTier: "tier1",
    resolve(token) {
      return resolveFecToken(token);
    },
    extract(raw) {
      return (raw.match(/FEC:[A-Za-z0-9_/-]+/gi) || []).flatMap(ref => {
        const resolved = resolveFecToken(ref);
        if (!resolved) return [];
        return [{ key: resolved.url || resolved.label, label: resolved.label, url: resolved.url }];
      });
    },
  },
  {
    id: "fara",
    tokenPattern: "FARA:\\d+",
    healthTier: "tier3",
    resolve(token) {
      const match = token.match(/FARA:(\d+)/i);
      if (!match) return null;
      const regNum = match[1];
      return { key: `fara:${regNum}`, label: `FARA #${regNum}`, url: buildFaraUrl(regNum) };
    },
    extract(raw) {
      return (raw.match(/FARA:\d+/gi) || []).map(ref => {
        const regNum = ref.replace(/FARA:/i, "");
        const url = buildFaraUrl(regNum);
        return { key: url, label: `FARA:${regNum}`, url };
      });
    },
  },
  {
    id: "usvi",
    tokenPattern: "USVI:[A-Za-z0-9]+",
    healthTier: "tier3",
    resolve(token) {
      const match = token.match(/USVI:([A-Za-z0-9]+)/i);
      if (!match) return null;
      const entityId = match[1];
      return { key: `usvi:${entityId}`, label: `USVI ${entityId}`, url: buildRegistryUrl("USVI", entityId) };
    },
    extract(raw) {
      return (raw.match(/USVI:[A-Za-z0-9]+/gi) || []).map(ref => {
        const entityId = ref.replace(/USVI:/i, "");
        const url = buildRegistryUrl("USVI", entityId);
        return { key: url, label: `USVI:${entityId}`, url };
      });
    },
  },
  {
    id: "fl_sunbiz",
    tokenPattern: "FL[-_]?SunBiz[:\\s]+[A-Za-z0-9]+",
    healthTier: "tier1",
    resolve(token) {
      return resolveFlSunBizToken(token);
    },
    extract(raw) {
      return (raw.match(/FL[-_]?SunBiz[:\s]+[A-Za-z0-9]+/gi) || []).flatMap(ref => {
        const resolved = resolveFlSunBizToken(ref);
        if (!resolved) return [];
        return [{ key: resolved.url || resolved.label, label: resolved.label, url: resolved.url }];
      });
    },
  },
  {
    id: "nm_sos",
    tokenPattern: "NM[-_]?SoS[:\\s]+[A-Za-z0-9]+",
    healthTier: "tier3",
    resolve(token) {
      return resolveNmSosToken(token);
    },
    extract(raw) {
      return (raw.match(/NM[-_]?SoS[:\s]+[A-Za-z0-9]+/gi) || []).flatMap(ref => {
        const resolved = resolveNmSosToken(ref);
        if (!resolved) return [];
        return [{ key: resolved.url || resolved.label, label: resolved.label, url: resolved.url }];
      });
    },
  },
  {
    id: "ny_sos",
    tokenPattern: "NY[-_]?SoS[:\\s]+[A-Za-z0-9]+",
    healthTier: "tier3",
    resolve(token) {
      return resolveNySosToken(token);
    },
    extract(raw) {
      return (raw.match(/NY[-_]?SoS[:\s]+[A-Za-z0-9]+/gi) || []).flatMap(ref => {
        const resolved = resolveNySosToken(ref);
        if (!resolved) return [];
        return [{ key: resolved.url || resolved.label, label: resolved.label, url: resolved.url }];
      });
    },
  },
  {
    id: "reg",
    tokenPattern: "REG:[A-Z]{2}:[A-Za-z0-9]+",
    healthTier: "tier1",
	    resolve(token) {
	      const match = token.match(/REG:([A-Z]{2}):([A-Za-z0-9]+)/i);
	      if (!match) return null;
	      const jurisdiction = normalizeRegistryJurisdiction(match[1]);
	      const entityId = match[2];
	      return {
	        key: `reg:${jurisdiction}:${entityId}`,
	        label: `${jurisdiction} ${entityId}`,
	        url: buildRegistryUrl(jurisdiction, entityId),
      };
    },
	    extract(raw) {
	      return (raw.match(/REG:[A-Z]{2}:[A-Za-z0-9]+/gi) || []).flatMap(ref => {
	        const regMatch = ref.match(/REG:([A-Z]{2}):([A-Za-z0-9]+)/i);
	        if (!regMatch) return [];
	        const jurisdiction = normalizeRegistryJurisdiction(regMatch[1]);
	        const entityId = regMatch[2];
	        const url = buildRegistryUrl(jurisdiction, entityId);
	        return [{ key: url, label: `REG:${jurisdiction}:${entityId}`, url }];
	      });
	    },
  },
  {
    id: "ds10",
    tokenPattern: "DS10(?::[A-Za-z0-9_-]+)?",
    healthTier: "label-only",
    resolve(token) {
      const match = token.match(/^DS10(?::[A-Za-z0-9_-]+)?$/i);
      if (!match) return null;
      const normalized = token.replace(/^ds10/i, "DS10");
      return {
        key: `dataset:${normalized.toLowerCase()}`,
        label: normalized,
        url: buildDs10Url(),
      };
    },
    extract(raw) {
      return (raw.match(/\bDS10(?::[A-Za-z0-9_-]+)?\b/gi) || []).map(ref => {
        const label = ref.replace(/^ds10/i, "DS10");
        const url = buildDs10Url();
        return { key: url, label, url };
      });
    },
    stripPattern: /\bDS10(?::[A-Za-z0-9_-]+)?\b/gi,
  },
  {
    id: "kpmg",
    tokenPattern: "KPMG:[A-Za-z0-9_-]+",
    healthTier: "label-only",
    resolve(token) {
      const match = token.match(/KPMG:([A-Za-z0-9_-]+)/i);
      if (!match) return null;
      const subject = match[1];
      return {
        key: `kpmg:${subject.toLowerCase()}`,
        label: `KPMG: ${subject}`,
      };
    },
    extract(raw) {
      return (raw.match(/KPMG:[A-Za-z0-9_-]+/gi) || []).flatMap(ref => {
        const m = ref.match(/KPMG:([A-Za-z0-9_-]+)/i);
        if (!m) return [];
        return [{ key: `KPMG:${m[1]}`, label: `KPMG:${m[1]}` }];
      });
    },
  },
  {
    id: "lda",
    tokenPattern: "LDA:[A-Za-z0-9_ -]+",
    healthTier: "tier2",
    resolve(token) {
      const match = token.match(/LDA:([A-Za-z0-9_ -]+)/i);
      if (!match) return null;
      const registrant = match[1].trim();
      return {
        key: `lda:${registrant.toLowerCase()}`,
        label: `LDA: ${registrant}`,
        url: buildLdaUrl(registrant),
      };
    },
    extract(raw) {
      return (raw.match(/LDA:[A-Za-z0-9_ -]+/gi) || []).flatMap(ref => {
        const m = ref.match(/LDA:([A-Za-z0-9_ -]+)/i);
        if (!m) return [];
        const registrant = m[1].trim();
        const url = buildLdaUrl(registrant);
        return [{ key: url, label: `LDA:${registrant}`, url }];
      });
    },
  },
  {
    id: "opensanctions",
    tokenPattern: "OpenSanctions:[A-Za-z0-9]+",
    healthTier: "tier1",
    resolve(token) {
      const match = token.match(/OpenSanctions:([A-Za-z0-9]+)/i);
      if (!match) return null;
      const entityId = match[1];
      return {
        key: `opensanctions:${entityId}`,
        label: `OpenSanctions ${entityId}`,
        url: buildOpenSanctionsUrl(entityId),
      };
    },
    extract(raw) {
      return (raw.match(/OpenSanctions:[A-Za-z0-9]+/gi) || []).flatMap(ref => {
        const m = ref.match(/OpenSanctions:([A-Za-z0-9]+)/i);
        if (!m) return [];
        const url = buildOpenSanctionsUrl(m[1]);
        return [{ key: url, label: `OpenSanctions:${m[1]}`, url }];
      });
    },
  },
  {
    id: "documentcloud",
    tokenPattern: "DOCUMENTCLOUD:\\d+",
    healthTier: "tier2",
    resolve(token) {
      const match = token.match(/DOCUMENTCLOUD:(\d+)/i);
      if (!match) return null;
      const docId = match[1];
      return {
        key: `documentcloud:${docId}`,
        label: `DocumentCloud ${docId}`,
        url: buildDocumentCloudUrl(docId),
      };
    },
    extract(raw) {
      return (raw.match(/DOCUMENTCLOUD:\d+/gi) || []).map(ref => {
        const docId = ref.replace(/DOCUMENTCLOUD:/i, "");
        const url = buildDocumentCloudUrl(docId);
        return { key: url, label: `DOCUMENTCLOUD:${docId}`, url };
      });
    },
  },
  {
    id: "offshorealert",
    tokenPattern: "OffshoreAlert:[A-Za-z0-9_-]+",
    healthTier: "tier3",
    resolve(token) {
      const match = token.match(/OffshoreAlert:([A-Za-z0-9_-]+)/i);
      if (!match) return null;
      const slug = match[1];
      return {
        key: `offshorealert:${slug.toLowerCase()}`,
        label: `OffshoreAlert:${slug}`,
        url: buildOffshoreAlertUrl(slug),
      };
    },
    extract(raw) {
      return (raw.match(/OffshoreAlert:[A-Za-z0-9_-]+/gi) || []).flatMap(ref => {
        const m = ref.match(/OffshoreAlert:([A-Za-z0-9_-]+)/i);
        if (!m) return [];
        const url = buildOffshoreAlertUrl(m[1]);
        return [{ key: url, label: `OffshoreAlert:${m[1]}`, url }];
      });
    },
  },
  {
    id: "muckrock",
    tokenPattern: "MUCKROCK:\\d+(?:\\/[A-Za-z0-9_.-]+)?",
    healthTier: "tier2",
    resolve(token) {
      const match = token.match(/MUCKROCK:(\d+)(?:\/([A-Za-z0-9_.-]+))?/i);
      if (!match) return null;
      const requestId = match[1];
      const fileName = match[2];
      const label = fileName ? `MuckRock ${requestId}/${fileName}` : `MuckRock ${requestId}`;
      return {
        key: `muckrock:${requestId}`,
        label,
        url: buildMuckRockUrl(requestId),
      };
    },
    extract(raw) {
      return (raw.match(/MUCKROCK:\d+(?:\/[A-Za-z0-9_.-]+)?/gi) || []).flatMap(ref => {
        const m = ref.match(/MUCKROCK:(\d+)(?:\/([A-Za-z0-9_.-]+))?/i);
        if (!m) return [];
        const url = buildMuckRockUrl(m[1]);
        const label = m[2] ? `MUCKROCK:${m[1]}/${m[2]}` : `MUCKROCK:${m[1]}`;
        return [{ key: url, label, url }];
      });
    },
  },
  {
    id: "littlesis",
    tokenPattern: "LittleSis[_:]?\\d+",
    healthTier: "tier2",
    resolve(token) {
      const match = token.match(/LittleSis[_:]?(\d+)/i);
      if (!match) return null;
      const entityId = match[1];
      return {
        key: `littlesis:${entityId}`,
        label: `LittleSis ${entityId}`,
        url: buildLittleSisUrl(entityId),
      };
    },
    extract(raw) {
      return (raw.match(/LittleSis[_:]\d+/gi) || []).flatMap(ref => {
        const m = ref.match(/LittleSis[_:](\d+)/i);
        if (!m) return [];
        const url = buildLittleSisUrl(m[1]);
        return [{ key: url, label: `LittleSis:${m[1]}`, url }];
      });
    },
  },
  {
    id: "icij",
    tokenPattern: "ICIJ(?:-PP|-node)?[:\\s]\\d+",
    healthTier: "tier2",
    resolve(token) {
      const match = token.match(/ICIJ(?:-PP|-node)?[:\s](\d+)/i);
      if (!match) return null;
      const nodeId = match[1];
      return {
        key: `icij:${nodeId}`,
        label: `ICIJ ${nodeId}`,
        url: buildIcijUrl(nodeId),
      };
    },
    extract(raw) {
      return (raw.match(/ICIJ(?:-PP|-node)?[:\s]\d+/gi) || []).flatMap(ref => {
        const m = ref.match(/ICIJ(?:-PP|-node)?[:\s](\d+)/i);
        if (!m) return [];
        const url = buildIcijUrl(m[1]);
        return [{ key: url, label: ref.trim(), url }];
      });
    },
  },
];

// ---------------------------------------------------------------------------
// Derived patterns from registry
// ---------------------------------------------------------------------------

const CITE_TOKEN_PATTERNS = [
  ...CITATION_REGISTRY.map(t => t.tokenPattern),
  "https?:\\/\\/[^\\s,;)]+",
];
const CITE_TOKEN_PATTERN = CITE_TOKEN_PATTERNS.join("|");
const CITE_TOKEN_RE = new RegExp(`(?:${CITE_TOKEN_PATTERN})`, "i");

function getCiteTokenGlobalRe(): RegExp {
  return new RegExp(CITE_TOKEN_PATTERN, "gi");
}

function isLikelyMarkdownLinkTarget(text: string, openParenIndex: number): boolean {
  let cursor = openParenIndex - 1;
  while (cursor >= 0 && /\s/.test(text[cursor])) {
    cursor -= 1;
  }
  return cursor >= 0 && text[cursor] === "]";
}

export function createCitationState(): CitationState {
  return {
    entries: [],
    index: new Map<string, number>(),
  };
}

export function splitCitationGroup(group: string): string[] {
  const normalized = cleanToken(group);
  if (!normalized) return [];

  const tokenRe = getCiteTokenGlobalRe();
  const matches = normalized.match(tokenRe);
  if (matches && matches.length > 0) {
    const remainder = normalized
      .replace(tokenRe, "")
      .replace(/[\s,;|/]+/g, "")
      .replace(/and/gi, "")
      .trim();

    if (!remainder) {
      return uniqueInOrder(matches.map(cleanToken).filter(Boolean));
    }
  }

  return uniqueInOrder(
    normalized
      .split(";")
      .flatMap(part => part.split(","))
      .flatMap(part => part.split(/\s+and\s+/i))
      .map(cleanToken)
      .filter(Boolean),
  );
}

function resolveKnownSourceToken(token: string): RawCitationResolution | null {
  const trimmed = cleanToken(token);
  if (!trimmed || /^Finding\s*#\s*\d+$/i.test(trimmed)) return null;

  const urlMatch = trimmed.match(/https?:\/\/[^\s\]]+/i);
  if (urlMatch && urlMatch[0]) {
    const url = cleanUrl(urlMatch[0]);
    return { key: url, label: url, url };
  }

  for (const type of CITATION_REGISTRY) {
    if (type.id === "finding") continue;
    const result = type.resolve(trimmed, {});
    if (result) return result;
  }

  return null;
}

function getFindingEvidenceRefs(
  findingId: string,
  options: EvidenceExtractionOptions,
): string[] {
  const map = options.findingEvidenceMap || loadFindingEvidenceMap();
  return Array.isArray(map?.[findingId]) ? map[findingId] : [];
}

// ---------------------------------------------------------------------------
// extractEvidenceLinks — registry-driven
// ---------------------------------------------------------------------------

export function extractEvidenceLinks(raw: string, options: EvidenceExtractionOptions = {}): CitationLink[] {
  const links: CitationLink[] = [];
  const candidates: CitationLink[] = [];
  const seen = new Set<string>();
  const add = (link: CitationLink) => {
    if (!link.key || seen.has(link.key)) return;
    seen.add(link.key);
    links.push(link);
  };

  if (!raw) return links;

  const addSourceCandidate = (candidate: CitationLink, rawToken: string): void => {
    const record = buildSourceRecord(candidate, rawToken);
    if (!record.publishValid || record.kind === "private_internal") return;
    add(sourceRecordToCitationLink(record, candidate.label));
  };

  const urls = raw.match(URL_RE) || [];
  for (const url of urls) {
    const cleaned = cleanUrl(url);
    candidates.push({ key: cleaned, label: cleaned, url: cleaned });
  }

  for (const type of CITATION_REGISTRY) {
    if (type.id === "finding") continue;
    for (const link of type.extract(raw)) candidates.push(link);
  }

  for (const link of candidates) {
    addSourceCandidate(link, link.label || link.key || raw);
  }

  let remainder = raw.replace(URL_RE, "");
  for (const type of CITATION_REGISTRY) {
    if (type.stripPattern === false) continue;
    const strip = type.stripPattern ?? new RegExp(type.tokenPattern, "gi");
    remainder = remainder.replace(strip, "");
  }
  remainder = remainder.replace(/[;:,]+/g, " ").replace(/\s+/g, " ").trim();

  if (remainder) {
    const cleanedRemainder = cleanToken(remainder);
    if (isMeaningfulFallbackSourceToken(cleanedRemainder, candidates.length > 0)) {
      const known = resolveKnownSourceToken(cleanedRemainder);
      if (known) {
        addSourceCandidate(known, cleanedRemainder);
      } else {
        addSourceCandidate({ key: cleanedRemainder, label: cleanedRemainder }, cleanedRemainder);
      }
    }
  }

  if (links.length === 0) {
    const fallback = cleanToken(raw);
    if (isMeaningfulFallbackSourceToken(fallback, false)) {
      const known = resolveKnownSourceToken(fallback);
      if (known) {
        addSourceCandidate(known, fallback);
      } else {
        addSourceCandidate({ key: fallback, label: fallback }, fallback);
      }
    }
  }

  const nextSeen = options.seenFindingIds || new Set<string>();
  for (const findingId of extractReferencedFindingIds(raw)) {
    if (nextSeen.has(findingId)) continue;
    nextSeen.add(findingId);
    for (const ref of getFindingEvidenceRefs(findingId, options)) {
      for (const link of extractEvidenceLinks(ref, { ...options, seenFindingIds: nextSeen })) {
        add(link);
      }
    }
  }

  return links;
}

export function extractEvidenceSourceRecords(raw: string, options: EvidenceExtractionOptions = {}): SourceRecord[] {
  const records: SourceRecord[] = [];
  const seen = new Set<string>();
  const add = (record: SourceRecord) => {
    if (seen.has(record.id) || !record.publishValid || record.kind === "private_internal") return;
    seen.add(record.id);
    records.push(record);
  };

  if (!raw) return records;

  const candidates: Array<{ candidate: CitationLink; rawToken: string }> = [];
  const urls = raw.match(URL_RE) || [];
  for (const url of urls) {
    const cleaned = cleanUrl(url);
    candidates.push({ candidate: { key: cleaned, label: cleaned, url: cleaned }, rawToken: cleaned });
  }

  for (const type of CITATION_REGISTRY) {
    if (type.id === "finding") continue;
    for (const link of type.extract(raw)) {
      candidates.push({ candidate: link, rawToken: link.label || link.key || raw });
    }
  }

  for (const { candidate, rawToken } of candidates) {
    add(buildSourceRecord(candidate, rawToken));
  }

  let remainder = raw.replace(URL_RE, "");
  for (const type of CITATION_REGISTRY) {
    if (type.stripPattern === false) continue;
    const strip = type.stripPattern ?? new RegExp(type.tokenPattern, "gi");
    remainder = remainder.replace(strip, "");
  }
  remainder = remainder.replace(/[;:,]+/g, " ").replace(/\s+/g, " ").trim();

  if (remainder) {
    const cleanedRemainder = cleanToken(remainder);
    if (isMeaningfulFallbackSourceToken(cleanedRemainder, candidates.length > 0)) {
      const known = resolveKnownSourceToken(cleanedRemainder);
      add(buildSourceRecord(known || { key: cleanedRemainder, label: cleanedRemainder }, cleanedRemainder));
    }
  }

  if (records.length === 0) {
    const fallback = cleanToken(raw);
    if (isMeaningfulFallbackSourceToken(fallback, false)) {
      const known = resolveKnownSourceToken(fallback);
      add(buildSourceRecord(known || { key: fallback, label: fallback }, fallback));
    }
  }

  const nextSeen = options.seenFindingIds || new Set<string>();
  for (const findingId of extractReferencedFindingIds(raw)) {
    if (nextSeen.has(findingId)) continue;
    nextSeen.add(findingId);
    for (const ref of getFindingEvidenceRefs(findingId, options)) {
      for (const record of extractEvidenceSourceRecords(ref, { ...options, seenFindingIds: nextSeen })) {
        add(record);
      }
    }
  }

  return records;
}

// ---------------------------------------------------------------------------
// resolveCitationToken — registry-driven
// ---------------------------------------------------------------------------

function resolveCitationToken(token: string, options: CitationOptions): RawCitationResolution {
  const trimmed = cleanToken(token);
  if (!trimmed) {
    return { key: "unknown", label: "Unknown", kind: "source", targetKind: "source_record" };
  }

  for (const type of CITATION_REGISTRY) {
    const result = type.resolve(trimmed, options);
    if (!result) continue;
    if (type.id === "finding" || result.kind === "finding") {
      return {
        ...result,
        kind: "finding",
        targetKind: "finding_popover",
      };
    }

    const record = buildSourceRecord(result, trimmed);
    return {
      ...sourceRecordToCitationLink(record, result.label, result.key),
      kind: "source",
      targetKind: record.kind === "record_only" ? "source_record" : "artifact",
    };
  }

  const record = resolveSourceRecord(trimmed);
  if (record) {
    return {
      ...sourceRecordToCitationLink(record, trimmed, record.canonicalRef),
      kind: "source",
      targetKind: record.kind === "record_only" ? "source_record" : "artifact",
    };
  }

  return {
    key: trimmed,
    label: trimmed,
    kind: "source",
    targetKind: "source_record",
  };
}

function citationEntryToSuperscript(entry: CitationEntry): string {
  const href = entry.targetKind === "finding_popover" ? `#fn-${entry.number}` : (entry.url || `#fn-${entry.number}`);
  const external = entry.targetKind !== "finding_popover" && isExternalUrl(entry.url);
  const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : "";
  return `<sup class="citation"><a href="${escapeHtml(href)}"${attrs} data-citation-number="${entry.number}" data-citation-key="${escapeHtml(entry.key)}" aria-label="Source ${entry.number}: ${escapeHtml(entry.label)}">${entry.number}</a></sup>`;
}

function registerCitationEntry(citationState: CitationState, resolved: RawCitationResolution): CitationEntry {
  const key = resolved.key;
  let number = citationState.index.get(key);
  const normalizedResolved: CitationEntry = {
    ...resolved,
    number: 0,
    kind: resolved.kind || (key.startsWith("finding:") ? "finding" : "source"),
    targetKind: resolved.targetKind || (key.startsWith("finding:") ? "finding_popover" : "artifact"),
  };

  if (!number) {
    number = citationState.entries.length + 1;
    citationState.entries.push({ ...normalizedResolved, number });
    citationState.index.set(key, number);
  }

  return citationState.entries[number - 1];
}

function isLooseSourceLabelToken(value: string): boolean {
  const cleaned = cleanToken(value);
  if (!cleaned || isPrivateInternalReference(cleaned)) return false;
  if (/^claim type:/i.test(cleaned) || /^confidence:/i.test(cleaned)) return false;
  if (/^EFTA confirmed direct quote$/i.test(cleaned)) return false;
  if (/^(?:synthesis|graph analysis|analysis-run)\s*#?\s*\d+/i.test(cleaned)) return false;
  if (/^secondHop data$/i.test(cleaned)) return false;
  if (/^\d{4}$/.test(cleaned)) return false;
  if (/^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?$/i.test(cleaned)) return false;
  if (cleaned.length > 120) return false;
  if (!/[0-9:./_-]/.test(cleaned) && cleaned === cleaned.toLowerCase() && !LOOSE_SOURCE_LABEL_ALLOWLIST.has(cleaned)) {
    return false;
  }
  return cleaned.split(/\s+/).length <= 10;
}

function renderLooseSourceCitation(part: string, citationState: CitationState): string | null {
  const token = cleanToken(part);
  if (!isLooseSourceLabelToken(token)) return null;
  const record = resolveSourceRecord(token);
  if (!record || !record.publishValid || record.kind === "private_internal") return null;

  const entry = registerCitationEntry(citationState, {
    ...sourceRecordToCitationLink(record, token, record.canonicalRef),
    kind: "source",
    targetKind: record.kind === "record_only" ? "source_record" : "artifact",
  });
  return citationEntryToSuperscript(entry);
}

// ---------------------------------------------------------------------------
// Health tier lookup (for optional use by check-citation-health.mjs)
// ---------------------------------------------------------------------------

export function getCitationHealthTier(citationKey: string): HealthTier | "skip" {
  const prefix = citationKey.split(":")[0];
  const type = CITATION_REGISTRY.find(t => t.id === prefix);
  return type?.healthTier ?? "skip";
}

/**
 * Normalize common citation patterns to canonical bracket token format.
 * Example: (Finding #2108, EFTA01296686) -> [Finding #2108, EFTA01296686]
 */
function normalizeCitationPatterns(text: string): string {
  return text.replace(/\(([^()]+)\)/g, (full, inner, offset, source) => {
    const index = typeof offset === "number" ? offset : 0;
    const content = typeof source === "string" ? source : "";

    // Keep markdown links intact: [label](https://...)
    if (isLikelyMarkdownLinkTarget(content, index)) {
      return full;
    }

    const candidate = cleanToken(String(inner));
    if (!candidate || !CITE_TOKEN_RE.test(candidate)) {
      return full;
    }

    const tokenRe = getCiteTokenGlobalRe();
    const matches = candidate.match(tokenRe);
    if (!matches || matches.length === 0) {
      return full;
    }

    const remainder = candidate
      .replace(tokenRe, "")
      .replace(/[\s,;|/]+/g, "")
      .replace(/and/gi, "")
      .trim();

    if (remainder) {
      return full;
    }

    const renderedTokens = matches
      .map(token => cleanToken(token))
      .filter(Boolean);
    if (!renderedTokens.length) {
      return full;
    }

    return `[${renderedTokens.join(", ")}]`;
  });
}

function renderCitationSuperscripts(inner: string, options: CitationOptions, citationState: CitationState, fallback: string): string {
  const tokens = splitCitationGroup(inner);
  if (!tokens.length || !tokens.every(isInlineCitationToken)) {
    return fallback;
  }

  const resolvedTokens = tokens.map(token => resolveCitationToken(token, options));

  // If a group cites a finding and one of the finding's own evidence refs,
  // suppress the duplicate standalone source citation in that group.
  const findingSourceFingerprints = new Set<string>();
  for (const resolved of resolvedTokens) {
    if (!resolved.key.startsWith("finding:") || !resolved.sources) continue;
    for (const source of resolved.sources) {
      const byLabel = sourceFingerprint(source.label);
      const byUrl = sourceFingerprint(source.url);
      if (byLabel) findingSourceFingerprints.add(byLabel);
      if (byUrl) findingSourceFingerprints.add(byUrl);
    }
  }

  const filteredResolved: RawCitationResolution[] = [];
  const seenResolvedKeys = new Set<string>();

  for (const resolved of resolvedTokens) {
    if (!resolved.key.startsWith("finding:") && findingSourceFingerprints.size > 0) {
      const byLabel = sourceFingerprint(resolved.label);
      const byUrl = sourceFingerprint(resolved.url);
      if ((byLabel && findingSourceFingerprints.has(byLabel)) || (byUrl && findingSourceFingerprints.has(byUrl))) {
        continue;
      }
    }

    if (seenResolvedKeys.has(resolved.key)) continue;
    seenResolvedKeys.add(resolved.key);
    filteredResolved.push(resolved);
  }

  if (!filteredResolved.length) {
    return fallback;
  }

  const rendered = filteredResolved
    .map(resolved => citationEntryToSuperscript(registerCitationEntry(citationState, resolved)));

  return rendered.join("");
}

function renderInternalReference(ref: InternalReference): string {
  const label = `${INTERNAL_REFERENCE_LABELS[ref.kind]} #${ref.id}`;
  if (ref.kind === "finding") {
    return `<a href="#finding-${escapeHtml(ref.id)}" class="inline-reference inline-reference--finding" data-citation-key="finding:${escapeHtml(ref.id)}">${escapeHtml(label)}</a>`;
  }
  return `<em class="inline-reference inline-reference--${escapeHtml(ref.kind)}">${escapeHtml(label)}</em>`;
}

function renderInternalReferenceGroup(inner: string, options: CitationOptions, citationState: CitationState): string | null {
  const trimmed = cleanToken(inner);
  if (!trimmed) return null;

  if (/^<a\b[\s\S]*<\/a>$/i.test(trimmed)) {
    return `<span class="inline-reference-group">${trimmed}</span>`;
  }

  const parts = trimmed
    .split(";")
    .flatMap(part => part.split(","))
    .flatMap(part => part.split(/\s+and\s+/i))
    .map(cleanToken)
    .filter(Boolean);

  if (!parts.length) return null;

  const renderedParts: string[] = [];
  let currentKind: InternalReferenceKind | null = null;
  let sawStructuredReference = false;

  for (const part of parts) {
    if (/^<a\b[\s\S]*<\/a>$/i.test(part)) {
      renderedParts.push(part);
      currentKind = null;
      sawStructuredReference = true;
      continue;
    }

    if (/^viz_data\b/i.test(part)) {
      renderedParts.push(escapeHtml(part));
      currentKind = null;
      sawStructuredReference = true;
      continue;
    }

    const typedMatch = part.match(/^([A-Za-z]+)\s*#\s*(\d+)$/);
    if (typedMatch) {
      const kind = normalizeInternalReferenceKindToken(typedMatch[1]);
      if (!kind) return null;
      currentKind = kind;
      sawStructuredReference = true;

      if (kind === "finding") {
        const renderedCitation = renderCitationSuperscripts(`Finding #${typedMatch[2]}`, options, citationState, part);
        if (renderedCitation === part) return null;
        renderedParts.push(renderedCitation);
      } else {
        renderedParts.push(renderInternalReference({ kind, id: typedMatch[2] }));
      }
      continue;
    }

    const shorthandMatch = part.match(/^#\s*(\d+)$/);
    if (shorthandMatch && currentKind) {
      sawStructuredReference = true;
      if (currentKind === "finding") {
        const renderedCitation = renderCitationSuperscripts(`Finding #${shorthandMatch[1]}`, options, citationState, part);
        if (renderedCitation === part) return null;
        renderedParts.push(renderedCitation);
      } else {
        renderedParts.push(renderInternalReference({ kind: currentKind, id: shorthandMatch[1] }));
      }
      continue;
    }

    const renderedCitation = renderCitationSuperscripts(part, options, citationState, part);
    if (renderedCitation !== part) {
      renderedParts.push(renderedCitation);
      currentKind = null;
      sawStructuredReference = true;
      continue;
    }

    const looseSourceCitation = renderLooseSourceCitation(part, citationState);
    if (looseSourceCitation) {
      renderedParts.push(looseSourceCitation);
      currentKind = null;
      sawStructuredReference = true;
      continue;
    }

    return null;
  }

  if (!sawStructuredReference || !renderedParts.length) return null;
  return `<span class="inline-reference-group">${renderedParts.join(", ")}</span>`;
}

function applyCitationReplacementsToText(text: string, options: CitationOptions, citationState: CitationState): string {
  const normalized = normalizeCitationPatterns(text);
  return normalized.replace(/\[([^\]]+)\]/g, (match, inner) => {
    const citations = renderCitationSuperscripts(inner, options, citationState, match);
    if (citations !== match) return citations;

    const internalRefs = renderInternalReferenceGroup(inner, options, citationState);
    if (internalRefs) return internalRefs;

    return match;
  });
}

function splitInlineCitationNodes(value: string): Array<{ type: "text" | "html"; value: string }> {
  const chunkRe = /(<sup class="citation">[\s\S]*?<\/sup>|<span class="inline-reference-group">[\s\S]*?<\/span>)/g;
  const chunks = value.split(chunkRe);
  const out: Array<{ type: "text" | "html"; value: string }> = [];
  for (const chunk of chunks) {
    if (!chunk) continue;
    if (/^<sup class="citation">[\s\S]*<\/sup>$/.test(chunk) || /^<span class="inline-reference-group">[\s\S]*<\/span>$/.test(chunk)) {
      out.push({ type: "html", value: chunk });
    } else {
      out.push({ type: "text", value: chunk });
    }
  }
  return out;
}

function transformCitationTextNodes(node: any, options: CitationOptions, citationState: CitationState, excluded = false): void {
  if (!node || typeof node !== "object") return;
  const type = typeof node.type === "string" ? node.type : "";
  const isExcluded = excluded || type === "link" || type === "linkReference" || type === "inlineCode" || type === "code";

  if (!Array.isArray(node.children)) return;

  for (let index = 0; index < node.children.length; index += 1) {
    const child = node.children[index];
    if (!child || typeof child !== "object") continue;

    if (!isExcluded && child.type === "text" && typeof child.value === "string") {
      const replaced = applyCitationReplacementsToText(child.value, options, citationState);
      if (replaced !== child.value) {
        const replacementNodes = splitInlineCitationNodes(replaced);
        node.children.splice(index, 1, ...replacementNodes);
        index += replacementNodes.length - 1;
        continue;
      }
    }

    if (!isExcluded && child.type === "html" && typeof child.value === "string") {
      const replaced = applyCitationReplacementsToText(child.value, options, citationState);
      if (replaced !== child.value) {
        child.value = replaced;
      }
      continue;
    }

    transformCitationTextNodes(child, options, citationState, isExcluded);
  }
}

export function applyCitations(markdown: string, options: CitationOptions = {}, state?: CitationState) {
  const citationState = state ?? createCitationState();

  try {
    const parser = unified().use(remarkParse).use(remarkGfm);
    const tree = parser.parse(markdown) as any;
    transformCitationTextNodes(tree, options, citationState, false);

    const output = unified()
      .use(remarkStringify as any, { allowDangerousHtml: true } as any)
      .stringify(tree);

    return { markdown: output, entries: citationState.entries };
  } catch {
    // Fall back to direct text replacement if AST parsing fails.
    const replaced = applyCitationReplacementsToText(markdown, options, citationState);
    return { markdown: replaced, entries: citationState.entries };
  }
}

export function renderFootnotes(entries: CitationEntry[]): string {
  if (!entries.length) return "";

  const renderActionLink = (href: string, text: string, dataAttr: string): string => {
    const attrs = isExternalUrl(href) ? ' target="_blank" rel="noopener noreferrer"' : "";
    return `<a href="${escapeHtml(href)}"${attrs} class="citation-action" ${dataAttr}>${escapeHtml(text)}</a>`;
  };

  const renderPrimaryLink = (entry: CitationEntry): string => {
    const label = escapeHtml(entry.label);
    const number = entry.number;
    const href = entry.targetKind === "finding_popover" ? `#fn-${number}` : entry.url;
    if (!href) {
      return `<span data-citation-number="${number}" data-citation-key="${escapeHtml(entry.key)}">${label}</span>`;
    }
    const attrs = entry.targetKind !== "finding_popover" && isExternalUrl(href) ? ' target="_blank" rel="noopener noreferrer"' : "";
    return `<a href="${escapeHtml(href)}"${attrs} data-citation-number="${number}" data-citation-key="${escapeHtml(entry.key)}">${label}</a>`;
  };

  const renderSourceLink = (source: CitationLink, parentKey: string): string => {
    const label = escapeHtml(source.label);
    const primaryHref = source.url;
    const sourceAttrs = primaryHref && isExternalUrl(primaryHref) ? ' target="_blank" rel="noopener noreferrer"' : "";
    const primary = primaryHref
      ? `<a href="${escapeHtml(primaryHref)}"${sourceAttrs} data-source-key="${escapeHtml(source.key)}" data-parent-citation-key="${escapeHtml(parentKey)}">${label}</a>`
      : `<span data-source-key="${escapeHtml(source.key)}" data-parent-citation-key="${escapeHtml(parentKey)}">${label}</span>`;

    const actions: string[] = [];
    if (source.openUrl) {
      actions.push(renderActionLink(source.openUrl, "Open artifact", `data-source-open="${escapeHtml(source.key)}"`));
    }
    if (source.sourceRecordUrl) {
      actions.push(renderActionLink(source.sourceRecordUrl, "View source record", `data-source-record="${escapeHtml(source.key)}"`));
    }

    if (!actions.length) return primary;
    return `${primary}<span class="citation-actions">${actions.join("")}</span>`;
  };

  const items = entries.map(entry => {
    const number = entry.number;
    const link = renderPrimaryLink(entry);

    const actions: string[] = [];
    if (entry.kind === "source" && entry.openUrl) {
      actions.push(renderActionLink(entry.openUrl, "Open artifact", `data-citation-open="${escapeHtml(entry.key)}"`));
    }
    if (entry.kind === "source" && entry.sourceRecordUrl) {
      actions.push(renderActionLink(entry.sourceRecordUrl, "View source record", `data-citation-record="${escapeHtml(entry.key)}"`));
    }
    const actionHtml = actions.length ? `<div class="citation-actions">${actions.join("")}</div>` : "";

    let sources = "";
    if (entry.sources && entry.sources.length) {
      const sourceLinks = entry.sources
        .map(source => renderSourceLink(source, entry.key))
        .join('<span class="citation-source-separator">, </span>');
      sources = `<div class="citation-sources">Sources: ${sourceLinks}</div>`;
    }

    return `<li id="fn-${number}"><span class="citation-index">${number}.</span><span class="citation-entry">${link}${actionHtml}${sources}</span></li>`;
  });

  return `
    <section class="citation-block">
      <div class="section-label">Sources</div>
      <ol class="citation-list">
        ${items.join("\n")}
      </ol>
    </section>
  `;
}
