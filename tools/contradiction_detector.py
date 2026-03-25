#!/usr/bin/env python3
"""
Contradiction detection for OSINT investigations.

Scans findings for temporal, factual, relationship, and source contradictions.
Each contradiction generates a pending_triage lead for resolution.

Part of investigation.db.

Usage:
    python tools/contradiction_detector.py scan [--limit 50] [--dry-run]
    python tools/contradiction_detector.py review --pair F1 F2
"""

import argparse
import json
import re
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


def _extract_amounts(text):
    """Extract dollar amounts from text."""
    if not text:
        return []
    # Match patterns like $1M, $1,000,000, $1.5 million, etc.
    amounts = []
    for m in re.finditer(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|M|B|K))?', text, re.IGNORECASE):
        raw = m.group()
        amounts.append(raw.lower())
    return amounts


def _extract_dates(text):
    """Extract date references from text."""
    if not text:
        return []
    dates = []
    for m in re.finditer(r'\d{4}-\d{2}-\d{2}', text):
        try:
            dates.append(datetime.strptime(m.group(), "%Y-%m-%d"))
        except ValueError:
            pass
    return dates


def detect_temporal_contradictions(db, limit=50):
    """Find findings where same person is in different locations on same date.

    Looks for findings with same target_name and same date_of_event but
    different location references.
    """
    rows = db.execute("""
        SELECT f1.id as f1_id, f1.summary as f1_summary, f1.target_name,
               f1.date_of_event as f1_date, f1.detail as f1_detail,
               f2.id as f2_id, f2.summary as f2_summary,
               f2.date_of_event as f2_date, f2.detail as f2_detail,
               f1.confidence as f1_conf, f2.confidence as f2_conf
        FROM findings f1
        JOIN findings f2 ON f1.target_name = f2.target_name
            AND f1.date_of_event = f2.date_of_event
            AND f1.id < f2.id
        WHERE f1.date_of_event IS NOT NULL
          AND f1.verification_status != 'retracted'
          AND f2.verification_status != 'retracted'
        LIMIT ?
    """, (limit * 3,)).fetchall()

    contradictions = []
    for r in rows:
        # Check if summaries describe different things (crude heuristic)
        s1 = (r["f1_summary"] or "").lower()
        s2 = (r["f2_summary"] or "").lower()

        # Look for location words
        locations_1 = set(re.findall(r'\b(?:new york|london|paris|palm beach|virgin islands|'
                                      r'miami|washington|israel|manhattan|st[\. ]thomas)\b', s1))
        locations_2 = set(re.findall(r'\b(?:new york|london|paris|palm beach|virgin islands|'
                                      r'miami|washington|israel|manhattan|st[\. ]thomas)\b', s2))

        if locations_1 and locations_2 and not locations_1.intersection(locations_2):
            contradictions.append({
                "type": "temporal_impossibility",
                "finding_a": r["f1_id"],
                "finding_b": r["f2_id"],
                "target": r["target_name"],
                "date": r["f1_date"],
                "summary_a": r["f1_summary"],
                "summary_b": r["f2_summary"],
                "locations_a": sorted(locations_1),
                "locations_b": sorted(locations_2),
                "confidence_a": r["f1_conf"],
                "confidence_b": r["f2_conf"],
            })
            if len(contradictions) >= limit:
                break

    return contradictions


def detect_relationship_conflicts(db, limit=50):
    """Find connections with conflicting relationship types for same person pair."""
    rows = db.execute("""
        SELECT c1.id as c1_id, c1.person_a, c1.person_b,
               c1.relationship_type as type_1, c1.description as desc_1,
               c2.id as c2_id, c2.relationship_type as type_2, c2.description as desc_2,
               c1.strength as str_1, c2.strength as str_2
        FROM connections c1
        JOIN connections c2 ON
            ((c1.person_a = c2.person_a AND c1.person_b = c2.person_b) OR
             (c1.person_a = c2.person_b AND c1.person_b = c2.person_a))
            AND c1.id < c2.id
        WHERE c1.verification_status != 'retracted'
          AND c2.verification_status != 'retracted'
          AND c1.relationship_type != c2.relationship_type
        LIMIT ?
    """, (limit * 2,)).fetchall()

    # Only flag genuinely conflicting types
    conflict_pairs = {
        frozenset({"financial", "legal"}),  # could be adversarial
    }
    # These are NOT conflicts — same people can have multiple relationship types
    compatible_types = {
        frozenset({"social", "employment"}),
        frozenset({"financial", "employment"}),
        frozenset({"social", "financial"}),
        frozenset({"corporate", "employment"}),
        frozenset({"advisory", "financial"}),
        frozenset({"political", "social"}),
    }

    contradictions = []
    for r in rows:
        pair = frozenset({r["type_1"], r["type_2"]})
        if pair in compatible_types:
            continue

        # Check descriptions for actual conflict keywords
        d1 = (r["desc_1"] or "").lower()
        d2 = (r["desc_2"] or "").lower()
        conflict_words_1 = {"hostile", "adversarial", "lawsuit", "sued", "opposed"}
        conflict_words_2 = {"partner", "ally", "friend", "collaborat"}

        has_hostile = any(w in d1 or w in d2 for w in conflict_words_1)
        has_friendly = any(w in d1 or w in d2 for w in conflict_words_2)

        if has_hostile and has_friendly:
            contradictions.append({
                "type": "relationship_conflict",
                "connection_a": r["c1_id"],
                "connection_b": r["c2_id"],
                "person_a": r["person_a"],
                "person_b": r["person_b"],
                "type_a": r["type_1"],
                "type_b": r["type_2"],
                "desc_a": r["desc_1"],
                "desc_b": r["desc_2"],
            })
            if len(contradictions) >= limit:
                break

    return contradictions


