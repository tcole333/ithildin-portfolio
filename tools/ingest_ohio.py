#!/usr/bin/env python3
"""
Ohio Secretary of State corporate registry integration.

Uses the Ohio SoS Business Search API at businesssearchapi.ohiosos.gov.
The API is behind Cloudflare Turnstile — requires a cf_clearance cookie
obtained from a real browser session.

To get the cookie:
  1. Open https://businesssearch.ohiosos.gov/ in Chrome
  2. Open DevTools > Application > Cookies > .ohiosos.gov
  3. Copy the cf_clearance value
  4. Pass via --cookie flag or OHIO_SOS_CF_CLEARANCE env var

API endpoints:
  NS_{query}_{status}  — Business Name Search
  EN_{query}_{status}  — Exact Name Search
  AE_{query}_{status}  — Agent/Registrant Search
  OI_{query}_{status}  — Organizer/Incorporator Search
  CI_{query}           — Charter Number Search
  VD_{charter_num}     — Entity Detail (filing history + registrant)

Status codes: X=All, A=Active, C=Cancelled, D=Dead, F=Fraudulent

Usage:
    python tools/ingest_ohio.py search "Wexner" --cookie "YOUR_CF_CLEARANCE"
    python tools/ingest_ohio.py search "Epstein" --status active
    python tools/ingest_ohio.py search-agent "Corporation Service Company"
    python tools/ingest_ohio.py detail 436405
    python tools/ingest_ohio.py ingest-entity 436405
    python tools/ingest_ohio.py ingest-batch "Wexner" "Epstein" "Limited Brands"
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as req_lib

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

SEARCH_URL = "https://businesssearch.ohiosos.gov/"
API_BASE = "https://businesssearchapi.ohiosos.gov"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_DELAY = 1.5

STATUS_MAP = {
    "all": "X",
    "active": "A",
    "cancelled": "C",
    "dead": "D",
    "fraudulent": "F",
}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_session = None
_cf_clearance = None


def _get_session():
    """Get or create a curl_cffi session with Chrome TLS impersonation."""
    global _session
    if _session is not None:
        return _session

    if not _cf_clearance:
        print("ERROR: No cf_clearance cookie provided.", file=sys.stderr)
        print("Get it from Chrome DevTools: Application > Cookies > .ohiosos.gov > cf_clearance", file=sys.stderr)
        print("Pass via --cookie FLAG or OHIO_SOS_CF_CLEARANCE env var.", file=sys.stderr)
        sys.exit(1)

    _session = req_lib.Session(impersonate="chrome")
    _session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Origin": SEARCH_URL.rstrip("/"),
        "Referer": SEARCH_URL,
    })
    _session.cookies.set("cf_clearance", _cf_clearance, domain=".ohiosos.gov")
    return _session


def _api_get(path):
    """Make a GET request to the Ohio SoS API."""
    session = _get_session()
    url = f"{API_BASE}/{path}"

    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 403:
            print(f"ERROR: HTTP 403 — cf_clearance cookie may be expired. Get a fresh one.", file=sys.stderr)
            return None
        else:
            print(f"ERROR: HTTP {resp.status_code} for {url}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Search functions
# ---------------------------------------------------------------------------

def search_business(query, status="all", limit=100):
    """Search Ohio entities by business name."""
    sc = STATUS_MAP.get(status.lower(), "X")
    path = f"NS_{quote(query.upper(), safe='')}_{sc}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return []
    return data["data"][:limit]


def search_exact(query, status="all", limit=100):
    """Search Ohio entities by exact business name."""
    sc = STATUS_MAP.get(status.lower(), "X")
    path = f"EN_{quote(query.upper(), safe='')}_{sc}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return []
    return data["data"][:limit]


def search_agent(query, status="all", limit=100):
    """Search Ohio entities by agent/registrant name."""
    sc = STATUS_MAP.get(status.lower(), "X")
    path = f"AE_{quote(query.upper(), safe='')}_{sc}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return []
    return data["data"][:limit]


def search_incorporator(query, status="all", limit=100):
    """Search Ohio entities by organizer/incorporator name."""
    sc = STATUS_MAP.get(status.lower(), "X")
    path = f"OI_{quote(query.upper(), safe='')}_{sc}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return []
    return data["data"][:limit]


def search_charter(charter_num):
    """Look up an entity by charter/entity number."""
    path = f"CI_{charter_num}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return []
    return data["data"]


def get_detail(charter_num):
    """Get full entity detail (registrant + filing history)."""
    path = f"VD_{charter_num}?_={int(time.time()*1000)}"
    data = _api_get(path)
    if not data or "data" not in data:
        return None

    result = {"charter_num": str(charter_num), "registrant": None, "filings": []}
    for section in data["data"]:
        if isinstance(section, dict):
            if "registrant" in section and section["registrant"]:
                result["registrant"] = section["registrant"][0]
            if "listing" in section and section["listing"]:
                result["filings"] = section["listing"]
    return result


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Search Ohio business entities by name."""
    results = search_business(args.query, status=args.status, limit=args.limit)
    log_search("ohio_sos", args.query, len(results))

    if write_output(results, args, summary=f"Ohio SoS '{args.query}'"):
        return

    total = results[0]["result_count"] if results and results[0].get("result_count") else len(results)
    print(f"Found {total} Ohio entities matching '{args.query}' (showing {len(results)})\n")

    for r in results:
        charter = r.get("charter_num", "?")
        name = r.get("business_name", "?")
        btype = r.get("business_type", "?")
        stat = r.get("status", "?")
        location = r.get("business_location", "-")
        county = r.get("county_name", "-")
        effect = (r.get("effect_date") or "")[:10]
        print(f"  [{charter}] {name}")
        print(f"    Type: {btype}  |  Status: {stat}  |  Filed: {effect}  |  {location}, {county}")
        print()


