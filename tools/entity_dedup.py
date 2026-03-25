#!/usr/bin/env python3
"""Entity and name deduplication for investigation.db.

Manages name_aliases table for canonical name resolution. Used by export
pipelines (network, dossiers, financials) and write paths (findings, connections)
to eliminate duplicate nodes and merge split dossier pages.

Usage:
    python tools/entity_dedup.py add-alias --canonical "Ehud Barak" --alias "Barak" --type person_variant
    python tools/entity_dedup.py list-aliases [--canonical NAME] [--type TYPE]
    python tools/entity_dedup.py remove-alias --alias "Barak"
    python tools/entity_dedup.py scan [--threshold 0.8]
    python tools/entity_dedup.py apply [--dry-run]
    python tools/entity_dedup.py seed [--dry-run]
    python tools/entity_dedup.py merge --keep-id N --delete-id M [--dry-run]
    python tools/entity_dedup.py stats
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"

ALIAS_TYPES = ("person_variant", "entity_variant", "entity_as_person")

# Known person name variants: alias -> canonical
PERSON_SEEDS = {
    "Barak": "Ehud Barak",
    "Rod-Larsen": "Terje Rod-Larsen",
    "Landon Thomas Jr": "Landon Thomas Jr.",
    "Bernard W. Indyke": "Bernard Indyke",
    "Darren K. Indyke": "Darren Indyke",
    "Epstein": "Jeffrey Epstein",
    "Wexner": "Les Wexner",
    "Leslie Wexner": "Les Wexner",
    "Weingarten": "Reid Weingarten",
    "Schoen": "David Schoen",
    "Deripaska": "Oleg Deripaska",
    "Alrasheed": "Anas Alrasheed",
    "Tamince": "Fettah Tamince",
    "Bondevik": "Kjell Magne Bondevik",
    "Herbert J. Siegel": "Herbert Siegel",
    "Kenneth Starr": "Ken Starr",
    "Larry Summers": "Lawrence Summers",
    "Kathy Ruemmler": "Kathryn Ruemmler",
    "William B Wachtel": "William B. Wachtel",
    "Richardson": "Bill Richardson",
    "Kellerhals": "Erika Kellerhals",
    # DS10 OCR variants
    "Jeffrey E Epstein": "Jeffrey Epstein",
    "J EPSTEIN": "Jeffrey Epstein",
    "JEFFREY EPSTEIN": "Jeffrey Epstein",
    "LEON BLACK": "Leon Black",
    "Leon D Black": "Leon Black",
    "ARIANE DE ROTHSCHILD": "Ariane de Rothschild",
    "Edmond De Rothschild": "Edmond de Rothschild",
}

# Known entity name variants: alias -> canonical
ENTITY_SEEDS = {
    "Gratitude America": "Gratitude America Ltd",
    "Southern Trust Company": "Southern Trust Company Inc",
    "Southern Country International": "Southern Country International Ltd",
    "Business Basics VI": "Business Basics VI LLC",
    "Liquid Funding": "Liquid Funding Ltd",
    "Gold & Wachtel": "Gold and Wachtel",
    "Jackie Fine Arts": "Jackie Fine Arts Inc",
    "Poplar Inc.": "Poplar Inc",
    "Honeycomb Asset Management": "Honeycomb Asset Management LP",
    "Honeycomb Partners": "Honeycomb Asset Management LP",
    "Nardello": "Nardello & Co.",
    "Nardello & Co": "Nardello & Co.",
    "IPI": "International Peace Institute",
    "HDI": "Humpty Dumpty Institute",
    "Carbyne": "Carbyne/Reporty",
    "Boies Schiller": "Boies Schiller Flexner",
    "Maple Inc": "Maple, Inc.",
    "Financial Trust Company Inc": "Financial Trust Company",
    # DS10 OCR entity variants
    "PLAN D LLC": "Plan D LLC",
    "PLAN D, LLC": "Plan D LLC",
    "Plan D, LLC": "Plan D LLC",
    "STC LLC": "Southern Trust Company Inc",
    "STC": "Southern Trust Company Inc",
    "SOUTHERN TRUST COMPANY": "Southern Trust Company Inc",
    "Southern Trust Co": "Southern Trust Company Inc",
    "W E LLC": "WE LLC",
    "JEGE INC": "JEGE Inc",
    "Jege Inc": "JEGE Inc",
    "JEGE, INC": "JEGE Inc",
    "IGO COMPANY LLC": "IGO Company LLC",
    "Igo Company Llc": "IGO Company LLC",
    "MAPLE INC": "Maple, Inc.",
    "Maple Inc.": "Maple, Inc.",
    "FTC": "Financial Trust Company",
    "FINANCIAL TRUST COMPANY": "Financial Trust Company",
    "DEUTSCHE BANK": "Deutsche Bank",
    "Deutsche Bank Trust": "Deutsche Bank",
    "DB TRUST": "Deutsche Bank",
}


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _ensure_aliases_table(db):
    """Create name_aliases table if it doesn't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS name_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            alias TEXT NOT NULL UNIQUE,
            alias_type TEXT NOT NULL CHECK(alias_type IN (
                'person_variant', 'entity_variant', 'entity_as_person'
            )),
            entity_id INTEGER REFERENCES entities(id),
            created_by TEXT DEFAULT 'system',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON name_aliases(canonical_name);
        CREATE INDEX IF NOT EXISTS idx_aliases_alias ON name_aliases(alias);
        CREATE INDEX IF NOT EXISTS idx_aliases_type ON name_aliases(alias_type);
    """)


def add_alias(db, canonical, alias, alias_type, entity_id=None, created_by="system"):
    """Insert a single alias. Returns True if inserted, False if already exists."""
    if alias_type not in ALIAS_TYPES:
        print(f"  ERROR: alias_type must be one of {ALIAS_TYPES}")
        return False
    try:
        db.execute(
            "INSERT INTO name_aliases (canonical_name, alias, alias_type, entity_id, created_by) VALUES (?, ?, ?, ?, ?)",
            (canonical, alias, alias_type, entity_id, created_by),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def cmd_add_alias(args):
    db = get_db()
    _ensure_aliases_table(db)
    if add_alias(db, args.canonical, args.alias, args.type, args.entity_id):
        db.commit()
        print(f"Added alias: '{args.alias}' -> '{args.canonical}' ({args.type})")
    else:
        print(f"Alias '{args.alias}' already exists")
    db.close()


def cmd_remove_alias(args):
    db = get_db()
    _ensure_aliases_table(db)
    cursor = db.execute("DELETE FROM name_aliases WHERE alias = ?", (args.alias,))
    if cursor.rowcount:
        db.commit()
        print(f"Removed alias: '{args.alias}'")
    else:
        print(f"Alias '{args.alias}' not found")
    db.close()


def cmd_list_aliases(args):
    db = get_db()
    _ensure_aliases_table(db)

    conditions, params = [], []
    if args.canonical:
        conditions.append("canonical_name = ?")
        params.append(args.canonical)
    if args.type:
        conditions.append("alias_type = ?")
        params.append(args.type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT * FROM name_aliases {where} ORDER BY alias_type, canonical_name, alias",
        params,
    ).fetchall()

    if not rows:
        print("No aliases found.")
        db.close()
        return

    current_type = None
    for row in rows:
        if row["alias_type"] != current_type:
            current_type = row["alias_type"]
            print(f"\n[{current_type}]")
        eid = f" (entity:{row['entity_id']})" if row["entity_id"] else ""
        print(f"  '{row['alias']}' -> '{row['canonical_name']}'{eid}")

    print(f"\nTotal: {len(rows)} aliases")
    db.close()


def cmd_scan(args):
    """Auto-detect probable duplicates across findings, connections, and entities."""
    db = get_db()
    _ensure_aliases_table(db)

    existing = {r[0].lower() for r in db.execute("SELECT alias FROM name_aliases").fetchall()}

    # 1. Entity names that appear verbatim in connections (entity_as_person)
    entity_rows = db.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
    conn_persons = set()
    for r in db.execute("SELECT DISTINCT person_a FROM connections UNION SELECT DISTINCT person_b FROM connections").fetchall():
        conn_persons.add(r[0])

    print("=== Entity names also in connections (entity_as_person) ===")
    eap_count = 0
    for row in entity_rows:
        if row["name"] in conn_persons and row["name"].lower() not in existing:
            eap_count += 1
            conn_count = db.execute(
                "SELECT COUNT(*) FROM connections WHERE person_a = ? OR person_b = ?",
                (row["name"], row["name"]),
            ).fetchone()[0]
            print(f"  entity:{row['id']} '{row['name']}' ({conn_count} connections)")
    print(f"  Found: {eap_count}\n")

    # 2. Finding target_name variants (punctuation/suffix differences)
    targets = [r[0] for r in db.execute("SELECT DISTINCT target_name FROM findings ORDER BY target_name").fetchall()]
    print("=== Probable target_name variants ===")
    import re
    def normalize_for_compare(s):
        return re.sub(r'[^a-z0-9]', '', s.lower())

    seen = set()
    variant_groups = []
    for i, t1 in enumerate(targets):
        n1 = normalize_for_compare(t1)
        if n1 in seen or len(n1) < 4:
            continue
        group = [t1]
        for t2 in targets[i + 1:]:
            n2 = normalize_for_compare(t2)
            if n2 == n1 or (len(n1) > 6 and (n1.startswith(n2) or n2.startswith(n1))):
                if t2.lower() not in existing:
                    group.append(t2)
        if len(group) > 1:
            seen.add(n1)
            variant_groups.append(group)
            counts = []
            for t in group:
                cnt = db.execute("SELECT COUNT(*) FROM findings WHERE target_name = ?", (t,)).fetchone()[0]
                counts.append(f"'{t}' ({cnt})")
            print(f"  {' | '.join(counts)}")

    print(f"  Found: {len(variant_groups)} groups\n")

    # 3. Connection person_name variants
    print("=== Probable connection person variants ===")
    persons_list = sorted(conn_persons)
    person_groups = []
    seen_persons = set()
    for i, p1 in enumerate(persons_list):
        n1 = normalize_for_compare(p1)
        if n1 in seen_persons or len(n1) < 4:
            continue
        group = [p1]
        for p2 in persons_list[i + 1:]:
            n2 = normalize_for_compare(p2)
            if n2 == n1 and p2.lower() not in existing:
                group.append(p2)
        if len(group) > 1:
            seen_persons.add(n1)
            person_groups.append(group)
            print(f"  {' | '.join(group)}")

    print(f"  Found: {len(person_groups)} groups")
    db.close()


def cmd_apply(args):
    """Auto-populate entity_as_person aliases for all entity/person collisions."""
    db = get_db()
    _ensure_aliases_table(db)

    entity_rows = db.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
    conn_persons = set()
    for r in db.execute("SELECT DISTINCT person_a FROM connections UNION SELECT DISTINCT person_b FROM connections").fetchall():
        conn_persons.add(r[0])

    added = 0
    skipped = 0
    for row in entity_rows:
        if row["name"] in conn_persons:
            if add_alias(db, row["name"], row["name"], "entity_as_person", entity_id=row["id"]):
                added += 1
                if not args.dry_run:
                    print(f"  Added: '{row['name']}' -> entity:{row['id']}")
                else:
                    print(f"  Would add: '{row['name']}' -> entity:{row['id']}")
            else:
                skipped += 1

    if not args.dry_run:
        db.commit()
        print(f"\nApplied: {added} entity_as_person aliases ({skipped} already existed)")
    else:
        db.rollback()
        print(f"\nDry run: would add {added} entity_as_person aliases ({skipped} already exist)")
    db.close()


def cmd_seed(args):
    """Populate all known person and entity variant aliases."""
    db = get_db()
    _ensure_aliases_table(db)

    added = 0
    skipped = 0

    print("=== Person variants ===")
    for alias, canonical in sorted(PERSON_SEEDS.items()):
        if add_alias(db, canonical, alias, "person_variant"):
            added += 1
            print(f"  '{alias}' -> '{canonical}'")
        else:
            skipped += 1

    print("\n=== Entity variants ===")
    for alias, canonical in sorted(ENTITY_SEEDS.items()):
        if add_alias(db, canonical, alias, "entity_variant"):
            added += 1
            print(f"  '{alias}' -> '{canonical}'")
        else:
            skipped += 1

    if not args.dry_run:
        db.commit()
        print(f"\nSeeded: {added} aliases ({skipped} already existed)")
    else:
        db.rollback()
        print(f"\nDry run: would seed {added} aliases ({skipped} already exist)")
    db.close()


def cmd_merge(args):
    """Merge one entity into another (moves roles, addresses, relations)."""
    db = get_db()

    keep = db.execute("SELECT * FROM entities WHERE id = ?", (args.keep_id,)).fetchone()
    delete = db.execute("SELECT * FROM entities WHERE id = ?", (args.delete_id,)).fetchone()

    if not keep:
        print(f"ERROR: keep entity {args.keep_id} not found")
        db.close()
        return
    if not delete:
        print(f"ERROR: delete entity {args.delete_id} not found")
        db.close()
        return

    print(f"Merging entity:{args.delete_id} ({delete['name']}) -> entity:{args.keep_id} ({keep['name']})")

    tables = [
        ("entity_roles", "entity_id"),
        ("entity_addresses", "entity_id"),
    ]
    for table, col in tables:
        count = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (args.delete_id,)).fetchone()[0]
        if count:
            if not args.dry_run:
                db.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?", (args.keep_id, args.delete_id))
                db.execute(f"DELETE FROM {table} WHERE {col} = ?", (args.delete_id,))
            print(f"  Moved {count} {table} rows")

    for col in ("entity_a_id", "entity_b_id"):
        count = db.execute(f"SELECT COUNT(*) FROM entity_relations WHERE {col} = ?", (args.delete_id,)).fetchone()[0]
        if count:
            if not args.dry_run:
                db.execute(f"UPDATE OR IGNORE entity_relations SET {col} = ? WHERE {col} = ?", (args.keep_id, args.delete_id))
                db.execute(f"DELETE FROM entity_relations WHERE {col} = ?", (args.delete_id,))
            print(f"  Moved {count} entity_relations ({col})")

    # Update name_aliases pointing to the deleted entity
    alias_count = db.execute("SELECT COUNT(*) FROM name_aliases WHERE entity_id = ?", (args.delete_id,)).fetchone()[0]
    if alias_count:
        if not args.dry_run:
            db.execute("UPDATE name_aliases SET entity_id = ? WHERE entity_id = ?", (args.keep_id, args.delete_id))
        print(f"  Moved {alias_count} name_aliases")

    if not args.dry_run:
        db.execute("DELETE FROM entities WHERE id = ?", (args.delete_id,))
        db.commit()
        print(f"  Deleted entity:{args.delete_id}")
    else:
        print(f"  Would delete entity:{args.delete_id}")

    db.close()


def cmd_stats(args):
    db = get_db()
    _ensure_aliases_table(db)

    total = db.execute("SELECT COUNT(*) FROM name_aliases").fetchone()[0]
    by_type = db.execute(
        "SELECT alias_type, COUNT(*) as cnt FROM name_aliases GROUP BY alias_type ORDER BY alias_type"
    ).fetchall()

    print(f"Total aliases: {total}")
    for row in by_type:
        print(f"  {row['alias_type']}: {row['cnt']}")

    # Check for orphaned entity_ids
    orphans = db.execute("""
        SELECT na.id, na.alias, na.entity_id
        FROM name_aliases na
        LEFT JOIN entities e ON na.entity_id = e.id
        WHERE na.entity_id IS NOT NULL AND e.id IS NULL
    """).fetchall()
    if orphans:
        print(f"\nOrphaned aliases (entity deleted): {len(orphans)}")
        for o in orphans:
            print(f"  alias:{o['id']} '{o['alias']}' -> entity:{o['entity_id']} (missing)")

    # Entity/connection collision stats
    entity_names = {r[0] for r in db.execute("SELECT name FROM entities").fetchall()}
    conn_persons = set()
    for r in db.execute("SELECT DISTINCT person_a FROM connections UNION SELECT DISTINCT person_b FROM connections").fetchall():
        conn_persons.add(r[0])
    collisions = entity_names & conn_persons
    aliased = {r[0] for r in db.execute("SELECT alias FROM name_aliases WHERE alias_type = 'entity_as_person'").fetchall()}
    unresolved = collisions - aliased

    print(f"\nEntity/connection collisions: {len(collisions)} total, {len(aliased)} aliased, {len(unresolved)} unresolved")
    if unresolved:
        for name in sorted(unresolved):
            print(f"  {name}")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="Entity & name deduplication for investigation.db")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add-alias", help="Register a name alias")
    p_add.add_argument("--canonical", required=True, help="Canonical name")
    p_add.add_argument("--alias", required=True, help="Alias to resolve")
    p_add.add_argument("--type", required=True, choices=ALIAS_TYPES, help="Alias type")
    p_add.add_argument("--entity-id", type=int, help="Entity ID (for entity_as_person)")

    p_rm = sub.add_parser("remove-alias", help="Remove a name alias")
    p_rm.add_argument("--alias", required=True, help="Alias to remove")

    p_list = sub.add_parser("list-aliases", help="List aliases")
    p_list.add_argument("--canonical", help="Filter by canonical name")
    p_list.add_argument("--type", choices=ALIAS_TYPES, help="Filter by type")

    p_scan = sub.add_parser("scan", help="Auto-detect probable duplicates")

    p_apply = sub.add_parser("apply", help="Auto-populate entity_as_person aliases")
    p_apply.add_argument("--dry-run", action="store_true")

    p_seed = sub.add_parser("seed", help="Populate known person/entity variant aliases")
    p_seed.add_argument("--dry-run", action="store_true")

    p_merge = sub.add_parser("merge", help="Merge entity records")
    p_merge.add_argument("--keep-id", type=int, required=True)
    p_merge.add_argument("--delete-id", type=int, required=True)
    p_merge.add_argument("--dry-run", action="store_true")

    p_stats = sub.add_parser("stats", help="Alias and collision stats")

    args = parser.parse_args()

    commands = {
        "add-alias": cmd_add_alias,
        "remove-alias": cmd_remove_alias,
        "list-aliases": cmd_list_aliases,
        "scan": cmd_scan,
        "apply": cmd_apply,
        "seed": cmd_seed,
        "merge": cmd_merge,
        "stats": cmd_stats,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
