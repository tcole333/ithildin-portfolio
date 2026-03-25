#!/usr/bin/env python3
"""
New York Department of State corporate registry ingester.

Uses the Socrata SODA API to query data.ny.gov datasets:
  - Active Corporations (n9v6-gdp6): 4.1M+ entities
  - All Filings (63wc-4exh): 20M+ filing records
  - All Filings - Addresses (2tms-hftb): ~17M address records

Usage:
    python tools/ingest_newyork.py search "Epstein"
    python tools/ingest_newyork.py search "LSJE" --limit 50
    python tools/ingest_newyork.py search-officers "Darren Indyke"
    python tools/ingest_newyork.py search-address "457 Madison"
    python tools/ingest_newyork.py search-address "71st"
    python tools/ingest_newyork.py ingest-entity <DOS_ID>    # Ingest specific entity + filings
    python tools/ingest_newyork.py ingest-batch "Epstein"    # Ingest all matching entities
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

# Load .env for optional app token
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

# Dataset IDs
ACTIVE_CORPS_ID = "n9v6-gdp6"
ALL_FILINGS_ID = "63wc-4exh"
FILINGS_ADDR_ID = "2tms-hftb"

BASE_URL = "https://data.ny.gov/resource"


def _soda_request(dataset_id, params, limit=1000):
    """Make a SODA API request."""
    url = f"{BASE_URL}/{dataset_id}.json"
    params["$limit"] = limit

    token = os.environ.get("NY_SODA_APP_TOKEN")
    if token:
        params["$$app_token"] = token

    full_url = url + "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    req = Request(full_url, headers=headers)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return []


def _soda_paginate(dataset_id, where_clause, limit=5000, max_results=50000):
    """Paginate through SODA results."""
    all_results = []
    offset = 0
    page_size = min(limit, 5000)

    while len(all_results) < max_results:
        params = {
            "$where": where_clause,
            "$limit": page_size,
            "$offset": offset,
        }
        batch = _soda_request(dataset_id, params, limit=page_size)
        if not batch:
            break
        all_results.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
        time.sleep(0.5)

    return all_results[:max_results]


def cmd_search(args):
    """Search NY active corporations by name."""
    # SoQL uses upper() for case-insensitive LIKE
    where = f"upper(current_entity_name) LIKE upper('%{args.query}%')"
    results = _soda_request(ACTIVE_CORPS_ID, {"$where": where}, limit=args.limit)

    print(f"Found {len(results)} NY entities matching '{args.query}'")
    print()
    for r in results:
        name = r.get("current_entity_name", "?")
        dos_id = r.get("dos_id", "?")
        etype = r.get("entity_type", r.get("entity_type", "?"))
        status = r.get("current_status", "?")
        filed = r.get("initial_dos_filing_date", "")
        county = r.get("county", "")
        jurisdiction = r.get("jurisdiction", "")

        print(f"  [NY] {name} ({etype}, {status})")
        print(f"    DOS ID: {dos_id}")
        if filed:
            print(f"    Filed: {filed[:10]}")
        if county:
            print(f"    County: {county}")
        if jurisdiction and jurisdiction != "NEW YORK":
            print(f"    Jurisdiction: {jurisdiction}")

        # Process server (serves as principal address)
        ps_name = r.get("dos_process_name", "")
        ps_addr = r.get("dos_process_address_1", "")
        if ps_addr:
            addr = ps_addr
            if r.get("dos_process_address_2"):
                addr += " " + r["dos_process_address_2"]
            city = r.get("dos_process_city", "")
            state = r.get("dos_process_state", "")
            zipcode = r.get("dos_process_zip", "")
            print(f"    Process server: {ps_name}")
            print(f"    Address: {addr}, {city}, {state} {zipcode}")

        # CEO
        ceo_name = r.get("chairman_name", "")
        if ceo_name:
            ceo_addr = r.get("chairman_address_1", "")
            print(f"    CEO: {ceo_name}")
            if ceo_addr:
                ceo_full = ceo_addr
                if r.get("chairman_city"):
                    ceo_full += f", {r['chairman_city']}"
                if r.get("chairman_state"):
                    ceo_full += f", {r['chairman_state']}"
                print(f"    CEO addr: {ceo_full}")

        # Registered agent
        ra_name = r.get("registered_agent_name", "")
        if ra_name:
            print(f"    Reg agent: {ra_name}")

        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_search_officers(args):
    """Search by CEO/officer name across active corporations."""
    where = f"upper(chairman_name) LIKE upper('%{args.name}%')"
    results = _soda_request(ACTIVE_CORPS_ID, {"$where": where}, limit=args.limit)

    print(f"Found {len(results)} entities with CEO/Chairman matching '{args.name}'")
    print()
    for r in results:
        name = r.get("current_entity_name", "?")
        dos_id = r.get("dos_id", "?")
        ceo = r.get("chairman_name", "?")
        status = r.get("current_status", "?")
        print(f"  {ceo} — [{r.get('entity_type', '?')}] {name} (DOS: {dos_id}, {status})")
        ceo_addr = r.get("chairman_address_1", "")
        if ceo_addr:
            city = r.get("chairman_city", "")
            state = r.get("chairman_state", "")
            print(f"    Address: {ceo_addr}, {city}, {state}")
        print()


def cmd_search_address(args):
    """Search by address across active corporations."""
    # Search process server, CEO, and registered agent addresses
    clauses = [
        f"upper(dos_process_address_1) LIKE upper('%{args.query}%')",
        f"upper(chairman_address_1) LIKE upper('%{args.query}%')",
        f"upper(registered_agent_address_1) LIKE upper('%{args.query}%')",
    ]
    where = " OR ".join(clauses)
    results = _soda_request(ACTIVE_CORPS_ID, {"$where": where}, limit=args.limit)

    print(f"Found {len(results)} entities with address matching '{args.query}'")
    print()
    for r in results:
        name = r.get("current_entity_name", "?")
        dos_id = r.get("dos_id", "?")
        status = r.get("current_status", "?")
        print(f"  [NY] {name} (DOS: {dos_id}, {status})")

        # Show which address matched
        addr_fields = [
            ("dos_process", "Process server"),
            ("chairman", "CEO/Chairman"),
            ("registered_agent", "Reg agent"),
        ]
        for prefix, label in addr_fields:
            addr = r.get(f"{prefix}_address_1", "")
            if addr and args.query.upper() in addr.upper():
                pname = r.get(f"{prefix}_name", "")
                city = r.get(f"{prefix}_city", "")
                state = r.get(f"{prefix}_state", "")
                print(f"    {label}: {pname}")
                print(f"    Address: {addr}, {city}, {state}")
        print()


def cmd_ingest_entity(args):
    """Ingest a specific entity by DOS ID into registry.db."""
    db = get_db()

    # Fetch entity
    results = _soda_request(ACTIVE_CORPS_ID, {"$where": f"dos_id='{args.dos_id}'"}, limit=1)
    if not results:
        print(f"Entity DOS ID {args.dos_id} not found in active corporations")
        return

    r = results[0]
    entity_id = _upsert_entity(db, r)
    print(f"Ingested entity: {r.get('current_entity_name')} (registry ID: {entity_id})")

    # Fetch filings
    time.sleep(0.5)
    filings = _soda_paginate(ALL_FILINGS_ID, f"corpid_num='{args.dos_id}'", max_results=5000)
    filing_count = 0
    for f in filings:
        _upsert_filing(db, entity_id, f)
        filing_count += 1
    print(f"  Loaded {filing_count} filings")

    # Fetch addresses from filings
    time.sleep(0.5)
    addresses = _soda_paginate(FILINGS_ADDR_ID, f"corpid_num='{args.dos_id}'", max_results=5000)
    addr_count = _process_addresses(db, entity_id, addresses)
    print(f"  Processed {addr_count} address records")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass


def cmd_ingest_batch(args):
    """Ingest all entities matching a search query."""
    db = get_db()

    where = f"upper(current_entity_name) LIKE upper('%{args.query}%')"
    results = _soda_request(ACTIVE_CORPS_ID, {"$where": where}, limit=args.limit)

    print(f"Ingesting {len(results)} entities matching '{args.query}'")
    for i, r in enumerate(results):
        entity_id = _upsert_entity(db, r)
        dos_id = r.get("dos_id", "")
        print(f"  [{i+1}/{len(results)}] {r.get('current_entity_name')} (DOS: {dos_id})")

        if args.with_filings and dos_id:
            time.sleep(0.5)
            filings = _soda_paginate(ALL_FILINGS_ID, f"corpid_num='{dos_id}'", max_results=1000)
            for f in filings:
                _upsert_filing(db, entity_id, f)
            if filings:
                print(f"    + {len(filings)} filings")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass
    print(f"\nBatch ingest complete: {len(results)} entities")


def _upsert_entity(db, r):
    """Insert or update an entity from SODA response."""
    dos_id = r.get("dos_id", "")
    name = r.get("current_entity_name", "?")

    # Map entity type
    etype_raw = r.get("entity_type", r.get("entity_type", ""))
    etype_map = {
        "DOMESTIC BUSINESS CORPORATION": "corp",
        "FOREIGN BUSINESS CORPORATION": "foreign_corp",
        "DOMESTIC LIMITED LIABILITY COMPANY": "llc",
        "FOREIGN LIMITED LIABILITY COMPANY": "foreign_llc",
        "DOMESTIC NOT-FOR-PROFIT CORPORATION": "nonprofit",
        "FOREIGN NOT-FOR-PROFIT CORPORATION": "foreign_nonprofit",
        "DOMESTIC LIMITED PARTNERSHIP": "lp",
        "FOREIGN LIMITED PARTNERSHIP": "foreign_lp",
        "DOMESTIC LIMITED LIABILITY PARTNERSHIP": "llp",
    }
    etype = etype_map.get(etype_raw, etype_raw.lower() if etype_raw else None)

    status_raw = r.get("current_status", "")
    status_map = {"ACTIVE": "active", "INACTIVE": "inactive"}
    status = status_map.get(status_raw, status_raw.lower() if status_raw else None)

    filed_date = r.get("initial_dos_filing_date", "")
    if filed_date:
        filed_date = filed_date[:10]

    # Process server as principal address
    princ_addr = r.get("dos_process_address_1", "")
    if r.get("dos_process_address_2"):
        princ_addr += " " + r["dos_process_address_2"]

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, principal_address, principal_city, principal_state,
            principal_zip, principal_country, state_of_formation, source_url, raw_data
        ) VALUES ('ny', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?)
    """, [
        dos_id, name, etype, status, filed_date,
        princ_addr.strip() or None,
        r.get("dos_process_city"), r.get("dos_process_state"), r.get("dos_process_zip"),
        r.get("jurisdiction", "NEW YORK"),
        f"https://appext20.dos.ny.gov/corp_public/CORPSEARCH.ENTITY_INFORMATION?p_nameid={dos_id}&p_corpid=&p_entity_name=&p_name_type=&p_search_type=BEGINS&p_srch_results_page=0",
        json.dumps(r, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='ny' AND source_id=?",
        [dos_id]
    ).fetchone()
    entity_id = row[0]

    # CEO as officer
    ceo_name = r.get("chairman_name", "")
    if ceo_name:
        ceo_addr = r.get("chairman_address_1", "")
        if r.get("chairman_address_2"):
            ceo_addr += " " + r["chairman_address_2"]
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip)
                VALUES (?, ?, 'CEO', 'person', ?, ?, ?, ?)
            """, [entity_id, ceo_name, ceo_addr.strip() or None,
                  r.get("chairman_city"), r.get("chairman_state"), r.get("chairman_zip")])
        except sqlite3.IntegrityError:
            pass

    # Registered agent
    ra_name = r.get("registered_agent_name", "")
    if ra_name:
        ra_addr = r.get("registered_agent_addr_1", "")
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [entity_id, ra_name, ra_addr or None,
                  r.get("registered_agent_city"), r.get("registered_agent_state"),
                  r.get("registered_agent_zip")])
        except sqlite3.IntegrityError:
            pass

    return entity_id


def _upsert_filing(db, entity_id, f):
    """Insert a filing record."""
    filing_date = f.get("date_filed", f.get("filing_date", ""))
    if filing_date:
        filing_date = filing_date[:10]
    eff_date = f.get("effective_date", "")
    if eff_date:
        eff_date = eff_date[:10]

    doc_type = f.get("document_type", f.get("filing_type", ""))
    name_at_time = f.get("entity_name", "")

    try:
        db.execute("""
            INSERT OR IGNORE INTO registry_filings
            (entity_id, filing_type, filing_date, effective_date, description, entity_name_at_time, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [entity_id, doc_type, filing_date, eff_date or None,
              doc_type, name_at_time or None, json.dumps(f, default=str)])
    except sqlite3.IntegrityError:
        pass


