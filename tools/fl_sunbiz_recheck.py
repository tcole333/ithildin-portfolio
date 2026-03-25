#!/usr/bin/env python3
"""
FL SunBiz backlog re-verification tool.

After the FL SunBiz entity-name fix (Feb 15, 2026), re-searches entity names
that previously returned false negatives due to ingesting corprindata instead
of cordata files.

Usage:
    python tools/fl_sunbiz_recheck.py tier1          # Re-verify Finding #508 entities
    python tools/fl_sunbiz_recheck.py tier2          # Re-verify 12 negative-result findings
    python tools/fl_sunbiz_recheck.py tier3          # Re-search open leads
    python tools/fl_sunbiz_recheck.py tier4          # Spot-check completed leads
    python tools/fl_sunbiz_recheck.py search "NAME"  # Single entity search
    python tools/fl_sunbiz_recheck.py all            # Run all tiers
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DB_PATH = Path(__file__).parent.parent / "registry.db"

# ═══════════════════════════════════════════════════════
# TIER 1: Finding #508 entities marked as "absent"
# ═══════════════════════════════════════════════════════
TIER1_ENTITIES = [
    {"name": "LSJE", "full": "LSJE LLC", "finding_id": 508},
    {"name": "GRATITUDE AMERICA", "full": "Gratitude America Ltd", "finding_id": 508},
    {"name": "ENHANCED EDUCATION", "full": "Enhanced Education NY/USVI", "finding_id": 508},
    {"name": "FLORIDA SCIENCE FOUNDATION", "full": "Florida Science Foundation", "finding_id": 508},
    {"name": "BUTTERFLY TRUST", "full": "Butterfly Trust", "finding_id": 508},
    {"name": "SOUTHERN TRUST", "full": "Southern Trust Company", "finding_id": 508},
    {"name": "NAUTILUS", "full": "Nautilus Inc", "finding_id": 508},
    {"name": "LAUREL", "full": "Laurel Inc", "finding_id": 508},
    {"name": "MAPLE", "full": "Maple Inc", "finding_id": 508},
    {"name": "CYPRESS", "full": "Cypress Inc", "finding_id": 508},
    {"name": "HBRK", "full": "HBRK Associates", "finding_id": 508},
    {"name": "DKIP", "full": "DKIP LLC", "finding_id": 508},
]

# ═══════════════════════════════════════════════════════
# TIER 1B: Other direct FL findings to re-verify
# ═══════════════════════════════════════════════════════
TIER1B_ENTITIES = [
    {"name": "NEPTUNE CORP", "full": "Neptune Corp", "finding_id": 720},
    {"name": "SIGNATURE TITLE GROUP", "full": "Signature Title Group LLC", "finding_id": 726},
    {"name": "JR WATERSPORTS", "full": "JR Watersports Inc", "finding_id": 727},
    {"name": "ALBA FIBER SYSTEMS", "full": "Alba Fiber Systems Inc", "finding_id": 755},
]

# ═══════════════════════════════════════════════════════
# TIER 2: Negative-result findings referencing FL
# ═══════════════════════════════════════════════════════
TIER2_SEARCHES = [
    {"finding_id": 799, "target": "Ron Soffer", "searches": ["SOFFER", "RON SOFFER"]},
    {"finding_id": 831, "target": "Barkmere Group", "searches": ["BARKMERE"]},
    {"finding_id": 1681, "target": "Honeycomb Asset Mgmt", "searches": ["HONEYCOMB"]},
    {"finding_id": 1890, "target": "Bill Siegel", "searches": ["SIEGEL", "BILL SIEGEL"]},
    {"finding_id": 1955, "target": "Herbert J. Siegel", "searches": ["HERBERT SIEGEL", "SIEGEL"]},
    {"finding_id": 1973, "target": "Marvin Davis", "searches": ["DAVIS PETROLEUM"]},
    {"finding_id": 2171, "target": "Robbie Karp", "searches": ["ROBBIE KARP", "KARP"]},
    {"finding_id": 2295, "target": "Ivan Fisher", "searches": ["FISHER & SOFFER", "FISHER GROUP", "IVAN FISHER"]},
    {"finding_id": 2321, "target": "Jack Abramoff", "searches": ["ABRAMOFF"]},
    {"finding_id": 1736, "target": "Darren Indyke", "searches": ["INDYKE"]},
    {"finding_id": 2037, "target": "Jeffrey Schantz", "searches": ["SCHANTZ", "NY STRATEGY GROUP", "STRATEGY GROUP"]},
    {"finding_id": 2168, "target": "Robbie Karp", "searches": ["KARP RANDEL"]},
]

# ═══════════════════════════════════════════════════════
# TIER 3: Open leads requiring FL search
# ═══════════════════════════════════════════════════════
TIER3_LEADS = [
    {"lead_id": 360, "searches": ["FLORIDA SCIENCE FOUNDATION"]},
    {"lead_id": 631, "searches": ["COUNTY ROAD PROPERTY"]},
    {"lead_id": 653, "searches": ["HAZE TRUST", "FTC", "SFL"]},
    {"lead_id": 738, "searches": ["MONTAVON", "KELLERHALS"]},
    {"lead_id": 745, "searches": ["FLORIDA SCIENCE FOUNDATION"]},
    {"lead_id": 749, "searches": ["DUBIN"]},
    {"lead_id": 750, "searches": ["MONTAVON", "KELLERHALS CARRARD"]},
    {"lead_id": 751, "searches": ["INDYKE"]},
]

# ═══════════════════════════════════════════════════════
# TIER 4: Completed leads to spot-check
# ═══════════════════════════════════════════════════════
TIER4_LEADS = [
    {"lead_id": 735, "searches": ["EPSTEIN", "INDYKE", "KAHN"]},
    {"lead_id": 736, "searches": ["FINANCIAL STRATEGY GROUP", "EPSTEIN"]},
    {"lead_id": 741, "searches": ["DUBIN"]},
    {"lead_id": 747, "searches": ["FARKAS", "FLORIDA SCIENCE FOUNDATION"]},
]


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def search_fl_entities(db, query, limit=25):
    """Search FL entities by name (LIKE match)."""
    rows = db.execute("""
        SELECT id, source_id, entity_name, entity_type, status,
               formation_date, dissolution_date, ein,
               principal_address, principal_city, principal_state, principal_zip
        FROM registry_entities
        WHERE source_jurisdiction = 'fl' AND entity_name LIKE ?
        ORDER BY entity_name
        LIMIT ?
    """, [f"%{query}%", limit]).fetchall()
    return [dict(r) for r in rows]


def format_entity(e):
    """Format entity for display."""
    addr_parts = [e.get("principal_address"), e.get("principal_city"),
                  e.get("principal_state"), e.get("principal_zip")]
    addr = ", ".join(p for p in addr_parts if p)
    status = e.get("status", "unknown")
    etype = e.get("entity_type", "unknown")
    formed = e.get("formation_date", "?")
    ein = f" EIN:{e['ein']}" if e.get("ein") else ""
    return f"  {e['entity_name']} ({etype}, {status}) | {e['source_id']} | Formed: {formed}{ein}\n    {addr}"


def run_tier1(db, output_file=None):
    """Re-verify Finding #508 entities."""
    print("=" * 70)
    print("TIER 1: Re-verifying Finding #508 — 12 'absent' entities")
    print("=" * 70)

    results = []
    found_count = 0

    for item in TIER1_ENTITIES:
        matches = search_fl_entities(db, item["name"])
        is_found = len(matches) > 0
        if is_found:
            found_count += 1

        result = {
            "search_term": item["name"],
            "original_claim": f"{item['full']} absent from FL SunBiz",
            "finding_id": item["finding_id"],
            "now_found": is_found,
            "match_count": len(matches),
            "matches": matches,
        }
        results.append(result)

        status = "FOUND" if is_found else "still absent"
        print(f"\n[{status}] {item['full']} (search: '{item['name']}')")
        if matches:
            # Filter to most relevant (skip obvious non-matches)
            for m in matches[:10]:
                print(format_entity(m))
        else:
            print("  No matches")

    print(f"\n--- Tier 1 Summary: {found_count}/{len(TIER1_ENTITIES)} entities now found ---")

    # Run Tier 1B
    print("\n" + "=" * 70)
    print("TIER 1B: Re-verifying Findings #720, #726, #727, #755")
    print("=" * 70)

    for item in TIER1B_ENTITIES:
        matches = search_fl_entities(db, item["name"])
        is_found = len(matches) > 0
        if is_found:
            found_count += 1

        result = {
            "search_term": item["name"],
            "original_claim": f"{item['full']} FL search",
            "finding_id": item["finding_id"],
            "now_found": is_found,
            "match_count": len(matches),
            "matches": matches,
        }
        results.append(result)

        status = "FOUND" if is_found else "still absent"
        print(f"\n[{status}] {item['full']} (Finding #{item['finding_id']}, search: '{item['name']}')")
        if matches:
            for m in matches[:10]:
                print(format_entity(m))
        else:
            print("  No matches")

    if output_file:
        write_output({"tier": "1+1B", "results": results, "timestamp": datetime.now().isoformat()}, output_file)

    return results


