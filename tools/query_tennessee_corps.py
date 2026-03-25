#!/usr/bin/env python3
"""
Tennessee Secretary of State (TNCaB) corporate registry tool.

Queries the TN SOS business entity search at tncab.tnsos.gov.
The site uses Cloudflare Turnstile for bot protection, so a Node.js browser
helper (_tn_browser_helper.js) is required for requests.

Data available: entity name, control number, entity type, status,
formation state, registration date, registered agent, principal address,
mailing address, filing history, standing information.

Control numbers are 9-digit zero-padded IDs (e.g., 001338859).

Usage:
    python tools/query_tennessee_corps.py search "FISHBOWL SPIRITS"
    python tools/query_tennessee_corps.py search "CHESNEY" --active-only
    python tools/query_tennessee_corps.py entity 001338859
    python tools/query_tennessee_corps.py officers 001338859
    python tools/query_tennessee_corps.py ingest 001338859
    python tools/query_tennessee_corps.py ingest-search "FISHBOWL SPIRITS" --limit 10
"""

import argparse
import json
import subprocess
import sys
import re
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


HELPER_PATH = Path(__file__).parent / "_tn_browser_helper.js"

# Entity type mapping: TN raw type -> unified type
TYPE_MAP = {
    "domestic limited liability company (llc)": "llc",
    "foreign limited liability company (llc)": "foreign_llc",
    "domestic for-profit corporation": "corp",
    "domestic corporation": "corp",
    "foreign for-profit corporation": "foreign_corp",
    "foreign corporation": "foreign_corp",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "domestic limited partnership (lp)": "lp",
    "foreign limited partnership (lp)": "foreign_lp",
    "domestic limited liability partnership (llp)": "llp",
    "foreign limited liability partnership (llp)": "foreign_llp",
    "domestic general partnership": "gp",
    "foreign general partnership": "foreign_gp",
    "domestic professional corporation": "professional_corp",
    "foreign professional corporation": "foreign_professional_corp",
    "domestic professional llc": "professional_llc",
    "foreign professional llc": "foreign_professional_llc",
    "domestic business trust": "trust",
    "foreign business trust": "foreign_trust",
    "cooperative": "coop",
}

# Status mapping
STATUS_MAP = {
    "active": "active",
    "inactive": "inactive",
    "dissolved": "dissolved",
    "revoked": "revoked",
    "cancelled": "cancelled",
    "expired": "expired",
    "merged": "dissolved",
    "converted": "dissolved",
    "withdrawn": "withdrawn",
}


def _run_helper(args_list, timeout=90):
    """Run the TN browser helper and return parsed JSON."""
    if not HELPER_PATH.exists():
        print(f"ERROR: Browser helper not found at {HELPER_PATH}", file=sys.stderr)
        print("  Ensure _tn_browser_helper.js is in tools/", file=sys.stderr)
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
    # MM/DD/YYYY HH:MM:SS AM/PM
    match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_str)
    if match:
        return f"{match.group(3)}-{match.group(1).zfill(2)}-{match.group(2).zfill(2)}"
    # ISO format 2022-08-03T18:50:10.887Z
    match = re.match(r'(\d{4})-(\d{2})-(\d{2})', date_str)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _normalize_type(raw_type):
    """Normalize TN entity type to unified type."""
    if not raw_type:
        return None
    key = raw_type.strip().lower()
    return TYPE_MAP.get(key, key.replace(" ", "_").replace("(", "").replace(")", ""))


def _normalize_status(raw_status):
    """Normalize TN status to unified status."""
    if not raw_status:
        return None
    key = raw_status.strip().lower()
    return STATUS_MAP.get(key, key.replace(" ", "_"))


