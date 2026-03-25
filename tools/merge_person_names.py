#!/usr/bin/env python3
"""Person name merge tool — standardized process for merging name variants.

Scans findings/connections for duplicate person names, merges variants into
a canonical name, deduplicates connections, and logs all changes.

Usage:
    uv run python tools/merge_person_names.py scan [--threshold 85]
    uv run python tools/merge_person_names.py merge "Alias Name" "Canonical Name" [--entity-id ID] [--dry-run]
    uv run python tools/merge_person_names.py dedup-connections "Person Name" [--dry-run]
    uv run python tools/merge_person_names.py show "Person Name"
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_scan(args):
    """Scan for potential person name duplicates across findings and connections."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        print("Install rapidfuzz: uv pip install rapidfuzz")
        sys.exit(1)

    db = get_db()
    threshold = args.threshold

    # Collect all person names from findings and connections
    names = set()
    for row in db.execute("SELECT DISTINCT target_name FROM findings WHERE target_name IS NOT NULL"):
        names.add(row["target_name"])
    for row in db.execute("SELECT DISTINCT person_a FROM connections"):
        names.add(row["person_a"])
    for row in db.execute("SELECT DISTINCT person_b FROM connections"):
        names.add(row["person_b"])

    # Filter out entity-like names and slash variants
    person_names = sorted(n for n in names if n and " / " not in n)

    # Check existing aliases
    existing_aliases = set()
    for row in db.execute("SELECT canonical_name, alias FROM name_aliases"):
        existing_aliases.add((row["canonical_name"], row["alias"]))
        existing_aliases.add((row["alias"], row["canonical_name"]))

    # Find fuzzy matches
    candidates = []
    for i, a in enumerate(person_names):
        for b in person_names[i + 1:]:
            if (a, b) in existing_aliases:
                continue
            score = fuzz.ratio(a.lower(), b.lower())
            if score >= threshold:
                # Count findings for each
                fa = db.execute("SELECT COUNT(*) FROM findings WHERE target_name = ?", (a,)).fetchone()[0]
                fb = db.execute("SELECT COUNT(*) FROM findings WHERE target_name = ?", (b,)).fetchone()[0]
                candidates.append((score, a, fa, b, fb))

    candidates.sort(key=lambda x: -x[0])

    if not candidates:
        print(f"No potential duplicates found (threshold={threshold})")
        return

    print(f"Potential person name duplicates (threshold={threshold}):\n")
    for score, a, fa, b, fb in candidates[:args.limit]:
        canonical = a if fa >= fb else b
        alias = b if fa >= fb else a
        print(f"  {score}% | '{alias}' ({min(fa,fb)} findings) -> '{canonical}' ({max(fa,fb)} findings)")
        print(f"         merge: uv run python tools/merge_person_names.py merge \"{alias}\" \"{canonical}\"")
        print()


def cmd_merge(args):
    """Merge a name variant into the canonical name."""
    db = get_db()
    alias, canonical = args.alias, args.canonical

    # Count affected rows
    f_count = db.execute(
        "SELECT COUNT(*) FROM findings WHERE target_name = ?", (alias,)
    ).fetchone()[0]
    f_slash = db.execute(
        "SELECT COUNT(*) FROM findings WHERE target_name LIKE ?", (f"{canonical} / %",)
    ).fetchone()[0]
    c_a = db.execute(
        "SELECT COUNT(*) FROM connections WHERE person_a = ?", (alias,)
    ).fetchone()[0]
    c_b = db.execute(
        "SELECT COUNT(*) FROM connections WHERE person_b = ?", (alias,)
    ).fetchone()[0]

    print(f"Merge: '{alias}' -> '{canonical}'")
    print(f"  Findings to update: {f_count}")
    print(f"  Slash-variant findings to normalize: {f_slash}")
    print(f"  Connections (person_a) to update: {c_a}")
    print(f"  Connections (person_b) to update: {c_b}")

    if f_count + c_a + c_b == 0:
        print("\nNothing to merge.")
        return

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        # Preview connection duplicates that would result
        _preview_connection_dupes(db, alias, canonical)
        return

    # Execute merge
    db.execute(
        "UPDATE findings SET target_name = ? WHERE target_name = ?",
        (canonical, alias),
    )
    if f_slash:
        db.execute(
            "UPDATE findings SET target_name = ? WHERE target_name LIKE ?",
            (canonical, f"{canonical} / %"),
        )
    # Delete connections that would become duplicates after rename
    db.execute(
        """DELETE FROM connections WHERE person_a = ? AND id IN (
               SELECT c1.id FROM connections c1
               INNER JOIN connections c2
               ON c2.person_a = ? AND c1.person_b = c2.person_b
               AND c1.relationship_type = c2.relationship_type
               AND c1.profile_id IS NOT DISTINCT FROM c2.profile_id
               WHERE c1.person_a = ?
           )""",
        (alias, canonical, alias),
    )
    db.execute(
        """DELETE FROM connections WHERE person_b = ? AND id IN (
               SELECT c1.id FROM connections c1
               INNER JOIN connections c2
               ON c2.person_b = ? AND c1.person_a = c2.person_a
               AND c1.relationship_type = c2.relationship_type
               AND c1.profile_id IS NOT DISTINCT FROM c2.profile_id
               WHERE c1.person_b = ?
           )""",
        (alias, canonical, alias),
    )
    db.execute(
        "UPDATE connections SET person_a = ? WHERE person_a = ?",
        (canonical, alias),
    )
    db.execute(
        "UPDATE connections SET person_b = ? WHERE person_b = ?",
        (canonical, alias),
    )

    # Add name alias
    db.execute(
        """INSERT OR IGNORE INTO name_aliases
           (canonical_name, alias, alias_type, entity_id, created_by)
           VALUES (?, ?, 'person_variant', ?, 'merge_person_names')""",
        (canonical, alias, args.entity_id),
    )

    # Log the merge as a correction
    db.execute(
        """INSERT INTO corrections (table_name, record_id, field_name, old_value, new_value, reason, corrected_by, correction_type)
           VALUES ('findings', 0, 'target_name', ?, ?, 'Person name variant merge', 'merge_person_names', 'merge')""",
        (alias, canonical),
    )

    db.commit()
    print("\nMerge completed.")

    # Verify
    remaining = db.execute(
        "SELECT COUNT(*) FROM findings WHERE target_name = ?", (alias,)
    ).fetchone()[0]
    total = db.execute(
        "SELECT COUNT(*) FROM findings WHERE target_name = ?", (canonical,)
    ).fetchone()[0]
    print(f"  Remaining '{alias}' findings: {remaining}")
    print(f"  Total '{canonical}' findings: {total}")

    # Check for resulting duplicate connections
    _show_connection_dupes(db, canonical)


