# Portfolio Demo Fixture

This sample investigation is a small, reproducible Ithildin fixture intended for portfolio review.

It includes:

- `2` dossiers
- `1` longform article
- `11` findings across content and SQLite
- mixed citation types:
  - hosted demo artifact
  - hosted copy
  - metadata-only source record

Use it with:

```bash
make demo
```

`make demo` exports `ITHILDIN_*` vars so the build reads from this fixture instead of your repo-root databases. That keeps your active research data separate from the portfolio showcase path.
In this public repo, the fixture is the primary runtime dataset.

Or by setting the demo paths directly:

```bash
ITHILDIN_CONTENT_ROOT=examples/portfolio-demo/content \
ITHILDIN_INVESTIGATION_DB=examples/portfolio-demo/investigation.db \
ITHILDIN_REGISTRY_DB=examples/portfolio-demo/registry.db \
ITHILDIN_DOJ_DB=examples/portfolio-demo/doj_documents.db \
PUBLIC_ENABLE_EVIDENCE_MODE=true \
npm run build --prefix web
```
