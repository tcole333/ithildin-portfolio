import { initFindingPopover } from "./findingPopover";
import { initSupportMode } from "./supportMode";

export function initEvidencePage(): void {
  const evidenceRoot = document.querySelector<HTMLElement>("[data-evidence-page]");
  if (evidenceRoot) {
    initSupportMode();
  }

  if (document.getElementById("finding-detail-data")) {
    initFindingPopover();
  }
}
