#!/usr/bin/env python3
"""
California Secretary of State — bizfileonline web API tool.

KNOWN LIMITATION: Imperva WAF intermittently blocks API calls after the first
request in a session. Works for single lookups when MCP Chrome has a fresh page
load, but unreliable for batch operations. Prefer ingest_california.py with
CA_SOS_API_KEY when available.

Drives the CA SoS bizfileonline.sos.ca.gov Angular UI via CDP connection to
MCP Playwright's Chrome browser. Imperva WAF blocks direct HTTP and page.evaluate
fetch calls; only requests routed through Angular's HttpClient pass.

Requires: MCP Playwright server running (provides Chrome with valid Imperva session).

Usage:
    python tools/query_california.py search "PARAFI CAPITAL"
    python tools/query_california.py search "Epstein" --type corp
    python tools/query_california.py search "Apollo" --officer-last "BLACK"
    python tools/query_california.py entity 7175908
    python tools/query_california.py entity C0726332 --history
    python tools/query_california.py history C0726332
    python tools/query_california.py ingest 7175908
    python tools/query_california.py ingest-search "Epstein" --limit 50
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import log_search
except ImportError:
    try:
        from lead_tracker import log_search
    except ImportError:
        def log_search(*a, **kw):
            pass

try:
    from tools.query_registry import get_db, _rebuild_fts
except ImportError:
    from query_registry import get_db, _rebuild_fts


# ══════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════

BASE_URL = "https://bizfileonline.sos.ca.gov"

SEARCH_TYPE_MAP = {
    "keyword": "1",
    "number": "4",
    "officer": "1",
}

STATUS_MAP_FILTER = {
    "all": "",
    "active": "A",
    "suspended": "S",
    "dissolved": "D",
    "forfeited": "F",
    "surrendered": "U",
    "cancelled": "N",
}

FILING_TYPE_MAP = {
    "all": "",
    "corp": "SI_CORP",
    "llc": "SI_LLC",
    "lp": "SI_LP",
}

TYPE_MAP = {
    "stock corporation - ca": "corp",
    "stock corporation - out of state": "foreign_corp",
    "domestic stock corporation": "corp",
    "foreign stock corporation": "foreign_corp",
    "limited liability company - ca": "llc",
    "limited liability company - out of state": "foreign_llc",
    "domestic limited liability company": "llc",
    "foreign limited liability company": "foreign_llc",
    "limited partnership - ca": "lp",
    "limited partnership - out of state": "foreign_lp",
    "domestic limited partnership": "lp",
    "foreign limited partnership": "foreign_lp",
    "limited liability partnership": "llp",
    "general partnership": "gp",
    "nonprofit corporation - ca": "nonprofit",
    "nonprofit corporation - out of state": "foreign_nonprofit",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "corporation - ca": "corp",
    "corporation - out of state": "foreign_corp",
    "stock corporation - ca - general": "corp",
    "stock corporation - ca - close": "corp",
    "stock corporation - ca - professional": "prof_corp",
    "stock corporation - out of state - general": "foreign_corp",
    "stock corporation - out of state - professional": "foreign_prof_corp",
    "nonprofit corporation - ca - mutual benefit": "nonprofit",
    "nonprofit corporation - ca - public benefit": "nonprofit",
    "nonprofit corporation - ca - religious": "nonprofit",
    "nonprofit corporation - out of state - mutual benefit": "foreign_nonprofit",
    "nonprofit corporation - out of state - public benefit": "foreign_nonprofit",
    "nonprofit corporation - out of state - religious": "foreign_nonprofit",
    "limited liability company - ca - general": "llc",
    "limited liability company - ca - professional": "prof_llc",
    "limited liability company - out of state - general": "foreign_llc",
    "limited liability company - out of state - professional": "foreign_prof_llc",
    "limited partnership - ca - general": "lp",
    "limited partnership - out of state - general": "foreign_lp",
    "limited liability partnership - ca": "llp",
    "limited liability partnership - out of state": "foreign_llp",
}

REGISTRY_STATUS_MAP = {
    "active": "active",
    "suspended": "suspended",
    "suspended - sos": "suspended",
    "suspended - ftb": "suspended",
    "suspended - sos & ftb": "suspended",
    "dissolved": "dissolved",
    "forfeited": "forfeited",
    "surrendered": "surrendered",
    "cancelled": "cancelled",
    "merged out": "inactive",
    "converted out": "inactive",
    "withdrawn": "inactive",
    "converted": "inactive",
    "merged": "inactive",
}


# ══════════════════════════════════════════════════════════
# CDP BROWSER SESSION (connects to MCP Playwright Chrome)
# ══════════════════════════════════════════════════════════

_pw_instance = None
_cdp_browser = None
_page = None


def _find_cdp_port():
    """Find MCP Chrome's CDP port from process list."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    for line in result.stdout.split("\n"):
        if "Google Chrome" in line and "mcp-chrome" in line:
            m = re.search(r"--remote-debugging-port=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _wait_for_app(page, max_wait=15):
    """Wait for Angular app to load (past Imperva challenge)."""
    for i in range(max_wait):
        try:
            content = page.content()
        except Exception:
            time.sleep(1)
            continue
        if "app-root" in content or "Business Search" in content:
            return True
        time.sleep(1)
        if i % 5 == 4:
            print(f"  Waiting for app ({i+1}s)...", file=sys.stderr)
    return False


def _get_page():
    """Connect to MCP Chrome via CDP and reuse existing page or create a new one.

    Prefers reusing an existing bizfileonline page from MCP Chrome's context
    to avoid Imperva re-challenge issues that occur when opening new tabs.
    Falls back to creating a new page if none exist.
    """
    global _pw_instance, _cdp_browser, _page
    if _page is not None:
        return _page

    port = _find_cdp_port()
    if not port:
        print(
            "ERROR: MCP Playwright Chrome not found.\n"
            "  This tool requires the Playwright MCP server's Chrome browser.\n"
            "  Trigger it first with any browser_navigate or browser_snapshot call.",
            file=sys.stderr,
        )
        sys.exit(1)

    from playwright.sync_api import sync_playwright

    _pw_instance = sync_playwright().start()
    _cdp_browser = _pw_instance.chromium.connect_over_cdp(
        f"http://127.0.0.1:{port}"
    )
    ctx = _cdp_browser.contexts[0]

    # Prefer reusing the existing bizfileonline page (avoids Imperva re-challenge)
    for p in ctx.pages:
        if "bizfileonline.sos.ca.gov" in (p.url or ""):
            _page = p
            print("  Connected to existing bizfile page", file=sys.stderr)
            return _page

    # Fallback: use first available page, or create new
    if ctx.pages:
        _page = ctx.pages[0]
        print("  Using existing browser page", file=sys.stderr)
    else:
        _page = ctx.new_page()
        print("  Created new browser page", file=sys.stderr)
    return _page


def _cleanup():
    """Disconnect from CDP (does NOT close MCP's browser or page)."""
    global _pw_instance, _cdp_browser, _page
    # Do NOT close the page — it may be MCP Playwright's page that we're reusing.
    # Only disconnect the CDP connection.
    if _cdp_browser:
        try:
            _cdp_browser.close()
        except Exception:
            pass
    if _pw_instance:
        try:
            _pw_instance.stop()
        except Exception:
            pass
    _pw_instance = _cdp_browser = _page = None


def _ensure_search_page(page, force_reload=False):
    """Navigate to search page if not already there.

    force_reload: If True, always navigate even if already on the page.
    Useful when Imperva challenge needs to be re-passed.
    """
    url = page.url
    if force_reload or "/search/business" not in url or "/search/business/" in url:
        page.goto(
            f"{BASE_URL}/search/business", wait_until="load", timeout=30000
        )
        if not _wait_for_app(page):
            raise RuntimeError("CA bizfile app did not load")
        time.sleep(1.5)


# ══════════════════════════════════════════════════════════
# UI-DRIVEN API INTERACTION
# ══════════════════════════════════════════════════════════

def _ui_search(page, query, status="all", filing_type="all",
               officer_first="", officer_middle="", officer_last=""):
    """Drive search UI, optionally intercepting to patch filter fields.

    The Angular app only supports keyword search (SEARCH_TYPE_ID=1).
    Changing SEARCH_TYPE_ID causes server-side 500 errors, so we leave
    it alone and rely on keyword matching for both names and entity numbers.

    For status/filing_type/officer filters, we intercept and patch the
    POST body since those fields work reliably.
    """
    _ensure_search_page(page)

    need_interception = (
        status != "all"
        or filing_type != "all"
        or officer_first or officer_middle or officer_last
    )

    def _intercept(route):
        if not need_interception:
            route.continue_()
            return
        try:
            body = json.loads(route.request.post_data)
            if status != "all":
                body["STATUS_ID"] = STATUS_MAP_FILTER.get(status, "")
            if filing_type != "all":
                body["FILING_TYPE_ID"] = FILING_TYPE_MAP.get(filing_type, "")
            if officer_first or officer_middle or officer_last:
                body["OFFICER_OBJECT"] = {
                    "FIRST_NAME": officer_first,
                    "MIDDLE_NAME": officer_middle,
                    "LAST_NAME": officer_last,
                }
            route.continue_(post_data=json.dumps(body))
        except Exception:
            route.continue_()

    page.route("**/api/Records/businesssearch", _intercept)

    try:
        search_box = page.get_by_role(
            "textbox", name="Search by name or file number"
        )
        search_box.click()
        # Select all + delete to clear, then type character-by-character
        # to trigger Angular's reactive form validation
        search_box.press("Meta+a")
        search_box.press("Backspace")
        time.sleep(0.3)
        search_box.type(query, delay=30)
        # Wait for Angular to validate and enable the search button
        page.get_by_role("button", name="Execute search").wait_for(
            state="attached", timeout=5000
        )
        time.sleep(0.5)

        with page.expect_response(
            lambda r: "/api/Records/businesssearch" in r.url,
            timeout=20000,
        ) as resp_info:
            page.get_by_role("button", name="Execute search").click(
                force=True, timeout=10000
            )

        resp = resp_info.value
        if resp.status != 200:
            body = resp.text()
            print(f"  Warning: API returned {resp.status}: {body[:200]}", file=sys.stderr)
            return {}

        body = resp.text()
        if not body or not body.strip():
            print("  Warning: API returned empty response", file=sys.stderr)
            return {}

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            if "<html" in body.lower() or "incapsula" in body.lower():
                print("  Warning: Imperva challenge intercepted API call — retry", file=sys.stderr)
            else:
                print(f"  Warning: non-JSON response: {body[:200]}", file=sys.stderr)
            return {}
    finally:
        page.unroute("**/api/Records/businesssearch", _intercept)


def _click_search_result(page, results, result_index=0, want_history=False):
    """Click a search result button and capture detail response.

    The Angular app opens a detail drawer when a result button is clicked,
    triggering a FilingDetail API call. History requires a separate
    "View History" button click in the drawer.
    """
    if result_index >= len(results):
        result_index = 0

    target = results[result_index]
    title_text = target["title"]

    captured = {"detail": None, "history": None}

    # Click search result -> opens drawer with FilingDetail
    try:
        with page.expect_response(
            lambda r: "/api/FilingDetail/business/" in r.url and r.status == 200,
            timeout=15000,
        ) as detail_resp:
            page.get_by_role("button", name=title_text).click(timeout=10000)
        captured["detail"] = detail_resp.value.json()
        time.sleep(1)
    except Exception as e:
        print(f"  Warning: click result: {e}", file=sys.stderr)
        return captured

    # Click "View History" button in drawer (has aria-label="View History")
    if want_history:
        try:
            vh_btn = page.get_by_role("button", name="View History")
            if vh_btn.count() > 0:
                with page.expect_response(
                    lambda r: "/api/History/business/" in r.url and r.status == 200,
                    timeout=15000,
                ) as hist_resp:
                    vh_btn.click(timeout=5000)
                captured["history"] = hist_resp.value.json()
                time.sleep(1)
        except Exception as e:
            print(f"  Warning: View History: {e}", file=sys.stderr)

    return captured


def _normalize_entity_number(value):
    """Normalize entity number for keyword search.

    CA bizfile keyword search matches entity numbers but requires the
    zero-padded internal format:
      "726332"   → "0726332"  (pad to 7 digits)
      "C0726332" → "0726332"  (strip letter prefix)
      "0726332"  → "0726332"  (already correct)
    """
    # Strip common letter prefixes
    stripped = re.sub(r"^[A-Za-z]+", "", value)
    # If pure digits and < 7 chars, zero-pad
    if stripped.isdigit() and len(stripped) < 7:
        stripped = stripped.zfill(7)
    return stripped


def _search_entity_by_number(page, entity_num):
    """Search for entity by number, trying normalized forms.

    Returns (search_data, results_list) or raises if nothing found.
    """
    # Try the normalized form first
    normalized = _normalize_entity_number(entity_num)
    data = _ui_search(page, normalized)
    results = _parse_search_results(data)
    if results:
        return data, results

    # Try original value if different
    if normalized != entity_num:
        data = _ui_search(page, entity_num)
        results = _parse_search_results(data)
        if results:
            return data, results

    return data, []


def _search_and_get_detail(page, query, result_index=0, by_number=False,
                           want_history=False, **search_kwargs):
    """Search, click Nth result, return search results + detail + history."""
    if by_number:
        data, results = _search_entity_by_number(page, query)
    else:
        data = _ui_search(page, query, **search_kwargs)
        results = _parse_search_results(data)

    if not results:
        return {"search": data, "detail": None, "history": None, "results": []}

    captured = _click_search_result(
        page, results, result_index, want_history=want_history
    )

    return {
        "search": data,
        "detail": captured["detail"],
        "history": captured["history"],
        "results": results,
    }


# ══════════════════════════════════════════════════════════
# RESPONSE PARSING
# ══════════════════════════════════════════════════════════

def _parse_search_results(data):
    """Parse bizfileonline search response into a list of entity records."""
    if not data:
        return []

    rows = data.get("rows", {})
    if not isinstance(rows, dict):
        return []

    results = []
    for internal_id, row in rows.items():
        title = row.get("TITLE", "")
        if isinstance(title, list):
            title = title[0] if title else ""

        entity_num = None
        name = title
        m = re.search(r"\(([^)]+)\)\s*$", title)
        if m:
            entity_num = m.group(1)
            name = title[: m.start()].strip()

        results.append({
            "internal_id": str(internal_id),
            "entity_number": entity_num,
            "entity_name": name,
            "title": title,
            "record_num": row.get("RECORD_NUM", ""),
            "initial_filing_date": row.get("INITIAL_FILING_DATE", ""),
            "status": row.get("STATUS", ""),
            "entity_type": row.get("ENTITY_TYPE", ""),
            "standing_sos": row.get("STANDING_SOS", ""),
            "standing_ftb": row.get("STANDING_FTB", ""),
            "standing_agent": row.get("STANDING_AGENT", ""),
            "standing_vcfcf": row.get("STANDING_VCFCF", ""),
            "agent": row.get("AGENT", ""),
        })

    return results


def _parse_detail(data):
    """Parse entity detail (FilingDetail) response."""
    if not data:
        return None

    detail_list = data.get("DRAWER_DETAIL_LIST", [])
    detail = {}
    for item in detail_list:
        label = (item.get("LABEL") or "").strip()
        value = (item.get("VALUE") or "").strip()
        if label and value:
            detail[label] = value

    return {"parsed": detail, "raw": data}


def _parse_history(data):
    """Parse filing history response.

    Actual API field names:
      AMENDMENT_LIST: AMENDMENT_TYPE, AMENDMENT_NUM, AMENDMENT_DATE,
                      AMENDMENT_ID, EFFECTIVE_DATE, DOWNLOAD_LINK
      HISTORY_LIST:   FIELD_NAME, CHANGED_FROM, CHANGED_TO, DISPLAY_NAME,
                      AMENDMENT_ID, HISTORY_TYPE_ID
    """
    if not data:
        return None

    return {
        "amendments": data.get("AMENDMENT_LIST", []),
        "field_changes": data.get("HISTORY_LIST", []),
        "raw": data,
    }


# ══════════════════════════════════════════════════════════
# SEARCH COMMAND
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search CA business entities via bizfileonline."""
    page = _get_page()

    query = args.query
    if args.by_number:
        query = _normalize_entity_number(query)

    data = _ui_search(
        page,
        query,
        status=args.status,
        filing_type=args.type or "all",
        officer_first=args.officer_first or "",
        officer_middle=args.officer_middle or "",
        officer_last=args.officer_last or "",
    )

    results = _parse_search_results(data)

    log_search(args.query, "ca_bizfile", len(results))

    if write_output(
        results, args,
        summary=f"CA bizfile search '{args.query}' ({len(results)} results)",
    ):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {len(results)} CA entities matching '{args.query}'")
    print()
    for r in results:
        print(f"  {r['entity_name']} ({r['entity_type']})")
        print(f"    Entity #: {r['entity_number']} | ID: {r['internal_id']} | Status: {r['status']}")
        if r["initial_filing_date"]:
            print(f"    Filed: {r['initial_filing_date']}")
        if r["agent"]:
            print(f"    Agent: {r['agent']}")
        standings = []
        if r["standing_sos"]:
            standings.append(f"SOS: {r['standing_sos']}")
        if r["standing_ftb"]:
            standings.append(f"FTB: {r['standing_ftb']}")
        if standings:
            print(f"    Standing: {', '.join(standings)}")
        print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL COMMAND
# ══════════════════════════════════════════════════════════

def _is_entity_number(value):
    """Heuristic: entity numbers contain letters (C0726332, LLC201812345)."""
    return bool(re.search(r"[A-Za-z]", value))


def cmd_entity(args):
    """Get entity detail — accepts entity number (e.g. C0726332, 726332)."""
    page = _get_page()

    # Search by entity number (with normalization) and click the result.
    # Angular loads entity detail in a drawer, not via URL routing.
    resp = _search_and_get_detail(
        page, args.entity_id, by_number=True, want_history=args.history
    )

    detail = _parse_detail(resp["detail"])
    result = {"detail": detail}

    if resp["history"]:
        result["history"] = _parse_history(resp["history"])

    if write_output(result, args, summary=f"CA bizfile entity {args.entity_id}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    parsed = detail.get("parsed", {}) if detail else {}
    if not parsed:
        print(f"No detail found for {args.entity_id}")
        return

    print(f"\n  [CA bizfile] Entity Detail ({args.entity_id})")
    for label, value in parsed.items():
        print(f"    {label}: {value}")

    if "history" in result and result["history"]:
        _print_history(result["history"])
    print()


# ══════════════════════════════════════════════════════════
# HISTORY COMMAND
# ══════════════════════════════════════════════════════════

def cmd_history(args):
    """Get filing history by entity/record number."""
    page = _get_page()

    resp = _search_and_get_detail(
        page, args.record_num, by_number=True, want_history=True
    )

    history = _parse_history(resp.get("history"))

    if not history:
        print(f"No history for {args.record_num}")
        return

    if write_output(history, args, summary=f"CA bizfile history {args.record_num}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(history, indent=2, default=str))
        return

    print(f"Filing History for {args.record_num}")
    _print_history(history)
    print()


def _print_history(history):
    """Pretty-print history data."""
    amendments = history.get("amendments", [])
    changes = history.get("field_changes", [])

    if amendments:
        print(f"\n  Amendments ({len(amendments)}):")
        for a in amendments:
            date = a.get("AMENDMENT_DATE", "?")
            desc = a.get("AMENDMENT_TYPE", "?")
            num = a.get("AMENDMENT_NUM", "")
            dl = a.get("DOWNLOAD_LINK", "")
            line = f"    {date}: {desc}"
            if num:
                line += f" [{num}]"
            if dl:
                line += " (PDF available)"
            print(line)
    else:
        print("  No amendments found")

    if changes:
        print(f"\n  Field Changes ({len(changes)}):")
        for c in changes:
            field = c.get("DISPLAY_NAME") or c.get("FIELD_NAME") or "?"
            old = c.get("CHANGED_FROM", "")
            new = c.get("CHANGED_TO", "")
            print(f"    {field}")
            if old:
                print(f"      From: {old}")
            if new:
                print(f"      To:   {new}")


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _ingest_entity_data(db, detail_data, history_data=None,
                        internal_id=None, entity_name=None,
                        entity_number_hint=None):
    """Ingest pre-fetched entity data into registry.db.

    entity_name and entity_number_hint come from search results since
    the FilingDetail API doesn't include them in the response body.

    Returns (entity_id, entity_name) or (None, None).
    """
    if not detail_data:
        return None, None

    detail_list = detail_data.get("DRAWER_DETAIL_LIST", [])
    fields = {}
    for item in detail_list:
        label = (item.get("LABEL") or "").strip()
        value = (item.get("VALUE") or "").strip()
        if label:
            fields[label] = value

    entity_number = fields.get("Entity Number", entity_number_hint or "")
    name = entity_name or fields.get("Entity Name", "?")
    etype_raw = fields.get("Entity Type", "")
    status_raw = fields.get("Status", "")
    filed_date = (
        fields.get("Initial Filing Date")
        or fields.get("Registration Date")
        or fields.get("Filing Date", "")
    )
    jurisdiction = fields.get("Formed In", fields.get("Jurisdiction", ""))
    # Agent field may contain multi-line "AGENT_NUMBER\nAGENT_NAME"
    agent_raw = fields.get("Agent for Service of Process", fields.get("Agent", ""))
    agent_lines = [l.strip() for l in agent_raw.split("\n") if l.strip()]
    agent_name = agent_lines[-1] if agent_lines else ""  # Last line is the name
    # Address fields may contain newlines — normalize
    entity_addr = fields.get("Principal Address", fields.get("Entity Address", ""))
    entity_addr = entity_addr.replace("\n", ", ")
    mailing_addr = fields.get("Mailing Address", fields.get("Entity Mailing Address", ""))
    mailing_addr = mailing_addr.replace("\n", ", ")

    etype = TYPE_MAP.get(
        etype_raw.lower(),
        etype_raw.lower().replace(" ", "_") if etype_raw else None,
    )
    status = REGISTRY_STATUS_MAP.get(
        status_raw.lower(), status_raw.lower() if status_raw else None
    )

    if filed_date and "/" in filed_date:
        parts = filed_date.split("/")
        if len(parts) == 3:
            filed_date = f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"

    def _parse_addr(addr_str):
        if not addr_str:
            return {}
        parts = [p.strip() for p in addr_str.split(",")]
        result = {"full": addr_str}
        if len(parts) >= 3:
            result["street"] = parts[0]
            result["city"] = parts[1]
            sz = parts[-1].strip().split()
            if len(sz) >= 2:
                result["state"] = sz[0]
                result["zip"] = sz[1]
            elif sz:
                result["state"] = sz[0]
        elif len(parts) == 2:
            result["street"] = parts[0]
            result["city"] = parts[1]
        elif parts:
            result["street"] = parts[0]
        return result

    ent_addr = _parse_addr(entity_addr)
    mail_addr = _parse_addr(mailing_addr)

    source_id = entity_number or str(internal_id or "unknown")
    source_url = f"{BASE_URL}/search/business?filing-number={entity_number}" if entity_number else BASE_URL

    # Use upsert to avoid deleting child rows (agents, filings)
    db.execute(
        """
        INSERT INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date,
            principal_address, principal_city, principal_state, principal_zip,
            principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip,
            mailing_country,
            state_of_formation, source_url, raw_data
        ) VALUES ('ca', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?, ?, 'US', ?, ?, ?)
        ON CONFLICT(source_jurisdiction, source_id) DO UPDATE SET
            entity_name=excluded.entity_name,
            entity_type=excluded.entity_type,
            status=excluded.status,
            formation_date=excluded.formation_date,
            principal_address=excluded.principal_address,
            principal_city=excluded.principal_city,
            principal_state=excluded.principal_state,
            principal_zip=excluded.principal_zip,
            mailing_address=excluded.mailing_address,
            mailing_city=excluded.mailing_city,
            mailing_state=excluded.mailing_state,
            mailing_zip=excluded.mailing_zip,
            state_of_formation=excluded.state_of_formation,
            source_url=excluded.source_url,
            raw_data=excluded.raw_data
        """,
        [
            source_id, name, etype, status, filed_date or None,
            ent_addr.get("street"), ent_addr.get("city"),
            ent_addr.get("state"), ent_addr.get("zip"),
            mail_addr.get("street"), mail_addr.get("city"),
            mail_addr.get("state"), mail_addr.get("zip"),
            jurisdiction or "CALIFORNIA", source_url,
            json.dumps({"fields": fields, "raw": detail_data}, default=str),
        ],
    )

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='ca' AND source_id=?",
        [source_id],
    ).fetchone()
    entity_id = row[0]

    if agent_name:
        try:
            db.execute(
                "INSERT OR IGNORE INTO registry_agents (entity_id, agent_name) VALUES (?, ?)",
                [entity_id, agent_name],
            )
        except sqlite3.IntegrityError:
            pass

    # Ingest filing history
    if history_data:
        for a in history_data.get("AMENDMENT_LIST", []):
            filing_date = a.get("AMENDMENT_DATE", "")
            if filing_date and "/" in filing_date:
                fp = filing_date.split("/")
                if len(fp) == 3:
                    filing_date = f"{fp[2]}-{fp[0].zfill(2)}-{fp[1].zfill(2)}"
            desc = a.get("AMENDMENT_TYPE", "")
            try:
                db.execute(
                    """
                    INSERT OR IGNORE INTO registry_filings
                    (entity_id, filing_type, filing_date, description,
                     entity_name_at_time, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        entity_id, desc, filing_date or None, desc,
                        name, json.dumps(a, default=str),
                    ],
                )
            except sqlite3.IntegrityError:
                pass

        for c in history_data.get("HISTORY_LIST", []):
            display = (c.get("DISPLAY_NAME") or c.get("FIELD_NAME") or "").upper()
            if display in ("ENTITY NAME", "NAME"):
                old_name = c.get("CHANGED_FROM", "")
                if old_name:
                    try:
                        db.execute(
                            """
                            INSERT OR IGNORE INTO registry_name_history
                            (entity_id, previous_name, change_date)
                            VALUES (?, ?, ?)
                            """,
                            [entity_id, old_name, None],
                        )
                    except sqlite3.IntegrityError:
                        pass

    return entity_id, name


