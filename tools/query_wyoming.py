#!/usr/bin/env python3
"""
Wyoming Secretary of State (WyoBiz) corporate registry tool.

Queries the WY SoS entity search at wyobiz.wyo.gov/Business/FilingSearch.aspx.
The site uses F5 Advanced WAF with CAPTCHA, so a Node.js browser helper
(_wy_browser_helper.js) is required for requests.

Data available: entity name, filing ID, entity type, status, tax/RA standing,
formation date, formation jurisdiction, principal office, mailing address,
registered agent (name/address), filing history, parties (organizers/members).

Wyoming is the #1 crypto-friendly LLC state — key for investigating entities
like CIC Digital LLC, Fight Fight Fight LLC, and World Liberty Financial.

Usage:
    python tools/query_wyoming.py warmup
    python tools/query_wyoming.py search "TRUMP"
    python tools/query_wyoming.py search "WORLD LIBERTY" --mode contains
    python tools/query_wyoming.py entity 2021-001032098
    python tools/query_wyoming.py detail <eFNum>
    python tools/query_wyoming.py ingest 2021-001032098
    python tools/query_wyoming.py ingest-search "TRUMP" --limit 20
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


HELPER_PATH = Path(__file__).parent / "_wy_browser_helper.js"

# Entity type mapping: WY abbreviation -> unified type
TYPE_MAP = {
    "limited liability company - domestic": "llc",
    "limited liability company - foreign": "foreign_llc",
    "profit corporation - domestic": "corp",
    "profit corporation - foreign": "foreign_corp",
    "nonprofit corporation - domestic": "nonprofit",
    "nonprofit corporation - foreign": "foreign_nonprofit",
    "limited partnership - domestic": "lp",
    "limited partnership - foreign": "foreign_lp",
    "limited liability partnership - domestic": "llp",
    "limited liability partnership - foreign": "foreign_llp",
    "statutory trust - domestic": "trust",
    "statutory trust - foreign": "foreign_trust",
    "benefit corporation - domestic": "benefit_corp",
    "close limited liability company - domestic": "close_llc",
    # Abbreviations from search results
    "llc": "llc",
    "corp": "corp",
    "ncorp": "nonprofit",
    "lp": "lp",
    "llp": "llp",
}

# Status mapping: WY raw status -> unified status
STATUS_MAP = {
    "active": "active",
    "inactive": "inactive",
    "inactive - dissolved": "dissolved",
    "inactive - administratively dissolved (tax)": "dissolved",
    "inactive - administratively dissolved (no agent)": "dissolved",
    "inactive - revoked (tax)": "revoked",
    "inactive - revoked": "revoked",
    "inactive - expired": "expired",
    "inactive - withdrawn": "withdrawn",
    "inactive - merged": "dissolved",
}


def _run_helper(args_list, timeout=120):
    """Run the WY browser helper and return parsed JSON."""
    if not HELPER_PATH.exists():
        print(f"ERROR: Browser helper not found at {HELPER_PATH}", file=sys.stderr)
        print("  Ensure _wy_browser_helper.js is in tools/", file=sys.stderr)
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
    """Convert various date formats to YYYY-MM-DD."""
    if not date_str:
        return None
    import re
    # MM/DD/YYYY or M/D/YYYY
    match = re.match(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', date_str)
    if match:
        return f"{match.group(3)}-{match.group(1).zfill(2)}-{match.group(2).zfill(2)}"
    # YYYY-MM-DD (already correct)
    match = re.match(r'(\d{4})-(\d{2})-(\d{2})', date_str)
    if match:
        return date_str
    return None


def _normalize_status(raw_status):
    """Normalize WY status to unified status."""
    if not raw_status:
        return None
    key = raw_status.strip().lower()
    return STATUS_MAP.get(key, key.replace(" ", "_"))


def _normalize_type(raw_type):
    """Normalize WY entity type to unified type."""
    if not raw_type:
        return None
    key = raw_type.strip().lower()
    return TYPE_MAP.get(key, key.replace(" ", "_"))


def _parse_address(addr_str):
    """Parse a comma-separated address string into components."""
    if not addr_str:
        return {}
    parts = [p.strip() for p in addr_str.split(",")]
    result = {}
    if parts:
        result["street"] = parts[0]
    if len(parts) >= 3:
        # "1309 Coffeen Avenue STE 1200, Sheridan, WY 82801, USA"
        result["city"] = parts[1] if len(parts) > 1 else None
        # State + ZIP
        state_zip = parts[2] if len(parts) > 2 else ""
        import re
        sz_match = re.match(r'([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', state_zip)
        if sz_match:
            result["state"] = sz_match.group(1)
            result["zip"] = sz_match.group(2)
        else:
            result["state"] = state_zip
        if len(parts) > 3:
            result["country"] = parts[3]
    elif len(parts) == 2:
        result["city"] = parts[1]

    return result


# =====================================================
# WARMUP
# =====================================================

def cmd_warmup(args):
    """Open browser for manual F5 CAPTCHA solving."""
    print("Opening WY SoS browser for warmup...")
    print("  Solve any F5 CAPTCHA challenge in the browser window.")
    print("  Press Ctrl+C in the terminal when done.")
    print()
    cmd = ["node", str(HELPER_PATH), "warmup"]
    try:
        subprocess.run(cmd, timeout=300)
        print("\nWarmup complete. Cookies cached for future requests.")
    except subprocess.TimeoutExpired:
        print("\nWarmup timed out after 5 minutes.")
    except KeyboardInterrupt:
        print("\nWarmup cancelled.")


# =====================================================
# SEARCH
# =====================================================

def cmd_search(args):
    """Search WY business entities by name."""
    helper_args = ["search", args.query]
    if args.mode == "contains":
        helper_args.append("--contains")

    data = _run_helper(helper_args, timeout=180)
    if not data:
        print("Search failed (browser helper returned no data)")
        return

    if "error" in data:
        print(f"Search error: {data['error']}")
        return

    results = data.get("results", [])
    count = data.get("count", len(results))
    log_search(args.query, "wy_sos_registry", len(results))

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    if write_output({"count": count, "results": results}, args,
                     summary=f"WY search '{args.query}' ({count} total, {len(results)} returned)"):
        return

    mode_label = "starts with" if args.mode == "starts" else "contains"
    print(f"Found {count} WY entities matching '{args.query}' ({mode_label})")
    print()
    for r in results:
        name = r.get("entity_name", "?")
        fid = r.get("filing_id", "?")
        status = r.get("status", "")
        etype = r.get("entity_type_abbrev", "")
        date = r.get("filed_on", "")
        tax = r.get("tax_standing", "")
        ra = r.get("ra_standing", "")

        parts = [f"  {fid} | {name}"]
        if etype:
            parts.append(f"         Type: {etype}")
        if status:
            parts.append(f"         Status: {status}")
        standings = []
        if tax:
            standings.append(f"Tax: {tax}")
        if ra:
            standings.append(f"RA: {ra}")
        if standings:
            parts.append(f"         Standing: {', '.join(standings)}")
        if date:
            parts.append(f"         Filed: {date}")
        print("\n".join(parts))
    print()


# =====================================================
# ENTITY DETAIL (by filing ID)
# =====================================================

def cmd_entity(args):
    """Get full entity detail by WY filing ID (e.g. 2021-001032098)."""
    data = _run_helper(["full", str(args.filing_id)], timeout=120)
    if not data:
        print(f"Entity lookup failed for {args.filing_id}")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    if write_output(data, args, summary=f"WY entity {args.filing_id}"):
        return

    _print_entity_detail(data)


# =====================================================
# DETAIL (by encrypted eFNum)
# =====================================================

def cmd_detail(args):
    """Get entity detail by encrypted eFNum (from search result links)."""
    data = _run_helper(["detail", args.efnum], timeout=120)
    if not data:
        print(f"Detail lookup failed for eFNum {args.efnum}")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    if write_output(data, args, summary=f"WY entity (eFNum)"):
        return

    _print_entity_detail(data)


def _print_entity_detail(data):
    """Pretty-print entity detail."""
    name = data.get("entity_name", "?")
    print(f"\n  [WY] {name}")
    print(f"    Filing ID: {data.get('filing_id', '?')}")
    print(f"    Type: {data.get('entity_type', '?')}")
    print(f"    Status: {data.get('status', '?')}")
    if data.get("sub_status"):
        print(f"    Sub-Status: {data['sub_status']}")
    print(f"    Filed: {data.get('initial_filing_date', '?')}")

    # Standings
    standings = []
    if data.get("tax_standing"):
        standings.append(f"Tax: {data['tax_standing']}")
    if data.get("ra_standing"):
        standings.append(f"RA: {data['ra_standing']}")
    if data.get("other_standing"):
        standings.append(f"Other: {data['other_standing']}")
    if standings:
        print(f"    Standings: {', '.join(standings)}")

    if data.get("formed_in"):
        print(f"    Formed In: {data['formed_in']}")
    if data.get("term_of_duration"):
        print(f"    Term: {data['term_of_duration']}")
    if data.get("fictitious_name"):
        print(f"    Fictitious Name: {data['fictitious_name']}")

    # Addresses
    if data.get("principal_office"):
        print(f"\n    Principal Office: {data['principal_office']}")
    if data.get("mailing_address"):
        print(f"    Mailing Address: {data['mailing_address']}")

    # Registered Agent
    if data.get("agent_name"):
        print(f"\n    Registered Agent: {data['agent_name']}")
        if data.get("agent_address"):
            print(f"      Address: {data['agent_address']}")

    # AR info
    if data.get("latest_ar"):
        print(f"\n    Latest AR: {data['latest_ar']}")
    if data.get("license_tax"):
        print(f"    License Tax: {data['license_tax']}")

    # Parties
    parties = data.get("parties", [])
    if parties:
        print(f"\n    Parties ({len(parties)}):")
        for p in parties:
            role = p.get("role", "?")
            name_or_org = p.get("name") or p.get("organization") or "?"
            addr = p.get("address", "")
            print(f"      {role}: {name_or_org}")
            if addr:
                print(f"        {addr}")

    # Filing History
    filings = data.get("filings", [])
    if filings:
        print(f"\n    Filing History ({len(filings)}):")
        for f in filings:
            date = f.get("date", "?")
            desc = f.get("description", "?")
            print(f"      {date} | {desc}")

    print()


# =====================================================
# REGISTRY INGESTION
# =====================================================

def _ingest_entity(db, data):
    """Ingest a WY entity into registry.db. Returns entity_id."""
    source_id = (data.get("filing_id") or "").strip()
    if not source_id:
        return None

    name = data.get("entity_name", "?")
    etype = _normalize_type(data.get("entity_type"))
    status = _normalize_status(data.get("status"))

    formation_date = _parse_date(data.get("initial_filing_date"))

    # Parse principal office address
    principal_office = data.get("principal_office", "")
    addr_parts = _parse_address(principal_office)
    principal_address = addr_parts.get("street")
    principal_city = addr_parts.get("city")
    principal_state = addr_parts.get("state")
    principal_zip = addr_parts.get("zip")
    principal_country = addr_parts.get("country", "US")

    # State of formation
    state_of_formation = data.get("formed_in") or "Wyoming"

    source_url = data.get("url") or f"https://wyobiz.wyo.gov/Business/FilingSearch.aspx"
    raw_data = json.dumps(data, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='wy' AND source_id=?",
        [source_id],
    ).fetchone()

    if existing:
        entity_id = existing[0]
        db.execute(
            """UPDATE registry_entities SET
                entity_name=?, entity_type=?, status=?,
                formation_date=?,
                principal_address=?, principal_city=?, principal_state=?,
                principal_zip=?, principal_country=?,
                state_of_formation=?, source_url=?, raw_data=?,
                updated_at=datetime('now')
            WHERE id=?""",
            [name, etype, status, formation_date,
             principal_address, principal_city, principal_state,
             principal_zip, principal_country,
             state_of_formation, source_url, raw_data, entity_id],
        )
    else:
        db.execute(
            """INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                state_of_formation, source_url, raw_data
            ) VALUES ('wy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [source_id, name, etype, status, formation_date,
             principal_address, principal_city, principal_state,
             principal_zip, principal_country,
             state_of_formation, source_url, raw_data],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='wy' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Registered agent
    agent_name = (data.get("agent_name") or "").strip()
    if agent_name:
        agent_addr = data.get("agent_address", "")
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address)
                VALUES (?, ?, ?)""",
                [entity_id, agent_name, agent_addr],
            )
        except Exception:
            pass

    # Parties as officers
    for party in data.get("parties", []):
        pname = (party.get("name") or party.get("organization") or "").strip()
        if not pname:
            continue
        role = (party.get("role") or "").strip()
        address = (party.get("address") or "").strip()
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, address)
                VALUES (?, ?, ?, ?)""",
                [entity_id, pname, role, address],
            )
        except Exception:
            pass

    # Filing history
    for filing in data.get("filings", []):
        ftype = (filing.get("description") or "").strip()
        fdate = _parse_date(filing.get("date"))
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, description, raw_data)
                VALUES (?, ?, ?, ?, ?)""",
                [entity_id, ftype, fdate, ftype, json.dumps(filing, default=str)],
            )
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a specific WY entity into registry.db."""
    data = _run_helper(["full", str(args.filing_id)], timeout=120)
    if not data or "error" in data:
        print(f"Entity lookup failed for {args.filing_id}")
        if data:
            print(f"  Error: {data.get('error', 'unknown')}")
        return

    # Ensure filing ID is present
    if not data.get("filing_id"):
        data["filing_id"] = str(args.filing_id)

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")
    entity_id = _ingest_entity(db, data)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        name = data.get("entity_name", "?")
        print(f"Ingested: {name} ({args.filing_id}) -> registry ID {entity_id}")
    else:
        print(f"Failed to ingest WY entity {args.filing_id}")


