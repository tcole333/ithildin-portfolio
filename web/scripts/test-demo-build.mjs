import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const distRoot = resolve(process.cwd(), "web", "dist");

function readDist(path) {
  return readFileSync(resolve(distRoot, path), "utf-8");
}

assert.equal(existsSync(distRoot), true, "Demo build output missing at web/dist.");

const indexHtml = readDist("index.html");
const articleHtml = readDist("articles/port-watch-procurement-network/index.html");
const harborHtml = readDist("dossiers/harbor-ledger-holdings/index.html");
const linaHtml = readDist("dossiers/lina-ortega/index.html");
const financialsHtml = readDist("financials/index.html");

assert.match(indexHtml, /Investigative Infrastructure/);
assert.match(articleHtml, /The Port Watch Procurement Network/);
assert.match(articleHtml, /View source record/);
assert.match(harborHtml, /Harbor Ledger Holdings LLC/);
assert.match(harborHtml, /finding:9001/);
assert.match(linaHtml, /Lina Ortega/);
assert.match(financialsHtml, /Procurement Corridor/);
assert.match(financialsHtml, /Harbor Ledger Holdings LLC/);

const sourceDir = resolve(distRoot, "sources");
const sourcePages = readdirSync(sourceDir).filter((entry) =>
  existsSync(resolve(sourceDir, entry, "index.html")),
);
assert.ok(sourcePages.length >= 3, "Expected multiple source-record pages in the demo build.");

const sourceHtml = sourcePages.map((entry) => readDist(`sources/${entry}/index.html`));
assert.ok(
  sourceHtml.some((html) => /metadata only/.test(html) && /Port Watch source interview notes/.test(html)),
  "Expected a metadata-only source record page for the reporting notes.",
);
assert.ok(
  sourceHtml.some((html) => /Open Artifact/.test(html) && /Harbor Ledger board minutes excerpt/.test(html)),
  "Expected a hosted-copy source record page with an artifact action.",
);
assert.ok(
  sourceHtml.some((html) => /Public Uses/.test(html) && /port-watch-procurement-network/.test(html)),
  "Expected source-record pages to list public occurrences.",
);

process.stdout.write("Portfolio demo build checks passed.\n");