def cmd_ingest(args):
    """Ingest a single entity into registry.db."""
    page = _get_page()

    resp = _search_and_get_detail(
        page, args.entity_id, by_number=True, want_history=True
    )

    # Get entity name/number from search results
    sr = resp.get("results", [{}])
    ename = sr[0].get("entity_name") if sr else None
    enum = sr[0].get("entity_number") if sr else None

    db = get_db()
    entity_id, name = _ingest_entity_data(
        db, resp.get("detail"), resp.get("history"),
        internal_id=args.entity_id,
        entity_name=ename, entity_number_hint=enum,
    )

    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        print(f"Ingested: {name} (ID: {args.entity_id}, registry: {entity_id})")
    else:
        print(f"Failed to ingest entity {args.entity_id}")


def cmd_ingest_search(args):
    """Search and ingest matching entities into registry.db."""
    page = _get_page()

    data = _ui_search(
        page, args.query,
        status=args.status, filing_type=args.type or "all",
    )
    results = _parse_search_results(data)

    if not results:
        print(f"No results for '{args.query}'")
        return

    results = results[: args.limit]
    print(f"Found {len(results)} entities. Ingesting up to {args.limit}...")

    db = get_db()
    ingested = 0

    for i, r in enumerate(results):
        entity_num = r["entity_number"]
        name = r["entity_name"]

        if not entity_num:
            print(f"  [{i+1}/{len(results)}] SKIP: {name} (no entity number)", file=sys.stderr)
            continue

        try:
            detail_resp = _search_and_get_detail(
                page, entity_num, by_number=True, want_history=True
            )
            sr = detail_resp.get("results", [{}])
            eid, ename = _ingest_entity_data(
                db, detail_resp.get("detail"), detail_resp.get("history"),
                internal_id=r["internal_id"],
                entity_name=sr[0].get("entity_name") if sr else name,
                entity_number_hint=entity_num,
            )
        except Exception as e:
            print(f"  [{i+1}/{len(results)}] ERROR: {name} — {e}", file=sys.stderr)
            continue

        if eid:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {ename} (reg: {eid})")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name}")

        if ingested % 10 == 0:
            db.commit()

        time.sleep(1)  # Rate limit between entity fetches

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    try:
        db.execute(
            """
            INSERT INTO registry_ingest_log
            (jurisdiction, source_type, record_count, notes)
            VALUES ('ca', 'bizfile_web', ?, ?)
            """,
            [ingested, f"CA bizfile search: '{args.query}' ({args.status})"],
        )
        db.commit()
    except Exception:
        pass

    log_search(args.query, "ca_bizfile_ingest", ingested)
    print(f"\nIngested {ingested} of {len(results)} entities")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="CA SoS bizfileonline — web API search (no API key required)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search CA entities (up to 500 results)")
    p.add_argument("query", help="Entity name or number")
    p.add_argument(
        "--by-number", action="store_true",
        help="Search by entity number instead of name",
    )
    p.add_argument(
        "--status", choices=list(STATUS_MAP_FILTER.keys()), default="all",
        help="Filter by status (default: all)",
    )
    p.add_argument(
        "--type", choices=list(FILING_TYPE_MAP.keys()), default=None,
        help="Filter by filing type",
    )
    p.add_argument("--officer-first", help="Officer first name filter")
    p.add_argument("--officer-middle", help="Officer middle name filter")
    p.add_argument("--officer-last", help="Officer last name filter")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Entity detail by internal ID or number")
    p.add_argument("entity_id", help="Internal ID (numeric) or entity number (e.g. C0726332)")
    p.add_argument(
        "--history", action="store_true",
        help="Include filing history and field changes",
    )
    add_output_args(p)

    # history
    p = sub.add_parser("history", help="Filing history by entity number")
    p.add_argument("record_num", help="Entity number (e.g. C0726332)")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db")
    p.add_argument("entity_id", help="Internal ID or entity number")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search + ingest entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument(
        "--status", choices=list(STATUS_MAP_FILTER.keys()), default="all",
        help="Filter by status",
    )
    p.add_argument(
        "--type", choices=list(FILING_TYPE_MAP.keys()), default=None,
        help="Filter by filing type",
    )
    p.add_argument(
        "--limit", type=int, default=50, help="Max entities to ingest",
    )

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "history": cmd_history,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }

    try:
        handlers[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