def _process_addresses(db, entity_id, addresses):
    """Process address records from filings, tracking officer changes."""
    count = 0
    # Addr types: 1=Service of Process, 2=Registered Agent, 3=CEO, 4=Principal Office
    for a in addresses:
        addr_type = a.get("addr_type", "")
        name = a.get("name", "")
        addr = a.get("addr1", "")
        if a.get("addr2"):
            addr += " " + a["addr2"]
        city = a.get("city", "")
        state = a.get("state", "")
        zipcode = a.get("zip5", "")
        date = a.get("date_filed", "")
        if date:
            date = date[:10]

        if addr_type == "2" and name:
            # Registered agent
            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_agents
                    (entity_id, agent_name, address, city, state, zip, effective_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [entity_id, name, addr.strip() or None, city, state, zipcode, date])
            except sqlite3.IntegrityError:
                pass
        elif addr_type == "3" and name:
            # CEO/officer
            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_officers
                    (entity_id, officer_name, title, officer_type, address, city, state, zip, effective_date)
                    VALUES (?, ?, 'CEO', 'person', ?, ?, ?, ?, ?)
                """, [entity_id, name, addr.strip() or None, city, state, zipcode, date])
            except sqlite3.IntegrityError:
                pass

        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="New York corporate registry via SODA API")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search active corporations by name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    # search-officers
    p = sub.add_parser("search-officers", help="Search by CEO name")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)

    # search-address
    p = sub.add_parser("search-address", help="Search by address")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    # ingest-entity
    p = sub.add_parser("ingest-entity", help="Ingest specific entity by DOS ID")
    p.add_argument("dos_id")

    # ingest-batch
    p = sub.add_parser("ingest-batch", help="Ingest all matching entities")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--with-filings", action="store_true", help="Also fetch filing history")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "search-officers": cmd_search_officers,
        "search-address": cmd_search_address,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
