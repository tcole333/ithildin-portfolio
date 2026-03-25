#!/usr/bin/env python3
"""
Texas Comptroller franchise tax entity search tool.

Queries the TX Comptroller data-search proxy (no auth required) for franchise
tax account status, entity details, officers, and registered agents.

Data covers all entities registered for franchise tax in Texas — corporations,
LLCs, LPs, LLPs, professional entities, and foreign-qualified entities.

Endpoints:
  - Search: GET /data-search/franchise-tax?name=X | ?taxpayerId=X | ?fileNumber=X
  - Detail: GET /data-search/franchise-tax/{taxpayerId}

Usage:
    python tools/query_texas.py search "EPSTEIN"
    python tools/query_texas.py search "APOLLO" --limit 50
    python tools/query_texas.py search --taxpayer-id 32044352170
    python tools/query_texas.py search --file-number 0801432227
    python tools/query_texas.py entity 32044352170
    python tools/query_texas.py ingest 32044352170
    python tools/query_texas.py ingest-search "EPSTEIN"
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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
# API CONFIGURATION
# ══════════════════════════════════════════════════════════

BASE_URL = "https://comptroller.texas.gov/data-search/franchise-tax"

# Rate limit: 1 request per second
RATE_LIMIT_SECONDS = 1.0
_last_request_time = 0.0

# Entity status mapping: TX rightToTransactTX → registry status
STATUS_MAP = {
    "ACTIVE": "active",
    "ACTIVE, ELIGIBLE FOR TERMINATION/WITHDRAWAL": "active",
    "FORFEITED": "forfeited",
    "NOT ESTABLISHED": "inactive",
    "FRANCHISE TAX ENDED": "inactive",
    "FRANCHISE TAX INVOLUNTARILY ENDED": "inactive",
}

# SoS registration status → registry status (fallback)
SOS_STATUS_MAP = {
    "ACTIVE": "active",
    "INACTIVE": "inactive",
    "FORFEITED": "forfeited",
}


# ══════════════════════════════════════════════════════════
# HTTP HELPERS
# ══════════════════════════════════════════════════════════

def _api_get(url, retries=3):
    """Make a GET request to the TX Comptroller data-search proxy."""
    global _last_request_time

    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (compatible; OSINT-research/1.0)",
    }
    req = Request(url, headers=headers)

    for attempt in range(retries):
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                if not body.strip():
                    return None
                return json.loads(body)
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = (attempt + 1) * 3
                print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 413:
                err_body = e.read().decode()[:500]
                try:
                    err_data = json.loads(err_body)
                    print(f"ERROR: {err_data.get('error', 'Query too large')}", file=sys.stderr)
                except json.JSONDecodeError:
                    print(f"ERROR: HTTP 413 — query too large, refine search", file=sys.stderr)
                return None
            err_body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code}: {err_body}", file=sys.stderr)
            return None
        except (URLError, TimeoutError) as e:
            wait = (attempt + 1) * 3
            print(f"  Connection error: {e}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

    print("ERROR: Max retries exceeded", file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════════
# SEARCH COMMAND
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search TX franchise tax entities by name, taxpayer ID, or file number."""
    if args.taxpayer_id:
        url = f"{BASE_URL}?taxpayerId={quote(args.taxpayer_id)}"
        search_desc = f"taxpayer ID {args.taxpayer_id}"
    elif args.file_number:
        url = f"{BASE_URL}?fileNumber={quote(args.file_number)}"
        search_desc = f"file number {args.file_number}"
    elif args.query:
        url = f"{BASE_URL}?name={quote(args.query)}"
        search_desc = f"name '{args.query}'"
    else:
        print("ERROR: Provide a search query, --taxpayer-id, or --file-number", file=sys.stderr)
        return

    data = _api_get(url)
    if not data or not data.get("success"):
        error = data.get("error") if data else "No response"
        print(f"Search failed: {error}")
        return

    results = data.get("data", [])
    total = data.get("count", len(results))

    # Apply limit
    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    log_search(args.query or args.taxpayer_id or args.file_number, "tx_comptroller", len(results))

    if write_output(results, args, summary=f"TX search {search_desc} ({len(results)} of {total})"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {len(results)} of {total} TX entities matching {search_desc}")
    print()
    for r in results:
        name = r.get("name", "?")
        tid = r.get("taxpayerId", "?")
        zipcode = r.get("mailingAddressZip", "")
        print(f"  {name}")
        print(f"    Taxpayer ID: {tid} | Zip: {zipcode}")
        print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL COMMAND
# ══════════════════════════════════════════════════════════

def _fetch_detail(taxpayer_id):
    """Fetch full entity detail by taxpayer ID."""
    url = f"{BASE_URL}/{quote(str(taxpayer_id))}"
    data = _api_get(url)
    if not data or not data.get("success"):
        return None
    return data.get("data")


def cmd_entity(args):
    """Get full entity detail by taxpayer ID."""
    detail = _fetch_detail(args.taxpayer_id)
    if not detail:
        print(f"Entity {args.taxpayer_id} not found")
        return

    if write_output(detail, args, summary=f"TX entity {args.taxpayer_id}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(detail, indent=2, default=str))
        return

    # Pretty print
    print(f"\n  [TX] {detail.get('name', '?')}")
    if detail.get("dbaName"):
        print(f"    DBA: {detail['dbaName']}")
    print(f"    Taxpayer ID: {detail.get('taxpayerId', '?')}")
    if detail.get("feiNumber"):
        print(f"    FEI/EIN: {detail['feiNumber']}")
    print(f"    Right to Transact: {detail.get('rightToTransactTX', '?')}")
    if detail.get("stateOfFormation"):
        print(f"    State of Formation: {detail['stateOfFormation'].strip()}")

    # SoS info
    sos_status = detail.get("sosRegistrationStatus")
    sos_date = detail.get("effectiveSosRegistrationDate")
    sos_file = detail.get("sosFileNumber")
    if sos_status or sos_file:
        print(f"    SoS Status: {sos_status or '?'}", end="")
        if sos_date:
            print(f" (effective {sos_date})", end="")
        if sos_file:
            print(f" | File #: {sos_file}", end="")
        print()

    # Mailing address
    street = detail.get("mailingAddressStreet", "")
    city = detail.get("mailingAddressCity", "")
    state = detail.get("mailingAddressState", "")
    zipcode = detail.get("mailingAddressZip", "")
    zip4 = detail.get("mailingAddressZip4", "")
    if street:
        addr_parts = [street]
        cs = ", ".join(filter(None, [city, state]))
        if cs:
            z = f"{zipcode}-{zip4}" if zip4 else zipcode
            addr_parts.append(f"{cs} {z}".strip())
        print(f"    Mailing: {', '.join(addr_parts)}")

    # Registered Agent
    ra_name = detail.get("registeredAgentName")
    if ra_name:
        ra_street = detail.get("registeredOfficeAddressStreet", "")
        ra_city = detail.get("registeredOfficeAddressCity", "")
        ra_state = detail.get("registeredOfficeAddressState", "")
        ra_zip = detail.get("registeredOfficeAddressZip", "")
        print(f"\n    Registered Agent: {ra_name}")
        if ra_street:
            ra_parts = [ra_street]
            ra_cs = ", ".join(filter(None, [ra_city, ra_state]))
            if ra_cs:
                ra_parts.append(f"{ra_cs} {ra_zip}".strip())
            print(f"      Address: {', '.join(ra_parts)}")

    # Officers
    officers = detail.get("officerInfo", [])
    if officers:
        print(f"\n    Officers ({len(officers)}):")
        for o in officers:
            oname = o.get("AGNT_NM", "?")
            title = o.get("AGNT_TITL_TX", "?")
            year = o.get("AGNT_ACTV_YR", "")
            street = o.get("AD_STR_POB_TX", "")
            ocity = o.get("CITY_NM", "")
            ostate = o.get("ST_CD", "")
            ozip = o.get("AD_ZP", "")
            print(f"      {oname} — {title}" + (f" ({year})" if year else ""))
            if street:
                addr = ", ".join(filter(None, [street, ocity, f"{ostate} {ozip}".strip()]))
                print(f"        {addr}")

    print(f"\n    Report Year: {detail.get('reportYear', '?')}")
    if detail.get("lastUpdated"):
        print(f"    Last Updated: {detail['lastUpdated'][:10]}")
    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _ingest_entity_to_registry(db, taxpayer_id, detail=None):
    """Fetch entity detail and ingest into registry.db. Returns entity_id or None."""
    if not detail:
        detail = _fetch_detail(taxpayer_id)
    if not detail:
        return None

    name = detail.get("name", "?")
    dba = detail.get("dbaName")
    ein = detail.get("feiNumber")
    tid = detail.get("taxpayerId", str(taxpayer_id))

    # Status: prefer rightToTransactTX, fallback to sosRegistrationStatus
    right_tx = (detail.get("rightToTransactTX") or "").upper()
    status = STATUS_MAP.get(right_tx)
    if not status:
        sos_raw = (detail.get("sosRegistrationStatus") or "").upper()
        status = SOS_STATUS_MAP.get(sos_raw, sos_raw.lower() if sos_raw else None)

    # Formation date from SoS registration
    formation_date = detail.get("effectiveSosRegistrationDate")
    if formation_date and "/" in formation_date:
        parts = formation_date.split("/")
        if len(parts) == 3:
            formation_date = f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"

    state_of_formation = (detail.get("stateOfFormation") or "").strip()

    # Mailing address
    mail_street = (detail.get("mailingAddressStreet") or "").strip()
    mail_city = (detail.get("mailingAddressCity") or "").strip()
    mail_state = (detail.get("mailingAddressState") or "").strip()
    mail_zip = (detail.get("mailingAddressZip") or "").strip()

    sos_file = detail.get("sosFileNumber") or ""
    source_url = f"https://comptroller.texas.gov/taxes/franchise/account-status/search/{tid}"

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, ein,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip, mailing_country,
            state_of_formation, purpose, source_url, raw_data
        ) VALUES ('tx', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?, ?, 'US', ?, ?, ?, ?)
    """, [
        tid, name, status, formation_date or None, ein or None,
        mail_street or None, mail_city or None, mail_state or None, mail_zip or None,
        mail_street or None, mail_city or None, mail_state or None, mail_zip or None,
        state_of_formation or None,
        f"SoS File: {sos_file}" if sos_file else None,
        source_url,
        json.dumps(detail, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='tx' AND source_id=?",
        [tid]
    ).fetchone()
    entity_id = row[0]

    # Officers
    for o in detail.get("officerInfo", []):
        oname = (o.get("AGNT_NM") or "").strip()
        if not oname:
            continue
        title = (o.get("AGNT_TITL_TX") or "").strip()
        street = (o.get("AD_STR_POB_TX") or "").strip()
        city = (o.get("CITY_NM") or "").strip()
        state = (o.get("ST_CD") or "").strip()
        zipcode = (o.get("AD_ZP") or "").strip()
        year = (o.get("AGNT_ACTV_YR") or "").strip()
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip, effective_date)
                VALUES (?, ?, ?, 'person', ?, ?, ?, ?, ?)
            """, [
                entity_id, oname, title or None,
                street or None, city or None, state or None, zipcode or None,
                f"{year}-01-01" if year else None,
            ])
        except Exception:
            pass

    # Registered Agent
    ra_name = (detail.get("registeredAgentName") or "").strip()
    if ra_name:
        ra_street = (detail.get("registeredOfficeAddressStreet") or "").strip()
        ra_city = (detail.get("registeredOfficeAddressCity") or "").strip()
        ra_state = (detail.get("registeredOfficeAddressState") or "").strip()
        ra_zip = (detail.get("registeredOfficeAddressZip") or "").strip()
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                entity_id, ra_name,
                ra_street or None, ra_city or None, ra_state or None, ra_zip or None,
            ])
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a single entity by taxpayer ID into registry.db."""
    db = get_db()
    entity_id = _ingest_entity_to_registry(db, args.taxpayer_id)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        row = db.execute("SELECT entity_name FROM registry_entities WHERE id=?", [entity_id]).fetchone()
        name = row[0] if row else "?"
        print(f"Ingested: {name} (TX #{args.taxpayer_id}, registry ID: {entity_id})")
    else:
        print(f"Failed to ingest TX taxpayer ID {args.taxpayer_id}")


def cmd_ingest_search(args):
    """Search for entities and ingest all results into registry.db."""
    url = f"{BASE_URL}?name={quote(args.query)}"
    data = _api_get(url)
    if not data or not data.get("success"):
        print("Search failed")
        return

    results = data.get("data", [])
    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} TX entities. Fetching details and ingesting...")

    db = get_db()
    ingested = 0
    for i, r in enumerate(results):
        tid = r.get("taxpayerId", "")
        name = r.get("name", "?")
        if not tid:
            continue

        entity_id = _ingest_entity_to_registry(db, tid)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {name} (TX #{tid}, reg ID: {entity_id})")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} (TX #{tid})")

        if ingested % 10 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    try:
        db.execute("""
            INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('tx', 'api', ?, ?)
        """, [ingested, f"TX Comptroller franchise tax search: '{args.query}'"])
        db.commit()
    except Exception:
        pass

    log_search(args.query, "tx_comptroller-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="TX Comptroller franchise tax entity search (no auth required)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search TX entities by name, taxpayer ID, or file number")
    p.add_argument("query", nargs="?", help="Entity name search query")
    p.add_argument("--taxpayer-id", dest="taxpayer_id", help="Search by taxpayer ID")
    p.add_argument("--file-number", dest="file_number", help="Search by SoS file number")
    p.add_argument("--limit", type=int, default=100, help="Max results (default: 100)")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity detail by taxpayer ID")
    p.add_argument("taxpayer_id", help="TX taxpayer ID number")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest a single entity into registry.db")
    p.add_argument("taxpayer_id", help="TX taxpayer ID to ingest")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--limit", type=int, default=50, help="Max entities to ingest (default: 50)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
