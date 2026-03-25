type CitationLink = {
  key: string;
  label: string;
  url?: string;
  openUrl?: string;
  sourceRecordUrl?: string;
  sourceId?: string;
};

type SourceRecord = {
  id: string;
  label: string;
  title: string;
  kind: "external" | "hosted_copy" | "record_only" | "private_internal";
  recordUrl: string;
  externalUrl?: string;
  hostedAssetUrl?: string;
  pageOrLocator?: string;
  accessNote: string;
};

type FindingEvidenceDetail = {
  evidence_type: string;
  evidence_ref: string;
  source_quote?: string;
  source_page?: string;
  assessment?: string;
  resolved_links: CitationLink[];
  resolved_sources: SourceRecord[];
};

type FindingDetail = {
  id: string;
  summary: string;
  finding_type: string;
  confidence: string;
  claim_type: string;
  verification_status: string;
  date_of_event?: string;
  evidence: FindingEvidenceDetail[];
};

type FindingDetailMap = Record<string, FindingDetail>;

type FindingPopoverOptions = {
  dataScriptId?: string;
};

const POPOVER_CLASS = "finding-popover";
const INITIAL_EVIDENCE_COUNT = 3;

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function readDetailMap(scriptId: string): FindingDetailMap | null {
  const el = document.getElementById(scriptId);
  if (!el) return null;
  try {
    return JSON.parse(el.textContent || "") as FindingDetailMap;
  } catch {
    return null;
  }
}

function dismissPopover(): void {
  const existing = document.querySelector(`.${POPOVER_CLASS}`);
  if (existing) existing.remove();
}

function renderBadge(text: string, color: string): string {
  return `<span class="finding-popover__badge" style="border-color: ${color}; color: ${color};">${escapeHtml(text)}</span>`;
}

function renderActionLink(href: string, text: string, className: string): string {
  const attrs = /^https?:\/\//i.test(href) ? ' target="_blank" rel="noopener noreferrer"' : "";
  return `<a href="${escapeHtml(href)}"${attrs} class="${className}">${escapeHtml(text)}</a>`;
}

function renderSourceRecord(source: SourceRecord): string {
  const primaryHref = source.kind === "external"
    ? source.externalUrl
    : source.kind === "hosted_copy"
      ? source.hostedAssetUrl
      : source.recordUrl;

  const primary = primaryHref
    ? renderActionLink(primaryHref, source.label, "finding-popover__evidence-link")
    : `<span class="finding-popover__evidence-ref">${escapeHtml(source.label)}</span>`;

  const actions: string[] = [];
  if (source.externalUrl || source.hostedAssetUrl) {
    actions.push(renderActionLink(source.externalUrl || source.hostedAssetUrl || "", "Open artifact", "finding-popover__action"));
  }
  actions.push(renderActionLink(source.recordUrl, "View source record", "finding-popover__action"));

  const metaBits = [source.pageOrLocator, source.accessNote].filter(Boolean).map((value) => escapeHtml(value || ""));
  const meta = metaBits.length ? `<div class="finding-popover__evidence-meta">${metaBits.join(" · ")}</div>` : "";
  return `<div class="finding-popover__source">${primary}<span class="finding-popover__actions">${actions.join("")}</span>${meta}</div>`;
}

function renderEvidenceItem(ev: FindingEvidenceDetail): string {
  const links = (ev.resolved_sources || [])
    .map((source) => renderSourceRecord(source))
    .join("");

  const typeTag = `<span class="finding-popover__evidence-type">[${escapeHtml(ev.evidence_type)}]</span>`;
  const locator = ev.source_page ? `<div class="finding-popover__evidence-meta">${escapeHtml(ev.source_page)}</div>` : "";

  let quote = "";
  if (ev.source_quote) {
    const truncated =
      ev.source_quote.length > 300
        ? `${ev.source_quote.slice(0, 300)}...`
        : ev.source_quote;
    quote = `<blockquote class="finding-popover__quote">${escapeHtml(truncated)}</blockquote>`;
  }

  let assessment = "";
  if (ev.assessment) {
    assessment = `<div class="finding-popover__assessment">${escapeHtml(ev.assessment)}</div>`;
  }

  return `<div class="finding-popover__evidence-item">${typeTag}${links}${locator}${quote}${assessment}</div>`;
}

