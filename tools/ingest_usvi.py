#!/usr/bin/env python3
"""
US Virgin Islands Division of Corporations and Trademarks registry scraper.

Uses the Catalyst platform at corporationsandtrademarks.vi.gov.
The search is a server-rendered HTML form (not a REST API); entity detail
requires a two-step session: GET search → POST AJAX callback.

Data available publicly (no login required):
  - Entity name, identifier, type, sub-type
  - Status (with full history)
  - Registration date, inactive date, removal reason
  - Resident agent name + address
  - Principal office / mailing address
  - Term, purpose

NOT available without paid certificate request:
  - Officers, directors, members, managers (principals)

Usage:
    python tools/ingest_usvi.py search "LSJE"
    python tools/ingest_usvi.py search "Epstein" --contains
    python tools/ingest_usvi.py search "Southern Trust"
    python tools/ingest_usvi.py detail 581737                # By entity identifier
    python tools/ingest_usvi.py ingest-entity 581737
    python tools/ingest_usvi.py ingest-batch "LSJE" "Maple" "Nautilus" "Laurel" "Cypress"
    python tools/ingest_usvi.py ingest-batch "J Epstein" "Southern Trust" "Enhanced Education"
"""

import argparse
import html as html_mod
import json
import re
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)
from urllib.error import HTTPError, URLError

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

BASE_URL = "https://www.corporationsandtrademarks.vi.gov"
SEARCH_URL = f"{BASE_URL}/usvi-master/service/create.html"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) OSINT-Research/1.0"

# Rate limiting: be respectful to a small government portal
REQUEST_DELAY = 2  # seconds between requests


def _make_opener():
    """Create a URL opener with cookie support."""
    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))
    return opener


