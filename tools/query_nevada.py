#!/usr/bin/env python3
"""
Nevada Secretary of State (SilverFlume) corporate registry tool.

Queries the NV SOS entity search at esos.nv.gov/EntitySearch.
The site uses Incapsula WAF, so a Node.js browser helper
(_nv_browser_helper.js) is required for requests.

Data available: entity name, entity number, NV Business ID, type, status,
formation date, termination date, jurisdiction, registered agent (name/status/
address), officers (president/secretary/treasurer/director with addresses),
filing history, name history, stock information.

Usage:
    python tools/query_nevada.py probe
    python tools/query_nevada.py search "EPSTEIN"
    python tools/query_nevada.py search "APOLLO" --mode contains
    python tools/query_nevada.py entity E1234567890-2024
    python tools/query_nevada.py ingest E1234567890-2024
    python tools/query_nevada.py ingest-search "EPSTEIN" --limit 20
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


HELPER_PATH = Path(__file__).parent / "_nv_browser_helper.js"

# Entity type mapping: NV raw type -> unified type
TYPE_MAP = {
    "domestic corporation": "corp",
    "corporation": "corp",
    "foreign corporation": "foreign_corp",
    "domestic limited-liability company": "llc",
    "limited-liability company": "llc",
    "limited liability company": "llc",
    "foreign limited-liability company": "foreign_llc",
    "domestic limited partnership": "lp",
    "limited partnership": "lp",
    "foreign limited partnership": "foreign_lp",
    "domestic limited-liability partnership": "llp",
    "limited-liability partnership": "llp",
    "foreign limited-liability partnership": "foreign_llp",
    "nonprofit corporation": "nonprofit",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "business trust": "trust",
    "domestic business trust": "trust",
    "foreign business trust": "foreign_trust",
    "professional corporation": "professional_corp",
    "professional limited-liability company": "professional_llc",
    "sole proprietor": "sole_prop",
}

# Status mapping: NV raw status -> unified status
STATUS_MAP = {
    "active": "active",
    "default": "active",
    "permanently revoked": "revoked",
    "revoked": "revoked",
    "dissolved": "dissolved",
    "merged out": "dissolved",
    "expired": "expired",
    "withdrawn": "withdrawn",
    "terminated": "terminated",
}


def _run_helper(args_list, timeout=120):
    """Run the NV browser helper and return parsed JSON."""
    if not HELPER_PATH.exists():
        print(f"ERROR: Browser helper not found at {HELPER_PATH}", file=sys.stderr)
        print("  Ensure _nv_browser_helper.js is in tools/", file=sys.stderr)
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
    # Try other formats
    match = re.match(r'(\w+)\s+(\d{1,2}),?\s+(\d{4})', date_str)
    if match:
        months = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12',
            'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
            'jun': '06', 'jul': '07', 'aug': '08', 'sep': '09',
            'oct': '10', 'nov': '11', 'dec': '12',
        }
        month = months.get(match.group(1).lower())
        if month:
            return f"{match.group(3)}-{month}-{match.group(2).zfill(2)}"
    return None


def _normalize_status(raw_status):
    """Normalize NV status to unified status."""
    if not raw_status:
        return None
    key = raw_status.strip().lower()
    return STATUS_MAP.get(key, key.replace(" ", "_"))


def _normalize_type(raw_type):
    """Normalize NV entity type to unified type."""
    if not raw_type:
        return None
    key = raw_type.strip().lower()
    return TYPE_MAP.get(key, key.replace(" ", "_"))


# ══════════════════════════════════════════════════════════
# PROBE
# ══════════════════════════════════════════════════════════

def cmd_warmup(args):
    """Open browser for manual Incapsula challenge solving."""
    print("Opening NV SOS browser for warmup...")
    print("  Solve any Incapsula/CAPTCHA challenge in the browser window.")
    print("  Press Enter in the terminal when done.")
    print()
    # Run warmup interactively (not captured — user needs to see stdin prompt)
    cmd = ["node", str(HELPER_PATH), "warmup"]
    try:
        subprocess.run(cmd, timeout=300)
        print("\nWarmup complete. Cookies cached for future requests.")
    except subprocess.TimeoutExpired:
        print("\nWarmup timed out after 5 minutes.")
    except KeyboardInterrupt:
        print("\nWarmup cancelled.")


def cmd_probe(args):
    """Probe the NV SOS portal to discover its structure."""
    data = _run_helper(["probe"])
    if not data:
        print("Probe failed (browser helper returned no data)")
        return

    if write_output(data, args, summary="NV SOS portal probe"):
        return

    print("\n  NV SOS Portal Structure")
    print(f"  URL: {data.get('url', '?')}")
    print(f"  Title: {data.get('title', '?')}")

    meta = data.get("meta", {})
    if meta.get("isAngular"):
        print("  Framework: Angular")
    elif meta.get("isReact"):
        print("  Framework: React")
    elif meta.get("hasViewState"):
        print("  Framework: ASP.NET WebForms")
    if meta.get("hasAntiForgery"):
        print("  Anti-forgery token: Yes")

    forms = data.get("forms", [])
    print(f"\n  Forms ({len(forms)}):")
    for f in forms:
        print(f"    id={f.get('id', '?')} action={f.get('action', '?')} method={f.get('method', '?')}")

    inputs = data.get("inputs", [])
    print(f"\n  Inputs ({len(inputs)}):")
    for inp in inputs:
        label = inp.get("label", "")
        print(f"    id={inp.get('id', '?')} name={inp.get('name', '?')} type={inp.get('type', '?')}")
        if label:
            print(f"      label: {label}")

    selects = data.get("selects", [])
    print(f"\n  Selects ({len(selects)}):")
    for sel in selects:
        opts = sel.get("options", [])
        print(f"    id={sel.get('id', '?')} name={sel.get('name', '?')} label={sel.get('label', '')}")
        for opt in opts[:10]:
            print(f"      {opt.get('value', '?')}: {opt.get('text', '?')}")
        if len(opts) > 10:
            print(f"      ... and {len(opts) - 10} more")

    buttons = data.get("buttons", [])
    print(f"\n  Buttons ({len(buttons)}):")
    for btn in buttons:
        print(f"    id={btn.get('id', '?')} type={btn.get('type', '?')} text={btn.get('text', '?')}")

    scripts = data.get("scripts", [])
    if scripts:
        print(f"\n  Scripts ({len(scripts)}):")
        for s in scripts[:10]:
            print(f"    {s}")
    print()


# ══════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search NV business entities by name."""
    helper_args = ["search", args.query]
    if args.mode:
        helper_args.extend(["--mode", args.mode])

    data = _run_helper(helper_args)
    if not data:
        print("Search failed (browser helper returned no data)")
        return

    if "error" in data:
        print(f"Search error: {data['error']}")
        return

    # Handle API intercept results
    if data.get("source") == "api_intercept":
        results = data.get("data", [])
        if isinstance(results, dict):
            results = results.get("results", results.get("data", [results]))
    else:
        results = data.get("results", [])

    count = data.get("count", len(results))
    log_search(args.query, "nv_sos_registry", len(results))

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    if write_output({"count": count, "results": results}, args,
                     summary=f"NV search '{args.query}' ({count} total, {len(results)} returned)"):
        return

    mode_label = {
        "starts": "starts with", "contains": "contains",
        "exact": "exact match", "all": "all words",
    }.get(args.mode or "starts", args.mode)
    print(f"Found {count} NV entities matching '{args.query}' ({mode_label})")
    print()
    for r in results:
        name = r.get("entity_name", "?")
        num = r.get("entity_number", r.get("nv_business_id", "?"))
        status = r.get("status", "")
        etype = r.get("entity_type", "")
        date = r.get("filing_date", "")
        parts = [f"  {num} | {name}"]
        if etype:
            parts.append(f"         Type: {etype}")
        if status:
            parts.append(f"         Status: {status}")
        if date:
            parts.append(f"         Filed: {date}")
        print("\n".join(parts))
    print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL
