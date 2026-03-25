#!/usr/bin/env python3
"""
New Jersey Division of Revenue business entity search tool.

Scrapes the NJ Portal Business Name Search at njportal.com/DOR/BusinessNameSearch.
The portal uses HTML form POST with CSRF tokens (no REST API). Returns 5 fields
per entity: name, entity_id, city, type, formation_date.

NOTE: NJ does not expose entity detail pages, officer/agent data, or filing
history through the free search portal. The paid Business Records Service at
njportal.com/DOR/businessrecords/ has richer data but requires account + payment.

Endpoints:
  - BusinessName: POST /DOR/BusinessNameSearch/Search/BusinessName
  - EntityId:     POST /DOR/BusinessNameSearch/Search/EntityId
  - Keywords:     POST /DOR/BusinessNameSearch/Search/Keywords

Usage:
    python tools/query_newjersey.py search "EPSTEIN"
    python tools/query_newjersey.py search "EPSTEIN" --keywords
    python tools/query_newjersey.py entity 0600092144
    python tools/query_newjersey.py ingest 0600092144
    python tools/query_newjersey.py ingest-search "EPSTEIN" --limit 50
"""

import argparse
import html as html_mod
import json
import re
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

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
    try:
        from query_registry import get_db, _rebuild_fts
    except ImportError:
        def get_db():
            raise RuntimeError("query_registry not available")
        def _rebuild_fts(db):
            pass


BASE_URL = "https://www.njportal.com/DOR/BusinessNameSearch"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
REQUEST_DELAY = 1.5  # seconds between requests

# NJ entity type abbreviations -> unified schema
TYPE_MAP = {
    "DP": "corp",                   # Domestic Profit Corporation
    "NP": "nonprofit",              # Domestic Non-Profit Corporation
    "FR": "foreign_corp",           # Foreign For-Profit Corporation
    "FN": "foreign_nonprofit",      # Foreign Non-Profit Corporation
    "PA": "professional_corp",      # Professional Corporation
    "LLC": "llc",                   # Domestic Limited Liability Company
    "FLC": "foreign_llc",           # Foreign Limited Liability Company
    "LLP": "llp",                   # Domestic Limited Liability Partnership
    "FLP": "foreign_llp",           # Foreign Limited Liability Partnership
    "LP": "lp",                     # Limited Partnership
    "NJ": "government",            # New Jersey (state entity)
}

# Full names from <abbr title="..."> tags
TYPE_FULL_NAMES = {
    "DP": "Domestic Profit Corporation",
    "NP": "Domestic Non-Profit Corporation",
    "FR": "Foreign For-Profit Corporation",
    "FN": "Foreign Non-Profit Corporation",
    "PA": "Professional Corporation",
    "LLC": "Domestic Limited Liability Company",
    "FLC": "Foreign Limited Liability Company",
    "LLP": "Domestic Limited Liability Partnership",
    "FLP": "Foreign Limited Liability Partnership",
    "LP": "Limited Partnership",
    "NJ": "New Jersey State Entity",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_session():
    """Create opener with cookie support."""
    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))
    return opener