def detect_factual_conflicts(db, limit=50):
    """Find findings about same event with different amounts, dates, or participants."""
    # Get findings pairs with same target and overlapping date range
    rows = db.execute("""
        SELECT f1.id as f1_id, f1.summary as f1_summary, f1.detail as f1_detail,
               f1.target_name, f1.date_of_event as f1_date,
               f2.id as f2_id, f2.summary as f2_summary, f2.detail as f2_detail,
               f2.date_of_event as f2_date,
               f1.confidence as f1_conf, f2.confidence as f2_conf
        FROM findings f1
        JOIN findings f2 ON f1.target_name = f2.target_name AND f1.id < f2.id
        WHERE f1.verification_status != 'retracted'
          AND f2.verification_status != 'retracted'
        LIMIT ?
    """, (limit * 5,)).fetchall()

    contradictions = []
    for r in rows:
        text_1 = f"{r['f1_summary'] or ''} {r['f1_detail'] or ''}"
        text_2 = f"{r['f2_summary'] or ''} {r['f2_detail'] or ''}"

        # Compare amounts
        amounts_1 = _extract_amounts(text_1)
        amounts_2 = _extract_amounts(text_2)

        if amounts_1 and amounts_2:
            # Check if they describe similar events with different amounts
            if amounts_1 != amounts_2:
                # Heuristic: both mention the same person and have amounts
                s1_words = set(r["f1_summary"].lower().split()) if r["f1_summary"] else set()
                s2_words = set(r["f2_summary"].lower().split()) if r["f2_summary"] else set()
                overlap = len(s1_words & s2_words) / max(len(s1_words | s2_words), 1)
                if overlap > 0.3:  # Summaries are similar enough to compare
                    contradictions.append({
                        "type": "factual_conflict",
                        "finding_a": r["f1_id"],
                        "finding_b": r["f2_id"],
                        "target": r["target_name"],
                        "summary_a": r["f1_summary"],
                        "summary_b": r["f2_summary"],
                        "amounts_a": amounts_1,
                        "amounts_b": amounts_2,
                        "confidence_a": r["f1_conf"],
                        "confidence_b": r["f2_conf"],
                    })
                    if len(contradictions) >= limit:
                        break

    return contradictions


def detect_source_conflicts(db, limit=50):
    """Find high-confidence findings that contradict each other from different sources."""
    rows = db.execute("""
        SELECT f1.id as f1_id, f1.summary as f1_summary, f1.source_datasets as f1_src,
               f1.confidence as f1_conf,
               f2.id as f2_id, f2.summary as f2_summary, f2.source_datasets as f2_src,
               f2.confidence as f2_conf,
               f1.target_name
        FROM findings f1
        JOIN findings f2 ON f1.target_name = f2.target_name AND f1.id < f2.id
        WHERE f1.confidence IN ('high', 'confirmed')
          AND f2.confidence IN ('high', 'confirmed')
          AND f1.source_datasets != f2.source_datasets
          AND f1.verification_status != 'retracted'
          AND f2.verification_status != 'retracted'
        LIMIT ?
    """, (limit * 5,)).fetchall()

    contradictions = []
    # Look for negation patterns between related findings
    negation_pairs = [
        ("denied", "confirmed"), ("never", "regularly"), ("no evidence", "evidence"),
        ("refused", "agreed"), ("terminated", "continued"), ("ended", "ongoing"),
    ]

    for r in rows:
        s1 = (r["f1_summary"] or "").lower()
        s2 = (r["f2_summary"] or "").lower()

        for neg_a, neg_b in negation_pairs:
            if (neg_a in s1 and neg_b in s2) or (neg_b in s1 and neg_a in s2):
                contradictions.append({
                    "type": "source_conflict",
                    "finding_a": r["f1_id"],
                    "finding_b": r["f2_id"],
                    "target": r["target_name"],
                    "summary_a": r["f1_summary"],
                    "summary_b": r["f2_summary"],
                    "source_a": r["f1_src"],
                    "source_b": r["f2_src"],
                    "trigger": f"{neg_a}/{neg_b}",
                })
                if len(contradictions) >= limit:
                    return contradictions
                break

    return contradictions


