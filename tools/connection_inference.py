#!/usr/bin/env python3
"""
Rule-based connection inference engine for OSINT investigations.

Derives new connections from existing entity data using structural rules:
shared addresses, shared officers, corporate chains, co-occurrence, temporal proximity.

Outputs candidates as pending_triage leads — human/agent review required.

Part of investigation.db.

Usage:
    python tools/connection_inference.py scan [--dry-run] [--limit 50]
    python tools/connection_inference.py rules
    python tools/connection_inference.py apply --rule shared_address [--dry-run] [--limit 50]
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "investigation.db"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db


# ── Inference Rules ────────────────────────────────────────

RULES = {
    "shared_address": {
        "name": "Shared Address",
        "description": "Entities at the same normalized address → shares_address connection",
    },
    "shared_officer": {
        "name": "Shared Officer",
        "description": "Entities sharing an officer name → shares_officer connection",
    },
    "corporate_chain": {
        "name": "Corporate Chain",
        "description": "If A owns/controls B and B owns/controls C → inferred A↔C connection",
    },
    "co_occurrence": {
        "name": "Co-occurrence",
        "description": "Persons appearing in 3+ findings together but no connection → candidate connection",
    },
    "temporal_proximity": {
        "name": "Temporal Proximity",
        "description": "Entities created within 30 days at same address → candidate connection",
    },
}


def _normalize_address(addr):
    """Normalize address for comparison."""
    import re
    addr = addr.strip().lower()
    addr = re.sub(r'\b(suite|ste|apt|fl|floor|unit|#)\b.*', '', addr, flags=re.IGNORECASE)
    addr = re.sub(r'\b(llc|inc|corp)\b', '', addr)
    addr = addr.split(",")[0].strip()
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr[:50]


def _connection_exists(db, a, b, rel_type=None):
    """Check if a connection already exists between two names."""
    query = """
        SELECT id FROM connections
        WHERE (person_a = ? AND person_b = ?) OR (person_a = ? AND person_b = ?)
    """
    params = [a, b, b, a]
    if rel_type:
        query += " AND relationship_type = ?"
        params.append(rel_type)
    return db.execute(query, params).fetchone() is not None


def _lead_exists_for_inference(db, entity_a, entity_b, rule_id):
    """Check if an inference lead already exists for this pair."""
    pattern = f"Inferred ({rule_id}): %{entity_a[:20]}%{entity_b[:20]}%"
    row = db.execute("SELECT id FROM leads WHERE title LIKE ?", (pattern,)).fetchone()
    return row is not None


def rule_shared_address(db, dry_run=False, limit=50):
    """Find entities that share a normalized address."""
    rows = db.execute("""
        SELECT ea.entity_id, ea.address, e.name as entity_name
        FROM entity_addresses ea
        JOIN entities e ON ea.entity_id = e.id
        WHERE ea.address IS NOT NULL AND LENGTH(ea.address) > 10
    """).fetchall()

    # Group by normalized address
    addr_groups = defaultdict(list)
    for r in rows:
        norm = _normalize_address(r["address"])
        if len(norm) >= 8:
            addr_groups[norm].append({
                "entity_id": r["entity_id"],
                "entity_name": r["entity_name"],
                "address": r["address"],
            })

    candidates = []
    for addr, entities in addr_groups.items():
        if len(entities) < 2:
            continue
        # Generate pairs
        for i, a in enumerate(entities):
            for b in entities[i + 1:]:
                if a["entity_id"] == b["entity_id"]:
                    continue
                if _connection_exists(db, a["entity_name"], b["entity_name"]):
                    continue
                candidates.append({
                    "rule": "shared_address",
                    "entity_a": a["entity_name"],
                    "entity_b": b["entity_name"],
                    "evidence": f"Shared address: {addr}",
                    "confidence": "medium",
                    "entity_a_id": a["entity_id"],
                    "entity_b_id": b["entity_id"],
                })
                if len(candidates) >= limit:
                    return candidates

    return candidates


def rule_shared_officer(db, dry_run=False, limit=50):
    """Find entities that share an officer."""
    from rapidfuzz import fuzz

    rows = db.execute("""
        SELECT er.entity_id, er.person_name, e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
        WHERE er.person_name IS NOT NULL
    """).fetchall()

    # Group by normalized person name
    person_entities = defaultdict(list)
    for r in rows:
        norm = r["person_name"].strip().lower()
        person_entities[norm].append({
            "entity_id": r["entity_id"],
            "entity_name": r["entity_name"],
            "person_name": r["person_name"],
        })

    candidates = []
    for person, entities in person_entities.items():
        if len(entities) < 2:
            continue
        for i, a in enumerate(entities):
            for b in entities[i + 1:]:
                if a["entity_id"] == b["entity_id"]:
                    continue
                if _connection_exists(db, a["entity_name"], b["entity_name"]):
                    continue
                candidates.append({
                    "rule": "shared_officer",
                    "entity_a": a["entity_name"],
                    "entity_b": b["entity_name"],
                    "evidence": f"Shared officer: {a['person_name']}",
                    "confidence": "high",
                    "entity_a_id": a["entity_id"],
                    "entity_b_id": b["entity_id"],
                })
                if len(candidates) >= limit:
                    return candidates

    return candidates


def rule_corporate_chain(db, dry_run=False, limit=50):
    """If A→B and B→C via entity_relations, infer A↔C."""
    rows = db.execute("""
        SELECT er.entity_a_id, er.entity_b_id, er.relation_type,
               ea.name as name_a, eb.name as name_b
        FROM entity_relations er
        JOIN entities ea ON er.entity_a_id = ea.id
        JOIN entities eb ON er.entity_b_id = eb.id
        WHERE er.relation_type IN ('owns', 'controls', 'subsidiary', 'parent')
    """).fetchall()

    # Build directed graph of ownership
    owns = defaultdict(set)  # entity_id -> set of owned entity_ids
    id_to_name = {}
    for r in rows:
        owns[r["entity_a_id"]].add(r["entity_b_id"])
        id_to_name[r["entity_a_id"]] = r["name_a"]
        id_to_name[r["entity_b_id"]] = r["name_b"]

    candidates = []
    for a_id, b_ids in owns.items():
        for b_id in b_ids:
            for c_id in owns.get(b_id, set()):
                if c_id == a_id:
                    continue
                a_name = id_to_name.get(a_id, str(a_id))
                c_name = id_to_name.get(c_id, str(c_id))
                b_name = id_to_name.get(b_id, str(b_id))
                if _connection_exists(db, a_name, c_name):
                    continue
                candidates.append({
                    "rule": "corporate_chain",
                    "entity_a": a_name,
                    "entity_b": c_name,
                    "evidence": f"Chain: {a_name} → {b_name} → {c_name}",
                    "confidence": "medium",
                    "entity_a_id": a_id,
                    "entity_b_id": c_id,
                })
                if len(candidates) >= limit:
                    return candidates

    return candidates


def rule_co_occurrence(db, dry_run=False, limit=50):
    """Find persons appearing in 3+ findings together but with no recorded connection."""
    # Get all findings with target_name
    rows = db.execute("""
        SELECT id, target_name FROM findings
        WHERE target_name IS NOT NULL
    """).fetchall()

    # Group finding IDs by target name
    person_findings = defaultdict(set)
    for r in rows:
        person_findings[r["target_name"]].add(r["id"])

    # Find co-occurring pairs (share 3+ finding IDs via cross-reference in connections)
    # Actually: count how many findings mention both persons
    persons = list(person_findings.keys())
    candidates = []

    for i, a in enumerate(persons):
        for b in persons[i + 1:]:
            # Count findings where both appear (by checking if a finding references both)
            # Simpler: count findings where target is A that also reference B in detail/summary
            shared = 0
            for fid in person_findings[a]:
                if fid in person_findings[b]:
                    shared += 1
            # Also check if B appears as target in findings whose detail mentions A
            if shared < 3:
                # Check detail/summary co-mention
                rows_a = db.execute("""
                    SELECT COUNT(*) as n FROM findings
                    WHERE target_name = ? AND (detail LIKE ? OR summary LIKE ?)
                """, (a, f"%{b}%", f"%{b}%")).fetchone()
                rows_b = db.execute("""
                    SELECT COUNT(*) as n FROM findings
                    WHERE target_name = ? AND (detail LIKE ? OR summary LIKE ?)
                """, (b, f"%{a}%", f"%{a}%")).fetchone()
                shared += rows_a["n"] + rows_b["n"]

            if shared >= 3 and not _connection_exists(db, a, b):
                candidates.append({
                    "rule": "co_occurrence",
                    "entity_a": a,
                    "entity_b": b,
                    "evidence": f"Co-occur in {shared} findings",
                    "confidence": "low",
                })
                if len(candidates) >= limit:
                    return candidates

    return candidates


def rule_temporal_proximity(db, dry_run=False, limit=50):
    """Entities created within 30 days at same address."""
    rows = db.execute("""
        SELECT e.id, e.name, e.date_formed, ea.address
        FROM entities e
        JOIN entity_addresses ea ON e.id = ea.entity_id
        WHERE e.date_formed IS NOT NULL AND ea.address IS NOT NULL
    """).fetchall()

    # Group by normalized address
    addr_entities = defaultdict(list)
    for r in rows:
        norm = _normalize_address(r["address"])
        if len(norm) >= 8:
            try:
                formed = datetime.strptime(r["date_formed"][:10], "%Y-%m-%d")
                addr_entities[norm].append({
                    "id": r["id"],
                    "name": r["name"],
                    "formed": formed,
                    "address": r["address"],
                })
            except (ValueError, TypeError):
                continue

    candidates = []
    for addr, entities in addr_entities.items():
        if len(entities) < 2:
            continue
        entities.sort(key=lambda e: e["formed"])
        for i, a in enumerate(entities):
            for b in entities[i + 1:]:
                delta = abs((b["formed"] - a["formed"]).days)
                if delta <= 30 and not _connection_exists(db, a["name"], b["name"]):
                    candidates.append({
                        "rule": "temporal_proximity",
                        "entity_a": a["name"],
                        "entity_b": b["name"],
                        "evidence": f"Formed {delta} days apart at {addr}",
                        "confidence": "medium",
                        "entity_a_id": a["id"],
                        "entity_b_id": b["id"],
                    })
                    if len(candidates) >= limit:
                        return candidates

    return candidates


RULE_FUNCTIONS = {
    "shared_address": rule_shared_address,
    "shared_officer": rule_shared_officer,
    "corporate_chain": rule_corporate_chain,
    "co_occurrence": rule_co_occurrence,
    "temporal_proximity": rule_temporal_proximity,
}


def scan_all(db, dry_run=False, limit=50):
    """Run all inference rules and return combined candidates."""
    all_candidates = []
    for rule_id, func in RULE_FUNCTIONS.items():
        remaining = limit - len(all_candidates)
        if remaining <= 0:
            break
        candidates = func(db, dry_run=dry_run, limit=remaining)
        all_candidates.extend(candidates)
    return all_candidates


def create_inference_leads(db, candidates, dry_run=False):
    """Create pending_triage leads from inference candidates."""
    created = 0
    for c in candidates:
        title = f"Inferred ({c['rule']}): {c['entity_a'][:25]} ↔ {c['entity_b'][:25]}"
        if _lead_exists_for_inference(db, c['entity_a'], c['entity_b'], c['rule']):
            continue

        notes = f"Rule: {c['rule']}. Evidence: {c['evidence']}. Confidence: {c['confidence']}."

        if dry_run:
            print(f"  [DRY] {title}")
        else:
            db.execute("""
                INSERT INTO leads (title, category, priority, status, source, target_name,
                                   created_at)
                VALUES (?, 'connection', ?, 'pending_triage', ?, ?, datetime('now'))
            """, (title, "medium" if c["confidence"] in ("medium", "high") else "low",
                  f"agent:connection_inference:{c['rule']}", c["entity_a"]))
            lead_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO lead_notes (lead_id, note, created_at) VALUES (?, ?, datetime('now'))",
                (lead_id, notes)
            )
            created += 1

    if not dry_run:
        db.commit()
    return created


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Connection inference engine")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Run all inference rules")
    p_scan.add_argument("--dry-run", action="store_true")
    p_scan.add_argument("--limit", type=int, default=50)
    add_output_args(p_scan)

    p_rules = sub.add_parser("rules", help="List available inference rules")

    p_apply = sub.add_parser("apply", help="Apply a specific rule")
    p_apply.add_argument("--rule", required=True, choices=list(RULES.keys()))
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--limit", type=int, default=50)
    add_output_args(p_apply)

    args = parser.parse_args()

    if args.command == "scan":
        db = get_db()
        candidates = scan_all(db, dry_run=args.dry_run, limit=args.limit)
        if write_output(candidates, args, summary=f"inference candidates ({len(candidates)})"):
            db.close()
            return
        print(f"\nConnection Inference Scan: {len(candidates)} candidates")
        print(f"{'Rule':<20} {'Entity A':<30} {'Entity B':<30} {'Conf':<8}")
        print("-" * 92)
        for c in candidates:
            print(f"{c['rule']:<20} {c['entity_a'][:30]:<30} {c['entity_b'][:30]:<30} {c['confidence']:<8}")
        if candidates and not args.dry_run:
            n = create_inference_leads(db, candidates, dry_run=False)
            print(f"\nCreated {n} pending_triage leads from inference candidates.")
        elif candidates and args.dry_run:
            print(f"\n[DRY RUN] Would create up to {len(candidates)} leads.")
        db.close()

    elif args.command == "rules":
        print("Inference Rules:")
        print("-" * 60)
        for rule_id, info in RULES.items():
            print(f"  {rule_id:<22} {info['description']}")

    elif args.command == "apply":
        db = get_db()
        func = RULE_FUNCTIONS[args.rule]
        candidates = func(db, dry_run=args.dry_run, limit=args.limit)
        if write_output(candidates, args, summary=f"{args.rule} candidates ({len(candidates)})"):
            db.close()
            return
        print(f"\n{RULES[args.rule]['name']} Rule: {len(candidates)} candidates")
        for c in candidates:
            print(f"  {c['entity_a'][:35]} ↔ {c['entity_b'][:35]}  [{c['evidence'][:40]}]")
        if candidates and not args.dry_run:
            n = create_inference_leads(db, candidates, dry_run=False)
            print(f"\nCreated {n} pending_triage leads.")
        db.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