def cmd_ingest_search(args):
    """Search and ingest all matching WY entities."""
    helper_args = ["search", args.query]
    if args.mode == "contains":
        helper_args.append("--contains")

    data = _run_helper(helper_args, timeout=180)
    if not data or "error" in data:
        print(f"Search failed: {data.get('error') if data else 'no response'}")
        return

    results = data.get("results", [])
    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} WY entities. Fetching details and ingesting...")
    print("  (Each entity requires a browser session -- this will be slow)")

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")
    ingested = 0
    for i, r in enumerate(results):
        fid = (r.get("filing_id") or "").strip()
        name = r.get("entity_name", "?")
        if not fid:
            print(f"  [{i+1}/{len(results)}] SKIP: {name} (no filing ID)")
            continue

        # Fetch full detail
        detail = _run_helper(["full", fid], timeout=120)
        if not detail or "error" in detail:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} ({fid})")
            continue

        # Ensure filing ID is present
        if not detail.get("filing_id"):
            detail["filing_id"] = fid

        entity_id = _ingest_entity(db, detail)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {name} ({fid}) -> registry ID {entity_id}")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} ({fid})")

        if ingested % 5 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    try:
        db.execute(
            """INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('wy', 'browser', ?, ?)""",
            [ingested, f"ingest-search: {args.query}"],
        )
        db.commit()
    except Exception:
        pass

    log_search(args.query, "wy_sos_registry-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# =====================================================
# CLI
# =====================================================

def main():
    parser = argparse.ArgumentParser(
        description="WY Secretary of State (WyoBiz) corporate registry (requires browser helper)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # warmup
    sub.add_parser("warmup", help="Open browser to solve F5 CAPTCHA challenge (caches cookies)")

    # search
    p = sub.add_parser("search", help="Search WY business entities by name")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--mode", choices=["starts", "contains"],
                   default="starts",
                   help="Search mode (default: starts)")
    p.add_argument("--limit", type=int, default=None, help="Max results to display")
    add_output_args(p)

    # entity (by filing ID)
    p = sub.add_parser("entity", help="Get entity detail by filing ID (e.g. 2021-001032098)")
    p.add_argument("filing_id", help="WY filing ID (e.g., 2021-001032098)")
    add_output_args(p)

    # detail (by encrypted eFNum)
    p = sub.add_parser("detail", help="Get entity detail by encrypted eFNum")
    p.add_argument("efnum", help="Encrypted entity parameter from search results")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db by filing ID")
    p.add_argument("filing_id", help="WY filing ID")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--mode", choices=["starts", "contains"],
                   default="starts",
                   help="Search mode (default: starts)")
    p.add_argument("--limit", type=int, default=20, help="Max entities to ingest (default: 20)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "warmup": cmd_warmup,
        "search": cmd_search,
        "entity": cmd_entity,
        "detail": cmd_detail,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
