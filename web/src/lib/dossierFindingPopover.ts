// Separate entry point for dossier finding popovers to avoid Astro
// script deduplication with the article [slug].astro version.
export { initFindingPopover } from './findingPopover';