def _preview_connection_dupes(db, alias, canonical):
    """Preview connection duplicates that would result from a merge."""
    # Get all connections for both names
    alias_conns = db.execute(
        "SELECT id, person_a, person_b, profile_id FROM connections WHERE person_a = ? OR person_b = ?",
        (alias, alias),
    ).fetchall()

    canonical_conns = db.execute(
        "SELECT id, person_a, person_b, profile_id FROM connections WHERE person_a = ? OR person_b = ?",
        (canonical, canonical),
    ).fetchall()

    # Normalize pairs
    def normalize_pair(a, b, name_from, name_to):
        a = name_to if a == name_from else a
        b = name_to if b == name_from else b
        return tuple(sorted([a, b]))

    canonical_pairs = defaultdict(list)
    for c in canonical_conns:
        pair = tuple(sorted([c["person_a"], c["person_b"]]))
        canonical_pairs[pair].append(dict(c))

    dupes = []
    for c in alias_conns:
        pair = normalize_pair(c["person_a"], c["person_b"], alias, canonical)
        if pair in canonical_pairs:
            dupes.append((dict(c), canonical_pairs[pair]))

    if dupes:
        print(f"\n  Would create {len(dupes)} duplicate connection(s):")
        for alias_conn, existing in dupes:
            print(f"    {alias_conn['person_a']} <-> {alias_conn['person_b']} (id={alias_conn['id']}, profile={alias_conn['profile_id']})")
            for e in existing:
                print(f"      already exists: id={e['id']}, profile={e['profile_id']}")
        print(f"\n  Run dedup-connections after merge to clean up.")


def _show_connection_dupes(db, name):
    """Show duplicate connections for a person."""
    rows = db.execute(
        """SELECT person_a, person_b, GROUP_CONCAT(id) as ids,
                  GROUP_CONCAT(COALESCE(profile_id, 'none')) as profiles, COUNT(*) as cnt
           FROM connections
           WHERE person_a = ? OR person_b = ?
           GROUP BY MIN(person_a, person_b), MAX(person_a, person_b)
           HAVING COUNT(*) > 1""",
        (name, name),
    ).fetchall()

    if rows:
        print(f"\n  Duplicate connections detected ({len(rows)}):")
        for r in rows:
            print(f"    {r['person_a']} <-> {r['person_b']}: ids=[{r['ids']}] profiles=[{r['profiles']}]")
        print(f"\n  Run: uv run python tools/merge_person_names.py dedup-connections \"{name}\"")