def scan_all(db, limit=50, dry_run=False):
    """Run all contradiction detection rules."""
    all_contradictions = []

    print("  Scanning temporal contradictions...")
    temporal = detect_temporal_contradictions(db, limit=limit)
    all_contradictions.extend(temporal)
    print(f"    Found {len(temporal)} temporal contradictions")

    print("  Scanning relationship conflicts...")
    relationship = detect_relationship_conflicts(db, limit=limit)
    all_contradictions.extend(relationship)
    print(f"    Found {len(relationship)} relationship conflicts")

    print("  Scanning factual conflicts...")
    factual = detect_factual_conflicts(db, limit=limit)
    all_contradictions.extend(factual)
    print(f"    Found {len(factual)} factual conflicts")

    print("  Scanning source conflicts...")
    source = detect_source_conflicts(db, limit=limit)
    all_contradictions.extend(source)
    print(f"    Found {len(source)} source conflicts")

    return all_contradictions


def create_contradiction_leads(db, contradictions, dry_run=False):
    """Create pending_triage leads from contradictions."""
    created = 0
    for c in contradictions:
        ctype = c["type"]
        if "finding_a" in c:
            title = f"Contradiction ({ctype}): F#{c['finding_a']} vs F#{c['finding_b']}"
            target = c.get("target", "")
        else:
            title = f"Contradiction ({ctype}): C#{c['connection_a']} vs C#{c['connection_b']}"
            target = c.get("person_a", "")

        # Check for existing lead
        existing = db.execute(
            "SELECT id FROM leads WHERE title LIKE ?", (f"%{title[:40]}%",)
        ).fetchone()
        if existing:
            continue

        notes = json.dumps(c, indent=2, default=str)

        if dry_run:
            print(f"  [DRY] {title}")
        else:
            db.execute("""
                INSERT INTO leads (title, category, priority, status, source,
                                   target_name, created_at)
                VALUES (?, 'connection', 'high', 'pending_triage',
                        'agent:contradiction_detector', ?, datetime('now'))
            """, (title, target))
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
    parser = argparse.ArgumentParser(description="Contradiction detection for findings")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan for contradictions across all findings")
    p_scan.add_argument("--limit", type=int, default=50)
    p_scan.add_argument("--dry-run", action="store_true")
    add_output_args(p_scan)

    p_review = sub.add_parser("review", help="Review a specific contradiction pair")
    p_review.add_argument("--pair", nargs=2, type=int, required=True,
                          metavar=("F1", "F2"), help="Two finding IDs to compare")
    add_output_args(p_review)

    args = parser.parse_args()

    if args.command == "scan":
        db = get_db()
        contradictions = scan_all(db, limit=args.limit, dry_run=args.dry_run)

        if write_output(contradictions, args, summary=f"contradictions ({len(contradictions)})"):
            db.close()
            return

        print(f"\nTotal Contradictions Found: {len(contradictions)}")
        if contradictions:
            print(f"\n{'Type':<25} {'Details':<60}")
            print("-" * 88)
            for c in contradictions:
                if "finding_a" in c:
                    detail = f"F#{c['finding_a']} vs F#{c['finding_b']} — {c.get('target', '')[:30]}"
                else:
                    detail = f"C#{c['connection_a']} vs C#{c['connection_b']} — {c.get('person_a', '')[:30]}"
                print(f"{c['type']:<25} {detail:<60}")

            if not args.dry_run:
                n = create_contradiction_leads(db, contradictions, dry_run=False)
                print(f"\nCreated {n} pending_triage leads for contradiction review.")
            else:
                print(f"\n[DRY RUN] Would create up to {len(contradictions)} leads.")

        db.close()

    elif args.command == "review":
        db = get_db()
        f1_id, f2_id = args.pair
        f1 = db.execute("SELECT * FROM findings WHERE id = ?", (f1_id,)).fetchone()
        f2 = db.execute("SELECT * FROM findings WHERE id = ?", (f2_id,)).fetchone()

        if not f1 or not f2:
            print(f"ERROR: Finding(s) not found. F#{f1_id}: {'found' if f1 else 'missing'}, "
                  f"F#{f2_id}: {'found' if f2 else 'missing'}")
            db.close()
            sys.exit(1)

        result = {
            "finding_a": dict(f1),
            "finding_b": dict(f2),
        }
        if write_output(result, args, summary=f"contradiction review F#{f1_id} vs F#{f2_id}"):
            db.close()
            return

        print(f"\nContradiction Review: F#{f1_id} vs F#{f2_id}")
        print(f"\n{'='*60}")
        print(f"Finding #{f1['id']}:")
        print(f"  Target:     {f1['target_name']}")
        print(f"  Summary:    {f1['summary']}")
        print(f"  Date:       {f1['date_of_event']}")
        print(f"  Confidence: {f1['confidence']}")
        print(f"  Source:     {f1['source_datasets']}")
        print(f"  Claim:      {f1.get('claim_type', '?')}")
        print(f"\n{'='*60}")
        print(f"Finding #{f2['id']}:")
        print(f"  Target:     {f2['target_name']}")
        print(f"  Summary:    {f2['summary']}")
        print(f"  Date:       {f2['date_of_event']}")
        print(f"  Confidence: {f2['confidence']}")
        print(f"  Source:     {f2['source_datasets']}")
        print(f"  Claim:      {f2.get('claim_type', '?')}")

        db.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