def _fetch(opener, url, data=None, method="GET"):
    """Fetch a URL with proper headers and error handling."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if data is not None:
        if isinstance(data, dict):
            data = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = Request(url, data=data, headers=headers, method=method)

    try:
        resp = opener.open(req, timeout=60)
        return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"ERROR: HTTP {e.code} for {url}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason} for {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"ERROR: {e} for {url}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _clean_html(text):
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_search_results(page_html):
    """Parse entity search results from Catalyst HTML.

    Returns a list of dicts with keys:
        entity_id, entity_name, status, entity_type, registration_date,
        last_ar_year, resident_agent, principal_address, click_node
    Also returns (update_url, vikey) for detail navigation.
    """
    results = []

    # Result count
    count_match = re.search(r"(\d+) Results?", page_html)
    total = int(count_match.group(1)) if count_match else 0

    # View instance info for detail navigation
    action_match = re.search(
        r'form[^>]*id="viewInstanceForm"[^>]*action="(.*?)"', page_html
    )
    update_url = action_match.group(1) if action_match else None
    vikey_match = re.search(r"viewInstanceKey:'(.*?)'", page_html)
    vikey = vikey_match.group(1) if vikey_match else None

    # Parse each result row
    # Entity link pattern: id="nodeWNNN" ... onclick="...invokeMenuCb..." ... Entity Name: NAME (ID)
    entity_pattern = re.compile(
        r'id="(nodeW\d+)"[^>]*href="#"[^>]*onclick="[^"]*invokeMenuCb[^"]*"'
        r"[^>]*>.*?Entity Name:\s*(.*?)\s*\((\w+)\)",
        re.DOTALL,
    )

    for match in entity_pattern.finditer(page_html):
        node_id = match.group(1)
        name = _clean_html(match.group(2))
        eid = match.group(3)

        # Find the enclosing result block for this entity
        # Go backwards to find the appRepeaterRowContent div
        pos = match.start()
        row_start = page_html.rfind("appRepeaterRowContent", 0, pos)
        start = max(0, row_start - 50) if row_start != -1 else max(0, pos - 500)
        # Look for the next appRepeaterRowContent or end
        end_match = re.search(
            r"appRepeaterRowContent", page_html[pos + 100:]
        )
        end = pos + 100 + end_match.start() if end_match else len(page_html)
        block = page_html[start:end]

        # Extract fields from the block
        result = {
            "entity_id": eid,
            "entity_name": name,
            "click_node": node_id.replace("node", ""),
            "status": None,
            "entity_type": None,
            "registration_date": None,
            "last_ar_year": None,
            "resident_agent": None,
            "principal_address": None,
            "register_type": None,
        }

        # Register badge (COR = corporations, BN = business names)
        badge = re.search(r'class="appBadge\s+(\w+)"', block)
        if badge:
            result["register_type"] = badge.group(1)

        # Status
        status = re.search(
            r"Business Entity Status.*?appMinimalValue\">(.*?)</span>", block, re.DOTALL
        )
        if status:
            result["status"] = _clean_html(status.group(1))

        # Entity type
        etype = re.search(
            r"Entity Type.*?appMinimalValue\">(.*?)</span>", block, re.DOTALL
        )
        if etype:
            result["entity_type"] = _clean_html(etype.group(1))

        # Registration date
        reg_date = re.search(
            r"Registration Date.*?appMinimalValue\"[^>]*>(.*?)</span>",
            block,
            re.DOTALL,
        )
        if reg_date:
            date_text = _clean_html(reg_date.group(1))
            # Extract MM/DD/YYYY
            date_match = re.search(r"(\d{2}/\d{2}/\d{4})", date_text)
            if date_match:
                result["registration_date"] = date_match.group(1)

        # Last AR year
        ar = re.search(
            r"Last Annual Report Filed.*?appMinimalValue\">(.*?)</span>",
            block,
            re.DOTALL,
        )
        if ar:
            result["last_ar_year"] = _clean_html(ar.group(1))

        # Resident agent
        agent = re.search(
            r"Resident Agent.*?appMinimalValue\">(.*?)</span>", block, re.DOTALL
        )
        if agent:
            result["resident_agent"] = _clean_html(agent.group(1))

        # Principal address
        addr = re.search(
            r"Principal Office.*?appMinimalValue\">(.*?)</span>", block, re.DOTALL
        )
        if addr:
            result["principal_address"] = _clean_html(addr.group(1))

        results.append(result)

    return results, total, update_url, vikey


def _parse_entity_detail(page_html):
    """Parse entity detail page from Catalyst HTML.

    Returns a dict with entity fields.
    """
    detail = {}

    # All label-value pairs
    pattern = re.compile(
        r'appLabelText">(.*?)</span>.*?appAttrValue"[^>]*>(.*?)</div>',
        re.DOTALL,
    )

    # Track field occurrences to handle duplicates
    seen = {}
    for match in pattern.finditer(page_html):
        label = match.group(1).strip()
        value = _clean_html(match.group(2))
        if not label or not value:
            continue

        key = label
        if key in seen:
            seen[key] += 1
            # For duplicate labels, store with suffix
            # Special handling for status history
            if key in ("Previous Status", "Start Date", "End Date", "Reason"):
                key = f"{key}_{seen[key]}"
            else:
                key = f"{key}_{seen[key]}"
        else:
            seen[key] = 0

        detail[key] = value

    # Extract resident agent name. Two patterns:
    # 1. Entity agent: linked via appAttrHyperlink EntityName
    # 2. Individual agent: stored as "Name" label in the agent section
    hyperlinks = re.findall(
        r'appAttrHyperlink[^>]*EntityName[^>]*>.*?<a[^>]*>(.*?)</a>',
        page_html,
        re.DOTALL,
    )
    agent_names = [_clean_html(h) for h in hyperlinks if _clean_html(h)]
    if agent_names:
        detail["_resident_agent_name"] = agent_names[0]
    elif detail.get("Name"):
        # Individual agent - "Name" field in agent section
        detail["_resident_agent_name"] = detail["Name"]

    # Extract status history
    status_history = []
    prev_statuses = re.findall(
        r"Previous Status.*?appAttrValue[^>]*>(.*?)</div>.*?"
        r"Start Date.*?appAttrValue[^>]*>(.*?)</div>.*?"
        r"End Date.*?appAttrValue[^>]*>(.*?)</div>",
        page_html,
        re.DOTALL,
    )
    for ps_html, start_html, end_html in prev_statuses:
        ps = _clean_html(ps_html)
        start = _clean_html(start_html)
        end = _clean_html(end_html)
        if ps:
            status_history.append({"status": ps, "start": start, "end": end})
    if status_history:
        detail["_status_history"] = status_history

    return detail


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def search_entities(query, contains=False, register=None, limit=10):
    """Search USVI corporate registry.

    Args:
        query: Search term
        contains: If True, use "Contains" mode (default: "Starts With")
        register: Filter by register type: "usvi-corporations" or "usvi-businessnames"
        limit: Max results to display (pagination not implemented for initial version)

    Returns:
        List of result dicts, total count, and session info for detail navigation.
    """
    opener = _make_opener()

    params = {"service": "registerItemSearch", "QueryString": query}
    url = SEARCH_URL + "?" + urlencode(params)

    html = _fetch(opener, url)
    if not html:
        return [], 0, None, None

    results, total, update_url, vikey = _parse_search_results(html)

    return results, total, (opener, update_url, vikey)


def get_entity_detail(opener, update_url, vikey, click_node, query):
    """Fetch entity detail by navigating from search results.

    Args:
        opener: URL opener with cookies from search
        update_url: Form action URL from search page
        vikey: View instance key from search page
        click_node: Node ID for the entity link (e.g., "W146")
        query: Original search query (needed for form submission)

    Returns:
        Parsed detail dict or None.
    """
    if not all([opener, update_url, vikey, click_node]):
        print("ERROR: Missing session info for detail navigation", file=sys.stderr)
        return None

    data = {
        "_CBNODE_": click_node,
        "_CBNAME_": "invokeMenuCb",
        "_VIKEY_": vikey,
        "QueryString": query,
        "_scrollTop": "0",
    }

    html = _fetch(opener, update_url, data=data, method="POST")
    if not html:
        return None

    # Verify we got a detail page
    if "sosViewEntity" not in html:
        print("WARNING: Did not receive entity detail page", file=sys.stderr)
        return None

    return _parse_entity_detail(html)


def search_and_detail(query, entity_id=None, contains=False):
    """Search then fetch detail for a specific entity by ID.

    This is the primary entry point for getting full entity detail.
    If entity_id is provided, first checks registry.db for the name (to search by),
    then falls back to searching by the ID string directly.
    """
    opener = _make_opener()

    # If entity_id is purely numeric, the Catalyst search won't find it by number.
    # Try: 1) name from registry.db, 2) the query parameter if provided, 3) raw entity_id.
    search_queries = []
    if entity_id:
        # Check registry.db for a known entity with this source_id
        try:
            db = get_db()
            row = db.execute(
                "SELECT entity_name FROM registry_entities WHERE source_jurisdiction='vi' AND source_id=?",
                [str(entity_id)],
            ).fetchone()
            if row:
                search_queries.append(row["entity_name"])
        except Exception:
            pass
        # If a name query was also provided, try that
        if query and query != str(entity_id):
            search_queries.append(query)
        # Also try the raw entity_id as a search term (may work for trade names like TN0002420)
        search_queries.append(str(entity_id))
    else:
        search_queries.append(query)

    for search_query in search_queries:
        params = {"service": "registerItemSearch", "QueryString": search_query}
        url = SEARCH_URL + "?" + urlencode(params)

        html = _fetch(opener, url)
        if not html:
            continue

        results, total, update_url, vikey = _parse_search_results(html)
        if not results:
            continue

        # Find the target entity
        target = None
        if entity_id:
            for r in results:
                if r["entity_id"] == str(entity_id):
                    target = r
                    break
        if not target:
            # If searching by name and only one result, use it
            if len(results) == 1:
                target = results[0]
            elif not entity_id:
                # No entity_id specified and multiple results
                return None

        if target:
            time.sleep(REQUEST_DELAY)

            detail = get_entity_detail(
                opener, update_url, vikey, target["click_node"], search_query
            )
            if detail:
                detail["_search_data"] = target
                return detail

    return None


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Search USVI corporate registry."""
    opener = _make_opener()

    params = {"service": "registerItemSearch", "QueryString": args.query}
    url = SEARCH_URL + "?" + urlencode(params)

    html = _fetch(opener, url)
    if not html:
        print("Failed to fetch search results")
        return

    results, total, update_url, vikey = _parse_search_results(html)

    print(f"Found {total} USVI entities matching '{args.query}'")
    print()
    for r in results:
        badge = r.get("register_type", "")
        badge_label = {"sosCorporations": "COR", "sosBusinessNames": "BN"}.get(
            badge, badge
        )
        name = r["entity_name"]
        eid = r["entity_id"]
        status = r.get("status", "?")
        etype = r.get("entity_type", "?")
        reg_date = r.get("registration_date", "?")
        agent = r.get("resident_agent", "")
        addr = r.get("principal_address", "")
        last_ar = r.get("last_ar_year", "")

        print(f"  [{badge_label}] {name} ({eid})")
        print(f"    Status: {status}")
        print(f"    Type: {etype}")
        print(f"    Registered: {reg_date}")
        if agent:
            print(f"    Resident Agent: {agent}")
        if addr:
            print(f"    Address: {addr}")
        if last_ar:
            print(f"    Last AR: {last_ar}")
        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_detail(args):
    """Fetch full detail for a USVI entity by identifier or name."""
    name_hint = getattr(args, "name", None)
    if name_hint:
        # Search by name, then match by entity_id
        detail = search_and_detail(query=name_hint, entity_id=args.entity_id)
    else:
        detail = search_and_detail(query=args.entity_id, entity_id=args.entity_id)
    if not detail:
        print(f"Entity {args.entity_id} not found or failed to fetch detail")
        print("TIP: If the entity isn't in registry.db yet, use --name 'Entity Name' to search first")
        return

    print(f"=== USVI Entity Detail ===")
    print()

    # Core fields
    core_fields = [
        ("Entity Name", "Entity Name"),
        ("Entity Identifier", "Entity Identifier"),
        ("Entity Status", "Entity Status"),
        ("Entity Type", "Entity Type"),
        ("Business Entity Sub-Type", "Business Entity Sub-Type"),
        ("Registration Date", "Registration Date"),
        ("Inactive Date", "Inactive Date"),
        ("Removal Reason", "Removal Reason"),
        ("Last AR Filed Date", "Last AR Filed Date"),
        ("State", "State"),
        ("Country", "Country"),
        ("Term", "Term"),
        ("Nature of Business/Purpose", "Nature of Business/Purpose"),
    ]

    for display, key in core_fields:
        val = detail.get(key)
        if val:
            # Clean up date strings (remove redundant text)
            val = re.sub(r"\s+[A-Z][a-z]+ \d+ \d{4}.*$", "", val)
            print(f"  {display}: {val}")

    # Resident agent
    agent_name = detail.get("_resident_agent_name")
    if agent_name:
        print(f"\n  Resident Agent: {agent_name}")
    agent_phys = detail.get("Physical Address")
    if agent_phys:
        print(f"  Agent Physical Address: {agent_phys}")
    agent_mail = detail.get("Mailing Address")
    if agent_mail and "same as" not in agent_mail.lower():
        print(f"  Agent Mailing Address: {agent_mail}")
    agent_email = detail.get("Email Address")
    if agent_email:
        print(f"  Agent Email: {agent_email}")

    # Principal office
    princ_addr = detail.get("Principal Office or Place of Business")
    if princ_addr:
        print(f"\n  Principal Office: {princ_addr}")

    # Status history
    history = detail.get("_status_history", [])
    if history:
        print(f"\n  Status History:")
        for h in history:
            start = re.sub(r"\s+[A-Z][a-z]+ \d+ \d{4}.*$", "", h["start"])
            end = re.sub(r"\s+[A-Z][a-z]+ \d+ \d{4}.*$", "", h["end"])
            print(f"    {h['status']}: {start} to {end}")

    print()

    if args.json_out:
        print(json.dumps(detail, indent=2, default=str))


