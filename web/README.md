# Web Surface

This Astro app renders Ithildin’s public publishing surface: dossiers, articles, source records, network views, and financial pages.

## Commands

```bash
npm install
npm run dev
npm run check
npm run test:citations
npm run test:citations:snapshots
npm run build
```

## Demo mode

Build the bundled portfolio fixture by setting the shared Ithildin env vars:

```bash
ITHILDIN_CONTENT_ROOT=../examples/portfolio-demo/content \
ITHILDIN_INVESTIGATION_DB=../examples/portfolio-demo/investigation.db \
ITHILDIN_REGISTRY_DB=../examples/portfolio-demo/registry.db \
ITHILDIN_DOJ_DB=../examples/portfolio-demo/doj_documents.db \
PUBLIC_ENABLE_EVIDENCE_MODE=true \
npm run build
```

Or from the repo root:

```bash
make demo
```

The demo build is isolated to the fixture databases under [`../examples/portfolio-demo`](../examples/portfolio-demo/README.md). This public repo does not require any private investigation database.

## Quality surface

- `npm run check`: Astro/type checks
- `npm run test:citations`: resolver, source-record, and rendering tests
- `npm run test:citations:snapshots`: regression snapshots for citation output
- `npm run test:citations:build`: smoke assertions against built HTML
- `npm run test:e2e:smoke`: lightweight browser pass for the portfolio demo

If you want the full portfolio path from the repo root, use:

```bash
make test
make demo
```
