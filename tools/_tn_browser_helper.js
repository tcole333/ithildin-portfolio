#!/usr/bin/env node
/**
 * TN Secretary of State (TNCaB) browser helper — handles Cloudflare Turnstile
 * challenge via Playwright headless Chrome, then queries the Kendo Grid API.
 *
 * The TNCaB portal at tncab.tnsos.gov uses Cloudflare Turnstile for bot
 * protection. Once Turnstile auto-resolves in the browser, the Kendo Grid
 * search endpoint returns results. Entity detail is loaded in a modal dialog.
 *
 * Uses persistent Chrome context so Turnstile clearance persists across
 * invocations (~5 min cache).
 *
 * Usage:
 *   node tools/_tn_browser_helper.js search "FISHBOWL SPIRITS"
 *   node tools/_tn_browser_helper.js search "CHESNEY" --active-only
 *   node tools/_tn_browser_helper.js entity 001338859
 *   node tools/_tn_browser_helper.js detail 1774209
 *
 * Outputs JSON to stdout. Errors/progress to stderr.
 */

const path = require('path');
const os = require('os');
const fs = require('fs');

const SEARCH_URL = 'https://tncab.tnsos.gov/business-entity-search';
const USER_DATA_DIR = path.join(os.homedir(), '.cache', 'tn-sos-browser');

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

    // Cloudflare Turnstile requires a real-looking browser.
    // Use system Chrome via channel for best compatibility.
    // Falls back to bundled Chromium if Chrome is not installed.
    let context;
    try {
        context = await chromium.launchPersistentContext(USER_DATA_DIR, {
            channel: 'chrome',
            headless: false,
            viewport: { width: 1280, height: 720 },
            args: [
                '--disable-blink-features=AutomationControlled',
                '--window-position=-2400,-2400', // Off-screen
            ],
            ignoreDefaultArgs: ['--enable-automation'],
        });
    } catch {
        // Chrome not installed, try Chromium headless with new headless mode
        process.stderr.write('  Chrome not available, trying Chromium headless=new\n');
        context = await chromium.launchPersistentContext(USER_DATA_DIR, {
            headless: true,
            viewport: { width: 1280, height: 720 },
            args: [
                '--disable-blink-features=AutomationControlled',
                '--headless=new',
            ],
            ignoreDefaultArgs: ['--enable-automation'],
        });
    }
    return context;
}

async function waitForTurnstile(page, maxWaitSec = 30) {
    // Wait for Turnstile to auto-solve. The page sets
    // sessionStorage["recaptcha_valid"] = "true" when done.
    for (let i = 0; i < maxWaitSec; i++) {
        await page.waitForTimeout(1000);
        const valid = await page.evaluate(() => sessionStorage.getItem('recaptcha_valid'));
        if (valid === 'true') {
            process.stderr.write('  Turnstile verified\n');
            return true;
        }
        if (i === 10) {
            process.stderr.write('  Waiting for Turnstile challenge...\n');
        }
    }
    // Even if sessionStorage doesn't show valid, the search might work
    process.stderr.write('  Turnstile verification not confirmed, attempting search anyway\n');
    return false;
}