def cmd_ingest_entity(args):
    """Ingest a specific USVI entity into registry.db."""
    name_hint = getattr(args, "name", None)
    detail = search_and_detail(
        query=name_hint if name_hint else args.entity_id,
        entity_id=args.entity_id,
    )
    if not detail:
        print(f"Entity {args.entity_id} not found or failed to fetch detail")
        print("TIP: Use --name 'Entity Name' to search by name first")
        return

    db = get_db()
    entity_id = _upsert_entity(db, detail)
    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    name = detail.get("Entity Name", "?")
    print(f"Ingested: {name} ({args.entity_id}) -> registry ID {entity_id}")


def cmd_ingest_batch(args):
    """Ingest all entities matching multiple search queries."""
    db = get_db()
    total_ingested = 0

    for query in args.queries:
        print(f"\n--- Searching: '{query}' ---")
        opener = _make_opener()

        params = {"service": "registerItemSearch", "QueryString": query}
        url = SEARCH_URL + "?" + urlencode(params)

        html = _fetch(opener, url)
        if not html:
            print(f"  Failed to fetch search results for '{query}'")
            continue

        results, total, update_url, vikey = _parse_search_results(html)
        print(f"  Found {total} results")

        for i, r in enumerate(results):
            eid = r["entity_id"]
            name = r["entity_name"]

            # Check if already ingested
            existing = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='vi' AND source_id=?",
                [eid],
            ).fetchone()
            if existing and not args.force:
                print(f"  [{i+1}/{len(results)}] SKIP (already ingested): {name} ({eid})")
                continue

            print(f"  [{i+1}/{len(results)}] Fetching detail: {name} ({eid})...")

            # Each detail request needs a fresh session because the Catalyst
            # platform changes server-side state after viewing entity detail.
            time.sleep(REQUEST_DELAY)
            fresh_opener = _make_opener()
            fresh_html = _fetch(fresh_opener, url)
            if not fresh_html:
                print(f"    FAILED to re-establish search session")
                continue

            fresh_results, _, fresh_update_url, fresh_vikey = _parse_search_results(fresh_html)

            # Find the matching entity in the fresh results (node IDs may differ)
            fresh_target = None
            for fr in fresh_results:
                if fr["entity_id"] == eid:
                    fresh_target = fr
                    break

            if not fresh_target:
                print(f"    FAILED: entity {eid} not in fresh search results")
                continue

            time.sleep(REQUEST_DELAY)
            detail = get_entity_detail(
                fresh_opener, fresh_update_url, fresh_vikey,
                fresh_target["click_node"], query,
            )
            if not detail:
                print(f"    FAILED to get detail")
                continue

            detail["_search_data"] = r
            rid = _upsert_entity(db, detail)
            total_ingested += 1
            print(f"    -> registry ID {rid}")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    print(f"\nBatch ingest complete: {total_ingested} entities ingested")