def run_tier2(db, output_file=None):
    """Re-verify negative-result findings."""
    print("\n" + "=" * 70)
    print("TIER 2: Re-verifying 12 negative-result findings")
    print("=" * 70)

    results = []

    for item in TIER2_SEARCHES:
        all_matches = {}
        for term in item["searches"]:
            matches = search_fl_entities(db, term)
            if matches:
                all_matches[term] = matches

        has_results = len(all_matches) > 0
        result = {
            "finding_id": item["finding_id"],
            "target": item["target"],
            "search_terms": item["searches"],
            "has_new_results": has_results,
            "matches": {k: v for k, v in all_matches.items()},
        }
        results.append(result)

        status = "NEW RESULTS" if has_results else "confirmed zero"
        print(f"\n[{status}] Finding #{item['finding_id']} — {item['target']}")
        if all_matches:
            for term, matches in all_matches.items():
                relevant = [m for m in matches if _is_possibly_relevant(m, item["target"])]
                print(f"  Search '{term}': {len(matches)} total, {len(relevant)} possibly relevant")
                for m in relevant[:5]:
                    print(format_entity(m))
        else:
            for term in item["searches"]:
                print(f"  Search '{term}': 0 results")

    if output_file:
        write_output({"tier": "2", "results": results, "timestamp": datetime.now().isoformat()}, output_file)

    return results


