#!/usr/bin/env python3
"""
Massachusetts Secretary of the Commonwealth corporate registry tool.

Queries the MA Corporations Division search at corp.sec.state.ma.us.
The site uses ASP.NET WebForms behind Incapsula/Imperva WAF, so a Node.js
browser helper (_ma_browser_helper.js) is required for requests.

Data available: entity name, ID number, type, formation date, inactive date,
principal address, registered agent, officers (title/name/address), name
changes, fiscal date, publicly traded flag.

Usage:
    python tools/query_massachusetts.py search "EPSTEIN"
    python tools/query_massachusetts.py search "APOLLO" --type F
    python tools/query_massachusetts.py entity 000487270
    python tools/query_massachusetts.py ingest 000487270
    python tools/query_massachusetts.py ingest-search "EPSTEIN"
"""

import argparse
import json
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


HELPER_PATH = Path(__file__).parent / "_ma_browser_helper.js"

# Entity type mapping
TYPE_MAP = {
    "business corporation": "corp",
    "domestic business corporation": "corp",
    "foreign business corporation": "foreign_corp",
    "nonprofit corporation": "nonprofit",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "professional corporation": "professional_corp",
    "limited liability company": "llc",
    "domestic limited liability company": "llc",
    "foreign limited liability company": "foreign_llc",
    "limited partnership": "lp",
    "domestic limited partnership": "lp",
    "foreign limited partnership": "foreign_lp",
    "limited liability partnership": "llp",
    "domestic limited liability partnership": "llp",
    "foreign limited liability partnership": "foreign_llp",
    "general partnership": "gp",
    "business trust": "trust",
}


