import type { SupportMap } from "./supportSpans";

type SupportModeOptions = {
  rootSelector?: string;
  defaultMapScriptId?: string;
};

function readSupportMap(root: HTMLElement, defaultMapScriptId: string): SupportMap | null {
  const scriptId = root.dataset.supportMapId || defaultMapScriptId;
  const scriptEl = document.getElementById(scriptId);
  if (!scriptEl) return null;

  try {
    return JSON.parse(scriptEl.textContent || "") as SupportMap;
  } catch {
    return null;
  }
}

function getSourceDataKey(nodeKey: string): string {
  return nodeKey.startsWith("source:") ? nodeKey.slice("source:".length) : "";
}

function hasModifierKey(event: MouseEvent): boolean {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

export function initSupportMode(options: SupportModeOptions = {}): void {
  const rootSelector = options.rootSelector || "[data-evidence-page]";
  const defaultMapScriptId = options.defaultMapScriptId || "support-map-data";

  const root = document.querySelector<HTMLElement>(rootSelector);
  if (!root) return;
  if (root.dataset.supportModeInitialized === "true") return;

  const supportMap = readSupportMap(root, defaultMapScriptId);
  if (!supportMap) return;

  const toggle = root.querySelector<HTMLInputElement>("[data-evidence-mode-toggle]");
  if (!toggle) return;

  root.dataset.supportModeInitialized = "true";

  const spanEls = Array.from(
    root.querySelectorAll<HTMLElement>("[data-support-span-id]"),
  );
  const spanById = new Map<string, HTMLElement>();
  for (const el of spanEls) {
    const id = el.dataset.supportSpanId;
    if (!id) continue;
    spanById.set(id, el);
  }

  const citationEls = Array.from(
    root.querySelectorAll<HTMLElement>("[data-citation-key], [data-source-key]"),
  );

  function clearActiveState(): void {
    for (const el of spanEls) {
      el.classList.remove("support-span--active");
    }
    for (const el of citationEls) {
      el.classList.remove("citation--active");
    }
  }

  function activateNodes(nodeKeys: string[]): void {
    const uniqueNodeKeys = Array.from(new Set(nodeKeys.filter(Boolean)));
    if (uniqueNodeKeys.length === 0) return;

    const spanIds = new Set<string>();
    const citationKeys = new Set<string>();
    const sourceKeys = new Set<string>();

    for (const nodeKey of uniqueNodeKeys) {
      const node = supportMap.nodes[nodeKey];
      if (!node) continue;
      for (const spanId of node.span_ids || []) {
        spanIds.add(spanId);
      }
      for (const citationKey of node.citation_keys || []) {
        citationKeys.add(citationKey);
      }
      const sourceDataKey = getSourceDataKey(node.key);
      if (sourceDataKey) {
        sourceKeys.add(sourceDataKey);
      }
    }

    for (const spanId of spanIds) {
      spanById.get(spanId)?.classList.add("support-span--active");
    }

    for (const el of citationEls) {
      const citationKey = el.dataset.citationKey;
      const sourceKey = el.dataset.sourceKey;
      if ((citationKey && citationKeys.has(citationKey)) || (sourceKey && sourceKeys.has(sourceKey))) {
        el.classList.add("citation--active");
      }
    }
  }

  function activateFromSpan(spanId: string): void {
    const span = supportMap.spans.find((item) => item.id === spanId);
    clearActiveState();
    if (!span) return;
    const el = spanById.get(spanId);
    if (!el) return;

    if (span.node_keys.length === 0) {
      el.classList.add("support-span--active");
      return;
    }

    activateNodes(span.node_keys);
    el.classList.add("support-span--active");
  }

  function setEnabled(enabled: boolean): void {
    if (enabled) {
      root.classList.add("evidence-mode--enabled");
    } else {
      root.classList.remove("evidence-mode--enabled");
      clearActiveState();
    }
  }

  toggle.addEventListener("change", () => {
    setEnabled(Boolean(toggle.checked));
  });

  root.addEventListener("click", (event) => {
    if (!toggle.checked) return;
    const mouseEvent = event as MouseEvent;
    const target = event.target as HTMLElement | null;
    if (!target) return;

    const sourceEl = target.closest<HTMLElement>("[data-source-key]");
    if (sourceEl) {
      if (!hasModifierKey(mouseEvent)) {
        event.preventDefault();
      }
      clearActiveState();
      const sourceKey = sourceEl.dataset.sourceKey || "";
      sourceEl.classList.add("citation--active");
      activateNodes([`source:${sourceKey}`]);
      return;
    }

    const citationEl = target.closest<HTMLElement>("[data-citation-key]");
    if (citationEl) {
      if (!hasModifierKey(mouseEvent)) {
        event.preventDefault();
      }
      clearActiveState();
      const citationKey = citationEl.dataset.citationKey || "";
      const nodeKeys = supportMap.citation_to_nodes[citationKey] || [`citation:${citationKey}`];
      citationEl.classList.add("citation--active");
      activateNodes(nodeKeys);
      return;
    }

    const spanEl = target.closest<HTMLElement>("[data-support-span-id]");
    if (spanEl) {
      activateFromSpan(spanEl.dataset.supportSpanId || "");
    }
  });

  setEnabled(Boolean(toggle.checked));
}
