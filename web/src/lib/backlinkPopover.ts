/**
 * Backlink preview popover — shows inline previews of dossiers and articles
 * when clicking internal links. Follows the findingPopover pattern: click
 * delegation on document, absolute-positioned popover, mobile bottom-sheet.
 */

type DossierPreview = {
  type: "dossier";
  name: string;
  role: string;
  lead: string;
  stats: { findings: number; connections: number; entities: number };
  topConnections: string[];
};

type ArticlePreview = {
  type: "article";
  title: string;
  subtitle: string;
};

type PreviewEntry = DossierPreview | ArticlePreview;
type PreviewIndex = Record<string, PreviewEntry>;

const POPOVER_CLASS = "backlink-popover";
const INTERNAL_LINK_RE = /^\/(dossiers|articles)\/([a-z0-9-]+)\/?$/;

let indexPromise: Promise<PreviewIndex> | null = null;

function loadPreviewIndex(): Promise<PreviewIndex> {
  if (!indexPromise) {
    indexPromise = fetch("/content/preview-index.json")
      .then((r) => {
        if (!r.ok) throw new Error(`Preview index: ${r.status}`);
        return r.json() as Promise<PreviewIndex>;
      })
      .catch(() => ({}) as PreviewIndex);
  }
  return indexPromise;
}

function parseInternalHref(href: string): { type: string; slug: string; key: string } | null {
  try {
    const url = new URL(href, window.location.origin);
    const match = url.pathname.match(INTERNAL_LINK_RE);
    if (!match) return null;
    return { type: match[1], slug: match[2], key: `${match[1]}/${match[2]}` };
  } catch {
    return null;
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderDossierPreview(entry: DossierPreview, href: string): string {
  const header = `<div class="${POPOVER_CLASS}__header">
    <span class="${POPOVER_CLASS}__name">${escapeHtml(entry.name)}</span>
    <button class="${POPOVER_CLASS}__close" aria-label="Close">&times;</button>
  </div>`;

  const role = entry.role
    ? `<div class="${POPOVER_CLASS}__role">${escapeHtml(entry.role)}</div>`
    : "";

  const { findings, connections, entities } = entry.stats;
  const stats = `<div class="${POPOVER_CLASS}__stats">
    <span>${findings} findings</span>
    <span>${connections} connections</span>
    <span>${entities} entities</span>
  </div>`;

  const lead = entry.lead
    ? `<div class="${POPOVER_CLASS}__lead">${escapeHtml(entry.lead)}</div>`
    : "";

  let connectionsHtml = "";
  if (entry.topConnections.length > 0) {
    const chips = entry.topConnections
      .map((name) => `<span class="${POPOVER_CLASS}__chip">${escapeHtml(name)}</span>`)
      .join("");
    connectionsHtml = `<div class="${POPOVER_CLASS}__connections">${chips}</div>`;
  }

  const footer = `<div class="${POPOVER_CLASS}__footer">
    <a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" class="${POPOVER_CLASS}__open">Open full page &rarr;</a>
  </div>`;

  return `${header}${role}${stats}${lead}${connectionsHtml}${footer}`;
}

function renderArticlePreview(entry: ArticlePreview, href: string): string {
  const header = `<div class="${POPOVER_CLASS}__header">
    <span class="${POPOVER_CLASS}__name">${escapeHtml(entry.title)}</span>
    <button class="${POPOVER_CLASS}__close" aria-label="Close">&times;</button>
  </div>`;

  const subtitle = entry.subtitle
    ? `<div class="${POPOVER_CLASS}__role">${escapeHtml(entry.subtitle)}</div>`
    : "";

  const footer = `<div class="${POPOVER_CLASS}__footer">
    <a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" class="${POPOVER_CLASS}__open">Open full page &rarr;</a>
  </div>`;

  return `${header}${subtitle}${footer}`;
}

function renderLoading(): string {
  return `<div class="${POPOVER_CLASS}__loading">Loading preview&hellip;</div>`;
}

function dismissPopover(): void {
  const existing = document.querySelector(`.${POPOVER_CLASS}`);
  if (existing) existing.remove();
}

function positionPopover(popover: HTMLElement, anchor: HTMLElement): void {
  const isMobile = window.innerWidth < 640;

  if (isMobile) {
    popover.classList.add(`${POPOVER_CLASS}--mobile`);
    return;
  }

  popover.classList.remove(`${POPOVER_CLASS}--mobile`);

  const rect = anchor.getBoundingClientRect();
  const scrollY = window.scrollY;
  const scrollX = window.scrollX;
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const gap = 8;

  popover.style.visibility = "hidden";
  popover.style.display = "block";
  const popoverRect = popover.getBoundingClientRect();
  popover.style.visibility = "";

  let top: number;
  if (rect.bottom + gap + popoverRect.height <= viewportHeight) {
    top = rect.bottom + scrollY + gap;
  } else {
    top = rect.top + scrollY - popoverRect.height - gap;
  }

  let left = rect.left + scrollX + rect.width / 2 - popoverRect.width / 2;
  const margin = 12;
  left = Math.max(scrollX + margin, Math.min(left, scrollX + viewportWidth - popoverRect.width - margin));

  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
}

function showPopover(entry: PreviewEntry, href: string, anchor: HTMLElement): void {
  dismissPopover();

  const popover = document.createElement("div");
  popover.className = POPOVER_CLASS;

  if (entry.type === "dossier") {
    popover.innerHTML = renderDossierPreview(entry as DossierPreview, href);
  } else {
    popover.innerHTML = renderArticlePreview(entry as ArticlePreview, href);
  }

  document.body.appendChild(popover);
  positionPopover(popover, anchor);

  const closeBtn = popover.querySelector(`.${POPOVER_CLASS}__close`);
  if (closeBtn) {
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dismissPopover();
    });
  }
}

const DETAIL_PAGE_RE = /^\/(dossiers|articles)\/[a-z0-9-]+\/?$/;

export function initBacklinkPopover(): void {
  // Only activate on detail pages, not index/landing pages
  if (!DETAIL_PAGE_RE.test(window.location.pathname)) return;

  document.addEventListener("click", async (event) => {
    const target = event.target as HTMLElement | null;
    if (!target) return;

    // Let finding popover handle citation clicks
    if (target.closest("[data-citation-key]")) return;

    // Handle clicks inside the popover (open link, close)
    if (target.closest(`.${POPOVER_CLASS}`)) {
      // Let <a> clicks inside popover work naturally (open full page)
      return;
    }

    // Find closest anchor
    const anchor = target.closest<HTMLAnchorElement>("a[href]");
    if (!anchor) {
      dismissPopover();
      return;
    }

    const parsed = parseInternalHref(anchor.href);
    if (!parsed) {
      dismissPopover();
      return;
    }

    // Don't intercept same-page links
    const currentPath = window.location.pathname.replace(/\/$/, "");
    const targetPath = `/${parsed.type}/${parsed.slug}`;
    if (currentPath === targetPath) return;

    event.preventDefault();
    event.stopPropagation();
    dismissPopover();

    // Show loading state on first load
    const loadingPopover = document.createElement("div");
    loadingPopover.className = POPOVER_CLASS;
    loadingPopover.innerHTML = renderLoading();
    document.body.appendChild(loadingPopover);
    positionPopover(loadingPopover, anchor);

    const index = await loadPreviewIndex();
    dismissPopover(); // Remove loading popover

    const entry = index[parsed.key];
    if (!entry) {
      // No preview available — open in new tab
      window.open(anchor.href, "_blank");
      return;
    }

    showPopover(entry, anchor.href, anchor);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dismissPopover();
    }
  });
}
