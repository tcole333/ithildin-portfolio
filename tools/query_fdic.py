#!/usr/bin/env python3
"""FDIC BankFind API — bank institutions, failures, financials, branches.

Free, no auth required. Data includes all FDIC-insured institutions,
branch locations, quarterly financials, failure history, and merger events.

API: https://api.fdic.gov/banks/

Usage:
    uv run python tools/query_fdic.py search "Deutsche Bank"
    uv run python tools/query_fdic.py institution 59017
    uv run python tools/query_fdic.py failures [--state NY] [--year 2008]
    uv run python tools/query_fdic.py locations 59017
    uv run python tools/query_fdic.py financials 59017 [--date 20231231]
    uv run python tools/query_fdic.py history 59017
    uv run python tools/query_fdic.py ingest 59017
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://api.fdic.gov/banks"
DB_PATH = Path(__file__).parent.parent / "investigation.db"

HEADERS = {"Accept": "application/json"}


def _fetch(endpoint, params=None):
    """Fetch from FDIC API and return parsed JSON."""
    url = f"{BASE_URL}/{endpoint}"
    if params:
        # Build query string manually to preserve wildcards (*) in filters
        # urlencode encodes * to %2A which the FDIC Elasticsearch backend
        # does not recognize as a wildcard
        parts = []
        for k, v in params.items():
            parts.append(f"{quote(str(k))}={quote(str(v), safe='*:\"')}")
        url += "?" + "&".join(parts)
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {body[:500]}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _extract_records(response):
    """Extract record dicts from FDIC nested response format."""
    if not response or "data" not in response:
        return []
    return [item["data"] for item in response["data"] if "data" in item]


def _total(response):
    """Get total count from response metadata.

    Note: For wildcard NAME filters, meta.total may return the full index
    count rather than the filtered count. When returned data < limit, we
    use the data count as the true total.
    """
    count = 0
    if response and "totals" in response:
        count = response["totals"].get("count", 0)
    elif response and "meta" in response:
        count = response["meta"].get("total", 0)

    # If returned records < limit, they represent the true total
    data_len = len(response.get("data", [])) if response else 0
    limit = int(response.get("meta", {}).get("parameters", {}).get("limit", 0) or 0)
    if data_len > 0 and data_len < limit and count > data_len:
        return data_len
    return count


def cmd_search(args):
    """Search institutions by name."""
    # FDIC uses Elasticsearch query syntax on filters, not a search param.
    # Multi-word: use first word with wildcard (API doesn't support phrase wildcards).
    # Results are typically few enough for agent-side filtering.
    query = args.query.strip()
    first_word = query.split()[0] if query else query
    filter_str = f"NAME:{first_word}*"
    params = {"filters": filter_str, "limit": args.limit, "offset": args.offset}
    resp = _fetch("institutions", params)
    records = _extract_records(resp)
    total = _total(resp)

    results = {
        "query": args.query,
        "total": total,
        "returned": len(records),
        "institutions": [
            {
                "cert": r.get("CERT"),
                "name": r.get("NAME") or r.get("INSTNAME"),
                "city": r.get("CITY"),
                "state": r.get("STALP"),
                "class": r.get("BKCLASS"),
                "active": r.get("ACTIVE"),
                "total_assets": r.get("ASSET"),
                "total_deposits": r.get("DEP"),
                "net_income": r.get("NETINC"),
                "established": r.get("ESTYMD"),
                "last_update": r.get("REPDTE"),
            }
            for r in records
        ],
    }

    if write_output(results, args, summary=f"FDIC search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"\nFDIC Institutions: '{args.query}' ({len(records)} of {total})")
    print(f"{'='*70}")
    for inst in results["institutions"]:
        assets = f"${inst['total_assets']:,.0f}K" if inst.get("total_assets") else "N/A"
        active = "Active" if inst.get("active") == 1 else "Inactive"
        print(f"  CERT {inst['cert']:>7}  {inst['name']}")
        print(f"    {inst['city']}, {inst['state']}  |  {active}  |  Assets: {assets}")


def cmd_institution(args):
    """Get full details for an institution by CERT number."""
    params = {"filters": f"CERT:{args.cert}", "limit": 1}
    resp = _fetch("institutions", params)
    records = _extract_records(resp)

    if not records:
        print(f"No institution found with CERT {args.cert}")
        return

    record = records[0]

    if write_output(record, args, summary=f"FDIC institution CERT:{args.cert}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(record, indent=2))
        return

    name = record.get("NAME") or record.get("INSTNAME", "?")
    print(f"\nFDIC Institution: {name}")
    print(f"{'='*70}")
    for key in sorted(record.keys()):
        val = record[key]
        if val is not None and val != "":
            print(f"  {key:30s} {val}")


def cmd_failures(args):
    """List bank failures, optionally filtered by state/year."""
    params = {"limit": args.limit, "offset": args.offset}
    filters = []
    if args.state:
        filters.append(f"PSTALP:{args.state.upper()}")
    if args.year:
        filters.append(f"FAILYR:{args.year}")
    if filters:
        params["filters"] = " AND ".join(filters)

    resp = _fetch("failures", params)
    records = _extract_records(resp)
    total = _total(resp)

    results = {
        "total": total,
        "returned": len(records),
        "failures": [
            {
                "cert": r.get("CERT"),
                "name": r.get("NAME"),
                "city": r.get("CITYST") or f"{r.get('CITY', '')}, {r.get('PSTALP', '')}",
                "fail_date": r.get("FAILDATE"),
                "fail_year": r.get("FAILYR"),
                "assets": r.get("QBFASSET"),
                "deposits": r.get("QBFDEP"),
                "cost": r.get("COST"),
                "acquirer": r.get("APTS") or r.get("ACQUIRER"),
                "resolution_type": r.get("RESTYPE") or r.get("RESTYPE1"),
            }
            for r in records
        ],
    }

    if write_output(results, args, summary=f"FDIC failures"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    state_str = f" in {args.state.upper()}" if args.state else ""
    year_str = f" ({args.year})" if args.year else ""
    print(f"\nFDIC Bank Failures{state_str}{year_str} ({len(records)} of {total})")
    print(f"{'='*70}")
    for f in results["failures"]:
        assets = f"${f['assets']:,.0f}K" if f.get("assets") else "N/A"
        print(f"  CERT {f['cert']:>7}  {f['name']}")
        print(f"    {f['city']}  |  Failed: {f['fail_date']}  |  Assets: {assets}")
        if f.get("acquirer"):
            print(f"    Acquired by: {f['acquirer']}")


def cmd_locations(args):
    """List branch locations for an institution."""
    params = {"filters": f"CERT:{args.cert}", "limit": args.limit, "offset": args.offset}
    resp = _fetch("locations", params)
    records = _extract_records(resp)
    total = _total(resp)

    results = {
        "cert": args.cert,
        "total": total,
        "returned": len(records),
        "locations": [
            {
                "name": r.get("NAME"),
                "address": r.get("ADDRESS"),
                "city": r.get("CITY"),
                "state": r.get("STNAME") or r.get("STALP"),
                "zip": r.get("ZIP"),
                "county": r.get("COUNTY"),
                "latitude": r.get("LATITUDE"),
                "longitude": r.get("LONGITUDE"),
                "main_office": r.get("MAINOFF"),
                "established": r.get("ESTYMD"),
            }
            for r in records
        ],
    }

    if write_output(results, args, summary=f"FDIC locations CERT:{args.cert}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"\nFDIC Locations for CERT {args.cert} ({len(records)} of {total})")
    print(f"{'='*70}")
    for loc in results["locations"]:
        main = " [MAIN]" if loc.get("main_office") == 1 else ""
        print(f"  {loc['name']}{main}")
        print(f"    {loc['address']}, {loc['city']}, {loc['state']} {loc.get('zip', '')}")


def cmd_financials(args):
    """Get financial statements for an institution."""
    params = {"filters": f"CERT:{args.cert}", "limit": args.limit, "offset": args.offset}
    if args.date:
        params["filters"] += f" AND REPDTE:{args.date}"

    resp = _fetch("financials", params)
    records = _extract_records(resp)
    total = _total(resp)

    results = {
        "cert": args.cert,
        "total": total,
        "returned": len(records),
        "financials": [
            {
                "report_date": r.get("REPDTE"),
                "name": r.get("NAME") or r.get("REPNM"),
                "total_assets": r.get("ASSET"),
                "total_liabilities": r.get("LIAB"),
                "equity": r.get("EQ"),
                "total_deposits": r.get("DEP"),
                "domestic_deposits": r.get("DEPDOM"),
                "foreign_deposits": r.get("DEPFRGN"),
                "net_income": r.get("NETINC"),
                "roa": r.get("ROA"),
                "roe": r.get("ROE"),
                "net_interest_margin": r.get("NIM"),
                "net_loans": r.get("LNLSNET"),
                "employees": r.get("NUMEMP"),
            }
            for r in records
        ],
    }

    if write_output(results, args, summary=f"FDIC financials CERT:{args.cert}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"\nFDIC Financials for CERT {args.cert} ({len(records)} of {total})")
    print(f"{'='*70}")
    for fin in results["financials"]:
        assets = f"${fin['total_assets']:,.0f}K" if fin.get("total_assets") else "N/A"
        deposits = f"${fin['total_deposits']:,.0f}K" if fin.get("total_deposits") else "N/A"
        income = f"${fin['net_income']:,.0f}K" if fin.get("net_income") else "N/A"
        print(f"  {fin['report_date']}  {fin.get('name', '?')}")
        print(f"    Assets: {assets}  |  Deposits: {deposits}  |  Net Income: {income}")
        if fin.get("roa") is not None:
            print(f"    ROA: {fin['roa']}%  |  ROE: {fin.get('roe', 'N/A')}%  |  NIM: {fin.get('net_interest_margin', 'N/A')}%")


def cmd_history(args):
    """Get institution history (mergers, name changes, relocations)."""
    params = {"filters": f"CERT:{args.cert}", "limit": args.limit, "offset": args.offset}
    resp = _fetch("history", params)
    records = _extract_records(resp)
    total = _total(resp)

    results = {
        "cert": args.cert,
        "total": total,
        "returned": len(records),
        "events": [
            {
                "institution": r.get("INSTNAME"),
                "change_code": r.get("CHANGECODE"),
                "change_desc": r.get("CHANGECODE_DESC"),
                "effective_date": r.get("EFFDATE"),
                "process_date": r.get("PROCDATE"),
                "end_date": r.get("ENDDATE"),
                "off_name": r.get("OFF_NAME"),
                "off_address": r.get("OFF_PADDR"),
                "off_city": r.get("OFF_PCITY"),
                "off_state": r.get("OFF_PSTATE"),
            }
            for r in records
        ],
    }

    if write_output(results, args, summary=f"FDIC history CERT:{args.cert}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"\nFDIC History for CERT {args.cert} ({len(records)} of {total})")
    print(f"{'='*70}")
    for evt in results["events"]:
        print(f"  {evt.get('effective_date', '?')}  [{evt.get('change_code', '?')}] {evt.get('change_desc', '?')}")
        if evt.get("institution"):
            print(f"    Institution: {evt['institution']}")
        if evt.get("off_name"):
            loc = f"{evt.get('off_city', '')}, {evt.get('off_state', '')}"
            print(f"    Office: {evt['off_name']} — {loc}")


def cmd_ingest(args):
    """Ingest an FDIC institution into investigation.db as an entity."""
    # Fetch institution data
    params = {"filters": f"CERT:{args.cert}", "limit": 1}
    resp = _fetch("institutions", params)
    records = _extract_records(resp)

    if not records:
        print(f"No institution found with CERT {args.cert}")
        return

    record = records[0]
    name = record.get("REPNM") or record.get("NAME") or record.get("INSTNAME")
    city = record.get("CITY", "")
    state = record.get("STALP", "")
    address = record.get("ADDRESS", "")
    full_address = f"{address}, {city}, {state}".strip(", ")

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    # Ensure schema exists
    try:
        from tools.lead_tracker import _ensure_schema
        _ensure_schema(db)
    except ImportError:
        pass

    # Check if entity already exists
    existing = db.execute(
        "SELECT id FROM entities WHERE name = ? AND jurisdiction = ?",
        (name, state or "US"),
    ).fetchone()

    if existing and not args.force:
        print(f"Entity already exists: #{existing['id']} {name}")
        print(f"Use --force to update.")
        db.close()
        return

    if existing:
        entity_id = existing["id"]
        db.execute(
            """UPDATE entities SET
               entity_type = 'inc', status = ?, ein = ?, source = ?, notes = ?
               WHERE id = ?""",
            (
                "active" if record.get("ACTIVE") == 1 else "unknown",
                record.get("FED_RSSD") or record.get("RSSDID"),
                f"FDIC BankFind CERT:{args.cert}",
                f"FDIC class: {record.get('BKCLASS')}; Assets: {record.get('ASSET')}K",
                entity_id,
            ),
        )
    else:
        db.execute(
            """INSERT INTO entities (name, entity_type, jurisdiction, ein, status, source, notes)
               VALUES (?, 'inc', ?, ?, ?, ?, ?)""",
            (
                name,
                state or "US",
                record.get("FED_RSSD") or record.get("RSSDID"),
                "active" if record.get("ACTIVE") == 1 else "unknown",
                f"FDIC BankFind CERT:{args.cert}",
                f"FDIC class: {record.get('BKCLASS')}; Assets: {record.get('ASSET')}K",
            ),
        )
        entity_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Add address
    if full_address:
        db.execute(
            """INSERT OR IGNORE INTO entity_addresses
               (entity_id, address, address_type, source)
               VALUES (?, ?, 'registered', ?)""",
            (entity_id, full_address, f"FDIC BankFind CERT:{args.cert}"),
        )

    db.commit()
    db.close()

    print(f"Ingested FDIC institution as entity #{entity_id}: {name}")
    print(f"  CERT: {args.cert}  |  {full_address}")


def main():
    parser = argparse.ArgumentParser(description="FDIC BankFind API — bank data lookup")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search institutions by name")
    p.add_argument("query", help="Search term")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    add_output_args(p)

    # institution
    p = sub.add_parser("institution", help="Full institution details by CERT number")
    p.add_argument("cert", type=int, help="FDIC certificate number")
    add_output_args(p)

    # failures
    p = sub.add_parser("failures", help="Bank failure history")
    p.add_argument("--state", help="Filter by state (2-letter code)")
    p.add_argument("--year", type=int, help="Filter by failure year")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--offset", type=int, default=0)
    add_output_args(p)

    # locations
    p = sub.add_parser("locations", help="Branch locations for an institution")
    p.add_argument("cert", type=int, help="FDIC certificate number")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    add_output_args(p)

    # financials
    p = sub.add_parser("financials", help="Financial statements for an institution")
    p.add_argument("cert", type=int, help="FDIC certificate number")
    p.add_argument("--date", help="Report date filter (YYYYMMDD)")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    add_output_args(p)

    # history
    p = sub.add_parser("history", help="Institution history (mergers, changes)")
    p.add_argument("cert", type=int, help="FDIC certificate number")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--offset", type=int, default=0)
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Store institution as entity in investigation.db")
    p.add_argument("cert", type=int, help="FDIC certificate number")
    p.add_argument("--force", action="store_true", help="Update if entity already exists")

    args = parser.parse_args()
    handlers = {
        "search": cmd_search,
        "institution": cmd_institution,
        "failures": cmd_failures,
        "locations": cmd_locations,
        "financials": cmd_financials,
        "history": cmd_history,
        "ingest": cmd_ingest,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