def _is_possibly_relevant(entity, target_name):
    """Heuristic: is this FL entity possibly relevant to the investigation target?"""
    name = entity.get("entity_name", "").upper()
    target_parts = target_name.upper().split()

    # Check if any part of the target name appears in entity name
    for part in target_parts:
        if len(part) > 3 and part in name:
            return True

    # Check known investigative addresses
    addr = (entity.get("principal_address") or "").upper()
    known_addresses = [
        "358 EL BRILLO", "9 E 71", "457 MADISON", "301 E 66",
        "139 N COUNTY", "PALM BEACH", "AVENTURA",
    ]
    for ka in known_addresses:
        if ka in addr:
            return True

    return False


def run_tier3(db, output_file=None):
    """Re-search open leads."""
    print("\n" + "=" * 70)
    print("TIER 3: Re-searching 8 open leads")
    print("=" * 70)

    results = []

    for item in TIER3_LEADS:
        all_matches = {}
        for term in item["searches"]:
            matches = search_fl_entities(db, term)
            if matches:
                all_matches[term] = matches

        has_results = len(all_matches) > 0
        result = {
            "lead_id": item["lead_id"],
            "search_terms": item["searches"],
            "has_results": has_results,
            "matches": all_matches,
        }
        results.append(result)

        status = "RESULTS" if has_results else "zero"
        print(f"\n[{status}] Lead #{item['lead_id']}")
        for term in item["searches"]:
            matches = all_matches.get(term, [])
            print(f"  Search '{term}': {len(matches)} results")
            for m in matches[:5]:
                print(format_entity(m))

    if output_file:
        write_output({"tier": "3", "results": results, "timestamp": datetime.now().isoformat()}, output_file)

    return results


def run_tier4(db, output_file=None):
    """Spot-check completed leads."""
    print("\n" + "=" * 70)
    print("TIER 4: Spot-checking 4 completed leads")
    print("=" * 70)

    results = []

    for item in TIER4_LEADS:
        all_matches = {}
        for term in item["searches"]:
            matches = search_fl_entities(db, term)
            if matches:
                all_matches[term] = matches

        has_results = len(all_matches) > 0
        result = {
            "lead_id": item["lead_id"],
            "search_terms": item["searches"],
            "has_results": has_results,
            "match_counts": {term: len(all_matches.get(term, [])) for term in item["searches"]},
        }
        results.append(result)

        print(f"\nLead #{item['lead_id']}:")
        for term in item["searches"]:
            matches = all_matches.get(term, [])
            print(f"  '{term}': {len(matches)} results")

    if output_file:
        write_output({"tier": "4", "results": results, "timestamp": datetime.now().isoformat()}, output_file)

    return results


def cmd_search_single(args):
    """Search for a single entity name."""
    db = get_db()
    matches = search_fl_entities(db, args.query, limit=args.limit)
    print(f"Found {len(matches)} FL entities matching '{args.query}'")
    for m in matches:
        print(format_entity(m))
    if args.output:
        write_output({"query": args.query, "results": matches}, args.output)


def main():
    parser = argparse.ArgumentParser(description="FL SunBiz backlog re-verification")
    sub = parser.add_subparsers(dest="command")

    t1 = sub.add_parser("tier1", help="Re-verify Finding #508 + #720/#726/#727/#755")
    add_output_args(t1)

    t2 = sub.add_parser("tier2", help="Re-verify 12 negative-result findings")
    add_output_args(t2)

    t3 = sub.add_parser("tier3", help="Re-search 8 open leads")
    add_output_args(t3)

    t4 = sub.add_parser("tier4", help="Spot-check 4 completed leads")
    add_output_args(t4)

    all_p = sub.add_parser("all", help="Run all tiers")
    add_output_args(all_p)

    s = sub.add_parser("search", help="Search single entity name")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=25)
    add_output_args(s)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        cmd_search_single(args)
        return

    db = get_db()
    output_file = getattr(args, "output", None)

    if args.command in ("tier1", "all"):
        run_tier1(db, output_file)
    if args.command in ("tier2", "all"):
        run_tier2(db, output_file)
    if args.command in ("tier3", "all"):
        run_tier3(db, output_file)
    if args.command in ("tier4", "all"):
        run_tier4(db, output_file)


if __name__ == "__main__":
    main()
