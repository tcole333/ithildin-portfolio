# Web (Astro Site)

Static site built with Astro, deployed to Cloudflare Pages. Articles (MDX) and dossiers (JSON) are rendered with citation-linked evidence and support spans.

## Citation System

**Registry**: `src/lib/citations.ts` defines all 24 citation types in a single `CITATION_REGISTRY` array.

**Adding a new citation type** = adding one object to the registry with `id`, `tokenPattern`, `healthTier`, `resolve()`, and `extract()`. No other files need to change for the engine to recognize it. See `docs/CITATION_SYSTEM.md` for the full guide and example.

**Tests**: `npm run test:citations` (48 unit tests), `npm run test:citations:snapshots` (regression), `npm run lint:citations`.

## Key Paths

| Component | Path |
|-----------|------|
| Citation registry | `src/lib/citations.ts` |
| Support spans | `src/lib/supportSpans.ts` |
| Content pipeline | `src/lib/contentEvidencePipeline.ts` |
| Articles | `../content/articles/*.mdx` |
| Dossiers | `../content/dossiers/*.json` |

## Commands

```bash
npm run dev              # Astro dev server
npm run build            # Production build (183 pages)
npm run test:citations   # Citation unit tests
npm run test:citations:snapshots  # Snapshot regression
npm run lint:citations   # Citation lint
npm run deploy           # Lint + test + build + deploy
```
