#!/usr/bin/env node
/**
 * MA Secretary of the Commonwealth corporate registry browser helper.
 *
 * Bypasses Incapsula/Imperva WAF using Playwright, then makes requests
 * from within the browser context and returns JSON.
 *
 * Uses persistent Chrome context so Incapsula clearance cookies persist
 * across invocations (challenge only needs solving once per ~30 min).
 *
 * Usage:
 *   node tools/_ma_browser_helper.js search "EPSTEIN"
 *   node tools/_ma_browser_helper.js search "EPSTEIN" --type F    # Full text
 *   node tools/_ma_browser_helper.js search-id 000487270
 *   node tools/_ma_browser_helper.js detail <sysvalue>
 *
 * Outputs JSON to stdout. Errors/progress to stderr.
 */

const path = require('path');
const os = require('os');
const fs = require('fs');

const BASE_URL = 'https://corp.sec.state.ma.us/corpweb/CorpSearch';
const USER_DATA_DIR = path.join(os.homedir(), '.cache', 'ma-corp-browser');

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
    const context = await chromium.launchPersistentContext(USER_DATA_DIR, {
        channel: 'chrome',
        headless: false,
        viewport: { width: 1280, height: 720 },
        args: ['--disable-blink-features=AutomationControlled'],
        ignoreDefaultArgs: ['--enable-automation'],
    });
    return context;
}

async function waitForIncapsula(page) {
    // Wait for Incapsula JS challenge to resolve.
    let prompted = false;
    for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(2000);
        const title = await page.title();
        const url = page.url();
        // Incapsula challenge pages have empty/generic titles
        if (title.includes('MA Corporations') || title.includes('CorpSearch')) {
            return true;
        }
        // Also check if the page has the search form
        const hasForm = await page.evaluate(() => {
            return !!document.getElementById('__VIEWSTATE');
        }).catch(() => false);
        if (hasForm) return true;

        if (i === 5 && !prompted) {
            process.stderr.write('\n  *** Incapsula challenge detected ***\n');
            process.stderr.write('  If a browser window opened, complete any challenge.\n');
            process.stderr.write('  Cookies will be cached for future requests.\n\n');
            prompted = true;
        }
        if (i % 10 === 0 && i > 0) {
            process.stderr.write(`  [${i * 2}s] still waiting for Incapsula...\n`);
        }
    }
    return false;
}

// ── Search by entity name ────────────────────────────────

async function doSearch(page, query, searchType = 'B', perPage = '100') {
    // searchType: B=Begins with, M=Exact match, F=Full text, S=Soundex
    const result = await page.evaluate(async (params) => {
        const { query, searchType, perPage, baseUrl } = params;

        // Step 1: GET search page for ViewState
        const getResp = await fetch(`${baseUrl}/CorpSearch.aspx`);
        const html = await getResp.text();

        const vs = html.match(/id="__VIEWSTATE"[^>]*value="([^"]*)"/);
        const vsg = html.match(/id="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"/);
        const ev = html.match(/id="__EVENTVALIDATION"[^>]*value="([^"]*)"/);
        const vse = html.match(/id="__VIEWSTATEENCRYPTED"[^>]*value="([^"]*)"/);

        if (!vs || !ev) return { error: 'Could not extract ViewState tokens' };

        // Step 2: POST search
        const body = new URLSearchParams();
        body.append('__LASTFOCUS', '');
        body.append('__EVENTTARGET', '');
        body.append('__EVENTARGUMENT', '');
        body.append('__VIEWSTATE', vs[1]);
        body.append('__VIEWSTATEGENERATOR', vsg ? vsg[1] : '');
        body.append('__SCROLLPOSITIONX', '0');
        body.append('__SCROLLPOSITIONY', '0');
        body.append('__VIEWSTATEENCRYPTED', vse ? vse[1] : '');
        body.append('__EVENTVALIDATION', ev[1]);
        body.append('ctl00$MainContent$hdnApplyMasterPageWitoutSidebar', '0');
        body.append('ctl00$MainContent$hdn1', '0');
        body.append('ctl00$MainContent$CorpSearch', 'rdoByEntityName');
        body.append('ctl00$MainContent$txtEntityName', query);
        body.append('ctl00$MainContent$ddBeginsWithEntityName', searchType);
        body.append('ctl00$MainContent$ddRecordsPerPage', perPage);
        body.append('ctl00$MainContent$btnSearch', 'Search Corporations');
        body.append('ctl00$MainContent$hdnW', '');
        body.append('ctl00$MainContent$hdnH', '');
        body.append('ctl00$MainContent$SearchControl$hdnRecordsPerPage', '');

        const postResp = await fetch(`${baseUrl}/CorpSearch.aspx`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
            redirect: 'follow'
        });

        const resultHtml = await postResp.text();

        // Step 3: Parse results
        const countMatch = resultHtml.match(/Number of records:\s*(\d+)/);
        const recordCount = countMatch ? parseInt(countMatch[1]) : 0;

        const results = [];
        // Entity search grid rows: <th> with <a href='CorpSummary...'> then <td> cells
        // HTML uses single quotes for href and class='link'
        const rowRegex = /<tr[^>]*>\s*<th[^>]*>\s*<a\s+href='CorpSummary\.aspx\?sysvalue=([^']+)'[^>]*>([^<]+)<\/a>\s*<\/th>\s*<td[^>]*>([^<]*)<\/td>\s*<td[^>]*>([^<]*)<\/td>\s*<td[^>]*>([\s\S]*?)<\/td>/g;
        let match;
        while ((match = rowRegex.exec(resultHtml)) !== null) {
            results.push({
                sysvalue: match[1],
                entity_name: match[2].trim(),
                id_number: match[3].trim(),
                old_id_number: match[4].trim(),
                address: match[5].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
            });
        }

        return { count: recordCount, results };
    }, { query, searchType, perPage, baseUrl: BASE_URL });

    return result;
}