def _run_helper(args_list, timeout=120):
    """Run the MA browser helper and return parsed JSON."""
    if not HELPER_PATH.exists():
        print(f"ERROR: Browser helper not found at {HELPER_PATH}", file=sys.stderr)
        return None

    cmd = ["node", str(HELPER_PATH)] + args_list

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    print(f"  {line}", file=sys.stderr)
        if result.returncode != 0:
            print(f"ERROR: Browser helper exited with code {result.returncode}", file=sys.stderr)
            return None
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("ERROR: Browser helper timed out", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON from browser helper: {e}", file=sys.stderr)
        return None


def _parse_date(date_str):
    """Convert MM/DD/YYYY or MM-DD-YYYY to YYYY-MM-DD."""
    if not date_str:
        return None
    import re
    match = re.match(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', date_str)
    if match:
        return f"{match.group(3)}-{match.group(1).zfill(2)}-{match.group(2).zfill(2)}"
    return None


# ══════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search MA business entities by name."""
    helper_args = ["search", args.query]
    if args.type:
        helper_args.extend(["--type", args.type])

    data = _run_helper(helper_args)
    if not data:
        print("Search failed (browser helper returned no data)")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    results = data.get("results", [])
    count = data.get("count", len(results))
    log_search(args.query, "ma_corp_registry", len(results))

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    if write_output({"count": count, "results": results}, args,
                     summary=f"MA search '{args.query}' ({count} total, {len(results)} returned)"):
        return

    type_label = {"B": "begins with", "M": "exact", "F": "full text", "S": "soundex"}.get(
        args.type or "B", args.type)
    print(f"Found {count} MA entities matching '{args.query}' ({type_label})")
    print()
    for r in results:
        name = r.get("entity_name", "?")
        eid = r.get("id_number", "?")
        addr = r.get("address", "")
        print(f"  {eid} | {name}")
        if addr:
            print(f"         {addr}")
    print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL
# ══════════════════════════════════════════════════════════

def cmd_entity(args):
    """Get full entity detail by MA ID number."""
    data = _run_helper(["search-id", str(args.id_number)])
    if not data:
        print(f"Entity lookup failed for {args.id_number}")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    if write_output(data, args, summary=f"MA entity {args.id_number}"):
        return

    # Pretty print
    name = data.get("entity_name", "?")
    print(f"\n  [MA] {name}")
    print(f"    ID: {data.get('id_number', '?')}")
    if data.get("old_id_number"):
        print(f"    Old ID: {data['old_id_number']}")
    print(f"    Type: {data.get('entity_type', '?')}")
    print(f"    Organized: {data.get('organization_date', '?')}")
    if data.get("inactive_date"):
        label = data.get("inactive_date_label", "Inactive")
        print(f"    {label}: {data['inactive_date']}")
    if data.get("last_date_certain"):
        print(f"    Last Date Certain: {data['last_date_certain']}")
    if data.get("fiscal_date"):
        print(f"    Fiscal Year End: {data['fiscal_date']}")
    if data.get("publicly_traded"):
        print(f"    Publicly Traded: Yes")

    # Address
    street = data.get("principal_street")
    if street:
        city = data.get("principal_city", "")
        state = data.get("principal_state", "")
        zipcode = data.get("principal_zip", "")
        country = data.get("principal_country", "")
        print(f"\n    Principal Address: {street}")
        print(f"      {city}, {state} {zipcode} {country}".strip())

    # Agent
    agent = data.get("agent_name")
    if agent:
        print(f"\n    Registered Agent: {agent}")
        astreet = data.get("agent_street", "")
        acity = data.get("agent_city", "")
        astate = data.get("agent_state", "")
        azip = data.get("agent_zip", "")
        if astreet:
            print(f"      {astreet}")
            print(f"      {acity}, {astate} {azip}".strip())

    # Officers
    officers = data.get("officers", [])
    if officers:
        print(f"\n    Officers ({len(officers)}):")
        for o in officers:
            print(f"      {o.get('title', '?')}: {o.get('name', '?')}")
            if o.get("address"):
                print(f"        {o['address']}")

    # Name changes
    changes = data.get("name_changes", [])
    if changes:
        print(f"\n    Name Changes ({len(changes)}):")
        for c in changes:
            print(f"      Changed from '{c.get('from_name', '?')}' on {c.get('date', '?')}")

    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _ingest_entity(db, data, search_row=None):
    """Ingest a MA entity into registry.db. Returns entity_id."""
    source_id = data.get("id_number", "")
    if not source_id and search_row:
        source_id = search_row.get("id_number", "")
    if not source_id:
        return None

    name = data.get("entity_name", "?")
    etype_raw = (data.get("entity_type") or "").lower()
    etype = TYPE_MAP.get(etype_raw, etype_raw.replace(" ", "_") if etype_raw else None)

    # Determine status from inactive_date
    status = "active"
    if data.get("inactive_date"):
        label = (data.get("inactive_date_label") or "").lower()
        if "dissolut" in label:
            status = "dissolved"
        elif "revoc" in label:
            status = "revoked"
        elif "cancel" in label:
            status = "cancelled"
        else:
            status = "inactive"

    formation_date = _parse_date(data.get("organization_date"))
    dissolution_date = _parse_date(data.get("inactive_date"))

    source_url = "https://corp.sec.state.ma.us/corpweb/CorpSearch/CorpSearch.aspx"
    raw_data = json.dumps(data, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='ma' AND source_id=?",
        [source_id],
    ).fetchone()

    if existing:
        entity_id = existing[0]
        db.execute(
            """UPDATE registry_entities SET
                entity_name=?, entity_type=?, status=?,
                formation_date=?, dissolution_date=?,
                principal_address=?, principal_city=?, principal_state=?,
                principal_zip=?, principal_country=?,
                state_of_formation=?, source_url=?, raw_data=?,
                updated_at=datetime('now')
            WHERE id=?""",
            [name, etype, status, formation_date, dissolution_date,
             data.get("principal_street"), data.get("principal_city"),
             data.get("principal_state"), data.get("principal_zip"),
             data.get("principal_country") or "US",
             "Massachusetts", source_url, raw_data, entity_id],
        )
    else:
        db.execute(
            """INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date, dissolution_date,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                state_of_formation, source_url, raw_data
            ) VALUES ('ma', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Massachusetts', ?, ?)""",
            [source_id, name, etype, status, formation_date, dissolution_date,
             data.get("principal_street"), data.get("principal_city"),
             data.get("principal_state"), data.get("principal_zip"),
             data.get("principal_country") or "US",
             source_url, raw_data],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='ma' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Registered agent
    agent_name = data.get("agent_name", "").strip()
    if agent_name:
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [entity_id, agent_name, data.get("agent_street"),
                 data.get("agent_city"), data.get("agent_state"),
                 data.get("agent_zip")],
            )
        except Exception:
            pass

    # Officers
    for officer in data.get("officers", []):
        oname = officer.get("name", "").strip()
        if not oname:
            continue
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_officers
                (entity_id, name, title, address)
                VALUES (?, ?, ?, ?)""",
                [entity_id, oname, officer.get("title"), officer.get("address")],
            )
        except Exception:
            pass

    # Name changes -> name history
    for change in data.get("name_changes", []):
        fname = change.get("from_name", "").strip()
        if not fname:
            continue
        cdate = _parse_date(change.get("date"))
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_name_history
                (entity_id, old_name, change_date)
                VALUES (?, ?, ?)""",
                [entity_id, fname, cdate],
            )
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a specific MA entity into registry.db."""
    data = _run_helper(["search-id", str(args.id_number)])
    if not data or "error" in data:
        print(f"Entity lookup failed for {args.id_number}")
        return

    # Ensure id_number is present (detail page may not include it)
    if not data.get("id_number"):
        data["id_number"] = str(args.id_number)

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")  # Wait up to 30s for lock
    entity_id = _ingest_entity(db, data)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        name = data.get("entity_name", "?")
        print(f"Ingested: {name} ({args.id_number}) -> registry ID {entity_id}")
    else:
        print(f"Failed to ingest MA entity {args.id_number}")


def cmd_ingest_search(args):
    """Search and ingest all matching MA entities."""
    helper_args = ["search", args.query]
    if args.type:
        helper_args.extend(["--type", args.type])

    data = _run_helper(helper_args)
    if not data or "error" in data:
        print(f"Search failed: {data.get('error') if data else 'no response'}")
        return

    results = data.get("results", [])
    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} MA entities. Fetching details and ingesting...")
    print("  (Each entity requires a browser session — this will be slow)")

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")  # Wait up to 30s for lock
    ingested = 0
    for i, r in enumerate(results):
        eid = r.get("id_number", "").strip()
        name = r.get("entity_name", "?")
        if not eid:
            continue

        # Fetch full detail via ID search
        detail = _run_helper(["search-id", eid])
        if not detail or "error" in detail:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} ({eid})")
            continue

        # Ensure id_number is present (detail page may not include it)
        if not detail.get("id_number"):
            detail["id_number"] = eid

        entity_id = _ingest_entity(db, detail, search_row=r)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {name} ({eid}) -> registry ID {entity_id}")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} ({eid})")

        if ingested % 5 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    log_search(args.query, "ma_corp_registry", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MA Secretary of the Commonwealth corporate registry (requires browser helper)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search MA business entities by name")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--type", choices=["B", "M", "F", "S"], default="B",
                   help="Search type: B=Begins with, M=Exact, F=Full text, S=Soundex (default: B)")
    p.add_argument("--limit", type=int, default=None, help="Max results to display")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity detail by ID number")
    p.add_argument("id_number", help="MA entity ID number (e.g., 000487270)")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db")
    p.add_argument("id_number", help="MA entity ID number")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--type", choices=["B", "M", "F", "S"], default="B",
                   help="Search type (default: B=Begins with)")
    p.add_argument("--limit", type=int, default=20, help="Max entities to ingest (default: 20)")

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
