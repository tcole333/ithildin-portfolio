#!/usr/bin/env node
/**
 * MI LARA Business Registry browser helper — bypasses Cloudflare WAF
 * using Playwright, then makes API calls and returns JSON.
 *
 * Uses persistent Chrome context so Cloudflare clearance cookies persist
 * across invocations (challenge only needs solving once per ~30 min).
 *
 * Usage:
 *   node tools/_mi_browser_helper.js search "EPSTEIN"
 *   node tools/_mi_browser_helper.js search "EPSTEIN" --contains
 *   node tools/_mi_browser_helper.js detail 85956
 *   node tools/_mi_browser_helper.js history 802112570
 *   node tools/_mi_browser_helper.js full 85956 802112570
 *
 * Outputs JSON to stdout. Errors/progress to stderr.
 */

const path = require('path');
const os = require('os');
const fs = require('fs');

const BASE_URL = 'https://mibusinessregistry.lara.state.mi.us';
const USER_DATA_DIR = path.join(os.homedir(), '.cache', 'mi-lara-browser');

let chromium;
try {
    chromium = require('playwright').chromium;
} catch {
    try {
        chromium = require('playwright-core').chromium;
    } catch {
        process.stderr.write('ERROR: playwright package not found. Install with: npm i playwright\n');
        process.exit(1);
    }
}

async function launchBrowser() {
    fs.mkdirSync(USER_DATA_DIR, { recursive: true });

    // Use system Chrome via channel (avoids automation detection flags).
    // Cloudflare needs a real browser to solve JS challenges.
    // channel: 'chrome' uses installed Google Chrome with fewer automation markers.
    const context = await chromium.launchPersistentContext(USER_DATA_DIR, {
        channel: 'chrome',
        headless: false,
        viewport: { width: 1280, height: 720 },
        args: [
            '--disable-blink-features=AutomationControlled',
        ],
        ignoreDefaultArgs: ['--enable-automation'],
    });
    return context;
}

async function waitForCloudflare(page) {
    // Wait for Cloudflare challenge to resolve.
    // On first visit, user may need to interact (click checkbox).
    // On subsequent visits with cached cookies, it auto-resolves.
    let prompted = false;
    for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(2000);
        const title = await page.title();
        const url = page.url();
        if (!title.includes('Just a moment') && !title.includes('Checking') &&
            !url.includes('challenge') && title.length > 0) {
            return true;
        }
        if (i === 5 && !prompted) {
            process.stderr.write('\n  *** Cloudflare challenge detected ***\n');
            process.stderr.write('  If a browser window opened, click "Verify you are human"\n');
            process.stderr.write('  Cookies will be cached for future requests.\n\n');
            prompted = true;
        }
        if (i % 10 === 0 && i > 0) {
            process.stderr.write(`  [${i * 2}s] still waiting for Cloudflare...\n`);
        }
    }
    return false;
}

async function apiCall(page, endpoint) {
    // Make an API call from within the browser context (bypasses Cloudflare)
    const result = await page.evaluate(async (url) => {
        try {
            const resp = await fetch(url);
            if (!resp.ok) return { error: `HTTP ${resp.status}`, status: resp.status };
            return await resp.json();
        } catch (e) {
            return { error: e.message };
        }
    }, endpoint);
    return result;
}

async function doSearch(query, queryType = 2) {
    const context = await launchBrowser();
    const page = context.pages()[0] || await context.newPage();

    try {
        await page.goto(`${BASE_URL}/search/business`, { waitUntil: 'load', timeout: 30000 });
        const ready = await waitForCloudflare(page);
        if (!ready) throw new Error('Cloudflare challenge did not resolve after 40s');

        // Wait for Angular app to hydrate
        await page.waitForTimeout(2000);

        const endpoint = `/api/businessEntitySearch/webSearch?SEARCH_VALUE=${encodeURIComponent(query)}&SearchByOption=-1&QueryTypeId=${queryType}&BusinessRecordTypeId=0&BusinessStatusTypeId=0`;
        return await apiCall(page, endpoint);
    } finally {
        await context.close();
    }
}