async function getSearchPage(context) {
    const page = context.pages()[0] || await context.newPage();
    await page.goto(SEARCH_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Wait for the page to load and Kendo to initialize
    await page.waitForSelector('input[id^="Name_"]', { timeout: 15000 });
    await waitForTurnstile(page);

    return page;
}

async function doSearch(query, activeOnly = false) {
    const context = await launchBrowser();
    try {
        const page = await getSearchPage(context);

        // Use Playwright's native methods to fill the form
        const nameInput = page.locator('input[id^="Name_"]');
        await nameInput.fill(query);

        if (activeOnly) {
            const checkbox = page.locator('input[id^="ActiveOnly_"]');
            await checkbox.check();
        }

        // Click the search button using Playwright
        const searchBtn = page.locator('button[title="Search"]');
        await searchBtn.click();

        // Wait for the grid to appear and populate
        await page.waitForSelector('[data-role="grid"]', { timeout: 15000 }).catch(() => {});

        // Wait for the AJAX request to complete by watching for grid rows or total
        // Poll the Kendo Grid dataSource for results
        const results = await page.evaluate(async () => {
            return new Promise((resolve) => {
                const poll = (attempts) => {
                    if (attempts <= 0) {
                        // Return whatever we have
                        const g = document.querySelector('[data-role="grid"]');
                        if (g && $ && $(g).data('kendoGrid')) {
                            const grid = $(g).data('kendoGrid');
                            resolve({ data: grid.dataSource.data().toJSON(), total: grid.dataSource.total() });
                        } else {
                            resolve({ data: [], total: 0 });
                        }
                        return;
                    }
                    const g = document.querySelector('[data-role="grid"]');
                    if (g && $ && $(g).data('kendoGrid')) {
                        const grid = $(g).data('kendoGrid');
                        const data = grid.dataSource.data().toJSON();
                        const total = grid.dataSource.total();
                        // If we have data or the total is definitively 0 (not just unloaded)
                        if (data.length > 0) {
                            resolve({ data, total });
                            return;
                        }
                        // Check if the grid loading indicator is gone
                        const loading = g.querySelector('.k-loading-mask');
                        if (!loading && total >= 0) {
                            // Give it one more second to be sure
                            setTimeout(() => {
                                const finalData = grid.dataSource.data().toJSON();
                                const finalTotal = grid.dataSource.total();
                                resolve({ data: finalData, total: finalTotal });
                            }, 1000);
                            return;
                        }
                    }
                    setTimeout(() => poll(attempts - 1), 500);
                };
                // Start polling after a brief delay to let the AJAX fire
                setTimeout(() => poll(30), 1500);
            });
        });

        return results;
    } finally {
        await context.close();
    }
}

async function doSearchByControlNumber(controlNumber) {
    const context = await launchBrowser();
    try {
        const page = await getSearchPage(context);

        // Clear name field, fill control number
        const nameInput = page.locator('input[id^="Name_"]');
        await nameInput.fill('');
        const fileInput = page.locator('input[id^="Filenumber_"]');
        await fileInput.fill(controlNumber);

        // Click search
        const searchBtn = page.locator('button[title="Search"]');
        await searchBtn.click();

        await page.waitForSelector('[data-role="grid"]', { timeout: 15000 }).catch(() => {});

        const results = await page.evaluate(async () => {
            return new Promise((resolve) => {
                const poll = (attempts) => {
                    if (attempts <= 0) {
                        const g = document.querySelector('[data-role="grid"]');
                        if (g && $ && $(g).data('kendoGrid')) {
                            const grid = $(g).data('kendoGrid');
                            resolve({ data: grid.dataSource.data().toJSON(), total: grid.dataSource.total() });
                        } else {
                            resolve({ data: [], total: 0 });
                        }
                        return;
                    }
                    const g = document.querySelector('[data-role="grid"]');
                    if (g && $ && $(g).data('kendoGrid')) {
                        const grid = $(g).data('kendoGrid');
                        const data = grid.dataSource.data().toJSON();
                        if (data.length > 0) {
                            resolve({ data, total: grid.dataSource.total() });
                            return;
                        }
                        const loading = g.querySelector('.k-loading-mask');
                        if (!loading) {
                            setTimeout(() => {
                                const finalData = grid.dataSource.data().toJSON();
                                resolve({ data: finalData, total: grid.dataSource.total() });
                            }, 1000);
                            return;
                        }
                    }
                    setTimeout(() => poll(attempts - 1), 500);
                };
                setTimeout(() => poll(30), 1500);
            });
        });

        return results;
    } finally {
        await context.close();
    }
}

async function doDetail(entityId) {
    // entityId is the internal Id from search results (e.g., "1774209")
    // We need to search for it first, click Details, and parse the dialog
    const context = await launchBrowser();
    try {
        const page = await getSearchPage(context);

        // We need the Id_encrypted value to call the detail endpoint
        // Instead, we'll click the Details button in the grid after searching by control number
        // Actually, the detail endpoint takes parameters: id, widgetConfig, encoded
        // Let's extract the detail by clicking the button programmatically

        const detail = await page.evaluate(async ({ id }) => {
            return new Promise((resolve, reject) => {
                // Find the detail settings hidden input to get widgetConfig
                const detailSettings = document.querySelector('input[id^="DetailsSettings_"]');
                if (!detailSettings) {
                    reject('Detail settings not found');
                    return;
                }
                const widgetConfig = detailSettings.value;

                // Get CSRF token
                const csrfCookie = document.cookie.split(';')
                    .map(c => c.trim())
                    .find(c => c.startsWith('TNSOSGOV-TNCAB-Portal-Auth-Production-CSRF='));
                const csrfToken = csrfCookie ? csrfCookie.split('=').slice(1).join('=') : '';

                // Call the detail endpoint
                const params = new URLSearchParams();
                params.append('id', id);
                params.append('widgetConfig', widgetConfig);
                params.append('encoded', 'true');

                fetch('/portal/RegistrationSearch/Details', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-Requested-With': 'XMLHttpRequest',
                        'X-Valence-CSRF-Token': csrfToken,
                    },
                    body: params.toString(),
                })
                .then(resp => resp.text())
                .then(html => {
                    // Parse the HTML to extract detail fields
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');

                    const result = { raw_html_length: html.length };

                    // Extract the entity name (h2)
                    const h2 = doc.querySelector('h2');
                    if (h2) result.entity_name = h2.textContent.trim();

                    // Extract all h4 elements for key-value pairs
                    const h4s = doc.querySelectorAll('h4');
                    const fields = {};
                    const sections = [];
                    let currentSection = null;
                    let sectionItems = [];

                    for (const h4 of h4s) {
                        const text = h4.textContent.trim();
                        if (text.includes(':')) {
                            const colonIdx = text.indexOf(':');
                            const key = text.substring(0, colonIdx).trim();
                            const value = text.substring(colonIdx + 1).trim();
                            if (currentSection) {
                                sectionItems.push({ key, value });
                            } else {
                                fields[key] = value;
                            }
                        } else if (text === 'Registered Agent' || text === 'Principal Office Address' ||
                                   text === 'Mailing Address') {
                            if (currentSection) {
                                sections.push({ section: currentSection, items: sectionItems });
                            }
                            currentSection = text;
                            sectionItems = [];
                        } else if (text && currentSection) {
                            sectionItems.push({ line: text });
                        } else if (text) {
                            fields['_unnamed_' + Object.keys(fields).length] = text;
                        }
                    }
                    if (currentSection) {
                        sections.push({ section: currentSection, items: sectionItems });
                    }

                    result.fields = fields;
                    result.sections = sections;

                    // Extract filing history from any grid/table in the detail
                    const rows = doc.querySelectorAll('tr');
                    const history = [];
                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 2) {
                            const record = {};
                            for (let i = 0; i < cells.length; i++) {
                                record['col_' + i] = cells[i].textContent.trim();
                            }
                            history.push(record);
                        }
                    }
                    if (history.length > 0) result.history = history;

                    resolve(result);
                })
                .catch(err => reject(err.message));
            });
        }, { id: entityId });

        return detail;
    } finally {
        await context.close();
    }
}