def cmd_search_agent(args):
    """Search Ohio entities by agent/registrant name."""
    results = search_agent(args.query, status=args.status, limit=args.limit)
    log_search("ohio_sos_agent", args.query, len(results))

    if write_output(results, args, summary=f"Ohio agent '{args.query}'"):
        return

    print(f"Found {len(results)} Ohio entities with agent matching '{args.query}'\n")
    for r in results:
        print(f"  [{r.get('charter_num','?')}] {r.get('business_name','?')} -- {r.get('status','?')}")


def cmd_search_incorporator(args):
    """Search Ohio entities by organizer/incorporator name."""
    results = search_incorporator(args.query, status=args.status, limit=args.limit)
    log_search("ohio_sos_incorporator", args.query, len(results))

    if write_output(results, args, summary=f"Ohio incorporator '{args.query}'"):
        return

    print(f"Found {len(results)} Ohio entities with incorporator matching '{args.query}'\n")
    for r in results:
        print(f"  [{r.get('charter_num','?')}] {r.get('business_name','?')} -- {r.get('status','?')}")


def cmd_detail(args):
    """Fetch full entity detail by charter number."""
    detail = get_detail(args.charter_num)
    if not detail:
        print(f"Entity {args.charter_num} not found")
        return

    if write_output(detail, args, summary=f"Ohio detail {args.charter_num}"):
        return

    reg = detail.get("registrant")
    filings = detail.get("filings", [])

    print(f"=== Ohio Entity Detail: {args.charter_num} ===\n")
    if reg:
        print(f"  Charter#:  {reg.get('charter_num', '?')}")
        print(f"  Status:    {reg.get('status', '?')}")
        print(f"  Contact:   {reg.get('contact_name', '?')}")
        addr = ", ".join(p for p in [reg.get("contact_addr1",""), reg.get("contact_addr2","")] if p)
        print(f"  Address:   {addr}")
        print(f"             {reg.get('contact_city','')}, {reg.get('contact_state','')} {reg.get('contact_zip9','')}")
        print(f"  Effective: {reg.get('effective_date_time', '?')}\n")

    if filings:
        print(f"  Filing History ({len(filings)} records):")
        for f in filings:
            print(f"    {f.get('effect_date','?')}  {f.get('tran_code_desc','?')}  [{f.get('img_status','')}]")
        print()