def cmd_dedup_connections(args):
    """Deduplicate connections for a person — keep one per unique pair+profile.

    Also collapses null-profile connections into existing profiled ones.
    """
    db = get_db()
    name = args.name

    # First: find null-profile connections that duplicate a profiled one (same pair)
    null_dupes = db.execute(
        """SELECT c1.id as null_id, c2.id as profiled_id, c2.profile_id,
                  c1.person_a, c1.person_b
           FROM connections c1
           JOIN connections c2 ON
               MIN(c1.person_a, c1.person_b) = MIN(c2.person_a, c2.person_b)
               AND MAX(c1.person_a, c1.person_b) = MAX(c2.person_a, c2.person_b)
               AND c1.id != c2.id
           WHERE (c1.person_a = ? OR c1.person_b = ?)
               AND c1.profile_id IS NULL
               AND c2.profile_id IS NOT NULL""",
        (name, name),
    ).fetchall()

    if null_dupes:
        null_ids = [r["null_id"] for r in null_dupes]
        if args.dry_run:
            print(f"  Would remove {len(null_ids)} null-profile duplicate(s):")
            for r in null_dupes:
                print(f"    id={r['null_id']} ({r['person_a']} <-> {r['person_b']}) superseded by id={r['profiled_id']} (profile={r['profile_id']})")
        else:
            for nid in null_ids:
                db.execute("DELETE FROM connections WHERE id = ?", (nid,))
            db.commit()
            print(f"  Removed {len(null_ids)} null-profile duplicate(s).")

    # Find duplicates: same person pair, same profile
    dupes = db.execute(
        """SELECT MIN(person_a, person_b) as pa, MAX(person_a, person_b) as pb,
                  profile_id, GROUP_CONCAT(id ORDER BY id) as ids, COUNT(*) as cnt
           FROM connections
           WHERE person_a = ? OR person_b = ?
           GROUP BY MIN(person_a, person_b), MAX(person_a, person_b), profile_id
           HAVING COUNT(*) > 1""",
        (name, name),
    ).fetchall()

    if not dupes:
        print(f"No duplicate connections for '{name}'")
        return

    to_delete = []
    for d in dupes:
        ids = [int(x) for x in d["ids"].split(",")]
        keep = ids[0]
        drop = ids[1:]
        to_delete.extend(drop)
        print(f"  {d['pa']} <-> {d['pb']} (profile={d['profile_id']}): keep={keep}, drop={drop}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would remove {len(to_delete)} duplicate connections.")
        return

    for cid in to_delete:
        db.execute("DELETE FROM connections WHERE id = ?", (cid,))

    db.commit()
    print(f"\nRemoved {len(to_delete)} duplicate connections.")


def cmd_show(args):
    """Show all names, aliases, findings, and connections for a person."""
    db = get_db()
    name = args.name

    # Aliases
    aliases = db.execute(
        "SELECT * FROM name_aliases WHERE canonical_name = ? OR alias = ?",
        (name, name),
    ).fetchall()
    if aliases:
        print("Aliases:")
        for a in aliases:
            print(f"  '{a['alias']}' -> '{a['canonical_name']}' ({a['alias_type']})")
    else:
        print("No aliases.")

    # Findings by target_name variants
    variants = db.execute(
        "SELECT target_name, COUNT(*) as cnt, GROUP_CONCAT(DISTINCT profile_id) as profiles "
        "FROM findings WHERE target_name LIKE ? GROUP BY target_name",
        (f"%{name}%",),
    ).fetchall()
    print(f"\nFindings:")
    for v in variants:
        print(f"  '{v['target_name']}': {v['cnt']} findings (profiles: {v['profiles']})")

    # Connections
    conns = db.execute(
        "SELECT id, person_a, person_b, relationship_type, profile_id "
        "FROM connections WHERE person_a LIKE ? OR person_b LIKE ? ORDER BY person_a, person_b",
        (f"%{name}%", f"%{name}%"),
    ).fetchall()
    print(f"\nConnections ({len(conns)}):")
    for c in conns:
        print(f"  [{c['id']}] {c['person_a']} <-> {c['person_b']} ({c['relationship_type']}) [profile={c['profile_id']}]")

    # Entity
    entities = db.execute(
        "SELECT e.id, e.name, e.entity_type FROM entities e "
        "JOIN name_aliases na ON e.id = na.entity_id "
        "WHERE na.canonical_name = ? OR na.alias = ? "
        "UNION SELECT id, name, entity_type FROM entities WHERE name LIKE ?",
        (name, name, f"%{name}%"),
    ).fetchall()
    if entities:
        print(f"\nEntities:")
        for e in entities:
            print(f"  #{e['id']} {e['name']} ({e['entity_type']})")


def main():
    parser = argparse.ArgumentParser(description="Person name merge tool")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan for potential name duplicates")
    p_scan.add_argument("--threshold", type=int, default=85, help="Fuzzy match threshold (0-100)")
    p_scan.add_argument("--limit", type=int, default=20, help="Max candidates to show")

    p_merge = sub.add_parser("merge", help="Merge name variant into canonical")
    p_merge.add_argument("alias", help="Name variant to merge away")
    p_merge.add_argument("canonical", help="Canonical name to keep")
    p_merge.add_argument("--entity-id", type=int, help="Entity ID to link alias to")
    p_merge.add_argument("--dry-run", action="store_true")

    p_dedup = sub.add_parser("dedup-connections", help="Remove duplicate connections")
    p_dedup.add_argument("name", help="Person name to dedup connections for")
    p_dedup.add_argument("--dry-run", action="store_true")

    p_show = sub.add_parser("show", help="Show person name state")
    p_show.add_argument("name", help="Person name to show")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {"scan": cmd_scan, "merge": cmd_merge, "dedup-connections": cmd_dedup_connections, "show": cmd_show}[args.command](args)


if __name__ == "__main__":
    main()