// ── Search by ID number ──────────────────────────────────

async function doSearchById(page, idNumber) {
    const result = await page.evaluate(async (params) => {
        const { idNumber, baseUrl } = params;

        // GET search page
        const getResp = await fetch(`${baseUrl}/CorpSearch.aspx`);
        const html = await getResp.text();

        const vs = html.match(/id="__VIEWSTATE"[^>]*value="([^"]*)"/);
        const ev = html.match(/id="__EVENTVALIDATION"[^>]*value="([^"]*)"/);
        const vsg = html.match(/id="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"/);
        const vse = html.match(/id="__VIEWSTATEENCRYPTED"[^>]*value="([^"]*)"/);

        if (!vs || !ev) return { error: 'Could not extract ViewState' };

        const body = new URLSearchParams();
        body.append('__LASTFOCUS', '');
        body.append('__EVENTTARGET', '');
        body.append('__EVENTARGUMENT', '');
        body.append('__VIEWSTATE', vs[1]);
        body.append('__VIEWSTATEGENERATOR', vsg ? vsg[1] : '');
        body.append('__SCROLLPOSITIONX', '0');
        body.append('__SCROLLPOSITIONY', '0');
        body.append('__VIEWSTATEENCRYPTED', vse ? vse[1] : '');
        body.append('__EVENTVALIDATION', ev[1]);
        body.append('ctl00$MainContent$hdnApplyMasterPageWitoutSidebar', '0');
        body.append('ctl00$MainContent$hdn1', '0');
        body.append('ctl00$MainContent$CorpSearch', 'rdoByIdentification');
        body.append('ctl00$MainContent$txtIdentificationNumber', idNumber);
        body.append('ctl00$MainContent$ddRecordsPerPage', '25');
        body.append('ctl00$MainContent$btnSearch', 'Search Corporations');
        body.append('ctl00$MainContent$hdnW', '');
        body.append('ctl00$MainContent$hdnH', '');
        body.append('ctl00$MainContent$SearchControl$hdnRecordsPerPage', '');

        const postResp = await fetch(`${baseUrl}/CorpSearch.aspx`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
            redirect: 'follow'
        });

        const resultHtml = await postResp.text();
        const finalUrl = postResp.url;

        // ID search redirects directly to CorpSummary.aspx
        if (finalUrl.includes('CorpSummary.aspx')) {
            const sysMatch = finalUrl.match(/sysvalue=([^&]+)/);
            return { redirect: true, sysvalue: sysMatch ? sysMatch[1] : null, url: finalUrl };
        }

        // If no redirect, parse results table
        const results = [];
        const rowRegex = /<a\s+[^>]*href="CorpSummary\.aspx\?sysvalue=([^"]+)"[^>]*>([^<]+)<\/a>/g;
        let match;
        while ((match = rowRegex.exec(resultHtml)) !== null) {
            results.push({ sysvalue: match[1], entity_name: match[2].trim() });
        }

        return { redirect: false, results };
    }, { idNumber, baseUrl: BASE_URL });

    return result;
}

// ── Entity detail ────────────────────────────────────────