async function doEntityByControlNumber(controlNumber) {
    // Search by control number, get the entity detail
    const context = await launchBrowser();
    try {
        const page = await getSearchPage(context);

        // Use Playwright's fill/click to search by control number
        const nameInput = page.locator('input[id^="Name_"]');
        await nameInput.fill('');
        const fileInput = page.locator('input[id^="Filenumber_"]');
        await fileInput.fill(controlNumber);
        const searchBtn = page.locator('button[title="Search"]');
        await searchBtn.click();

        await page.waitForSelector('[data-role="grid"]', { timeout: 15000 }).catch(() => {});

        // Poll for search results
        const searchResult = await page.evaluate(async () => {
            return new Promise((resolve) => {
                const poll = (attempts) => {
                    if (attempts <= 0) { resolve(null); return; }
                    const g = document.querySelector('[data-role="grid"]');
                    if (g && $ && $(g).data('kendoGrid')) {
                        const grid = $(g).data('kendoGrid');
                        const data = grid.dataSource.data().toJSON();
                        if (data.length > 0) {
                            resolve(data[0]);
                            return;
                        }
                        const loading = g.querySelector('.k-loading-mask');
                        if (!loading) {
                            setTimeout(() => {
                                const finalData = grid.dataSource.data().toJSON();
                                resolve(finalData.length > 0 ? finalData[0] : null);
                            }, 1000);
                            return;
                        }
                    }
                    setTimeout(() => poll(attempts - 1), 500);
                };
                setTimeout(() => poll(30), 1500);
            });
        });

        if (!searchResult) {
            return { error: `No entity found for control number ${controlNumber}` };
        }

        // Now get the detail using the entity's Id
        const entityId = searchResult.Id;

        // Click the Details button to load the detail dialog
        const detail = await page.evaluate(async ({ id }) => {
            return new Promise((resolve, reject) => {
                const detailSettings = document.querySelector('input[id^="DetailsSettings_"]');
                if (!detailSettings) { reject('Detail settings not found'); return; }
                const widgetConfig = detailSettings.value;

                const csrfCookie = document.cookie.split(';')
                    .map(c => c.trim())
                    .find(c => c.startsWith('TNSOSGOV-TNCAB-Portal-Auth-Production-CSRF='));
                const csrfToken = csrfCookie ? csrfCookie.split('=').slice(1).join('=') : '';

                const params = new URLSearchParams();
                params.append('id', id);
                params.append('widgetConfig', widgetConfig);
                params.append('encoded', 'true');

                fetch('/portal/RegistrationSearch/Details', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-Requested-With': 'XMLHttpRequest',
                        'X-Valence-CSRF-Token': csrfToken,
                    },
                    body: params.toString(),
                })
                .then(resp => resp.text())
                .then(html => {
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');
                    const result = {};

                    const h2 = doc.querySelector('h2');
                    if (h2) result.entity_name = h2.textContent.trim();

                    const h4s = doc.querySelectorAll('h4');
                    const fields = {};
                    const addresses = {};
                    let currentSection = null;
                    let addressLines = [];

                    for (const h4 of h4s) {
                        const text = h4.textContent.trim();
                        if (!text) continue;

                        if (text === 'Registered Agent' || text === 'Principal Office Address' ||
                            text === 'Mailing Address') {
                            if (currentSection && addressLines.length > 0) {
                                addresses[currentSection] = addressLines;
                            }
                            currentSection = text;
                            addressLines = [];
                        } else if (text.includes(':')) {
                            if (currentSection && addressLines.length > 0) {
                                addresses[currentSection] = addressLines;
                                currentSection = null;
                                addressLines = [];
                            }
                            const colonIdx = text.indexOf(':');
                            const key = text.substring(0, colonIdx).trim();
                            const value = text.substring(colonIdx + 1).trim();
                            fields[key] = value;
                        } else if (currentSection) {
                            addressLines.push(text);
                        }
                    }
                    if (currentSection && addressLines.length > 0) {
                        addresses[currentSection] = addressLines;
                    }

                    result.fields = fields;
                    result.addresses = addresses;

                    // Extract history from tabstrip if present
                    const historyRows = doc.querySelectorAll('table tr');
                    const history = [];
                    for (const row of historyRows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 2) {
                            const type = cells[0] ? cells[0].textContent.trim() : '';
                            const date = cells[1] ? cells[1].textContent.trim() : '';
                            const trackNum = cells[2] ? cells[2].textContent.trim() : '';
                            const changes = cells[3] ? cells[3].textContent.trim() : '';
                            if (type) history.push({ type, date, tracking_number: trackNum, changes });
                        }
                    }
                    if (history.length > 0) result.history = history;

                    resolve(result);
                })
                .catch(err => reject(err.message));
            });
        }, { id: entityId });

        // Merge search data with detail data
        return {
            ...detail,
            search_data: searchResult,
        };
    } finally {
        await context.close();
    }
}

