import type { CitationEntry } from "./citations";

export type SupportNodeKind = "citation" | "source";

export type SupportSpan = {
  id: string;
  text: string;
  supported: boolean;
  citation_keys: string[];
  node_keys: string[];
};

export type SupportNode = {
  key: string;
  kind: SupportNodeKind;
  label: string;
  url?: string;
  citation_keys: string[];
  citation_numbers: number[];
  span_ids: string[];
};

export type SupportMap = {
  version: 1;
  spans: SupportSpan[];
  nodes: Record<string, SupportNode>;
  citation_to_nodes: Record<string, string[]>;
  orphan_citations: string[];
};

export type SupportMetrics = {
  total_sentences: number;
  supported_sentences: number;
  unsupported_sentences: number;
  supported_sentence_pct: number;
  orphan_citations_count: number;
  orphan_citations: string[];
  source_fanout: Record<string, number>;
};

export type AnnotateOptions = {
  spanIdPrefix?: string;
};

type SentenceRange = {
  start: number;
  end: number;
  text: string;
};

const BLOCK_RE = /<(p|li|blockquote|td|th)(\s[^>]*)?>([\s\S]*?)<\/\1>/gi;
const CITE_KEY_RE = /data-citation-key="([^"]+)"/gi;

function escapeAttr(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function decodeHtmlAttribute(value: string): string {
  return String(value)
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function uniqueInOrder(values: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

function mapPlainToHtml(inner: string): { plain: string; plainToHtml: number[] } {
  let inTag = false;
  const plainChars: string[] = [];
  const plainToHtml: number[] = [];

  for (let index = 0; index < inner.length; index += 1) {
    const char = inner[index];
    if (!inTag && char === "<") {
      inTag = true;
      continue;
    }
    if (inTag) {
      if (char === ">") {
        inTag = false;
      }
      continue;
    }
    plainChars.push(char);
    plainToHtml.push(index);
  }

  return { plain: plainChars.join(""), plainToHtml };
}

function segmentSentences(plain: string): SentenceRange[] {
  const out: SentenceRange[] = [];
  const clean = String(plain || "");
  if (!clean.trim()) return out;

  const SegmenterCtor = (Intl as unknown as { Segmenter?: new (locale: string, options: { granularity: "sentence" }) => { segment: (value: string) => Iterable<{ segment: string; index: number }> } }).Segmenter;
  if (SegmenterCtor) {
    const segmenter = new SegmenterCtor("en", { granularity: "sentence" });
    for (const item of segmenter.segment(clean)) {
      const segment = item.segment || "";
      const start = item.index || 0;
      const end = start + segment.length;
      out.push({ start, end, text: segment });
    }
    return out.filter((item) => item.text.trim().length > 0);
  }

  const fallback = /[^.!?]+[.!?]+(?:\s+|$)|[^.!?]+$/g;
  let match: RegExpExecArray | null = null;
  while ((match = fallback.exec(clean)) !== null) {
    out.push({
      start: match.index,
      end: match.index + match[0].length,
      text: match[0],
    });
  }
  return out.filter((item) => item.text.trim().length > 0);
}

function stripHtml(value: string): string {
  return String(value || "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function extractCitationKeys(sentenceHtml: string): string[] {
  const keys: string[] = [];
  for (const match of String(sentenceHtml || "").matchAll(CITE_KEY_RE)) {
    const raw = match[1] || "";
    const decoded = decodeHtmlAttribute(raw).trim();
    if (decoded) keys.push(decoded);
  }
  return uniqueInOrder(keys);
}

function shouldSkipBlock(attrs: string): boolean {
  const normalized = String(attrs || "");
  if (!normalized) return false;
  return /\bcitation-(?:block|list|entry|sources)\b/i.test(normalized);
}

function addNode(
  nodes: Record<string, SupportNode>,
  key: string,
  kind: SupportNodeKind,
  label: string,
  citationKey: string,
  citationNumber: number,
  url?: string,
): void {
  const existing = nodes[key];
  if (!existing) {
    nodes[key] = {
      key,
      kind,
      label,
      url,
      citation_keys: [citationKey],
      citation_numbers: [citationNumber],
      span_ids: [],
    };
    return;
  }
  if (!existing.citation_keys.includes(citationKey)) {
    existing.citation_keys.push(citationKey);
  }
  if (!existing.citation_numbers.includes(citationNumber)) {
    existing.citation_numbers.push(citationNumber);
  }
  if (!existing.url && url) {
    existing.url = url;
  }
}

function buildNodeMaps(entries: CitationEntry[]): {
  nodes: Record<string, SupportNode>;
  citationToNodes: Record<string, string[]>;
} {
  const nodes: Record<string, SupportNode> = {};
  const citationToNodes: Record<string, string[]> = {};

  for (const entry of entries || []) {
    const citationNodeKey = `citation:${entry.key}`;
    addNode(nodes, citationNodeKey, "citation", entry.label, entry.key, entry.number, entry.url);
    const nodeKeys = [citationNodeKey];

    for (const source of entry.sources || []) {
      const sourceNodeKey = `source:${source.key}`;
      addNode(nodes, sourceNodeKey, "source", source.label, entry.key, entry.number, source.url);
      nodeKeys.push(sourceNodeKey);
    }

    citationToNodes[entry.key] = uniqueInOrder([
      ...(citationToNodes[entry.key] || []),
      ...nodeKeys,
    ]);
  }

  return { nodes, citationToNodes };
}

export function computeSupportMetrics(supportMap: SupportMap): SupportMetrics {
  const totalSentences = supportMap.spans.length;
  const supportedSentences = supportMap.spans.filter((span) => span.supported).length;
  const unsupportedSentences = totalSentences - supportedSentences;
  const supportedSentencePct = totalSentences > 0
    ? Number(((supportedSentences / totalSentences) * 100).toFixed(1))
    : 0;

  const sourceFanout: Record<string, number> = {};
  for (const node of Object.values(supportMap.nodes)) {
    if (node.kind !== "source") continue;
    sourceFanout[node.key] = node.span_ids.length;
  }

  return {
    total_sentences: totalSentences,
    supported_sentences: supportedSentences,
    unsupported_sentences: unsupportedSentences,
    supported_sentence_pct: supportedSentencePct,
    orphan_citations_count: supportMap.orphan_citations.length,
    orphan_citations: [...supportMap.orphan_citations],
    source_fanout: sourceFanout,
  };
}

export function annotateSupportSpans(
  html: string,
  entries: CitationEntry[],
  options: AnnotateOptions = {},
): { html: string; supportMap: SupportMap; metrics: SupportMetrics } {
  const input = String(html || "");
  const spanIdPrefix = options.spanIdPrefix || "support";
  const { nodes, citationToNodes } = buildNodeMaps(entries);
  const usedCitationKeys = new Set<string>();
  const spans: SupportSpan[] = [];
  let spanIndex = 1;

  const output = input.replace(BLOCK_RE, (full, tag, attrs = "", inner = "") => {
    if (shouldSkipBlock(attrs)) {
      return full;
    }

    const blockInner = String(inner || "");
    if (!blockInner.trim()) {
      return full;
    }

    const { plain, plainToHtml } = mapPlainToHtml(blockInner);
    if (!plain.trim() || plainToHtml.length === 0) {
      return full;
    }

    const sentenceRanges = segmentSentences(plain);
    if (sentenceRanges.length === 0) {
      return full;
    }

    const starts = sentenceRanges.map((range) => plainToHtml[range.start] ?? 0);
    let rebuilt = "";
    const firstStart = starts[0] ?? 0;
    if (firstStart > 0) {
      rebuilt += blockInner.slice(0, firstStart);
    }

    for (let index = 0; index < sentenceRanges.length; index += 1) {
      const start = starts[index] ?? 0;
      const nextStart = index + 1 < starts.length ? starts[index + 1] : blockInner.length;
      const sentenceHtml = blockInner.slice(start, nextStart);
      const sentenceText = stripHtml(sentenceHtml);

      if (!sentenceText) {
        rebuilt += sentenceHtml;
        continue;
      }

      const citationKeys = extractCitationKeys(sentenceHtml);
      const nodeKeySet = new Set<string>();

      for (const citationKey of citationKeys) {
        usedCitationKeys.add(citationKey);
        const mapped = citationToNodes[citationKey] || [`citation:${citationKey}`];
        for (const nodeKey of mapped) {
          if (!nodes[nodeKey]) {
            addNode(nodes, nodeKey, "citation", citationKey, citationKey, 0);
          }
          nodeKeySet.add(nodeKey);
        }
      }

      const nodeKeys = uniqueInOrder(Array.from(nodeKeySet));
      const supported = nodeKeys.length > 0;
      const spanId = `${spanIdPrefix}-${spanIndex}`;
      spanIndex += 1;

      spans.push({
        id: spanId,
        text: sentenceText,
        supported,
        citation_keys: citationKeys,
        node_keys: nodeKeys,
      });

      for (const nodeKey of nodeKeys) {
        const node = nodes[nodeKey];
        if (!node) continue;
        if (!node.span_ids.includes(spanId)) {
          node.span_ids.push(spanId);
        }
      }

      const className = supported
        ? "support-span support-span--supported"
        : "support-span support-span--unsupported";
      rebuilt += `<span class="${className}" data-support-span-id="${escapeAttr(spanId)}">${sentenceHtml}</span>`;
    }

    return `<${tag}${attrs}>${rebuilt}</${tag}>`;
  });

  const orphanCitations = entries
    .map((entry) => entry.key)
    .filter((key) => !usedCitationKeys.has(key));

  const supportMap: SupportMap = {
    version: 1,
    spans,
    nodes,
    citation_to_nodes: citationToNodes,
    orphan_citations: orphanCitations,
  };

  const metrics = computeSupportMetrics(supportMap);
  return { html: output, supportMap, metrics };
}