def _fetch(opener, url, data=None, extra_headers=None):
    """Fetch URL with proper headers."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if extra_headers:
        headers.update(extra_headers)

    if data is not None:
        if isinstance(data, dict):
            data = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = Request(url, data=data, headers=headers)

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


def _get_csrf_token(opener, search_url):
    """GET the search page and extract CSRF token."""
    page_html = _fetch(opener, search_url)
    if not page_html:
        return None, None

    token_match = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page_html
    )
    if not token_match:
        print("ERROR: Could not find CSRF token", file=sys.stderr)
        return None, None

    return token_match.group(1), page_html


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_results_table(page_html):
    """Parse search results HTML table.

    Returns list of dicts with: entity_name, entity_id, city, type_code,
    type_full, formation_date.
    """
    results = []

    # The table has class "table table-data js-data-table"
    # Rows: <tr><td>NAME</td><td>ID</td><td>CITY</td><td><abbr title="FULL">CODE</abbr></td><td>DATE</td></tr>
    row_pattern = re.compile(
        r'<tr>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>\s*'
        r'<td[^>]*>\s*(\d{10})\s*</td>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>\s*'
        r'<td[^>]*>\s*(?:<abbr\s+title="([^"]*)">\s*)?(\w+)(?:\s*</abbr>)?\s*</td>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>\s*'
        r'</tr>',
        re.DOTALL,
    )

    for match in row_pattern.finditer(page_html):
        name = html_mod.unescape(match.group(1).strip())
        eid = match.group(2)
        city = html_mod.unescape(match.group(3).strip())
        type_full = html_mod.unescape(match.group(4).strip()) if match.group(4) else ""
        type_code = match.group(5).strip()
        date_raw = html_mod.unescape(match.group(6).strip())

        # Convert date from M/D/YYYY to YYYY-MM-DD
        formation_date = _parse_date(date_raw)

        results.append({
            "entity_name": name,
            "entity_id": eid,
            "city": city,
            "type_code": type_code,
            "type_full": type_full or TYPE_FULL_NAMES.get(type_code, type_code),
            "formation_date": formation_date,
            "formation_date_raw": date_raw,
        })

    return results


def _parse_date(date_str):
    """Convert M/D/YYYY or MM/DD/YYYY to YYYY-MM-DD."""
    if not date_str:
        return None
    match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_str)
    if match:
        return f"{match.group(3)}-{match.group(1).zfill(2)}-{match.group(2).zfill(2)}"
    return None


# ---------------------------------------------------------------------------
# Search functions
# ---------------------------------------------------------------------------

def search_by_name(query, limit=None):
    """Search NJ entities by business name."""
    opener = _make_session()
    url = f"{BASE_URL}/Search/BusinessName"

    token, _ = _get_csrf_token(opener, url)
    if not token:
        return []

    time.sleep(REQUEST_DELAY)

    data = {
        "__RequestVerificationToken": token,
        "BusinessName": query,
    }
    result_html = _fetch(opener, url, data=data, extra_headers={
        "Origin": "https://www.njportal.com",
        "Referer": url,
    })
    if not result_html:
        return []

    results = _parse_results_table(result_html)
    if limit:
        results = results[:limit]
    return results


def search_by_keywords(query, limit=None):
    """Search NJ entities by keywords (broader than name search)."""
    opener = _make_session()
    url = f"{BASE_URL}/Search/Keywords"

    token, _ = _get_csrf_token(opener, url)
    if not token:
        return []

    time.sleep(REQUEST_DELAY)

    # Split query into up to 5 keywords
    words = query.split()[:5]
    data = {"__RequestVerificationToken": token}
    for i, word in enumerate(words, 1):
        data[f"Keyword{i}"] = word

    result_html = _fetch(opener, url, data=data, extra_headers={
        "Origin": "https://www.njportal.com",
        "Referer": url,
    })
    if not result_html:
        return []

    results = _parse_results_table(result_html)
    if limit:
        results = results[:limit]
    return results


def search_by_entity_id(entity_id):
    """Search NJ entities by exact entity ID (10 digits)."""
    opener = _make_session()
    url = f"{BASE_URL}/Search/EntityId"

    token, _ = _get_csrf_token(opener, url)
    if not token:
        return []

    time.sleep(REQUEST_DELAY)

    # Pad to 10 digits
    eid = str(entity_id).zfill(10)

    data = {
        "__RequestVerificationToken": token,
        "EntityId": eid,
    }
    result_html = _fetch(opener, url, data=data, extra_headers={
        "Origin": "https://www.njportal.com",
        "Referer": url,
    })
    if not result_html:
        return []

    return _parse_results_table(result_html)


# ---------------------------------------------------------------------------
# Registry DB integration
# ---------------------------------------------------------------------------

def _upsert_entity(db, entity):
    """Insert or update a NJ entity in registry.db."""
    source_id = entity["entity_id"]
    name = entity["entity_name"]
    type_code = entity.get("type_code", "")
    etype = TYPE_MAP.get(type_code, type_code.lower() if type_code else None)
    formation_date = entity.get("formation_date")
    city = entity.get("city", "")

    source_url = f"https://www.njportal.com/DOR/BusinessNameSearch/Search/EntityId#{source_id}"
    raw_data = json.dumps(entity, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='nj' AND source_id=?",
        [source_id],
    ).fetchone()

    if existing:
        entity_id = existing[0]
        db.execute(
            """UPDATE registry_entities SET
                entity_name=?, entity_type=?, formation_date=?,
                principal_city=?, principal_state=?, principal_country=?,
                state_of_formation=?, source_url=?, raw_data=?,
                updated_at=datetime('now')
            WHERE id=?""",
            [name, etype, formation_date, city or None, "NJ", "US",
             "New Jersey", source_url, raw_data, entity_id],
        )
    else:
        db.execute(
            """INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type,
                formation_date, principal_city, principal_state, principal_country,
                state_of_formation, source_url, raw_data
            ) VALUES ('nj', ?, ?, ?, ?, ?, 'NJ', 'US', 'New Jersey', ?, ?)""",
            [source_id, name, etype, formation_date, city or None,
             source_url, raw_data],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='nj' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    return entity_id


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Search NJ business entities."""
    if args.keywords:
        results = search_by_keywords(args.query, limit=args.limit)
        search_mode = "keywords"
    else:
        results = search_by_name(args.query, limit=args.limit)
        search_mode = "name"

    log_search(args.query, "nj_corp_registry", len(results))

    if write_output(results, args, summary=f"NJ {search_mode} search '{args.query}'"):
        return

    print(f"Found {len(results)} NJ entities matching '{args.query}' ({search_mode} search)")
    print()
    for r in results:
        eid = r["entity_id"]
        name = r["entity_name"]
        city = r.get("city", "")
        tcode = r.get("type_code", "?")
        fdate = r.get("formation_date_raw", "?")
        print(f"  {eid} | {name} | {city} | {tcode} | {fdate}")
    print()