def cmd_ingest_entity(args):
    """Ingest a specific Ohio entity into registry.db."""
    search_results = search_charter(args.charter_num)
    if not search_results:
        print(f"Entity {args.charter_num} not found")
        return

    time.sleep(REQUEST_DELAY)
    detail = get_detail(args.charter_num)

    db = get_db()
    eid = _upsert_entity(db, search_results[0], detail)
    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass
    print(f"Ingested: {search_results[0].get('business_name','?')} ({args.charter_num}) -> registry ID {eid}")


def cmd_ingest_batch(args):
    """Search and ingest all entities matching multiple queries."""
    db = get_db()
    total = 0

    for query in args.queries:
        print(f"\n--- Searching: '{query}' ---")
        results = search_business(query, status=args.status)
        print(f"  Found {len(results)} results")

        for i, r in enumerate(results):
            charter = r.get("charter_num", "")
            name = r.get("business_name", "?")

            existing = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='oh' AND source_id=?",
                [charter],
            ).fetchone()
            if existing and not args.force:
                print(f"  [{i+1}/{len(results)}] SKIP: {name} ({charter})")
                continue

            print(f"  [{i+1}/{len(results)}] Ingesting: {name} ({charter})...")
            time.sleep(REQUEST_DELAY)
            detail = get_detail(charter)
            eid = _upsert_entity(db, r, detail)
            total += 1
            print(f"    -> registry ID {eid}")

        log_search("ohio_sos", query, len(results))

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass
    print(f"\nBatch ingest complete: {total} entities ingested")


# ---------------------------------------------------------------------------
# Registry DB integration
# ---------------------------------------------------------------------------

def _parse_date(raw):
    """Parse various date formats to YYYY-MM-DD."""
    if not raw or raw == "-":
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
              "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{2})", raw)
    if m:
        yr = int(m.group(3))
        yr = yr + 2000 if yr < 50 else yr + 1900
        return f"{yr}-{months.get(m.group(2),'01')}-{m.group(1)}"
    return None


def _map_entity_type(raw):
    if not raw:
        return None
    rl = raw.lower()
    if "non-profit" in rl or "nonprofit" in rl:
        return "nonprofit"
    if "limited liability" in rl:
        return "foreign_llc" if "foreign" in rl else "llc"
    if "limited partnership" in rl:
        return "foreign_lp" if "foreign" in rl else "lp"
    if "foreign" in rl:
        return "foreign_corp"
    if any(x in rl for x in ("corporation", "professional", "medical")):
        return "corp"
    if "trade name" in rl or "fictitious" in rl:
        return "trade_name"
    if "trust" in rl:
        return "trust"
    return rl.replace(" ", "_")


def _map_status(raw):
    if not raw:
        return None
    rl = raw.lower()
    m = {"active": "active", "dead": "inactive", "fraudulent": "revoked"}
    if rl in m:
        return m[rl]
    if "cancelled" in rl:
        return "cancelled"
    return rl


