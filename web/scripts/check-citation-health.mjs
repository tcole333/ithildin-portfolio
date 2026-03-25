#!/usr/bin/env node
/**
 * Citation Link Health Checker
 *
 * Extracts all citation URLs from articles and dossiers, deduplicates them,
 * and checks if they resolve. Reports broken, redirected, and healthy links.
 *
 * Tiered checking:
 *   Tier 1 (Public APIs): SEC, ProPublica 990, FEC, CourtListener, ACRIS, FL SunBiz, UK Companies, OpenSanctions
 *   Tier 2 (Search pages): LDA Senate, FEC alias search — may always 200, body check needed
 *   Tier 3 (Generic landing): FARA, USVI, NM-SoS, NY-SoS — skip (URL is just a homepage)
 *   Tier 4 (Critical dependency): EFTA/jmail.world — sample check
 *   Label-only: KPMG, DS10, Finding refs — no URL to check
 *
 * Usage:
 *   node scripts/check-citation-health.mjs [--fix] [--sample N] [--timeout MS]
 */

import { readFileSync, readdirSync, existsSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { createJiti } from "jiti";
import { articlesDir, dossiersDir } from "./lib/project-paths.mjs";

const cwd = process.cwd();
const resultsPath = resolve(cwd, "scripts", "citation-health-results.json");

const argv = process.argv.slice(2);
const args = new Set(argv.filter((a) => a.startsWith("--")));
const fixMode = args.has("--fix");
const sampleSize = (() => {
  const idx = argv.indexOf("--sample");
  return idx >= 0 ? parseInt(argv[idx + 1], 10) || 10 : 10;
})();
const timeoutMs = (() => {
  const idx = argv.indexOf("--timeout");
  return idx >= 0 ? parseInt(argv[idx + 1], 10) || 5000 : 5000;
})();

const jiti = createJiti(import.meta.url);
const { applyCitations, createCitationState } = jiti(
  resolve(cwd, "src", "lib", "citations.ts"),
);
const { loadFindingEvidenceMap } = jiti(
  resolve(cwd, "src", "lib", "findingEvidence.ts"),
);
const { buildDossierFindingEvidenceMap } = jiti(
  resolve(cwd, "src", "lib", "contentEvidencePipeline.ts"),
);

// ---------------------------------------------------------------------------
// URL tier classification
// ---------------------------------------------------------------------------

/** @param {string} url */
function classifyUrl(url) {
  if (!url || !url.startsWith("http")) return "skip";

  const lower = url.toLowerCase();

  // Tier 1: Public APIs with real entity pages
  if (lower.includes("sec.gov/Archives/edgar")) return "tier1";
  if (lower.includes("propublica.org/nonprofits")) return "tier1";
  if (lower.includes("fec.gov/data/committee/")) return "tier1";
  if (lower.includes("fec.gov/data/receipts/")) return "tier1";
  if (lower.includes("courtlistener.com/docket/")) return "tier1";
  if (lower.includes("a836-acris.nyc.gov")) return "tier1";
  if (lower.includes("search.sunbiz.org")) return "tier1";
  if (lower.includes("company-information.service.gov.uk")) return "tier1";
  if (lower.includes("opensanctions.org/entities/")) return "tier1";

  // Tier 2: Search pages — may always return 200
  if (lower.includes("lda.senate.gov/filings")) return "tier2";
  if (lower.includes("fec.gov/data/search/")) return "tier2";

  // Tier 3: Generic landing pages — skip checking
  if (lower.includes("efile.fara.gov")) return "tier3";
  if (lower.includes("ltg.gov.vi")) return "tier3";
  if (lower.includes("portal.sos.state.nm.us")) return "tier3";
  if (lower.includes("dos.ny.gov")) return "tier3";
  if (lower.includes("icis.corp.delaware.gov")) return "tier3";

  // Tier 4: Critical dependency — jmail.world
  if (lower.includes("jmail.world")) return "tier4";

  // Tier 4: House Oversight
  if (lower.includes("oversight.house.gov")) return "tier4";

  // Internal links
  if (url.startsWith("/")) return "internal";

  return "tier1"; // default: check external URLs
}

// ---------------------------------------------------------------------------
// Extract all citation URLs
// ---------------------------------------------------------------------------

/** @typedef {{ url: string, key: string, label: string, source: string, tier: string }} CitationUrl */

/** @returns {CitationUrl[]} */
function extractAllCitationUrls() {
  /** @type {Map<string, CitationUrl>} */
  const urlMap = new Map();

  function addUrl(url, key, label, source) {
    if (!url || url.startsWith("#")) return;
    const tier = classifyUrl(url);
    if (tier === "skip" || tier === "internal") return;
    if (!urlMap.has(url)) {
      urlMap.set(url, { url, key, label, source, tier });
    }
  }

  // Load the global finding → evidence map once (articles use DB-backed map)
  const findingEvidenceMap = loadFindingEvidenceMap();

  // Process articles
  if (existsSync(articlesDir)) {
    const files = readdirSync(articlesDir).filter((f) => f.endsWith(".mdx"));
    for (const fileName of files) {
      const abs = resolve(articlesDir, fileName);
      const raw = readFileSync(abs, "utf-8");
      const body = raw.replace(/^---\n[\s\S]*?\n---\n*/, "");
      const state = createCitationState();
      applyCitations(body, { findingEvidenceMap }, state);
      for (const entry of state.entries) {
        addUrl(entry.url, entry.key, entry.label, `article:${fileName}`);
        // Also collect URLs from finding sub-sources
        if (entry.sources) {
          for (const src of entry.sources) {
            addUrl(src.url, src.key, src.label, `article:${fileName}`);
          }
        }
      }
    }
  }

  // Process dossiers
  if (existsSync(dossiersDir)) {
    const files = readdirSync(dossiersDir).filter(
      (f) => f.endsWith(".json") && !f.startsWith("_"),
    );
    for (const fileName of files) {
      const abs = resolve(dossiersDir, fileName);
      const dossier = JSON.parse(readFileSync(abs, "utf-8"));
      const dossierFindingMap = buildDossierFindingEvidenceMap(dossier);
      const state = createCitationState();

      const lead = dossier.curation?.lead;
      if (typeof lead === "string" && lead.trim()) {
        applyCitations(lead, { findingEvidenceMap: dossierFindingMap }, state);
      }

      const sections = Array.isArray(dossier.curation?.sections)
        ? dossier.curation.sections
        : [];
      for (const section of sections) {
        const content =
          typeof section?.content === "string" ? section.content : "";
        if (content.trim()) {
          applyCitations(content, { findingEvidenceMap: dossierFindingMap }, state);
        }
      }

      for (const entry of state.entries) {
        addUrl(entry.url, entry.key, entry.label, `dossier:${fileName}`);
        if (entry.sources) {
          for (const src of entry.sources) {
            addUrl(src.url, src.key, src.label, `dossier:${fileName}`);
          }
        }
      }
    }
  }

  return Array.from(urlMap.values());
}

// ---------------------------------------------------------------------------
// HTTP health check
// ---------------------------------------------------------------------------

/**
 * @param {string} url
 * @param {number} timeout
 * @returns {Promise<{status: number|null, ok: boolean, redirect?: string, error?: string}>}
 */
async function checkUrl(url, timeout) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      method: "HEAD",
      signal: controller.signal,
      redirect: "manual",
      headers: {
        "User-Agent":
          "Ithildin-Citation-Health-Checker/1.0 (+https://github.com/ithildin)",
      },
    });

    clearTimeout(timer);

    if (response.status >= 200 && response.status < 300) {
      return { status: response.status, ok: true };
    }

    if (response.status === 301 || response.status === 302) {
      const location = response.headers.get("location") || "";
      return {
        status: response.status,
        ok: false,
        redirect: location,
      };
    }

    // Some servers block non-browser UAs with 403 — retry with browser UA
    if (response.status === 403) {
      const retryResponse = await fetch(url, {
        method: "GET",
        signal: AbortSignal.timeout(timeout),
        redirect: "follow",
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
      });
      return { status: retryResponse.status, ok: retryResponse.ok };
    }

    // Some servers don't support HEAD — try GET
    if (response.status === 405) {
      const getResponse = await fetch(url, {
        method: "GET",
        signal: AbortSignal.timeout(timeout),
        redirect: "follow",
        headers: {
          "User-Agent":
            "Ithildin-Citation-Health-Checker/1.0 (+https://github.com/ithildin)",
        },
      });
      return {
        status: getResponse.status,
        ok: getResponse.ok,
      };
    }

    return { status: response.status, ok: false };
  } catch (err) {
    clearTimeout(timer);
    const message =
      err.name === "AbortError"
        ? `Timeout after ${timeout}ms`
        : err.message || "Unknown error";
    return { status: null, ok: false, error: message };
  }
}