# ══════════════════════════════════════════════════════════

def cmd_entity(args):
    """Get full entity detail by NV entity number."""
    data = _run_helper(["entity", str(args.entity_number)])
    if not data:
        print(f"Entity lookup failed for {args.entity_number}")
        return

    if "error" in data:
        print(f"API error: {data['error']}")
        return

    if write_output(data, args, summary=f"NV entity {args.entity_number}"):
        return

    _print_entity_detail(data)


def _print_entity_detail(data):
    """Pretty-print entity detail."""
    name = data.get("entity_name", "?")
    print(f"\n  [NV] {name}")
    print(f"    Entity Number: {data.get('entity_number', '?')}")
    if data.get("nv_business_id"):
        print(f"    NV Business ID: {data['nv_business_id']}")
    print(f"    Type: {data.get('entity_type', '?')}")
    print(f"    Status: {data.get('status', '?')}")
    print(f"    Formation Date: {data.get('formation_date', '?')}")
    if data.get("termination_date"):
        print(f"    Termination Date: {data['termination_date']}")
    if data.get("jurisdiction"):
        print(f"    Jurisdiction: {data['jurisdiction']}")
    if data.get("annual_report_due"):
        print(f"    Annual Report Due: {data['annual_report_due']}")
    if data.get("compliance_hold"):
        print(f"    Compliance Hold: {data['compliance_hold']}")

    # Registered agent
    agent = data.get("agent_name")
    if agent:
        print(f"\n    Registered Agent: {agent}")
        if data.get("agent_status"):
            print(f"      Status: {data['agent_status']}")
        if data.get("agent_address"):
            print(f"      Address: {data['agent_address']}")

    # Officers
    officers = data.get("officers", [])
    if officers:
        print(f"\n    Officers ({len(officers)}):")
        for o in officers:
            print(f"      {o.get('title', '?')}: {o.get('name', '?')}")
            if o.get("address"):
                print(f"        {o['address']}")

    # Filing history
    filings = data.get("filings", [])
    if filings:
        print(f"\n    Filing History ({len(filings)}):")
        for f in filings:
            date = f.get("file_date", "?")
            ftype = f.get("document_type", f.get("filing_type", "?"))
            fnum = f.get("filing_number", "")
            print(f"      {date} | {ftype} {f'(#{fnum})' if fnum else ''}")

    # Name history
    name_hist = data.get("name_history", [])
    if name_hist:
        print(f"\n    Name History ({len(name_hist)}):")
        for n in name_hist:
            print(f"      {n.get('date', '?')}: {n.get('previous_name', '?')}")

    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _ingest_entity(db, data, search_row=None):
    """Ingest a NV entity into registry.db. Returns entity_id."""
    source_id = (data.get("entity_number") or data.get("nv_business_id") or "").strip()
    if not source_id and search_row:
        source_id = (search_row.get("entity_number") or
                     search_row.get("nv_business_id") or "").strip()
    if not source_id:
        return None

    name = data.get("entity_name", "?")
    etype = _normalize_type(data.get("entity_type"))
    status = _normalize_status(data.get("status"))

    formation_date = _parse_date(data.get("formation_date"))
    dissolution_date = _parse_date(data.get("termination_date"))

    # Address: NV detail page may have agent address as the main address
    # or the entity may have a principal address
    principal_address = data.get("principal_address")
    principal_city = data.get("principal_city")
    principal_state = data.get("principal_state")
    principal_zip = data.get("principal_zip")
    principal_country = data.get("principal_country")

    # State of formation from jurisdiction field
    jurisdiction = data.get("jurisdiction", "")
    state_of_formation = "Nevada" if "nevada" in jurisdiction.lower() else jurisdiction or None

    source_url = data.get("url") or "https://esos.nv.gov/EntitySearch/OnlineEntitySearch"
    raw_data = json.dumps(data, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='nv' AND source_id=?",
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
             principal_address, principal_city, principal_state,
             principal_zip, principal_country or "US",
             state_of_formation, source_url, raw_data, entity_id],
        )
    else:
        db.execute(
            """INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date, dissolution_date,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                state_of_formation, source_url, raw_data
            ) VALUES ('nv', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [source_id, name, etype, status, formation_date, dissolution_date,
             principal_address, principal_city, principal_state,
             principal_zip, principal_country or "US",
             state_of_formation, source_url, raw_data],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='nv' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Registered agent
    agent_name = (data.get("agent_name") or "").strip()
    if agent_name:
        # Parse agent address if it's a single string
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

    # Officers
    for officer in data.get("officers", []):
        oname = (officer.get("name") or "").strip()
        if not oname:
            continue
        title = (officer.get("title") or "").strip()
        address = (officer.get("address") or "").strip()
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_officers
                (entity_id, name, title, address)
                VALUES (?, ?, ?, ?)""",
                [entity_id, oname, title, address],
            )
        except Exception:
            pass

    # Filing history
    for filing in data.get("filings", []):
        ftype = (filing.get("document_type") or filing.get("filing_type") or "").strip()
        fdate = _parse_date(filing.get("file_date"))
        desc = (filing.get("amendment_type") or "").strip()
        fnum = (filing.get("filing_number") or "").strip()
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, description, raw_data)
                VALUES (?, ?, ?, ?, ?)""",
                [entity_id, ftype, fdate, desc or fnum, json.dumps(filing, default=str)],
            )
        except Exception:
            pass

    # Name history
    for change in data.get("name_history", []):
        prev_name = (change.get("previous_name") or "").strip()
        if not prev_name:
            continue
        cdate = _parse_date(change.get("date"))
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_name_history
                (entity_id, old_name, change_date)
                VALUES (?, ?, ?)""",
                [entity_id, prev_name, cdate],
            )
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a specific NV entity into registry.db."""
    data = _run_helper(["full", str(args.entity_number)])
    if not data or "error" in data:
        print(f"Entity lookup failed for {args.entity_number}")
        if data:
            print(f"  Error: {data.get('error', 'unknown')}")
        return

    # Ensure entity number is present
    if not data.get("entity_number") and not data.get("nv_business_id"):
        data["entity_number"] = str(args.entity_number)

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
        print(f"Ingested: {name} ({args.entity_number}) -> registry ID {entity_id}")
    else:
        print(f"Failed to ingest NV entity {args.entity_number}")


def cmd_ingest_search(args):
    """Search and ingest all matching NV entities."""
    helper_args = ["search", args.query]
    if args.mode:
        helper_args.extend(["--mode", args.mode])

    data = _run_helper(helper_args)
    if not data or "error" in data:
        print(f"Search failed: {data.get('error') if data else 'no response'}")
        return

    # Handle API intercept results
    if data.get("source") == "api_intercept":
        results = data.get("data", [])
        if isinstance(results, dict):
            results = results.get("results", results.get("data", [results]))
    else:
        results = data.get("results", [])

    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} NV entities. Fetching details and ingesting...")
    print("  (Each entity requires a browser session — this will be slow)")

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")
    ingested = 0
    for i, r in enumerate(results):
        eid = (r.get("entity_number") or r.get("nv_business_id") or "").strip()
        name = r.get("entity_name", "?")
        if not eid:
            print(f"  [{i+1}/{len(results)}] SKIP: {name} (no entity number)")
            continue

        # Fetch full detail
        detail = _run_helper(["full", eid])
        if not detail or "error" in detail:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} ({eid})")
            continue

        # Ensure entity number is present
        if not detail.get("entity_number") and not detail.get("nv_business_id"):
            detail["entity_number"] = eid

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

    try:
        db.execute(
            """INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('nv', 'browser', ?, ?)""",
            [ingested, f"ingest-search: {args.query}"],
        )
        db.commit()
    except Exception:
        pass

    log_search(args.query, "nv_sos_registry-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="NV Secretary of State (SilverFlume) corporate registry (requires browser helper)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # warmup
    sub.add_parser("warmup", help="Open browser to solve Incapsula challenge (caches cookies)")

    # probe
    p = sub.add_parser("probe", help="Inspect NV SOS portal structure")
    add_output_args(p)

    # search
    p = sub.add_parser("search", help="Search NV business entities by name")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--mode", choices=["starts", "contains", "exact", "all"],
                   default="starts",
                   help="Search mode (default: starts)")
    p.add_argument("--limit", type=int, default=None, help="Max results to display")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get entity detail by entity number")
    p.add_argument("entity_number", help="NV entity number (e.g., E1234567890-2024)")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db")
    p.add_argument("entity_number", help="NV entity number")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--mode", choices=["starts", "contains", "exact", "all"],
                   default="starts",
                   help="Search mode (default: starts)")
    p.add_argument("--limit", type=int, default=20, help="Max entities to ingest (default: 20)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "warmup": cmd_warmup,
        "probe": cmd_probe,
        "search": cmd_search,
        "entity": cmd_entity,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
