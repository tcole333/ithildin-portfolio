#!/usr/bin/env python3
"""
New Mexico Secretary of State corporate registry ingester.

Uses the REST API behind enterprise.sos.nm.gov (no auth required).
Rate limited by Azure WAF — needs 3-5 sec delays between requests.

Usage:
    python tools/ingest_newmexico.py search "Zorro Ranch"
    python tools/ingest_newmexico.py search "Epstein" --type llc
    python tools/ingest_newmexico.py detail <internal_id>
    python tools/ingest_newmexico.py history <record_num>
    python tools/ingest_newmexico.py ingest-entity <internal_id>
    python tools/ingest_newmexico.py ingest-batch "Zorro"
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

BASE_URL = "https://enterprise.sos.nm.gov/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://enterprise.sos.nm.gov/search/business",
    "Origin": "https://enterprise.sos.nm.gov",
}

# Entity type IDs from the portal's configuration
ENTITY_TYPES = {
    "all": 0,
    "corp": 1,       # Domestic Profit Corporation
    "foreign_corp": 2,
    "nonprofit": 3,
    "llc": 4,         # Domestic LLC
    "foreign_llc": 5,
    "lp": 6,          # Domestic LP
    "llp": 7,
    "foreign_lp": 8,
    "foreign_llp": 9,
}

# Retry with backoff for WAF rate limiting
MAX_RETRIES = 3
BASE_DELAY = 4  # seconds between requests


def _request(url, retry=0):
    """Make API request with WAF-aware rate limiting."""
    req = Request(url, headers=HEADERS)
    try:
        time.sleep(BASE_DELAY)  # Courtesy delay
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        if e.code == 403 and retry < MAX_RETRIES:
            wait = (2 ** retry) * 10  # 10, 20, 40 seconds
            print(f"  WAF rate limit hit (403). Waiting {wait}s before retry {retry+1}...", file=sys.stderr)
            time.sleep(wait)
            return _request(url, retry + 1)
        body = e.read().decode()[:300]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach NM SoS: {e.reason}", file=sys.stderr)
        return None


def _search(query, query_type=2, entity_type=0, status_type=0):
    """Search NM business entities.

    query_type: 1=Starts With, 2=Contains, 4=Name Availability
    """
    params = {
        "SEARCH_VALUE": query,
        "QueryTypeId": query_type,
        "BusinessRecordTypeId": entity_type,
        "BusinessStatusTypeId": status_type,
    }
    url = f"{BASE_URL}/businessEntitySearch/webSearch?{urlencode(params)}"
    return _request(url)


def _get_detail(internal_id):
    """Get entity detail by internal ID."""
    url = f"{BASE_URL}/FilingDetail/business/{internal_id}/false"
    return _request(url)


def _get_history(record_num):
    """Get filing history by record number."""
    url = f"{BASE_URL}/History/business/{record_num}"
    return _request(url)


def _parse_detail_fields(detail_list):
    """Parse the DRAWER_DETAIL_LIST into a dict."""
    fields = {}
    for item in detail_list:
        label = item.get("LABEL", "")
        value = item.get("VALUE", "")
        fields[label] = value
    return fields


def _parse_agent_address(agent_str):
    """Parse agent string which contains name + address separated by newlines."""
    if not agent_str:
        return None, None, None, None, None
    parts = agent_str.replace("\r\n", "\n").split("\n")
    parts = [p.strip() for p in parts if p.strip()]
    name = parts[0] if parts else None
    address = parts[1] if len(parts) > 1 else None
    # Try to parse city/state/zip from remaining parts
    city, state, zipcode = None, None, None
    if len(parts) > 2:
        last_line = parts[-1]
        # Common format: "CITY, ST ZIPCODE" or "CITY, STATE ZIPCODE"
        if "," in last_line:
            city_part, rest = last_line.split(",", 1)
            city = city_part.strip()
            rest_parts = rest.strip().split()
            if rest_parts:
                state = rest_parts[0]
            if len(rest_parts) > 1:
                zipcode = rest_parts[1]
        # If address was the city/state/zip line, fix up
        if address and not city and "," in address:
            city_part, rest = address.split(",", 1)
            city = city_part.strip()
            rest_parts = rest.strip().split()
            if rest_parts:
                state = rest_parts[0]
            if len(rest_parts) > 1:
                zipcode = rest_parts[1]
            address = None
    return name, address, city, state, zipcode


def cmd_search(args):
    """Search NM entities."""
    entity_type = ENTITY_TYPES.get(args.type, 0) if args.type else 0
    data = _search(args.query, entity_type=entity_type)
    if not data:
        print("No results or error.")
        return

    rows = data.get("rows", {})
    print(f"Found {len(rows)} NM entities matching '{args.query}' (max 100)")
    print()

    for key, r in rows.items():
        title = r.get("TITLE", ["?", ""])[0]
        record_num = r.get("RECORD_NUM", "?")
        internal_id = r.get("ID", key)
        etype = r.get("BusinessRecordTypeId", "?")
        status = r.get("BusinessStatusId", "?")
        formed_in = r.get("FormationLocale", "")
        reg_date = r.get("RegistrationDate", "")
        agent = r.get("Agent", "")

        print(f"  [NM] {title} ({etype}, {status})")
        print(f"    Record #: {record_num} | Internal ID: {internal_id}")
        if reg_date:
            print(f"    Registered: {reg_date}")
        if formed_in:
            print(f"    Formed in: {formed_in}")
        if agent:
            print(f"    Agent: {agent}")
        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_detail(args):
    """Get full entity details."""
    data = _get_detail(args.internal_id)
    if not data:
        print("No detail found or error.")
        return

    detail_list = data.get("DRAWER_DETAIL_LIST", [])
    fields = _parse_detail_fields(detail_list)

    print(f"=== Entity Detail (ID: {args.internal_id}) ===")
    for label, value in fields.items():
        # Clean up multiline values
        clean = value.replace("\r\n", " | ").replace("\n", " | ").strip()
        if clean:
            print(f"  {label}: {clean}")

    print()
    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_history(args):
    """Get filing history for an entity."""
    data = _get_history(args.record_num)
    if not data:
        print("No history found or error.")
        return

    amendments = data.get("AMENDMENT_LIST", [])
    history = data.get("HISTORY_LIST", [])

    print(f"=== Filing History (Record #: {args.record_num}) ===")

    if amendments:
        print(f"\n  Filings ({len(amendments)}):")
        for a in amendments:
            date = a.get("AMENDMENT_DATE", "?")
            atype = a.get("AMENDMENT_TYPE", "?")
            anum = a.get("AMENDMENT_NUM", "")
            print(f"    {date}: {atype}" + (f" (#{anum})" if anum else ""))

    if history:
        print(f"\n  Field Changes ({len(history)}):")
        for h in history:
            field = h.get("DISPLAY_NAME", h.get("FIELD_NAME", "?"))
            old = h.get("CHANGED_FROM", "")
            new = h.get("CHANGED_TO", "")
            if old or new:
                print(f"    {field}: '{old}' → '{new}'")

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_ingest_entity(args):
    """Ingest a specific entity into registry.db."""
    db = get_db()

    # Get detail
    detail_data = _get_detail(args.internal_id)
    if not detail_data:
        print(f"Could not fetch detail for ID {args.internal_id}")
        return

    fields = _parse_detail_fields(detail_data.get("DRAWER_DETAIL_LIST", []))

    entity_name = fields.get("Business Name", "?")
    record_num = fields.get("Record #", str(args.internal_id))
    status_raw = fields.get("Status", "")
    etype_raw = fields.get("Entity Type", "")
    sub_type = fields.get("Entity Sub-Type", "")
    formed_in = fields.get("Formed In", "")
    filing_date = fields.get("Initial Filing Date", "")
    agent_raw = fields.get("Agent", "")
    organizers_raw = fields.get("Organizers and Incorporators", "")

    # Map status
    status_map = {
        "Active": "active",
        "Revoked Final": "dissolved",
        "Dissolved": "dissolved",
        "Revoked": "inactive",
        "Withdrawn": "dissolved",
    }
    status = status_map.get(status_raw, status_raw.lower() if status_raw else None)

    # Map entity type
    etype_map = {
        "Domestic Limited Liability Company": "llc",
        "Foreign Limited Liability Company": "foreign_llc",
        "Domestic Profit Corporation": "corp",
        "Foreign Profit Corporation": "foreign_corp",
        "Domestic Non-Profit Corporation": "nonprofit",
        "Domestic Limited Partnership": "lp",
        "Domestic Limited Liability Partnership": "llp",
    }
    etype = etype_map.get(etype_raw, sub_type.lower() if sub_type else etype_raw.lower() if etype_raw else None)

    # Parse filing date (MM/DD/YYYY → YYYY-MM-DD)
    formation_date = None
    if filing_date and "/" in filing_date:
        parts = filing_date.split("/")
        if len(parts) == 3:
            formation_date = f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"

    # Parse agent
    agent_name, agent_addr, agent_city, agent_state, agent_zip = _parse_agent_address(agent_raw)

    # Insert entity
    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, state_of_formation,
            source_url, raw_data
        ) VALUES ('nm', ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        record_num, entity_name, etype, status, formation_date, formed_in,
        f"https://enterprise.sos.nm.gov/search/business",
        json.dumps(fields, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='nm' AND source_id=?",
        [record_num]
    ).fetchone()
    entity_id = row[0]
    print(f"Ingested entity: {entity_name} (registry ID: {entity_id})")

    # Insert agent
    if agent_name:
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [entity_id, agent_name, agent_addr, agent_city, agent_state, agent_zip])
        except Exception:
            pass

    # Parse and insert organizers/incorporators as officers
    if organizers_raw:
        for line in organizers_raw.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            # Format: "NAME: Role"
            if ":" in line:
                name, role = line.split(":", 1)
                name = name.strip()
                role = role.strip().lower()
            else:
                name = line
                role = "organizer"
            if name:
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO registry_officers
                        (entity_id, officer_name, title, officer_type)
                        VALUES (?, ?, ?, 'person')
                    """, [entity_id, name, role])
                except Exception:
                    pass

    # Get filing history
    history_data = _get_history(record_num)
    if history_data:
        amendments = history_data.get("AMENDMENT_LIST", [])
        for a in amendments:
            date_raw = a.get("AMENDMENT_DATE", "")
            filing_date_iso = None
            if date_raw and "/" in date_raw:
                parts = date_raw.split("/")
                if len(parts) == 3:
                    filing_date_iso = f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"

            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_filings
                    (entity_id, filing_type, filing_date, description)
                    VALUES (?, ?, ?, ?)
                """, [entity_id, a.get("AMENDMENT_TYPE"), filing_date_iso,
                      a.get("AMENDMENT_TYPE")])
            except Exception:
                pass

        # Track field changes (e.g. agent changes)
        for h in history_data.get("HISTORY_LIST", []):
            field_name = h.get("DISPLAY_NAME", h.get("FIELD_NAME", ""))
            old_val = h.get("CHANGED_FROM", "")
            new_val = h.get("CHANGED_TO", "")
            if field_name == "Registered Agent" and old_val and old_val != "(None)":
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO registry_agents
                        (entity_id, agent_name, end_date)
                        VALUES (?, ?, datetime('now'))
                    """, [entity_id, old_val])
                except Exception:
                    pass

    db.commit()
    print(f"  History: {len(history_data.get('AMENDMENT_LIST', []))} filings loaded" if history_data else "  No history")


