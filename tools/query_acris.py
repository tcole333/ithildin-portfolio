#!/usr/bin/env python3
"""
NYC ACRIS (Automated City Register Information System) property records.

Queries NYC Open Data (Socrata SODA) for real property transactions:
deeds, mortgages, liens, satisfactions, assignments, and UCC filings.

Datasets:
  - Real Property Master (bnx9-e6tj): Document metadata (type, amount, date)
  - Real Property Parties (636b-3b5g): Party names per document (grantor/grantee)
  - Real Property Legals (8h5j-fqxa): Borough/block/lot for each document

Performance notes:
  - Party search uses exact match first (upper(name) = 'X'), falls back to LIKE
  - History command batches master record fetches (20 per request) instead of N+1
  - Master records are cached within a session to avoid redundant fetches
  - Use --exact to force exact-only matching (fastest)
  - Use --timeout to control per-request timeout

Usage:
    python tools/query_acris.py party "Jeffrey Epstein"
    python tools/query_acris.py party "LSJE LLC" --exact
    python tools/query_acris.py party "EPSTEIN" --timeout 90
    python tools/query_acris.py address --borough 1 --block 1390 --lot 29
    python tools/query_acris.py document "2019012345678"
    python tools/query_acris.py batch-entities
    python tools/query_acris.py history --borough 1 --block 1390 --lot 29
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# Load .env for optional app token
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

# ACRIS dataset IDs on NYC Open Data
MASTER_ID = "bnx9-e6tj"
PARTIES_ID = "636b-3b5g"
LEGALS_ID = "8h5j-fqxa"

BASE_URL = "https://data.cityofnewyork.us/resource"

# Batch size for IN queries on master records
MASTER_BATCH_SIZE = 20

# Known Epstein-related NYC properties (BBL = Borough/Block/Lot)
KNOWN_PROPERTIES = {
    "9 E 71st St": {"borough": "1", "block": "1386", "lot": "10"},
    "11 E 71st St": {"borough": "1", "block": "1386", "lot": "12"},
    "457 Madison Ave": {"borough": "1", "block": "1312", "lot": "52"},
    "301 E 66th St": {"borough": "1", "block": "1419", "lot": "31"},
}

# ACRIS document type codes
DOC_TYPE_MAP = {
    "DEED": "Deed",
    "DEEDO": "Deed, Other",
    "MTGE": "Mortgage",
    "M&CON": "Mortgage & Consolidation",
    "AGMT": "Agreement",
    "ASST": "Assignment",
    "SAT": "Satisfaction",
    "RPTT": "Real Property Transfer Tax",
    "UCC1": "UCC1 Financing Statement",
    "UCC3": "UCC3 Amendment",
    "AL&R": "Assignment of Leases & Rents",
    "ALIS": "Lis Pendens",
    "MCON": "Mortgage Consolidation",
    "CORRM": "Corrective Mortgage",
    "SUBM": "Subordination of Mortgage",
}

# Party type codes
PARTY_TYPE = {"1": "grantor/seller", "2": "grantee/buyer", "3": "other"}

# Session-level cache for master records (avoids re-fetching same document)
_master_cache = {}


def _soda_request(dataset_id, params, limit=1000, timeout=60):
    """Make a SODA API request to NYC Open Data."""
    url = f"{BASE_URL}/{dataset_id}.json"
    params["$limit"] = limit

    token = os.environ.get("NYC_SODA_APP_TOKEN") or os.environ.get("NY_SODA_APP_TOKEN")
    if token:
        params["$$app_token"] = token

    full_url = url + "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    req = Request(full_url, headers=headers)

    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except TimeoutError:
        print(f"ERROR: Request timed out ({timeout}s). Try a more specific query or --exact.", file=sys.stderr)
        return []
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return []
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return []


def _get_master(document_id, timeout=60):
    """Get master record for a document, with session cache."""
    if document_id in _master_cache:
        return _master_cache[document_id]
    results = _soda_request(MASTER_ID, {"$where": f"document_id='{document_id}'"}, limit=1, timeout=timeout)
    record = results[0] if results else {}
    _master_cache[document_id] = record
    return record


def _get_masters_batch(document_ids, timeout=60):
    """Fetch master records in batches using SoQL IN clause.

    Instead of N+1 queries (one per doc), batches into groups of MASTER_BATCH_SIZE
    using: document_id IN ('id1','id2',...). Results are cached.

    Returns dict of {document_id: master_record}.
    """
    results = {}
    uncached_ids = []

    # Serve from cache first
    for doc_id in document_ids:
        if doc_id in _master_cache:
            results[doc_id] = _master_cache[doc_id]
        else:
            uncached_ids.append(doc_id)

    # Batch-fetch uncached IDs
    for i in range(0, len(uncached_ids), MASTER_BATCH_SIZE):
        batch = uncached_ids[i:i + MASTER_BATCH_SIZE]
        id_list = ",".join(f"'{did}'" for did in batch)
        where = f"document_id IN ({id_list})"
        rows = _soda_request(MASTER_ID, {"$where": where}, limit=MASTER_BATCH_SIZE, timeout=timeout)
        time.sleep(0.3)  # Rate limiting between batches

        # Index results by document_id and populate cache
        fetched = {}
        for row in rows:
            did = row.get("document_id")
            if did:
                fetched[did] = row
                _master_cache[did] = row

        # IDs not returned get cached as empty (doc doesn't exist)
        for did in batch:
            if did not in fetched:
                _master_cache[did] = {}
                results[did] = {}
            else:
                results[did] = fetched[did]

    return results


def _get_parties(document_id, timeout=60):
    """Get all parties for a document."""
    return _soda_request(PARTIES_ID, {"$where": f"document_id='{document_id}'"}, limit=100, timeout=timeout)


def _get_legals(document_id, timeout=60):
    """Get BBL info for a document."""
    return _soda_request(LEGALS_ID, {"$where": f"document_id='{document_id}'"}, limit=100, timeout=timeout)


def _format_amount(amt_str):
    """Format dollar amount."""
    try:
        amt = float(amt_str)
        if amt == 0:
            return ""
        return f"${amt:,.0f}"
    except (ValueError, TypeError):
        return ""


def _format_date(date_str):
    """Extract date from ISO timestamp."""
    if not date_str:
        return ""
    return date_str[:10]


def _print_transaction(master, parties, legals=None):
    """Print a formatted transaction summary."""
    doc_id = master.get("document_id", "?")
    doc_type = master.get("doc_type", "?")
    doc_type_desc = DOC_TYPE_MAP.get(doc_type, doc_type)
    amount = _format_amount(master.get("document_amt"))
    doc_date = _format_date(master.get("document_date"))
    recorded = _format_date(master.get("recorded_datetime"))
    borough = master.get("recorded_borough", "")

    print(f"  [{doc_type}] {doc_type_desc}")
    print(f"    Document: {doc_id} | CRFN: {master.get('crfn', 'N/A')}")
    if amount:
        print(f"    Amount: {amount}")
    if doc_date:
        print(f"    Date: {doc_date} (recorded: {recorded})")
    if borough:
        borough_names = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}
        print(f"    Borough: {borough_names.get(borough, borough)}")

    # Group parties by type
    grantors = [p for p in parties if p.get("party_type") == "1"]
    grantees = [p for p in parties if p.get("party_type") == "2"]
    others = [p for p in parties if p.get("party_type") not in ("1", "2")]

    if grantors:
        names = [p.get("name", "?") for p in grantors]
        print(f"    From (grantor): {', '.join(names)}")
        for p in grantors:
            addr = p.get("address_1", "")
            if addr:
                city = p.get("city", "")
                state = p.get("state", "")
                print(f"      Address: {addr}, {city}, {state}")

    if grantees:
        names = [p.get("name", "?") for p in grantees]
        print(f"    To (grantee): {', '.join(names)}")
        for p in grantees:
            addr = p.get("address_1", "")
            if addr:
                city = p.get("city", "")
                state = p.get("state", "")
                print(f"      Address: {addr}, {city}, {state}")

    if others:
        for p in others:
            print(f"    Other party: {p.get('name', '?')}")

    if legals:
        for leg in legals[:3]:
            b = leg.get("borough", "")
            bl = leg.get("block", "")
            lo = leg.get("lot", "")
            if b and bl and lo:
                print(f"    Property: Borough {b}, Block {bl}, Lot {lo}")

    print()


def cmd_party(args):
    """Search ACRIS by party name — the primary investigation use case.

    Optimization: tries exact match first (upper(name) = 'X') which is much
    faster on SODA than LIKE '%X%'. Falls back to LIKE only if exact returns
    nothing and --exact was not specified.
    """
    timeout = getattr(args, 'timeout', 60)
    name = args.query.upper().replace("'", "''")  # Escape single quotes for SoQL
    exact_only = getattr(args, 'exact', False)

    # Phase 1: Exact match (fast — indexed equality on upper(name))
    where_exact = f"upper(name) = '{name}'"
    t0 = time.time()
    parties = _soda_request(PARTIES_ID, {"$where": where_exact}, limit=args.limit, timeout=timeout)
    elapsed = time.time() - t0

    search_mode = "exact"
    if parties:
        print(f"Found {len(parties)} ACRIS party records matching '{args.query}' (exact match, {elapsed:.1f}s)")
    elif not exact_only:
        # Phase 2: Fall back to LIKE (slow — full scan)
        print(f"No exact match for '{args.query}', falling back to LIKE search...", file=sys.stderr)
        where_like = f"upper(name) LIKE '%{name}%'"
        t0 = time.time()
        parties = _soda_request(PARTIES_ID, {"$where": where_like}, limit=args.limit, timeout=timeout)
        elapsed = time.time() - t0
        search_mode = "LIKE"
        print(f"Found {len(parties)} ACRIS party records matching '{args.query}' (LIKE, {elapsed:.1f}s)")
    else:
        print(f"Found 0 ACRIS party records matching '{args.query}' (exact only, {elapsed:.1f}s)")

    print()

    if not parties:
        return

    # Get unique document IDs and fetch master records in batch
    doc_ids = list(dict.fromkeys(p.get("document_id") for p in parties if p.get("document_id")))

    if write_output(parties, args, summary=f"ACRIS party search '{args.query}' ({len(doc_ids)} docs)"):
        return

    print(f"Across {len(doc_ids)} unique documents")
    print()

    # Batch-fetch master records for the docs we'll display
    display_ids = doc_ids[:args.max_docs]
    masters = _get_masters_batch(display_ids, timeout=timeout)

    for doc_id in display_ids:
        master = masters.get(doc_id, {})
        if not master:
            continue
        all_parties = _get_parties(doc_id, timeout=timeout)
        legals = _get_legals(doc_id, timeout=timeout)
        _print_transaction(master, all_parties, legals)
        time.sleep(0.3)  # Rate limiting

    if len(doc_ids) > args.max_docs:
        print(f"  ... and {len(doc_ids) - args.max_docs} more documents (use --max-docs to see more)")


def cmd_address(args):
    """Search ACRIS by BBL (Borough/Block/Lot) — property transaction history."""
    timeout = getattr(args, 'timeout', 60)
    borough = args.borough
    block = args.block
    lot = args.lot

    # Look up known property if name given
    if args.property_name:
        for name, bbl in KNOWN_PROPERTIES.items():
            if args.property_name.lower() in name.lower():
                borough = bbl["borough"]
                block = bbl["block"]
                lot = bbl["lot"]
                print(f"Resolved '{args.property_name}' to BBL: {borough}/{block}/{lot} ({name})")
                break
        else:
            print(f"Unknown property '{args.property_name}'. Known properties:")
            for name, bbl in KNOWN_PROPERTIES.items():
                print(f"  {name}: Borough {bbl['borough']}, Block {bbl['block']}, Lot {bbl['lot']}")
            return

    if not (borough and block and lot):
        print("Must specify --borough, --block, --lot or --property-name")
        return

    where = f"borough='{borough}' AND block='{block}' AND lot='{lot}'"
    legals = _soda_request(LEGALS_ID, {"$where": where, "$order": "document_id DESC"}, limit=args.limit, timeout=timeout)

    doc_ids = list(dict.fromkeys(l.get("document_id") for l in legals if l.get("document_id")))

    if write_output(legals, args, summary=f"ACRIS address BBL {borough}/{block}/{lot} ({len(doc_ids)} docs)"):
        return

    print(f"Found {len(legals)} documents for BBL: {borough}/{block}/{lot}")
    print()

    # Batch-fetch master records
    display_ids = doc_ids[:args.max_docs]
    masters = _get_masters_batch(display_ids, timeout=timeout)

    for doc_id in display_ids:
        master = masters.get(doc_id, {})
        if not master:
            continue
        parties = _get_parties(doc_id, timeout=timeout)
        _print_transaction(master, parties)
        time.sleep(0.3)

    if len(doc_ids) > args.max_docs:
        print(f"  ... and {len(doc_ids) - args.max_docs} more documents")


def cmd_document(args):
    """Get full details for a specific ACRIS document."""
    timeout = getattr(args, 'timeout', 60)
    doc_id = args.document_id
    master = _get_master(doc_id, timeout=timeout)
    if not master:
        print(f"Document {doc_id} not found")
        return

    parties = _get_parties(doc_id, timeout=timeout)
    legals = _get_legals(doc_id, timeout=timeout)

    data = {"master": master, "parties": parties, "legals": legals}
    if write_output(data, args, summary=f"ACRIS document {doc_id}"):
        pass
    elif args.json:
        print(f"=== Document {doc_id} ===")
        print()
        _print_transaction(master, parties, legals)
        print("\n=== Raw JSON ===")
        print(json.dumps(data, indent=2))
    else:
        print(f"=== Document {doc_id} ===")
        print()
        _print_transaction(master, parties, legals)


def cmd_history(args):
    """Full transaction history for a property (BBL), sorted by date.

    Optimization: fetches master records in batches of 20 using SoQL IN clause
    instead of one-by-one. Caches results to avoid redundant fetches.
    """
    timeout = getattr(args, 'timeout', 60)
    borough = args.borough
    block = args.block
    lot = args.lot

    if args.property_name:
        for name, bbl in KNOWN_PROPERTIES.items():
            if args.property_name.lower() in name.lower():
                borough = bbl["borough"]
                block = bbl["block"]
                lot = bbl["lot"]
                print(f"Resolved '{args.property_name}' to BBL: {borough}/{block}/{lot} ({name})")
                break
        else:
            print(f"Unknown property. Known: {list(KNOWN_PROPERTIES.keys())}")
            return

    if not (borough and block and lot):
        print("Must specify --borough, --block, --lot or --property-name")
        return

    where = f"borough='{borough}' AND block='{block}' AND lot='{lot}'"
    legals = _soda_request(LEGALS_ID, {"$where": where}, limit=5000, timeout=timeout)

    doc_ids = list(dict.fromkeys(l.get("document_id") for l in legals if l.get("document_id")))

    if write_output(legals, args, summary=f"ACRIS history BBL {borough}/{block}/{lot} ({len(doc_ids)} docs)"):
        return

    print(f"Found {len(doc_ids)} documents for BBL: {borough}/{block}/{lot}")
    print()

    # Batch-fetch all master records (instead of N+1 individual queries)
    t0 = time.time()
    masters = _get_masters_batch(doc_ids, timeout=timeout)
    elapsed = time.time() - t0
    print(f"Fetched {len([m for m in masters.values() if m])} master records in {elapsed:.1f}s "
          f"({len(doc_ids)} docs, batches of {MASTER_BATCH_SIZE})")
    print()

    # Build sorted transaction list
    transactions = []
    for doc_id in doc_ids:
        master = masters.get(doc_id, {})
        if master:
            transactions.append((doc_id, master))

    # Sort by document date
    transactions.sort(key=lambda x: x[1].get("document_date", "") or "")

    for doc_id, master in transactions[:args.limit]:
        parties = _get_parties(doc_id, timeout=timeout)
        _print_transaction(master, parties)
        time.sleep(0.2)


def cmd_batch_entities(args):
    """Cross-reference all investigation entities against ACRIS.

    Uses exact match for speed (entity names from the registry are typically
    exact legal names that match ACRIS records).
    """
    timeout = getattr(args, 'timeout', 15)
    db_path = Path(__file__).parent.parent / "investigation.db"
    if not db_path.exists():
        print("investigation.db not found")
        return

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    entities = db.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
    db.close()

    if not entities:
        print("No entities in investigation.db")
        return

    # Filter to entities worth querying (skip very short names)
    searchable = [e for e in entities if len(e["name"]) >= 4]
    print(f"Cross-referencing {len(searchable)} entities against ACRIS ({timeout}s timeout per query)")
    print("=" * 70)

    hits = 0
    skipped = 0
    all_matches = []
    for i, ent in enumerate(searchable):
        name = ent["name"]
        print(f"  [{i+1}/{len(searchable)}] {name}...", end=" ", flush=True)

        safe_name = name.upper().replace("'", "''")

        # Try exact match first (fast)
        where = f"upper(name) = '{safe_name}'"
        results = _soda_request(PARTIES_ID, {"$where": where}, limit=5, timeout=timeout)
        time.sleep(0.3)

        # Fall back to LIKE only if exact returned nothing
        if not results:
            where = f"upper(name) LIKE '%{safe_name}%'"
            results = _soda_request(PARTIES_ID, {"$where": where}, limit=5, timeout=timeout)
            time.sleep(0.3)

        if results is None or (isinstance(results, list) and len(results) == 0):
            print("0 hits")
            continue

        if results:
            hits += 1
            doc_ids = list(dict.fromkeys(r.get("document_id") for r in results))
            print(f"MATCH -- {len(results)} records in {len(doc_ids)} docs")
            all_matches.append({"entity_id": ent["id"], "entity_name": name, "records": results, "doc_count": len(doc_ids)})

            # Batch-fetch master records for display
            masters = _get_masters_batch(doc_ids[:2], timeout=timeout)
            for doc_id in doc_ids[:2]:
                master = masters.get(doc_id, {})
                if master:
                    doc_type = DOC_TYPE_MAP.get(master.get("doc_type", ""), master.get("doc_type", "?"))
                    amount = _format_amount(master.get("document_amt"))
                    date = _format_date(master.get("document_date"))
                    print(f"      {date} | {doc_type} | {amount}")
        else:
            print("0 hits")

    print(f"\n{'='*70}")
    print(f"Entities with ACRIS matches: {hits}/{len(searchable)}")

    write_output(all_matches, args, summary=f"ACRIS batch-entities ({hits}/{len(searchable)} matches)")


def cmd_known(args):
    """List known Epstein-related NYC properties and their BBLs."""
    data = [{"name": name, **bbl} for name, bbl in KNOWN_PROPERTIES.items()]
    if write_output(data, args, summary="ACRIS known properties"):
        return
    print("Known Epstein-related NYC properties:")
    print()
    for name, bbl in KNOWN_PROPERTIES.items():
        print(f"  {name}")
        print(f"    Borough: {bbl['borough']}, Block: {bbl['block']}, Lot: {bbl['lot']}")
        print()
    print("Use with: python tools/query_acris.py address --property-name '71st'")


def main():
    parser = argparse.ArgumentParser(description="NYC ACRIS property records via SODA API")
    sub = parser.add_subparsers(dest="command", required=True)

    # party -- search by party name
    p = sub.add_parser("party", help="Search by party name (grantor/grantee)")
    p.add_argument("query", help="Party name to search")
    p.add_argument("--limit", type=int, default=100, help="Max party records to fetch")
    p.add_argument("--max-docs", type=int, default=20, help="Max documents to show details for")
    p.add_argument("--exact", action="store_true", help="Exact name match only (fastest, no LIKE fallback)")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)")
    add_output_args(p)

    # address -- search by BBL
    p = sub.add_parser("address", help="Search by property address (BBL)")
    p.add_argument("--borough", help="Borough code (1=Manhattan, 2=Bronx, 3=Brooklyn, 4=Queens, 5=SI)")
    p.add_argument("--block", help="Block number")
    p.add_argument("--lot", help="Lot number")
    p.add_argument("--property-name", help="Look up known property by name fragment (e.g., '71st')")
    p.add_argument("--limit", type=int, default=50, help="Max records")
    p.add_argument("--max-docs", type=int, default=20, help="Max documents to show")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)")
    add_output_args(p)

    # document -- get specific document
    p = sub.add_parser("document", help="Get full details for a specific document")
    p.add_argument("document_id", help="ACRIS document ID")
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)")
    add_output_args(p)

    # history -- full property transaction history sorted by date
    p = sub.add_parser("history", help="Full transaction history for a property")
    p.add_argument("--borough", help="Borough code")
    p.add_argument("--block", help="Block number")
    p.add_argument("--lot", help="Lot number")
    p.add_argument("--property-name", help="Look up known property by name")
    p.add_argument("--limit", type=int, default=50, help="Max transactions to show")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)")
    add_output_args(p)

    # batch-entities -- cross-reference investigation entities
    p = sub.add_parser("batch-entities", help="Cross-ref all investigation entities against ACRIS")
    p.add_argument("--timeout", type=int, default=15, help="Per-request timeout in seconds (default: 15)")
    add_output_args(p)

    # known -- list known properties
    p = sub.add_parser("known", help="List known Epstein NYC properties with BBLs")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "party": cmd_party,
        "address": cmd_address,
        "document": cmd_document,
        "history": cmd_history,
        "batch-entities": cmd_batch_entities,
        "known": cmd_known,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