async function main() {
    const args = process.argv.slice(2);
    const command = args[0];

    if (!command) {
        process.stderr.write('Usage: node _tn_browser_helper.js <command> <args...>\n');
        process.stderr.write('  search <name> [--active-only]    Search by entity name\n');
        process.stderr.write('  entity <control_number>          Get entity detail by control number\n');
        process.stderr.write('  detail <internal_id>             Get detail by internal ID\n');
        process.exit(1);
    }

    try {
        let result;
        switch (command) {
            case 'search': {
                const query = args[1];
                if (!query) { process.stderr.write('Error: search query required\n'); process.exit(1); }
                const activeOnly = args.includes('--active-only');
                result = await doSearch(query, activeOnly);
                break;
            }
            case 'entity': {
                const controlNum = args[1];
                if (!controlNum) { process.stderr.write('Error: control number required\n'); process.exit(1); }
                result = await doEntityByControlNumber(controlNum);
                break;
            }
            case 'detail': {
                const id = args[1];
                if (!id) { process.stderr.write('Error: entity ID required\n'); process.exit(1); }
                result = await doDetail(id);
                break;
            }
            default:
                process.stderr.write(`Unknown command: ${command}\n`);
                process.exit(1);
        }

        process.stdout.write(JSON.stringify(result));
    } catch (err) {
        process.stderr.write(`Error: ${err.message || err}\n`);
        process.stdout.write(JSON.stringify({ error: String(err.message || err) }));
        process.exit(1);
    }
}

main();