def _upsert_entity(db, search_data, detail=None):
    charter = str(search_data.get("charter_num", ""))
    name = search_data.get("business_name", "?")
    etype = _map_entity_type(search_data.get("business_type"))
    status = _map_status(search_data.get("status"))
    formation = _parse_date(search_data.get("effect_date"))
    expiry = _parse_date(search_data.get("expiry_date"))
    location = search_data.get("business_location", "")
    county = search_data.get("county_name", "")
    state = search_data.get("state_name", "OHIO")

    agent_name = agent_addr = agent_city = agent_state = agent_zip = None
    last_filing = None

    if detail:
        reg = detail.get("registrant")
        if reg:
            agent_name = reg.get("contact_name")
            parts = [reg.get("contact_addr1",""), reg.get("contact_addr2","")]
            agent_addr = ", ".join(p for p in parts if p) or None
            agent_city = reg.get("contact_city")
            agent_state = reg.get("contact_state")
            agent_zip = reg.get("contact_zip9")
        filings = detail.get("filings", [])
        if filings:
            last_filing = _parse_date(filings[-1].get("effect_date"))

    principal = None
    if location and location != "-":
        principal = location
        if county and county not in ("-", "Conversion"):
            principal += f", {county} County"

    raw = {"search": search_data}
    if detail:
        raw["detail"] = detail
    raw_json = json.dumps(raw, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='oh' AND source_id=?",
        [charter],
    ).fetchone()

    if existing:
        eid = existing[0]
        db.execute(
            """UPDATE registry_entities SET
                entity_name=?, entity_type=?, status=?, formation_date=?,
                dissolution_date=?, last_filing_date=?, state_of_formation=?,
                principal_address=?, principal_city=?, principal_state=?,
                principal_country='US', source_url=?, raw_data=?, updated_at=datetime('now')
            WHERE id=?""",
            [name, etype, status, formation, expiry, last_filing,
             state if state != "-" else "OHIO",
             principal, location if location != "-" else None, "OH",
             SEARCH_URL, raw_json, eid],
        )
    else:
        db.execute(
            """INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date, dissolution_date, last_filing_date, state_of_formation,
                principal_address, principal_city, principal_state, principal_country,
                source_url, raw_data
            ) VALUES ('oh',?,?,?,?,?,?,?,?,?,?,'OH','US',?,?)""",
            [charter, name, etype, status, formation, expiry, last_filing,
             state if state != "-" else "OHIO",
             principal, location if location != "-" else None,
             SEARCH_URL, raw_json],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='oh' AND source_id=?",
            [charter],
        ).fetchone()
        eid = row[0]

    if agent_name:
        try:
            db.execute(
                """INSERT OR REPLACE INTO registry_agents
                (entity_id, agent_name, agent_type, address, city, state, zip, country)
                VALUES (?, ?, 'person', ?, ?, ?, ?, 'US')""",
                [eid, agent_name, agent_addr, agent_city, agent_state, agent_zip],
            )
        except Exception:
            pass

    return eid


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ohio Secretary of State corporate registry")
    parser.add_argument("--cookie", help="cf_clearance cookie value from browser (or set OHIO_SOS_CF_CLEARANCE env)")
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name, help_text in [
        ("search", "Search by business name"),
        ("search-agent", "Search by agent/registrant"),
        ("search-incorporator", "Search by organizer/incorporator"),
    ]:
        p = sub.add_parser(cmd_name, help=help_text)
        p.add_argument("query", help="Search term")
        p.add_argument("--status", choices=list(STATUS_MAP.keys()), default="all")
        p.add_argument("--limit", type=int, default=100)
        add_output_args(p)

    p = sub.add_parser("detail", help="Get entity detail by charter number")
    p.add_argument("charter_num", help="Charter/entity number")
    add_output_args(p)

    p = sub.add_parser("ingest-entity", help="Ingest specific entity into registry.db")
    p.add_argument("charter_num", help="Charter/entity number")

    p = sub.add_parser("ingest-batch", help="Search and ingest all matching entities")
    p.add_argument("queries", nargs="+", help="Search queries")
    p.add_argument("--status", choices=list(STATUS_MAP.keys()), default="all")
    p.add_argument("--force", action="store_true", help="Re-ingest existing")

    args = parser.parse_args()

    # Set cf_clearance from --cookie flag or env var
    global _cf_clearance
    _cf_clearance = args.cookie or os.environ.get("OHIO_SOS_CF_CLEARANCE")

    handlers = {
        "search": cmd_search,
        "search-agent": cmd_search_agent,
        "search-incorporator": cmd_search_incorporator,
        "detail": cmd_detail,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
