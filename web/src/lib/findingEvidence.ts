import { loadFindingEvidenceMap as loadCatalogFindingEvidenceMap, type FindingEvidenceMap } from "./findingCatalog";

export function loadFindingEvidenceMap(options: { includeDbFallback?: boolean } = {}): FindingEvidenceMap {
  return loadCatalogFindingEvidenceMap(options);
}