function renderPopoverContent(detail: FindingDetail, citationNumber: string): string {
  const typeColors: Record<string, string> = {
    financial: "#d1b36a",
    communication: "#8fd3e8",
    relationship: "#9aa6b2",
    legal: "#b7b1a3",
    corporate: "#7fa7a0",
    intelligence: "#a09c8a",
    identity: "#b6a3a3",
    location: "#8fa6b8",
    document: "#8c97a3",
  };

  const confidenceColors: Record<string, string> = {
    confirmed: "#d1b36a",
    high: "#8fd3e8",
    medium: "#8c97a3",
    low: "#8c97a3",
    unverified: "#8c97a3",
  };

  const typeColor = typeColors[detail.finding_type] || "#8c97a3";
  const confColor = confidenceColors[detail.confidence] || "#8c97a3";

  // Header
  const header = `<div class="finding-popover__header">
    <span class="finding-popover__title">Finding #${escapeHtml(detail.id)}</span>
    <button class="finding-popover__close" aria-label="Close">&times;</button>
  </div>`;

  // Badges
  const badges: string[] = [
    renderBadge(detail.finding_type, typeColor),
    renderBadge(detail.confidence, confColor),
    renderBadge(detail.claim_type, "#8c97a3"),
  ];
  if (detail.verification_status && detail.verification_status !== "unverified") {
    badges.push(renderBadge(detail.verification_status, "#d1b36a"));
  }
  if (detail.date_of_event) {
    badges.push(renderBadge(detail.date_of_event, "#8c97a3"));
  }
  const badgeRow = `<div class="finding-popover__badges">${badges.join("")}</div>`;

  // Summary
  const summary = `<div class="finding-popover__summary">${escapeHtml(detail.summary)}</div>`;
  const navigation = citationNumber
    ? `<div class="finding-popover__nav">${renderActionLink(`#fn-${citationNumber}`, "See source list entry", "finding-popover__action")}</div>`
    : "";

  // Evidence
  let evidenceSection = "";
  if (detail.evidence.length > 0) {
    const visible = detail.evidence.slice(0, INITIAL_EVIDENCE_COUNT);
    const hidden = detail.evidence.slice(INITIAL_EVIDENCE_COUNT);
    const visibleHtml = visible.map(renderEvidenceItem).join("");

    let hiddenHtml = "";
    if (hidden.length > 0) {
      const hiddenItems = hidden.map(renderEvidenceItem).join("");
      hiddenHtml = `<div class="finding-popover__evidence-hidden" style="display:none;">${hiddenItems}</div>
        <button class="finding-popover__evidence-expand" data-finding-expand>+${hidden.length} more</button>`;
    }

    evidenceSection = `<div class="finding-popover__evidence">
      <div class="finding-popover__evidence-label">Evidence</div>
      ${visibleHtml}${hiddenHtml}
    </div>`;
  }

  return `${header}${badgeRow}${summary}${navigation}${evidenceSection}`;
}

function positionPopover(popover: HTMLElement, anchor: HTMLElement): void {
  const isMobile = window.innerWidth < 640;

  if (isMobile) {
    popover.classList.add("finding-popover--mobile");
    return;
  }

  popover.classList.remove("finding-popover--mobile");

  const rect = anchor.getBoundingClientRect();
  const scrollY = window.scrollY;
  const scrollX = window.scrollX;
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const gap = 8;

  // Temporarily show to measure
  popover.style.visibility = "hidden";
  popover.style.display = "block";
  const popoverRect = popover.getBoundingClientRect();
  popover.style.visibility = "";

  // Vertical: prefer below, flip above if not enough room
  let top: number;
  if (rect.bottom + gap + popoverRect.height <= viewportHeight) {
    top = rect.bottom + scrollY + gap;
  } else {
    top = rect.top + scrollY - popoverRect.height - gap;
  }

  // Horizontal: center on anchor, clamp to viewport
  let left = rect.left + scrollX + rect.width / 2 - popoverRect.width / 2;
  const margin = 12;
  left = Math.max(scrollX + margin, Math.min(left, scrollX + viewportWidth - popoverRect.width - margin));

  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
}

function showPopover(detail: FindingDetail, anchor: HTMLElement): void {
  dismissPopover();
  const citationNumber = anchor.dataset.citationNumber || "";

  const popover = document.createElement("div");
  popover.className = POPOVER_CLASS;
  popover.innerHTML = renderPopoverContent(detail, citationNumber);
  document.body.appendChild(popover);

  positionPopover(popover, anchor);

  // Close button
  const closeBtn = popover.querySelector(".finding-popover__close");
  if (closeBtn) {
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dismissPopover();
    });
  }

  // Expand hidden evidence
  const expandBtn = popover.querySelector<HTMLButtonElement>("[data-finding-expand]");
  if (expandBtn) {
    expandBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const hidden = popover.querySelector<HTMLElement>(".finding-popover__evidence-hidden");
      if (hidden) {
        hidden.style.display = "";
        expandBtn.remove();
      }
    });
  }
}

export function initFindingPopover(options: FindingPopoverOptions = {}): void {
  const scriptId = options.dataScriptId || "finding-detail-data";
  const detailMap = readDetailMap(scriptId);
  if (!detailMap || Object.keys(detailMap).length === 0) return;

  // Click delegation — finding citations only
  document.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (!target) return;

    const citationEl = target.closest<HTMLElement>("[data-citation-key]");
    if (!citationEl) {
      // Click outside popover dismisses it
      if (!target.closest(`.${POPOVER_CLASS}`)) {
        dismissPopover();
      }
      return;
    }

    const key = citationEl.dataset.citationKey || "";
    if (!key.startsWith("finding:")) return;

    const findingId = key.slice("finding:".length);
    const detail = detailMap[findingId];
    if (!detail) return;

    event.preventDefault();
    event.stopPropagation();
    showPopover(detail, citationEl);
  });

  // Escape key dismisses
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dismissPopover();
    }
  });
}