# ══════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search TN business entities by name."""
    helper_args = ["search", args.query]
    if args.active_only:
        helper_args.append("--active-only")

    data = _run_helper(helper_args)
    if not data:
        print("Search failed (browser helper returned no data)")
        return

    if "error" in data:
        print(f"Search error: {data['error']}")
        return

    results = data.get("data", [])
    total = data.get("total", len(results))

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    log_search(args.query, "tn_sos_registry", len(results))

    out_data = {"count": total, "results": results}
    if write_output(out_data, args,
                    summary=f"TN search '{args.query}' ({total} total, {len(results)} returned)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(out_data, indent=2, default=str))
        return

    active_label = " (active only)" if args.active_only else ""
    print(f"Found {total} TN entities matching '{args.query}'{active_label}")
    print()
    for r in results:
        name = r.get("DisplayName", "?")
        control_no = r.get("FileNumber", "?")
        entity_type = r.get("EntityType", "")
        status = r.get("Status", "")
        state = r.get("StateName", "")
        reg_date = r.get("RegistrationDate", "")
        if reg_date:
            reg_date = _parse_date(reg_date) or reg_date[:10]
        other_names = r.get("OtherNames", "")

        print(f"  {control_no} | {name}")
        parts = []
        if entity_type:
            parts.append(f"Type: {entity_type}")
        if status:
            parts.append(f"Status: {status}")
        if state:
            parts.append(f"Formed in: {state}")
        if reg_date:
            parts.append(f"Registered: {reg_date}")
        if parts:
            print(f"         {' | '.join(parts)}")
        if other_names:
            print(f"         Also known as: {other_names}")
    print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL
# ══════════════════════════════════════════════════════════

def cmd_entity(args):
    """Get entity detail by control number."""
    data = _run_helper(["entity", str(args.control_number)])
    if not data:
        print(f"Entity lookup failed for {args.control_number}")
        return

    if "error" in data:
        print(f"Error: {data['error']}")
        return

    if write_output(data, args, summary=f"TN entity {args.control_number}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    _print_entity_detail(data)


def _print_entity_detail(data):
    """Pretty-print entity detail."""
    name = data.get("entity_name", "?")
    fields = data.get("fields", {})
    addresses = data.get("addresses", {})
    search_data = data.get("search_data", {})

    print(f"\n  [TN] {name}")

    # Key fields
    for key in ["Control Number", "Entity Type", "Status", "Formed in",
                "Initial Filing Date", "Term of Duration", "Managed By",
                "Series LLC", "Number of Members", "Fiscal Ending Month",
                "AR Due Date", "Obligated Member Entity"]:
        if key in fields:
            print(f"    {key}: {fields[key]}")

    # Standing info
    standing_keys = ["AR Standing", "RA Standing", "Other Standing", "Revenue Standing"]
    standing_parts = []
    for key in standing_keys:
        if key in fields:
            standing_parts.append(f"{key}: {fields[key]}")
    if standing_parts:
        print(f"\n    Standing: {' | '.join(standing_parts)}")

    # Addresses
    for section_name in ["Registered Agent", "Principal Office Address", "Mailing Address"]:
        if section_name in addresses:
            lines = addresses[section_name]
            print(f"\n    {section_name}:")
            for line in lines:
                print(f"      {line}")

    # Search data extras
    if search_data:
        if search_data.get("OtherNames"):
            print(f"\n    Other Names: {search_data['OtherNames']}")

    # History
    history = data.get("history", [])
    if history:
        print(f"\n    Filing History ({len(history)}):")
        for h in history:
            htype = h.get("type", "?")
            hdate = h.get("date", "?")
            tracking = h.get("tracking_number", "")
            changes = h.get("changes", "")
            print(f"      {hdate} | {htype}")
            if tracking:
                print(f"        Tracking: {tracking}")
            if changes:
                # Truncate long change descriptions
                if len(changes) > 100:
                    changes = changes[:100] + "..."
                print(f"        Changes: {changes}")

    print()


# ══════════════════════════════════════════════════════════
# OFFICERS
# ══════════════════════════════════════════════════════════

def cmd_officers(args):
    """Get officers/managers for an entity by control number.

    Note: TNCaB does not expose officer names in the public search detail page.
    Officers are listed in filing history change records (e.g., "Officers Changed")
    but individual names are not disclosed. The registered agent and addresses
    are available via the 'entity' command.
    """
    data = _run_helper(["entity", str(args.control_number)])
    if not data:
        print(f"Entity lookup failed for {args.control_number}")
        return

    if "error" in data:
        print(f"Error: {data['error']}")
        return

    name = data.get("entity_name", "?")
    fields = data.get("fields", {})
    addresses = data.get("addresses", {})
    history = data.get("history", [])

    # Collect what officer-related info we can find
    officer_info = {
        "entity_name": name,
        "control_number": args.control_number,
        "managed_by": fields.get("Managed By", "N/A"),
        "number_of_members": fields.get("Number of Members", "N/A"),
    }

    # Registered agent is the closest to an "officer" that TN provides publicly
    if "Registered Agent" in addresses:
        agent_lines = addresses["Registered Agent"]
        officer_info["registered_agent"] = {
            "name": agent_lines[0] if agent_lines else "N/A",
            "address": ", ".join(agent_lines[1:]) if len(agent_lines) > 1 else "N/A",
        }

    # Extract officer change events from history
    officer_changes = []
    for h in history:
        changes = h.get("changes", "")
        if "officer" in changes.lower():
            officer_changes.append({
                "date": h.get("date", "?"),
                "type": h.get("type", "?"),
                "changes": changes,
            })
    officer_info["officer_change_events"] = officer_changes

    if write_output(officer_info, args, summary=f"TN officers for {args.control_number}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(officer_info, indent=2, default=str))
        return

    print(f"\n  [TN] Officers/Agent Info: {name}")
    print(f"    Control Number: {args.control_number}")
    print(f"    Managed By: {officer_info['managed_by']}")
    print(f"    Number of Members: {officer_info['number_of_members']}")

    ra = officer_info.get("registered_agent")
    if ra:
        print(f"\n    Registered Agent: {ra['name']}")
        if ra["address"] != "N/A":
            print(f"      Address: {ra['address']}")

    if officer_changes:
        print(f"\n    Officer Change Events ({len(officer_changes)}):")
        for oc in officer_changes:
            print(f"      {oc['date']} | {oc['type']}")
            changes = oc.get("changes", "")
            if changes and len(changes) > 120:
                changes = changes[:120] + "..."
            if changes:
                print(f"        {changes}")
    else:
        print("\n    No officer change events found in filing history.")

    print("\n    Note: TNCaB does not expose individual officer names in public search.")
    print("    For officer details, request the entity's annual report from the TN SOS office.")
    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

def _ingest_entity(db, data):
    """Ingest a TN entity into registry.db. Returns entity_id or None."""
    search_data = data.get("search_data", {})
    fields = data.get("fields", {})
    addresses = data.get("addresses", {})

    source_id = fields.get("Control Number") or search_data.get("FileNumber", "")
    if not source_id:
        return None

    name = data.get("entity_name") or search_data.get("DisplayName", "?")
    etype = _normalize_type(fields.get("Entity Type") or search_data.get("EntityType"))
    status = _normalize_status(fields.get("Status") or search_data.get("Status"))

    formation_date = _parse_date(fields.get("Initial Filing Date"))
    if not formation_date and search_data.get("RegistrationDate"):
        formation_date = _parse_date(search_data["RegistrationDate"])

    state_of_formation = fields.get("Formed in") or search_data.get("StateName", "")

    # Principal address
    principal_lines = addresses.get("Principal Office Address", [])
    principal_address = principal_lines[0] if principal_lines else None
    principal_city = None
    principal_state = None
    principal_zip = None
    if len(principal_lines) > 1:
        city_state_zip = principal_lines[-1]
        csz_match = re.match(r'(.+?),\s*(\w{2})\s+(\d{5}(?:-\d{4})?)', city_state_zip)
        if csz_match:
            principal_city = csz_match.group(1).strip()
            principal_state = csz_match.group(2).strip()
            principal_zip = csz_match.group(3).strip()
            if len(principal_lines) > 2:
                principal_address = ", ".join(principal_lines[:-1])

    # Mailing address
    mailing_lines = addresses.get("Mailing Address", [])
    mailing_address = ", ".join(mailing_lines) if mailing_lines else None

    source_url = f"https://tncab.tnsos.gov/business-entity-search"
    raw_data = json.dumps(data, indent=2, default=str)

    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='tn' AND source_id=?",
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
             principal_zip, "US",
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
            ) VALUES ('tn', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?)""",
            [source_id, name, etype, status, formation_date,
             principal_address, principal_city, principal_state,
             principal_zip,
             state_of_formation, source_url, raw_data],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='tn' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Registered agent
    agent_lines = addresses.get("Registered Agent", [])
    if agent_lines:
        agent_name = agent_lines[0]
        agent_addr = ", ".join(agent_lines[1:]) if len(agent_lines) > 1 else ""
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address)
                VALUES (?, ?, ?)""",
                [entity_id, agent_name, agent_addr],
            )
        except Exception:
            pass

    # Filing history
    for h in data.get("history", []):
        ftype = (h.get("type") or "").strip()
        fdate = _parse_date(h.get("date"))
        tracking = (h.get("tracking_number") or "").strip()
        changes = (h.get("changes") or "").strip()
        try:
            db.execute(
                """INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, description, raw_data)
                VALUES (?, ?, ?, ?, ?)""",
                [entity_id, ftype, fdate, changes or tracking,
                 json.dumps(h, default=str)],
            )
        except Exception:
            pass

    return entity_id


def cmd_ingest(args):
    """Ingest a specific TN entity into registry.db."""
    data = _run_helper(["entity", str(args.control_number)])
    if not data or "error" in data:
        print(f"Entity lookup failed for {args.control_number}")
        if data:
            print(f"  Error: {data.get('error', 'unknown')}")
        return

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
        print(f"Ingested: {name} (TN #{args.control_number}) -> registry ID {entity_id}")
    else:
        print(f"Failed to ingest TN entity {args.control_number}")


def cmd_ingest_search(args):
    """Search and ingest all matching TN entities."""
    helper_args = ["search", args.query]
    if args.active_only:
        helper_args.append("--active-only")

    data = _run_helper(helper_args)
    if not data or "error" in data:
        print(f"Search failed: {data.get('error') if data else 'no response'}")
        return

    results = data.get("data", [])
    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    if args.limit and len(results) > args.limit:
        results = results[:args.limit]

    print(f"Found {len(results)} TN entities. Fetching details and ingesting...")
    print("  (Each entity requires a browser session — this will be slow)")

    db = get_db()
    db.execute("PRAGMA busy_timeout = 30000")
    ingested = 0
    for i, r in enumerate(results):
        control_no = (r.get("FileNumber") or "").strip()
        name = r.get("DisplayName", "?")
        if not control_no:
            print(f"  [{i + 1}/{len(results)}] SKIP: {name} (no control number)")
            continue

        # Fetch full detail
        detail = _run_helper(["entity", control_no])
        if not detail or "error" in detail:
            print(f"  [{i + 1}/{len(results)}] FAILED: {name} ({control_no})")
            continue

        entity_id = _ingest_entity(db, detail)
        if entity_id:
            ingested += 1
            print(f"  [{i + 1}/{len(results)}] {name} ({control_no}) -> registry ID {entity_id}")
        else:
            print(f"  [{i + 1}/{len(results)}] FAILED: {name} ({control_no})")

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
            VALUES ('tn', 'browser', ?, ?)""",
            [ingested, f"ingest-search: {args.query}"],
        )
        db.commit()
    except Exception:
        pass

    log_search(args.query, "tn_sos_registry-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="TN Secretary of State (TNCaB) corporate registry (requires browser helper)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search TN business entities by name")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--active-only", action="store_true", dest="active_only",
                   help="Show only active entities")
    p.add_argument("--limit", type=int, default=None, help="Max results to display")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get entity detail by control number")
    p.add_argument("control_number", help="TN SOS control number (e.g., 001338859)")
    add_output_args(p)

    # officers
    p = sub.add_parser("officers", help="Get officers/agent info for an entity")
    p.add_argument("control_number", help="TN SOS control number")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest entity into registry.db")
    p.add_argument("control_number", help="TN SOS control number")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--active-only", action="store_true", dest="active_only",
                   help="Show only active entities")
    p.add_argument("--limit", type=int, default=20,
                   help="Max entities to ingest (default: 20)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "officers": cmd_officers,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