async function doDetail(page, sysvalue) {
    const result = await page.evaluate(async (params) => {
        const { sysvalue, baseUrl } = params;

        const resp = await fetch(`${baseUrl}/CorpSummary.aspx?sysvalue=${sysvalue}`);
        const html = await resp.text();

        // Parse fields by element ID
        const fields = {};
        const idMap = {
            'MainContent_lblEntityName': 'entity_name',
            'MainContent_lblEntityType': 'entity_type',
            'MainContent_lblIDNumber': 'id_number',
            'MainContent_lblOldIDNumber': 'old_id_number',
            'MainContent_lblOrganisationDate': 'organization_date',
            'MainContent_lblRevivalDate': 'revival_date',
            'MainContent_lblInactiveDate': 'inactive_date',
            'MainContent_lblInactiveDateLabel': 'inactive_date_label',
            'MainContent_lblCertainDate': 'last_date_certain',
            'MainContent_lblFiscalDateCurrent': 'fiscal_date',
            'MainContent_lblPrincipleStreet': 'principal_street',
            'MainContent_lblPrincipleCity': 'principal_city',
            'MainContent_lblPrincipleState': 'principal_state',
            'MainContent_lblPrincipleZip': 'principal_zip',
            'MainContent_lblPrincipleCountry': 'principal_country',
            'MainContent_lblResidentAgentName': 'agent_name',
            'MainContent_lblResidentStreet': 'agent_street',
            'MainContent_lblResidentCity': 'agent_city',
            'MainContent_lblResidentState': 'agent_state',
            'MainContent_lblResidentZip': 'agent_zip',
        };

        for (const [htmlId, key] of Object.entries(idMap)) {
            const regex = new RegExp(`id="${htmlId}"[^>]*>([^<]*)<`);
            const match = html.match(regex);
            if (match && match[1].trim()) {
                fields[key] = match[1].trim();
            }
        }

        // Publicly traded checkbox
        const pubTradeMatch = html.match(/id="MainContent_chkPubTrade"[^>]*(checked)/i);
        fields.publicly_traded = !!pubTradeMatch;

        // Officers table
        const officers = [];
        const officerTableMatch = html.match(/id="MainContent_grdOfficers"[\s\S]*?<\/table>/);
        if (officerTableMatch) {
            const rowRegex = /<tr[^>]*>\s*<th[^>]*>([^<]*)<\/th>\s*<td[^>]*>([^<]*)<\/td>\s*<td[^>]*>([\s\S]*?)<\/td>\s*<\/tr>/g;
            let oMatch;
            while ((oMatch = rowRegex.exec(officerTableMatch[0])) !== null) {
                officers.push({
                    title: oMatch[1].trim(),
                    name: oMatch[2].trim(),
                    address: oMatch[3].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
                });
            }
        }
        fields.officers = officers;

        // Name change history
        const nameChanges = [];
        const nameTableMatch = html.match(/id="MainContent_tblNameChange"[\s\S]*?<\/table>/);
        if (nameTableMatch) {
            const ncRegex = /The name was changed from:\s*(.+?)\s+on\s+(\d{2}-\d{2}-\d{4})/g;
            let ncMatch;
            while ((ncMatch = ncRegex.exec(nameTableMatch[0])) !== null) {
                nameChanges.push({
                    from_name: ncMatch[1].trim(),
                    date: ncMatch[2]
                });
            }
        }
        fields.name_changes = nameChanges;

        return fields;
    }, { sysvalue, baseUrl: BASE_URL });

    return result;
}

// ── Main ─────────────────────────────────────────────────

async function main() {
    const args = process.argv.slice(2);
    const command = args[0];

    if (!command) {
        process.stderr.write('Usage: node _ma_browser_helper.js <search|search-id|detail> <args...>\n');
        process.stderr.write('  search <query> [--type B|M|F|S]  Search by entity name\n');
        process.stderr.write('  search-id <id_number>            Search by ID (goes to detail)\n');
        process.stderr.write('  detail <sysvalue>                Get entity detail page\n');
        process.exit(1);
    }

    const context = await launchBrowser();
    const page = context.pages()[0] || await context.newPage();

    try {
        // Navigate to search page to trigger Incapsula
        await page.goto(`${BASE_URL}/CorpSearch.aspx`, { waitUntil: 'load', timeout: 30000 });
        const ready = await waitForIncapsula(page);
        if (!ready) throw new Error('Incapsula challenge did not resolve after 120s');
        await page.waitForTimeout(1000);

        let result;
        switch (command) {
            case 'search': {
                const query = args[1];
                if (!query) { process.stderr.write('Error: query required\n'); process.exit(1); }
                const typeIdx = args.indexOf('--type');
                const searchType = typeIdx !== -1 && args[typeIdx + 1] ? args[typeIdx + 1] : 'B';
                result = await doSearch(page, query, searchType);
                break;
            }
            case 'search-id': {
                const id = args[1];
                if (!id) { process.stderr.write('Error: id_number required\n'); process.exit(1); }
                const idResult = await doSearchById(page, id);

                // If ID search redirected to detail, fetch the detail
                if (idResult.redirect && idResult.sysvalue) {
                    result = await doDetail(page, idResult.sysvalue);
                    result._search_method = 'id_redirect';
                    result._sysvalue = idResult.sysvalue;
                } else {
                    result = idResult;
                }
                break;
            }
            case 'detail': {
                const sv = args[1];
                if (!sv) { process.stderr.write('Error: sysvalue required\n'); process.exit(1); }
                result = await doDetail(page, sv);
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
    } finally {
        await context.close();
    }
}

main();