def cmd_entity(args):
    """Look up NJ entity by ID."""
    results = search_by_entity_id(args.entity_id)
    log_search(args.entity_id, "nj_corp_registry", len(results))

    if not results:
        print(f"No NJ entity found with ID {args.entity_id}")
        return

    if write_output(results[0], args, summary=f"NJ entity {args.entity_id}"):
        return

    r = results[0]
    print(f"=== NJ Entity {r['entity_id']} ===")
    print(f"  Name: {r['entity_name']}")
    print(f"  Type: {r['type_full']} ({r['type_code']})")
    print(f"  City: {r.get('city', 'N/A')}")
    print(f"  Incorporated: {r.get('formation_date_raw', 'N/A')}")
    print()
    print("NOTE: NJ free portal does not provide officer, agent, or filing data.")
    print("For richer data, use the paid NJ Business Records Service.")


def cmd_ingest(args):
    """Ingest a specific NJ entity into registry.db."""
    results = search_by_entity_id(args.entity_id)
    if not results:
        print(f"No NJ entity found with ID {args.entity_id}")
        return

    db = get_db()
    rid = _upsert_entity(db, results[0])
    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    name = results[0]["entity_name"]
    print(f"Ingested: {name} ({args.entity_id}) -> registry ID {rid}")


def cmd_ingest_search(args):
    """Search and ingest all matching NJ entities."""
    if args.keywords:
        results = search_by_keywords(args.query, limit=args.limit)
    else:
        results = search_by_name(args.query, limit=args.limit)

    if not results:
        print(f"No NJ entities found for '{args.query}'")
        return

    db = get_db()
    ingested = 0
    for r in results:
        rid = _upsert_entity(db, r)
        ingested += 1
        print(f"  [{ingested}/{len(results)}] {r['entity_name']} ({r['entity_id']}) -> {rid}")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    log_search(args.query, "nj_corp_registry", len(results))
    print(f"\nIngested {ingested} NJ entities for '{args.query}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="New Jersey Division of Revenue business entity search"
    )
    add_output_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name or keywords")
    p.add_argument("query", help="Search term")
    p.add_argument("--keywords", action="store_true",
                   help="Use keyword search (broader, AND logic across words)")
    p.add_argument("--limit", type=int, default=None, help="Max results")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Look up entity by ID")
    p.add_argument("entity_id", help="NJ entity ID (10 digits)")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db")
    p.add_argument("entity_id", help="NJ entity ID (10 digits)")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matches")
    p.add_argument("query", help="Search term")
    p.add_argument("--keywords", action="store_true",
                   help="Use keyword search")
    p.add_argument("--limit", type=int, default=50, help="Max results to ingest")

    args = parser.parse_args()
    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
