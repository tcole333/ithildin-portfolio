#!/usr/bin/env python3
"""
Michigan LARA Business Registry tool.

Queries the MI LARA Division of Corporations portal API for business entity
information. The API is behind Cloudflare WAF, so a Node.js browser helper
(_mi_browser_helper.js) is used for requests.

Covers domestic/foreign corporations, LLCs, LPs, LLPs, nonprofits, and
professional entities registered in Michigan.

Usage:
    python tools/query_michigan.py search "EPSTEIN"
    python tools/query_michigan.py search "APOLLO" --contains
    python tools/query_michigan.py entity 85956 802112570
    python tools/query_michigan.py ingest 85956 802112570
    python tools/query_michigan.py ingest-search "EPSTEIN"
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


# ══════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════

HELPER_PATH = Path(__file__).parent / "_mi_browser_helper.js"

# Entity type mapping: MI type string → registry schema
TYPE_MAP = {
    "domestic limited liability company": "llc",
    "foreign limited liability company": "foreign_llc",
    "domestic profit corporation": "corp",
    "foreign profit corporation": "foreign_corp",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "domestic professional corporation": "prof_corp",
    "foreign professional corporation": "foreign_prof_corp",
    "domestic professional limited liability company": "prof_llc",
    "foreign professional limited liability company": "foreign_prof_llc",
    "domestic limited partnership": "lp",
    "foreign limited partnership": "foreign_lp",
    "domestic limited liability partnership": "llp",
    "foreign limited liability partnership": "foreign_llp",
    "domestic ecclesiastical corporation": "ecclesiastical_corp",
    "domestic low-profit limited liability company": "l3c",
}

STATUS_MAP = {
    "active": "active",
    "dissolved - certificate of dissolution": "dissolved",
    "dissolved - operation of law": "dissolved",
    "dissolved - term expired": "dissolved",
    "dissolved - court order": "dissolved",
    "revoked - failure to comply": "revoked",
    "revoked - operation of law": "revoked",
    "withdrawn - certificate of withdrawal": "withdrawn",
    "withdrawn - court order": "withdrawn",
    "cancelled - certificate of cancellation": "cancelled",
    "cancelled - court order": "cancelled",
    "cancelled - term expired": "cancelled",
    "existence ceased - consolidated": "inactive",
    "existence ceased - merged": "inactive",
    "rescinded": "inactive",
    "converted": "inactive",
    "not registered": "inactive",
    "expired": "expired",
    "other": "inactive",
}


# ══════════════════════════════════════════════════════════
# BROWSER HELPER INTERFACE
# ══════════════════════════════════════════════════════════

def _run_helper(args_list, timeout=120):
    """Run the MI browser helper and return parsed JSON."""
    if not HELPER_PATH.exists():
        print(f"ERROR: Browser helper not found at {HELPER_PATH}", file=sys.stderr)
        return None

    cmd = ["node", str(HELPER_PATH)] + args_list

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
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


# ══════════════════════════════════════════════════════════
# SEARCH COMMAND
# ══════════════════════════════════════════════════════════

def _parse_search_results(data):
    """Parse MI search API response into a list of entity dicts."""
    if not data or "rows" not in data:
        return []

    results = []
    rows = data["rows"]
    for key in sorted(rows.keys(), key=lambda k: rows[k].get("SORT_INDEX", 0)):
        row = rows[key]
        title = row.get("TITLE", ["?", ""])
        name = title[0] if isinstance(title, list) else title
        assumed = title[1] if isinstance(title, list) and len(title) > 1 else ""

        results.append({
            "internal_id": row.get("ID"),
            "filing_number": row.get("RECORD_NUM") or row.get("EntityId"),
            "name": name,
            "assumed_name": assumed if assumed else None,
            "entity_type": row.get("BusinessRecordTypeId"),
            "status": row.get("BusinessStatusId"),
            "filing_date": row.get("RegistrationDate"),
            "agent": row.get("Agent"),
            "ar_standing": row.get("ARStanding"),
            "ar_due_date": row.get("AnnualReportDueDate"),
        })

    return results


def cmd_search(args):
    """Search MI business entities."""
    helper_args = ["search", args.query]
    if args.contains:
        helper_args.append("--contains")

    data = _run_helper(helper_args)
    if not data:
        print("Search failed (browser helper returned no data)")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    results = _parse_search_results(data)
    log_search(args.query, "mi_lara", len(results))

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    if write_output(results, args, summary=f"MI search '{args.query}' ({len(results)} results)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {len(results)} MI entities matching '{args.query}'")
    print()
    for r in results:
        print(f"  {r['name']} ({r['entity_type']})")
        print(f"    ID: {r['filing_number']} (internal: {r['internal_id']}) | Status: {r['status']} | Filed: {r['filing_date']}")
        if r.get("agent"):
            print(f"    Agent: {r['agent']}")
        if r.get("assumed_name"):
            print(f"    Assumed name: {r['assumed_name']}")
        print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL COMMAND
# ══════════════════════════════════════════════════════════

def _parse_detail(detail_data):
    """Parse MI FilingDetail response into a flat dict."""
    if not detail_data or "DRAWER_DETAIL_LIST" not in detail_data:
        return {}

    result = {}
    for item in detail_data["DRAWER_DETAIL_LIST"]:
        label = item.get("LABEL", "")
        value = item.get("VALUE", "")
        result[label] = value

    return result


def cmd_entity(args):
    """Get full entity detail + history."""
    data = _run_helper(["full", str(args.internal_id), str(args.filing_number)])
    if not data:
        print(f"Entity lookup failed")
        return

    detail = _parse_detail(data.get("detail", {}))
    history = data.get("history", {})
    assumed = data.get("assumed_names", {})

    result = {
        "detail": detail,
        "history": history,
        "assumed_names": assumed,
        "internal_id": args.internal_id,
        "filing_number": args.filing_number,
    }

    if write_output(result, args, summary=f"MI entity {args.filing_number}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    # Pretty print
    name = detail.get("Entity Name", "?")
    print(f"\n  [MI] {name}")
    print(f"    Filing #: {detail.get('Identification #', args.filing_number)}")
    print(f"    Type: {detail.get('Entity Type', '?')}")
    print(f"    Status: {detail.get('Entity Status', '?')}")
    if detail.get("Jurisdiction"):
        print(f"    Jurisdiction: {detail['Jurisdiction']}")
    if detail.get("Initial Filing Date"):
        print(f"    Filed: {detail['Initial Filing Date']}")
    if detail.get("Inactive Date"):
        print(f"    Inactive: {detail['Inactive Date']}")
    if detail.get("AR Standing"):
        print(f"    AR Standing: {detail['AR Standing']}")
    if detail.get("AR Due Date"):
        print(f"    AR Due Date: {detail['AR Due Date']}")
    if detail.get("Management Type"):
        print(f"    Management: {detail['Management Type']}")

    # Resident Agent
    ra = detail.get("Resident Agent Name")
    if ra:
        print(f"\n    Resident Agent: {ra}")
        addr = detail.get("Registered Office Street Address")
        if addr:
            print(f"      Address: {addr}")
        mailing = detail.get("Resident Agent Mailing Address")
        if mailing:
            print(f"      Mailing: {mailing}")

    # Filing History
    amendments = history.get("AMENDMENT_LIST", []) if isinstance(history, dict) else []
    if amendments:
        print(f"\n    Filing History ({len(amendments)} records):")
        for a in amendments:
            date = a.get("AMENDMENT_DATE", "?")
            ftype = a.get("AMENDMENT_TYPE", "?")
            print(f"      {date}: {ftype}")

    # Assumed Names
    names = assumed.get("Names", []) if isinstance(assumed, dict) else []
    if names:
        print(f"\n    Assumed Names ({len(names)}):")
        for n in names:
            active = "Active" if n.get("IsActive") else "Inactive"
            print(f"      {n.get('Name', '?')} ({active}, created {n.get('CreationDate', '?')})")

    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _parse_date(date_str):
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    if not date_str or "/" not in date_str:
        return date_str
    parts = date_str.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return date_str


def _parse_address(addr_str):
    """Parse a MI address string like '3825 CARPENTER RD, YPSILANTI, MI 48197'."""
    if not addr_str:
        return {}
    parts = [p.strip() for p in addr_str.split(",")]
    if len(parts) >= 3:
        # "STREET, CITY, STATE ZIP"
        street = parts[0]
        city = parts[1] if len(parts) > 1 else ""
        state_zip = parts[2] if len(parts) > 2 else ""
        state_parts = state_zip.split()
        state = state_parts[0] if state_parts else ""
        zipcode = state_parts[1] if len(state_parts) > 1 else ""
        return {"street": street, "city": city, "state": state, "zip": zipcode}
    elif len(parts) == 2:
        return {"street": parts[0], "city": parts[1]}
    return {"street": addr_str}


def _ingest_entity_to_registry(db, internal_id, filing_number, detail=None, search_row=None):
    """Ingest a MI entity into registry.db. Returns entity_id or None."""
    if not detail:
        data = _run_helper(["full", str(internal_id), str(filing_number)])
        if not data:
            return None
        detail = _parse_detail(data.get("detail", {}))
        history = data.get("history", {})
    else:
        history = {}

    name = detail.get("Entity Name", "?")
    if name == "?" and search_row:
        name = search_row.get("name", "?")

    etype_raw = (detail.get("Entity Type") or "").lower()
    etype = TYPE_MAP.get(etype_raw, etype_raw.replace(" ", "_") if etype_raw else None)

    status_raw = (detail.get("Entity Status") or "").lower()
    status = STATUS_MAP.get(status_raw, status_raw if status_raw else None)

    formation_date = _parse_date(detail.get("Initial Filing Date"))
    inactive_date = _parse_date(detail.get("Inactive Date"))
    jurisdiction = detail.get("Jurisdiction", "Michigan")

    # Parse registered office address
    addr = _parse_address(detail.get("Registered Office Street Address"))

    source_url = f"https://mibusinessregistry.lara.state.mi.us/search/business"

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, dissolution_date,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            state_of_formation, source_url, raw_data
        ) VALUES ('mi', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?)
    """, [
        str(filing_number), name, etype, status,
        formation_date or None, inactive_date or None,
        addr.get("street"), addr.get("city"),
        addr.get("state"), addr.get("zip"),
        jurisdiction or "Michigan",
        source_url,
        json.dumps({"detail": detail, "internal_id": internal_id}, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='mi' AND source_id=?",
        [str(filing_number)]
    ).fetchone()
    entity_id = row[0]

    # Resident Agent
    ra_name = (detail.get("Resident Agent Name") or "").strip()
    if ra_name:
        ra_addr = _parse_address(detail.get("Registered Office Street Address"))
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                entity_id, ra_name,
                ra_addr.get("street"), ra_addr.get("city"),
                ra_addr.get("state"), ra_addr.get("zip"),
            ])
        except Exception:
            pass

    # Filing history
    amendments = history.get("AMENDMENT_LIST", []) if isinstance(history, dict) else []
    for a in amendments:
        filing_date = _parse_date(a.get("AMENDMENT_DATE"))
        filing_type = a.get("AMENDMENT_TYPE", "")
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, description, raw_data)
                VALUES (?, ?, ?, ?, ?)
            """, [
                entity_id, filing_type, filing_date, filing_type,
                json.dumps(a, default=str),
            ])
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a single entity into registry.db."""
    db = get_db()
    entity_id = _ingest_entity_to_registry(db, args.internal_id, args.filing_number)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        row = db.execute("SELECT entity_name FROM registry_entities WHERE id=?", [entity_id]).fetchone()
        name = row[0] if row else "?"
        print(f"Ingested: {name} (MI #{args.filing_number}, registry ID: {entity_id})")
    else:
        print(f"Failed to ingest MI entity {args.filing_number}")


