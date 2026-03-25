#!/usr/bin/env python3
"""Finding deduplication for investigation.db.

Detects duplicate findings by EFTA cluster overlap, summary similarity,
and substring matching. Merges findings by moving evidence and connections,
with full corrections table audit trail.

Usage:
    python tools/finding_dedup.py scan [--threshold 0.5]
    python tools/finding_dedup.py merge --keep-id 1189 --absorb-ids 529,541
    python tools/finding_dedup.py show-cluster EFTA02454291
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"

_GENERIC_STOP_WORDS = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
              "for", "of", "with", "by", "from", "is", "was", "were", "are",
              "be", "been", "being", "have", "has", "had", "do", "does", "did",
              "will", "would", "could", "should", "may", "might", "shall",
              "this", "that", "these", "those", "it", "its", "as", "not", "no"}

def _build_stop_words():
    """Build stop words including primary subject name tokens (too common to be distinctive)."""
    words = set(_GENERIC_STOP_WORDS)
    try:
        from tools.investigation_context import get_active_profile
        profile = get_active_profile()
        if profile.primary_subject:
            words |= set(profile.primary_subject.lower().split())
    except Exception:
        pass
    return words

STOP_WORDS = _build_stop_words()


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _tokens(text):
    """Extract meaningful tokens for similarity."""
    return {w for w in re.findall(r'\w+', text.lower())
            if w not in STOP_WORDS and len(w) > 2 and not w.isdigit()}


def jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def cmd_scan(args):
    """Detect duplicate findings."""
    db = get_db()
    threshold = args.threshold

    # Strategy 1: Same EFTA cluster with high summary overlap
    print("=== Strategy 1: EFTA cluster + summary overlap ===")
    clusters = db.execute("""
        SELECT fe.evidence_ref, COUNT(DISTINCT fe.finding_id) as fcount
        FROM finding_evidence fe
        WHERE fe.evidence_type = 'efta'
        GROUP BY fe.evidence_ref
        HAVING COUNT(DISTINCT fe.finding_id) >= 2
        ORDER BY fcount DESC
    """).fetchall()

    efta_dups = []
    for cluster in clusters:
        efta = cluster["evidence_ref"]
        findings = db.execute("""
            SELECT f.id, f.target_name, f.summary, f.verification_status
            FROM findings f
            JOIN finding_evidence fe ON f.id = fe.finding_id
            WHERE fe.evidence_ref = ?
              AND f.verification_status != 'retracted'
            ORDER BY f.id
        """, (efta,)).fetchall()

        if len(findings) < 2:
            continue

        for i, f1 in enumerate(findings):
            t1 = _tokens(f1["summary"])
            for f2 in findings[i + 1:]:
                t2 = _tokens(f2["summary"])
                sim = jaccard(t1, t2)
                if sim >= threshold:
                    efta_dups.append({
                        "efta": efta,
                        "id_a": f1["id"], "target_a": f1["target_name"],
                        "id_b": f2["id"], "target_b": f2["target_name"],
                        "similarity": sim,
                    })

    print(f"  Found {len(efta_dups)} duplicate pairs (threshold={threshold})")
    for d in efta_dups[:20]:
        print(f"  #{d['id_a']} ({d['target_a']}) <-> #{d['id_b']} ({d['target_b']}) "
              f"[{d['efta']}] sim={d['similarity']:.1%}")

    # Strategy 2: Same target + similar summary (no shared EFTA required)
    print(f"\n=== Strategy 2: Same target + summary overlap ===")
    targets = db.execute("""
        SELECT target_name, COUNT(*) as cnt FROM findings
        WHERE verification_status != 'retracted'
        GROUP BY target_name HAVING cnt >= 2
        ORDER BY cnt DESC
    """).fetchall()

    target_dups = []
    for target_row in targets:
        target = target_row["target_name"]
        findings = db.execute("""
            SELECT id, summary FROM findings
            WHERE target_name = ? AND verification_status != 'retracted'
            ORDER BY id
        """, (target,)).fetchall()

        for i, f1 in enumerate(findings):
            t1 = _tokens(f1["summary"])
            for f2 in findings[i + 1:]:
                t2 = _tokens(f2["summary"])
                sim = jaccard(t1, t2)
                if sim >= 0.6:  # Higher threshold for non-EFTA matches
                    target_dups.append({
                        "target": target,
                        "id_a": f1["id"], "id_b": f2["id"],
                        "similarity": sim,
                    })

    print(f"  Found {len(target_dups)} duplicate pairs (threshold=0.6)")
    for d in target_dups[:20]:
        print(f"  #{d['id_a']} <-> #{d['id_b']} ({d['target']}) sim={d['similarity']:.1%}")

    # Strategy 3: Substring findings (shorter summary is substring of longer)
    print(f"\n=== Strategy 3: Subset findings ===")
    all_findings = db.execute("""
        SELECT id, target_name, summary FROM findings
        WHERE verification_status != 'retracted'
        ORDER BY LENGTH(summary)
    """).fetchall()

    subset_dups = []
    summaries = [(f["id"], f["target_name"], f["summary"].lower()) for f in all_findings]
    # Only check findings with same target for substring
    by_target = {}
    for fid, target, summary in summaries:
        by_target.setdefault(target, []).append((fid, summary))

    for target, entries in by_target.items():
        for i, (id_a, sum_a) in enumerate(entries):
            if len(sum_a) < 30:
                continue
            for id_b, sum_b in entries[i + 1:]:
                if len(sum_a) < len(sum_b) * 0.5:
                    continue  # Too short to be meaningful substring
                if sum_a in sum_b or sum_b in sum_a:
                    subset_dups.append({
                        "target": target,
                        "shorter_id": id_a if len(sum_a) <= len(sum_b) else id_b,
                        "longer_id": id_b if len(sum_a) <= len(sum_b) else id_a,
                    })

    print(f"  Found {len(subset_dups)} subset pairs")
    for d in subset_dups[:10]:
        print(f"  #{d['shorter_id']} is subset of #{d['longer_id']} ({d['target']})")

    # Summary
    all_ids = set()
    for d in efta_dups:
        all_ids.update([d["id_a"], d["id_b"]])
    for d in target_dups:
        all_ids.update([d["id_a"], d["id_b"]])
    for d in subset_dups:
        all_ids.update([d["shorter_id"], d["longer_id"]])

    print(f"\n{'=' * 50}")
    print(f"Total unique findings involved in duplicates: {len(all_ids)}")
    print(f"  EFTA cluster pairs: {len(efta_dups)}")
    print(f"  Same-target pairs: {len(target_dups)}")
    print(f"  Subset pairs: {len(subset_dups)}")

    db.close()


def cmd_show_cluster(args):
    """Show all findings citing a specific EFTA ID."""
    db = get_db()
    efta = args.efta_id

    findings = db.execute("""
        SELECT f.id, f.target_name, f.summary, f.claim_type, f.confidence,
               f.verification_status, fe.source_quote
        FROM findings f
        JOIN finding_evidence fe ON f.id = fe.finding_id
        WHERE fe.evidence_ref = ?
        ORDER BY f.id
    """, (efta,)).fetchall()

    print(f"Findings citing {efta}: {len(findings)}")
    for f in findings:
        status = f["verification_status"] or "?"
        print(f"\n  #{f['id']} [{f['claim_type']}/{f['confidence']}] ({status})")
        print(f"    Target: {f['target_name']}")
        print(f"    Summary: {f['summary'][:150]}...")
        if f["source_quote"]:
            print(f"    Quote: {f['source_quote'][:80]}...")

    # Pairwise similarity
    if len(findings) >= 2:
        print(f"\nPairwise similarity:")
        for i, f1 in enumerate(findings):
            t1 = _tokens(f1["summary"])
            for f2 in findings[i + 1:]:
                t2 = _tokens(f2["summary"])
                sim = jaccard(t1, t2)
                print(f"  #{f1['id']} <-> #{f2['id']}: {sim:.1%}")

    db.close()


def cmd_merge(args):
    """Merge findings: move evidence and connections from absorbed to keeper."""
    db = get_db()

    keep_id = args.keep_id
    absorb_ids = [int(x) for x in args.absorb_ids.split(",")]

    # Validate
    keep = db.execute("SELECT * FROM findings WHERE id = ?", (keep_id,)).fetchone()
    if not keep:
        print(f"ERROR: keep finding #{keep_id} not found")
        db.close()
        return

    for aid in absorb_ids:
        absorbed = db.execute("SELECT * FROM findings WHERE id = ?", (aid,)).fetchone()
        if not absorbed:
            print(f"ERROR: absorb finding #{aid} not found")
            db.close()
            return

    print(f"Merging {len(absorb_ids)} findings into #{keep_id} ({keep['target_name']})")

    for aid in absorb_ids:
        absorbed = db.execute("SELECT * FROM findings WHERE id = ?", (aid,)).fetchone()
        print(f"\n  Absorbing #{aid} ({absorbed['target_name']}):")

        # 1. Copy evidence rows
        evidence = db.execute(
            "SELECT * FROM finding_evidence WHERE finding_id = ?", (aid,)
        ).fetchall()
        ev_moved = 0
        for ev in evidence:
            try:
                if not args.dry_run:
                    db.execute("""
                        INSERT OR IGNORE INTO finding_evidence
                            (finding_id, evidence_type, evidence_ref, source_quote,
                             source_page, assessment, email_sender, email_date, chain_position)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (keep_id, ev["evidence_type"], ev["evidence_ref"],
                          ev["source_quote"], ev["source_page"], ev["assessment"],
                          ev.get("email_sender"), ev.get("email_date"),
                          ev.get("chain_position")))
                ev_moved += 1
            except Exception:
                pass
        print(f"    Evidence rows moved: {ev_moved}")

        # 2. Update connections
        conns = db.execute(
            "SELECT id FROM connections WHERE finding_id = ?", (aid,)
        ).fetchall()
        if conns:
            if not args.dry_run:
                db.execute(
                    "UPDATE connections SET finding_id = ? WHERE finding_id = ?",
                    (keep_id, aid)
                )
            print(f"    Connections redirected: {len(conns)}")

        # 3. Record merge in corrections
        if not args.dry_run:
            db.execute("""
                INSERT INTO corrections (table_name, record_id, field_name,
                                        old_value, new_value, reason,
                                        corrected_by, correction_type)
                VALUES ('findings', ?, 'verification_status', ?, 'retracted',
                        ?, 'human', 'merge')
            """, (aid, absorbed["verification_status"],
                  f"Merged into finding #{keep_id}: duplicate content"))

        # 4. Retract absorbed finding
        if not args.dry_run:
            db.execute("""
                UPDATE findings SET verification_status = 'retracted',
                    verified_by = 'finding_dedup', verified_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (aid,))
        print(f"    Status: retracted (merged into #{keep_id})")

    if not args.dry_run:
        db.commit()
        print(f"\nMerge complete. All changes committed.")
    else:
        print(f"\n[DRY RUN] No changes applied.")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="Finding deduplication for investigation.db")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Detect duplicate findings")
    p_scan.add_argument("--threshold", type=float, default=0.5,
                        help="Jaccard similarity threshold for EFTA clusters")

    p_show = sub.add_parser("show-cluster", help="Show findings citing an EFTA ID")
    p_show.add_argument("efta_id", help="EFTA ID to show cluster for")

    p_merge = sub.add_parser("merge", help="Merge duplicate findings")
    p_merge.add_argument("--keep-id", type=int, required=True, help="Finding ID to keep")
    p_merge.add_argument("--absorb-ids", required=True, help="Comma-separated finding IDs to absorb")
    p_merge.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    commands = {
        "scan": cmd_scan,
        "show-cluster": cmd_show_cluster,
        "merge": cmd_merge,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