async function doDetail(internalId) {
    const context = await launchBrowser();
    const page = context.pages()[0] || await context.newPage();

    try {
        await page.goto(`${BASE_URL}/search/business`, { waitUntil: 'load', timeout: 30000 });
        const ready = await waitForCloudflare(page);
        if (!ready) throw new Error('Cloudflare challenge did not resolve');

        await page.waitForTimeout(2000);

        const detail = await apiCall(page, `/api/FilingDetail/business/${internalId}/false`);
        const assumed = await apiCall(page, `/api/FilingDetail/business/assumedNameHistory/${internalId}`);

        return { detail, assumed_names: assumed };
    } finally {
        await context.close();
    }
}

async function doHistory(filingNumber) {
    const context = await launchBrowser();
    const page = context.pages()[0] || await context.newPage();

    try {
        await page.goto(`${BASE_URL}/search/business`, { waitUntil: 'load', timeout: 30000 });
        const ready = await waitForCloudflare(page);
        if (!ready) throw new Error('Cloudflare challenge did not resolve');

        await page.waitForTimeout(2000);

        return await apiCall(page, `/api/History/business/${filingNumber}`);
    } finally {
        await context.close();
    }
}

async function doFull(internalId, filingNumber) {
    // Get detail + history + assumed names in one browser session
    const context = await launchBrowser();
    const page = context.pages()[0] || await context.newPage();

    try {
        await page.goto(`${BASE_URL}/search/business`, { waitUntil: 'load', timeout: 30000 });
        const ready = await waitForCloudflare(page);
        if (!ready) throw new Error('Cloudflare challenge did not resolve');

        await page.waitForTimeout(2000);

        const detail = await apiCall(page, `/api/FilingDetail/business/${internalId}/false`);
        const history = await apiCall(page, `/api/History/business/${filingNumber}`);
        const assumed = await apiCall(page, `/api/FilingDetail/business/assumedNameHistory/${internalId}`);

        return { detail, history, assumed_names: assumed };
    } finally {
        await context.close();
    }
}

async function main() {
    const args = process.argv.slice(2);
    const command = args[0];

    if (!command) {
        process.stderr.write('Usage: node _mi_browser_helper.js <search|detail|history|full> <args...>\n');
        process.stderr.write('  search <query> [--contains]  Search by entity name\n');
        process.stderr.write('  detail <internal_id>         Get entity detail\n');
        process.stderr.write('  history <filing_number>      Get filing history\n');
        process.stderr.write('  full <internal_id> <filing_number>  Get detail + history\n');
        process.exit(1);
    }

    try {
        let result;
        switch (command) {
            case 'search': {
                const query = args[1];
                if (!query) { process.stderr.write('Error: query required\n'); process.exit(1); }
                const queryType = args.includes('--contains') ? 2 : 1; // 1=StartsWith, 2=Contains
                result = await doSearch(query, queryType);
                break;
            }
            case 'detail': {
                const id = args[1];
                if (!id) { process.stderr.write('Error: internal_id required\n'); process.exit(1); }
                result = await doDetail(id);
                break;
            }
            case 'history': {
                const num = args[1];
                if (!num) { process.stderr.write('Error: filing_number required\n'); process.exit(1); }
                result = await doHistory(num);
                break;
            }
            case 'full': {
                const id = args[1];
                const num = args[2];
                if (!id || !num) { process.stderr.write('Error: internal_id and filing_number required\n'); process.exit(1); }
                result = await doFull(id, num);
                break;
            }
            default:
                process.stderr.write(`Unknown command: ${command}\n`);
                process.exit(1);
        }

        process.stdout.write(JSON.stringify(result));
    } catch (err) {
        process.stderr.write(`Error: ${err.message}\n`);
        process.exit(1);
    }
}

main();