def cmd_ingest_search(args):
    """Search and ingest all matching entities."""
    helper_args = ["search", args.query, "--contains"]
    data = _run_helper(helper_args)
    if not data or "error" in data:
        print(f"Search failed: {data.get('error') if data else 'no response'}")
        return

    results = _parse_search_results(data)
    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} MI entities. Fetching details and ingesting...")
    print("  (Each entity requires a browser session — this will be slow)")

    db = get_db()
    ingested = 0
    for i, r in enumerate(results):
        iid = r.get("internal_id")
        fnum = r.get("filing_number")
        name = r.get("name", "?")
        if not iid or not fnum:
            continue

        entity_id = _ingest_entity_to_registry(db, iid, fnum, search_row=r)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {name} (MI #{fnum}, reg ID: {entity_id})")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} (MI #{fnum})")

        if ingested % 5 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    try:
        db.execute("""
            INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('mi', 'api', ?, ?)
        """, [ingested, f"MI LARA search: '{args.query}'"])
        db.commit()
    except Exception:
        pass

    log_search(args.query, "mi_lara-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MI LARA Business Registry (requires browser helper for Cloudflare)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search MI business entities by name")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--contains", action="store_true", help="Use 'contains' match (default: starts with)")
    p.add_argument("--limit", type=int, default=100, help="Max results (default: 100)")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity detail + history")
    p.add_argument("internal_id", help="MI internal ID (from search results)")
    p.add_argument("filing_number", help="MI public filing number")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest a single entity into registry.db")
    p.add_argument("internal_id", help="MI internal ID")
    p.add_argument("filing_number", help="MI public filing number")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
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
