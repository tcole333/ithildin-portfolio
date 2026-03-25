import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const distRoot = resolve(process.cwd(), "dist");

function readDist(path) {
  return readFileSync(resolve(distRoot, path), "utf-8");
}

const articleHtml = readDist("articles/port-watch-procurement-network/index.html");
const dossierHtml = readDist("dossiers/harbor-ledger-holdings/index.html");
const astroFiles = readdirSync(resolve(distRoot, "_astro"));
const evidenceBootstrapFile = astroFiles.find((file) => file.startsWith("evidencePageBootstrap."));
assert.ok(evidenceBootstrapFile, "Built assets should include the shared evidence bootstrap bundle.");
const bootstrapSource = readDist(`_astro/${evidenceBootstrapFile}`);

assert.ok(!articleHtml.includes("[REG:FL:L23000214567]"), "Raw registry citation token remained in built article HTML.");
assert.ok(!articleHtml.includes("[PORT-WATCH-CALL-NOTES-2025-02-14]"), "Raw metadata-only citation token remained in built article HTML.");
assert.ok(articleHtml.includes("/sources/"), "Built article should expose source-record links in the sources section.");

assert.ok(bootstrapSource.includes("finding-detail-data"), "Shared evidence bootstrap should initialize finding popovers.");
assert.ok(bootstrapSource.includes("data-evidence-page"), "Shared evidence bootstrap should initialize support mode when present.");
assert.ok(
  dossierHtml.includes('data-citation-key="finding:9001"') || dossierHtml.includes("finding:9001"),
  "Built dossier should include finding #9001 citation data.",
);
assert.ok(dossierHtml.includes('href="#fn-'), "Built dossier finding citations should target footnotes for popover interception.");
assert.ok(dossierHtml.includes("/sources/"), "Built dossier should expose source-record links.");

process.stdout.write("Citation build checks passed.\n");