def cmd_ingest_batch(args):
    """Ingest all entities matching a search."""
    data = _search(args.query)
    if not data:
        print("No results or error.")
        return

    rows = data.get("rows", {})
    print(f"Ingesting {len(rows)} entities matching '{args.query}'")

    for i, (key, r) in enumerate(rows.items()):
        internal_id = r.get("ID", key)
        title = r.get("TITLE", ["?"])[0]
        print(f"\n  [{i+1}/{len(rows)}] {title}")

        # Create a mock args object for ingest_entity
        class IngestArgs:
            def __init__(self, iid):
                self.internal_id = iid
                self.json_out = False

        cmd_ingest_entity(IngestArgs(internal_id))

        if i < len(rows) - 1:
            time.sleep(2)  # Extra delay between batch items

    db = get_db()
    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
        VALUES ('nm', 'api', ?, ?)
    """, [len(rows), f"Batch search: {args.query}"])
    db.commit()

    try:
        _rebuild_fts(db)
    except Exception:
        pass

    print(f"\nBatch ingest complete: {len(rows)} entities")


def main():
    parser = argparse.ArgumentParser(description="New Mexico SoS corporate registry")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="Search entities")
    p.add_argument("query")
    p.add_argument("--type", choices=list(ENTITY_TYPES.keys()), help="Entity type filter")

    p = sub.add_parser("detail", help="Get entity detail by internal ID")
    p.add_argument("internal_id")

    p = sub.add_parser("history", help="Get filing history by record number")
    p.add_argument("record_num")

    p = sub.add_parser("ingest-entity", help="Ingest specific entity")
    p.add_argument("internal_id")

    p = sub.add_parser("ingest-batch", help="Ingest all matching entities")
    p.add_argument("query")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "detail": cmd_detail,
        "history": cmd_history,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