# ---------------------------------------------------------------------------
# Registry DB integration
# ---------------------------------------------------------------------------

def _upsert_entity(db, detail):
    """Insert or update a USVI entity in registry.db."""
    search_data = detail.get("_search_data", {})

    source_id = detail.get("Entity Identifier", search_data.get("entity_id", ""))
    name = detail.get("Entity Name", search_data.get("entity_name", "?"))

    # Map entity type
    etype_raw = detail.get("Entity Type", search_data.get("entity_type", ""))
    etype_map = {
        "Domestic Limited Liability Company": "llc",
        "Domestic Business Corporation": "corp",
        "Domestic Nonprofit Corporation": "nonprofit",
        "Foreign Limited Liability Company": "foreign_llc",
        "Foreign Business Corporation": "foreign_corp",
        "Foreign Nonprofit Corporation": "foreign_nonprofit",
        "Domestic Limited Partnership": "lp",
        "Foreign Limited Partnership": "foreign_lp",
    }
    # Try exact match first, then partial
    etype = None
    for key, val in etype_map.items():
        if key.lower() in etype_raw.lower():
            etype = val
            break
    if not etype:
        etype = etype_raw.lower().replace(" ", "_") if etype_raw else None

    # Status mapping
    status_raw = detail.get("Entity Status", search_data.get("status", ""))
    status_map = {
        "In Good Standing": "active",
        "Active": "active",
        "Dissolved/Withdrawn": "dissolved",
        "Dissolved": "dissolved",
        "Involuntary Intent": "inactive",
        "Registered": "active",
    }
    status = status_map.get(status_raw, status_raw.lower() if status_raw else None)

    # Registration date
    reg_date_raw = detail.get("Registration Date", search_data.get("registration_date", ""))
    reg_date = None
    date_match = re.search(r"(\d{2}/\d{2}/\d{4})", reg_date_raw)
    if date_match:
        parts = date_match.group(1).split("/")
        reg_date = f"{parts[2]}-{parts[0]}-{parts[1]}"  # YYYY-MM-DD

    # Dissolution/inactive date
    inactive_raw = detail.get("Inactive Date", "")
    dissolution_date = None
    date_match = re.search(r"(\d{2}/\d{2}/\d{4})", inactive_raw)
    if date_match:
        parts = date_match.group(1).split("/")
        dissolution_date = f"{parts[2]}-{parts[0]}-{parts[1]}"

    # Last filing date (use Last AR Filed Date)
    last_ar_raw = detail.get("Last AR Filed Date", "")
    last_filing = None
    date_match = re.search(r"(\d{2}/\d{2}/\d{4})", last_ar_raw)
    if date_match:
        parts = date_match.group(1).split("/")
        last_filing = f"{parts[2]}-{parts[0]}-{parts[1]}"

    # Address
    princ_addr_raw = detail.get("Principal Office or Place of Business", "")
    princ_addr = princ_addr_raw
    princ_city = None
    princ_state = "VI"
    princ_zip = None
    princ_country = "US"

    # Try to parse city and zip from address
    zip_match = re.search(r"(\d{5})", princ_addr_raw)
    if zip_match:
        princ_zip = zip_match.group(1)
    city_match = re.search(r",\s*([^,]+),\s*(?:United States Virgin Islands|VI)", princ_addr_raw)
    if city_match:
        princ_city = city_match.group(1).strip()

    # Purpose
    purpose = detail.get("Nature of Business/Purpose", "")
    if purpose in ("", " "):
        purpose = None

    # Sub-type
    subtype = detail.get("Business Entity Sub-Type", "")

    # Source URL
    source_url = f"https://www.corporationsandtrademarks.vi.gov/CorporationSearch"

    # Build raw_data for storage
    raw_data = json.dumps(detail, indent=2, default=str)

    # Check if entity already exists
    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='vi' AND source_id=?",
        [source_id],
    ).fetchone()

    if existing:
        entity_id = existing[0]
        db.execute(
            """
            UPDATE registry_entities SET
                entity_name=?, entity_type=?, status=?,
                formation_date=?, dissolution_date=?, last_filing_date=?,
                state_of_formation='US Virgin Islands', purpose=?,
                principal_address=?, principal_city=?, principal_state=?,
                principal_zip=?, principal_country=?,
                source_url=?, raw_data=?, updated_at=datetime('now')
            WHERE id=?
            """,
            [
                name, etype, status,
                reg_date, dissolution_date, last_filing,
                purpose,
                princ_addr or None, princ_city, princ_state,
                princ_zip, princ_country,
                source_url, raw_data,
                entity_id,
            ],
        )
    else:
        db.execute(
            """
            INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date, dissolution_date, last_filing_date,
                state_of_formation, purpose,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                source_url, raw_data
            ) VALUES ('vi', ?, ?, ?, ?, ?, ?, ?, 'US Virgin Islands', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source_id, name, etype, status,
                reg_date, dissolution_date, last_filing,
                purpose,
                princ_addr or None, princ_city, princ_state,
                princ_zip, princ_country,
                source_url, raw_data,
            ],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='vi' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Resident agent (try detail page first, then search data)
    agent_name = detail.get("_resident_agent_name", "")
    if not agent_name and search_data.get("resident_agent"):
        agent_name = search_data["resident_agent"]

    # Determine agent type: entity if agent was found via hyperlink, person if from "Name" field
    # When the agent is an entity, it's captured via the hyperlink pattern.
    # When it's an individual, it shows up as a "Name" field.
    agent_type = "entity"
    if agent_name and detail.get("Name") == agent_name:
        agent_type = "person"

    if agent_name:
        agent_phys = detail.get("Physical Address", "")
        agent_mail = detail.get("Mailing Address", "")
        agent_email = detail.get("Email Address", "")

        # Parse agent address for city/state/zip
        agent_city = None
        agent_state = "VI"
        agent_zip = None
        zip_match = re.search(r"(\d{5})", agent_phys)
        if zip_match:
            agent_zip = zip_match.group(1)
        city_match = re.search(
            r",\s*([^,]+),\s*(?:United States Virgin Islands|VI)", agent_phys
        )
        if city_match:
            agent_city = city_match.group(1).strip()

        # Agent start date
        agent_start = detail.get("Start Date", "")
        agent_eff_date = None
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", agent_start)
        if date_match:
            parts = date_match.group(1).split("/")
            agent_eff_date = f"{parts[2]}-{parts[0]}-{parts[1]}"

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, agent_type, address, city, state, zip, country, effective_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'US', ?)
                """,
                [
                    entity_id,
                    agent_name,
                    agent_type,
                    agent_phys or agent_mail or None,
                    agent_city,
                    agent_state,
                    agent_zip,
                    agent_eff_date,
                ],
            )
        except Exception:
            pass

    return entity_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="US Virgin Islands corporate registry (Catalyst platform)"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_out", help="Output raw JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name")
    p.add_argument("query", help="Entity name to search for")
    p.add_argument(
        "--contains",
        action="store_true",
        help="Use 'Contains' mode (default: 'Starts With')",
    )

    # detail
    p = sub.add_parser("detail", help="Fetch full detail by entity identifier")
    p.add_argument("entity_id", help="USVI entity identifier (e.g., 581737)")
    p.add_argument(
        "--name",
        help="Entity name hint for search (needed if entity not yet in registry.db)",
    )

    # ingest-entity
    p = sub.add_parser(
        "ingest-entity", help="Ingest specific entity into registry.db"
    )
    p.add_argument("entity_id", help="USVI entity identifier")
    p.add_argument(
        "--name",
        help="Entity name hint for search (needed if entity not yet in registry.db)",
    )

    # ingest-batch
    p = sub.add_parser(
        "ingest-batch", help="Search and ingest all matching entities"
    )
    p.add_argument(
        "queries", nargs="+", help="Search queries to ingest"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest even if already in registry.db",
    )

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "detail": cmd_detail,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