// ---------------------------------------------------------------------------
// jmail.world body check (returns 200 but renders "Thread Not Found")
// ---------------------------------------------------------------------------

/**
 * @param {string} url
 * @param {number} timeout
 * @returns {Promise<{status: number|null, ok: boolean, broken: boolean, error?: string}>}
 */
async function checkJmailUrl(url, timeout) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        "User-Agent":
          "Ithildin-Citation-Health-Checker/1.0 (+https://github.com/ithildin)",
      },
    });
    clearTimeout(timer);
    const body = await response.text();
    const broken = body.includes("Thread Not Found");
    return { status: response.status, ok: !broken, broken };
  } catch (err) {
    clearTimeout(timer);
    return {
      status: null,
      ok: false,
      broken: true,
      error: err.message || "Unknown error",
    };
  }
}

// ---------------------------------------------------------------------------
// EFTA dataset lookup (for --fix mode DOJ URL construction)
// ---------------------------------------------------------------------------

/**
 * Look up which DOJ DataSet an EFTA ID belongs to by querying documents.db.
 * Falls back to DataSet 11 (the most common unindexed volume).
 * @param {string} eftaId
 * @returns {number}
 */
function lookupEftaDataset(eftaId) {
  const docsDb =
    process.env.EPSTEIN_DOCS_DB ||
    resolve(
      process.env.HOME,
      "projects",
      "epstein-docs",
      "output",
      "documents.db",
    );
  if (existsSync(docsDb)) {
    try {
      const row = execFileSync(
        "sqlite3",
        [docsDb, `SELECT pdf_path FROM documents WHERE bates_id='${eftaId}' LIMIT 1`],
        { encoding: "utf-8" },
      ).trim();
      const volMatch = row.match(/VOL(\d+)/);
      if (volMatch) return parseInt(volMatch[1], 10);
    } catch {
      // Fall through to default
    }
  }
  return 11;
}

/**
 * @param {string} eftaId
 * @param {number} datasetNum
 * @returns {string}
 */
function buildDojUrl(eftaId, datasetNum) {
  return `https://www.justice.gov/epstein/files/DataSet%20${datasetNum}/${eftaId}.pdf`;
}

/**
 * Extract an EFTA ID from a jmail.world URL.
 * @param {string} url
 * @returns {string|null}
 */
function extractEftaIdFromUrl(url) {
  const match = url.match(/jmail\.world\/thread\/(EFTA\d+|HOUSE_OVERSIGHT_\d+)/i);
  return match ? match[1].toUpperCase() : null;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  process.stdout.write("Extracting citation URLs from articles and dossiers...\n");
  const allUrls = extractAllCitationUrls();
  process.stdout.write(`Found ${allUrls.length} unique citation URLs.\n`);

  // Group by tier
  const byTier = {};
  for (const entry of allUrls) {
    if (!byTier[entry.tier]) byTier[entry.tier] = [];
    byTier[entry.tier].push(entry);
  }

  for (const [tier, entries] of Object.entries(byTier)) {
    process.stdout.write(`  ${tier}: ${entries.length} URLs\n`);
  }

  // Determine which URLs to check
  const toCheck = [];

  // Tier 1: Check all
  for (const entry of byTier.tier1 || []) {
    toCheck.push(entry);
  }

  // Tier 2: Check all (but results may be misleading)
  for (const entry of byTier.tier2 || []) {
    toCheck.push(entry);
  }

  // Tier 3: Skip
  // Tier 4: Sample (or all in --fix mode)
  const tier4 = byTier.tier4 || [];
  if (tier4.length > 0) {
    if (fixMode) {
      // In --fix mode, check ALL jmail URLs to find every broken one
      process.stdout.write(
        `  --fix mode: checking all ${tier4.length} tier4 (jmail.world) URLs...\n`,
      );
      for (const entry of tier4) {
        toCheck.push(entry);
      }
    } else {
      const shuffled = [...tier4].sort(() => Math.random() - 0.5);
      const sample = shuffled.slice(0, sampleSize);
      process.stdout.write(
        `  Sampling ${sample.length} of ${tier4.length} tier4 (jmail.world) URLs...\n`,
      );
      for (const entry of sample) {
        toCheck.push(entry);
      }
    }
  }

  process.stdout.write(`\nChecking ${toCheck.length} URLs...\n`);

  const results = {
    healthy: [],
    broken: [],
    redirected: [],
    unreachable: [],
    skipped: (byTier.tier3 || []).length,
    tier4_total: tier4.length,
    tier4_sampled: Math.min(sampleSize, tier4.length),
  };

  // Check in batches of 5 to avoid overwhelming servers
  const batchSize = 5;
  for (let i = 0; i < toCheck.length; i += batchSize) {
    const batch = toCheck.slice(i, i + batchSize);
    const checks = batch.map(async (entry) => {
      // Use body-check for jmail URLs (they return 200 even for missing threads)
      const isJmail = entry.url.includes("jmail.world");
      const result = isJmail
        ? await checkJmailUrl(entry.url, timeoutMs)
        : await checkUrl(entry.url, timeoutMs);
      return { ...entry, ...result };
    });

    const batchResults = await Promise.all(checks);

    for (const r of batchResults) {
      if (r.ok) {
        results.healthy.push({
          url: r.url,
          key: r.key,
          status: r.status,
          tier: r.tier,
        });
      } else if (r.redirect) {
        results.redirected.push({
          url: r.url,
          key: r.key,
          status: r.status,
          redirect: r.redirect,
          tier: r.tier,
          source: r.source,
        });
      } else if (r.error) {
        results.unreachable.push({
          url: r.url,
          key: r.key,
          error: r.error,
          tier: r.tier,
          source: r.source,
        });
      } else {
        results.broken.push({
          url: r.url,
          key: r.key,
          status: r.status,
          tier: r.tier,
          source: r.source,
        });

        // For EFTA broken links, suggest fallback
        if (r.key && r.key.startsWith("efta:")) {
          process.stdout.write(
            `  EFTA broken: ${r.url} -> suggest fallback to https://oversight.house.gov/release/epstein-documents/\n`,
          );
        }
      }
    }

    // Progress
    const done = Math.min(i + batchSize, toCheck.length);
    process.stdout.write(
      `  Checked ${done}/${toCheck.length} (${results.healthy.length} ok, ${results.broken.length} broken, ${results.unreachable.length} unreachable)\r`,
    );
  }

  process.stdout.write("\n");

  // Save results
  const report = {
    checked_at: new Date().toISOString(),
    total_urls: allUrls.length,
    checked: toCheck.length,
    healthy: results.healthy.length,
    broken: results.broken.length,
    redirected: results.redirected.length,
    unreachable: results.unreachable.length,
    skipped_tier3: results.skipped,
    tier4_total: results.tier4_total,
    tier4_sampled: results.tier4_sampled,
    broken_urls: results.broken,
    redirected_urls: results.redirected,
    unreachable_urls: results.unreachable,
  };

  writeFileSync(resultsPath, JSON.stringify(report, null, 2) + "\n", "utf-8");

  // --fix mode: generate jmail override mapping
  if (fixMode) {
    const overrides = {};
    const brokenJmail = [...results.broken, ...results.unreachable].filter(
      (r) => r.url && r.url.includes("jmail.world"),
    );

    for (const entry of brokenJmail) {
      const eftaId = extractEftaIdFromUrl(entry.url);
      if (!eftaId) continue;
      const datasetNum = lookupEftaDataset(eftaId);
      overrides[eftaId] = buildDojUrl(eftaId, datasetNum);
    }

    const overridesPath = resolve(cwd, "src", "data", "jmail-overrides.json");
    writeFileSync(
      overridesPath,
      JSON.stringify(overrides, null, 2) + "\n",
      "utf-8",
    );
    process.stdout.write(
      `\n--fix: wrote ${Object.keys(overrides).length} jmail overrides to ${overridesPath}\n`,
    );

    // Generate CourtListener overrides (slug lookup via API)
    const brokenCl = [...results.broken, ...results.unreachable].filter(
      (r) => r.url && r.url.includes("courtlistener.com/docket/"),
    );
    if (brokenCl.length > 0) {
      const clToken = process.env.COURTLISTENER_TOKEN;
      const clOverrides = {};
      for (const entry of brokenCl) {
        const match = entry.url.match(/\/docket\/(\d+)/);
        if (!match) continue;
        const docketId = match[1];
        if (clOverrides[docketId]) continue;
        try {
          const headers = {};
          if (clToken) headers["Authorization"] = `Token ${clToken}`;
          const resp = await fetch(
            `https://www.courtlistener.com/api/rest/v4/dockets/${docketId}/`,
            { headers, signal: AbortSignal.timeout(10000) },
          );
          if (resp.ok) {
            const data = await resp.json();
            if (data.absolute_url) {
              clOverrides[docketId] = `https://www.courtlistener.com${data.absolute_url}`;
            }
          }
        } catch {
          // skip unreachable CL API
        }
      }
      if (Object.keys(clOverrides).length > 0) {
        const clPath = resolve(cwd, "src", "data", "cl-overrides.json");
        writeFileSync(clPath, JSON.stringify(clOverrides, null, 2) + "\n", "utf-8");
        process.stdout.write(
          `--fix: wrote ${Object.keys(clOverrides).length} CL overrides to ${clPath}\n`,
        );
      }
    }
  }

  // Console summary
  process.stdout.write("\n--- Citation Health Report ---\n");
  process.stdout.write(`Total unique URLs: ${allUrls.length}\n`);
  process.stdout.write(`Checked: ${toCheck.length}\n`);
  process.stdout.write(`  Healthy: ${results.healthy.length}\n`);
  process.stdout.write(`  Broken: ${results.broken.length}\n`);
  process.stdout.write(`  Redirected: ${results.redirected.length}\n`);
  process.stdout.write(`  Unreachable: ${results.unreachable.length}\n`);
  process.stdout.write(`  Skipped (tier3): ${results.skipped}\n`);
  process.stdout.write(
    `  EFTA/jmail.world: ${results.tier4_sampled} sampled of ${results.tier4_total}\n`,
  );

  if (results.broken.length > 0) {
    process.stdout.write("\nBroken URLs:\n");
    for (const b of results.broken.slice(0, 20)) {
      process.stdout.write(`  [${b.status}] ${b.url} (${b.source})\n`);
    }
    if (results.broken.length > 20) {
      process.stdout.write(
        `  ... and ${results.broken.length - 20} more (see ${resultsPath})\n`,
      );
    }
  }

  if (results.unreachable.length > 0) {
    process.stdout.write("\nUnreachable URLs:\n");
    for (const u of results.unreachable.slice(0, 10)) {
      process.stdout.write(`  ${u.url}: ${u.error}\n`);
    }
  }

  process.stdout.write(`\nResults saved to ${resultsPath}\n`);
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
  process.exit(1);
});
