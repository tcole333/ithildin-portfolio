#!/usr/bin/env python3
"""
Flexible tag/annotation system for OSINT investigations.

Attach analytical metadata to any record (findings, connections, entities, leads)
without changing the fixed schema. Analysis agents use tags to mark patterns,
clusters, themes, and methods.

Part of investigation.db.

Usage:
    python tools/tag_manager.py tag --table findings --id 412 --type pattern --value "dependency_cycle:stage_3"
    python tools/tag_manager.py bulk-tag --table findings --ids 412,413,414 --type cluster --value "karp_nexus"
    python tools/tag_manager.py find --type pattern [--value "dependency*"]
    python tools/tag_manager.py list-values --type theme
    python tools/tag_manager.py record --table findings --id 412
    python tools/tag_manager.py remove --table findings --id 412 --type pattern --value "dependency_cycle:stage_3"
    python tools/tag_manager.py stats
"""

import argparse
import fnmatch
import json
import sqlite3
import sys
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

VALID_TABLES = ["findings", "connections", "entities", "leads", "hypotheses"]
VALID_TAG_TYPES = ["theme", "pattern", "cluster", "method", "geographic", "temporal", "operational", "systemic", "model"]


# ── Schema ────────────────────────────────────────────────────

def _ensure_tag_schema(db):
    """Create tags table if it doesn't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            record_id INTEGER NOT NULL,
            tag_type TEXT NOT NULL,
            tag_value TEXT NOT NULL,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(table_name, record_id, tag_type, tag_value)
        );

        CREATE INDEX IF NOT EXISTS idx_tags_type_value ON tags(tag_type, tag_value);
        CREATE INDEX IF NOT EXISTS idx_tags_record ON tags(table_name, record_id);
    """)


def get_tag_db():
    """Get DB connection with tag schema ensured."""
    db = get_db()
    _ensure_tag_schema(db)
    return db


# ── CRUD ────────────────────────────────────────────────────

def add_tag(table_name, record_id, tag_type, tag_value, created_by=None):
    """Add a tag to a record. Returns True if added, False if duplicate."""
    if table_name not in VALID_TABLES:
        print(f"ERROR: Invalid table '{table_name}'. Valid: {VALID_TABLES}")
        return False
    if tag_type not in VALID_TAG_TYPES:
        print(f"ERROR: Invalid tag_type '{tag_type}'. Valid: {VALID_TAG_TYPES}")
        return False

    db = get_tag_db()
    try:
        db.execute("""
            INSERT INTO tags (table_name, record_id, tag_type, tag_value, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (table_name, record_id, tag_type, tag_value, created_by))
        db.commit()
        db.close()
        return True
    except sqlite3.IntegrityError:
        db.close()
        return False  # duplicate


def bulk_tag(table_name, record_ids, tag_type, tag_value, created_by=None):
    """Tag multiple records at once. Returns count of tags added."""
    if table_name not in VALID_TABLES:
        print(f"ERROR: Invalid table '{table_name}'. Valid: {VALID_TABLES}")
        return 0
    if tag_type not in VALID_TAG_TYPES:
        print(f"ERROR: Invalid tag_type '{tag_type}'. Valid: {VALID_TAG_TYPES}")
        return 0

    db = get_tag_db()
    added = 0
    for rid in record_ids:
        try:
            db.execute("""
                INSERT INTO tags (table_name, record_id, tag_type, tag_value, created_by)
                VALUES (?, ?, ?, ?, ?)
            """, (table_name, int(rid), tag_type, tag_value, created_by))
            added += 1
        except sqlite3.IntegrityError:
            pass  # duplicate, skip
    db.commit()
    db.close()
    return added


def find_tags(tag_type=None, tag_value=None, table_name=None, limit=100):
    """Find tags matching criteria. tag_value supports glob patterns (*)."""
    db = get_tag_db()
    conditions = []
    params = []

    if tag_type:
        conditions.append("tag_type = ?")
        params.append(tag_type)
    if table_name:
        conditions.append("table_name = ?")
        params.append(table_name)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM tags {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    results = [dict(r) for r in rows]
    db.close()

    # Apply glob filter on tag_value (SQLite LIKE can't do glob patterns well)
    if tag_value and "*" in tag_value:
        results = [r for r in results if fnmatch.fnmatch(r["tag_value"], tag_value)]
    elif tag_value:
        results = [r for r in results if r["tag_value"] == tag_value]

    return results


def list_tag_values(tag_type=None, table_name=None):
    """List distinct tag values, optionally filtered by type."""
    db = get_tag_db()
    conditions = []
    params = []

    if tag_type:
        conditions.append("tag_type = ?")
        params.append(tag_type)
    if table_name:
        conditions.append("table_name = ?")
        params.append(table_name)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT tag_type, tag_value, COUNT(*) as count
        FROM tags {where}
        GROUP BY tag_type, tag_value
        ORDER BY tag_type, count DESC
    """
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def record_tags(table_name, record_id):
    """Get all tags on a specific record."""
    db = get_tag_db()
    rows = db.execute("""
        SELECT * FROM tags WHERE table_name = ? AND record_id = ?
        ORDER BY tag_type, tag_value
    """, (table_name, record_id)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def remove_tag(table_name, record_id, tag_type, tag_value):
    """Remove a specific tag from a record."""
    db = get_tag_db()
    cursor = db.execute("""
        DELETE FROM tags WHERE table_name = ? AND record_id = ? AND tag_type = ? AND tag_value = ?
    """, (table_name, record_id, tag_type, tag_value))
    db.commit()
    removed = cursor.rowcount
    db.close()
    return removed > 0


def tag_stats():
    """Get tag statistics."""
    db = get_tag_db()
    stats = {}

    total = db.execute("SELECT COUNT(*) as n FROM tags").fetchone()["n"]
    stats["total"] = total

    # By type
    by_type = {}
    for row in db.execute("SELECT tag_type, COUNT(*) as n FROM tags GROUP BY tag_type ORDER BY n DESC"):
        by_type[row["tag_type"]] = row["n"]
    stats["by_type"] = by_type

    # By table
    by_table = {}
    for row in db.execute("SELECT table_name, COUNT(*) as n FROM tags GROUP BY table_name ORDER BY n DESC"):
        by_table[row["table_name"]] = row["n"]
    stats["by_table"] = by_table

    # Unique values
    stats["unique_values"] = db.execute(
        "SELECT COUNT(DISTINCT tag_value) as n FROM tags"
    ).fetchone()["n"]

    # Unique records tagged
    stats["unique_records"] = db.execute(
        "SELECT COUNT(DISTINCT table_name || ':' || record_id) as n FROM tags"
    ).fetchone()["n"]

    db.close()
    return stats


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tag/annotation system for investigation records")
    sub = parser.add_subparsers(dest="command")

    # tag
    p_tag = sub.add_parser("tag", help="Add a tag to a record")
    p_tag.add_argument("--table", required=True, choices=VALID_TABLES)
    p_tag.add_argument("--id", type=int, required=True)
    p_tag.add_argument("--type", required=True, choices=VALID_TAG_TYPES)
    p_tag.add_argument("--value", required=True)
    p_tag.add_argument("--created-by")

    # bulk-tag
    p_bulk = sub.add_parser("bulk-tag", help="Tag multiple records")
    p_bulk.add_argument("--table", required=True, choices=VALID_TABLES)
    p_bulk.add_argument("--ids", required=True, help="Comma-separated record IDs")
    p_bulk.add_argument("--type", required=True, choices=VALID_TAG_TYPES)
    p_bulk.add_argument("--value", required=True)
    p_bulk.add_argument("--created-by")

    # find
    p_find = sub.add_parser("find", help="Find tags by type/value")
    p_find.add_argument("--type", choices=VALID_TAG_TYPES)
    p_find.add_argument("--value", help="Tag value (supports * glob)")
    p_find.add_argument("--table", choices=VALID_TABLES)
    p_find.add_argument("--limit", type=int, default=100)
    add_output_args(p_find)

    # list-values
    p_vals = sub.add_parser("list-values", help="List distinct tag values")
    p_vals.add_argument("--type", choices=VALID_TAG_TYPES)
    p_vals.add_argument("--table", choices=VALID_TABLES)
    add_output_args(p_vals)

    # record
    p_rec = sub.add_parser("record", help="Show all tags on a record")
    p_rec.add_argument("--table", required=True, choices=VALID_TABLES)
    p_rec.add_argument("--id", type=int, required=True)
    add_output_args(p_rec)

    # remove
    p_rem = sub.add_parser("remove", help="Remove a specific tag")
    p_rem.add_argument("--table", required=True, choices=VALID_TABLES)
    p_rem.add_argument("--id", type=int, required=True)
    p_rem.add_argument("--type", required=True, choices=VALID_TAG_TYPES)
    p_rem.add_argument("--value", required=True)

    # stats
    sub.add_parser("stats", help="Show tag statistics")

    args = parser.parse_args()

    if args.command == "tag":
        if add_tag(args.table, args.id, args.type, args.value, args.created_by):
            print(f"Tagged {args.table}#{args.id} with {args.type}={args.value}")
        else:
            print(f"Tag already exists (or error)")

    elif args.command == "bulk-tag":
        ids = [int(x.strip()) for x in args.ids.split(",")]
        added = bulk_tag(args.table, ids, args.type, args.value, args.created_by)
        print(f"Tagged {added}/{len(ids)} {args.table} records with {args.type}={args.value}")

    elif args.command == "find":
        results = find_tags(tag_type=args.type, tag_value=args.value,
                            table_name=args.table, limit=args.limit)
        if write_output(results, args, summary=f"tags found ({len(results)})"):
            return
        if not results:
            print("No tags found.")
            return
        print(f"Tags ({len(results)}):")
        for t in results:
            print(f"  {t['table_name']}#{t['record_id']:>5}  {t['tag_type']:<12} {t['tag_value']}"
                  f"  ({t['created_by'] or 'manual'})")

    elif args.command == "list-values":
        results = list_tag_values(tag_type=args.type, table_name=args.table)
        if write_output(results, args, summary=f"tag values ({len(results)})"):
            return
        if not results:
            print("No tags found.")
            return
        print(f"Tag values ({len(results)}):")
        current_type = None
        for r in results:
            if r["tag_type"] != current_type:
                current_type = r["tag_type"]
                print(f"\n  [{current_type}]")
            print(f"    {r['tag_value']:<40} ({r['count']} records)")

    elif args.command == "record":
        results = record_tags(args.table, args.id)
        if write_output(results, args, summary=f"tags on {args.table}#{args.id}"):
            return
        if not results:
            print(f"No tags on {args.table}#{args.id}")
            return
        print(f"Tags on {args.table}#{args.id} ({len(results)}):")
        for t in results:
            print(f"  {t['tag_type']:<12} {t['tag_value']}  ({t['created_by'] or 'manual'}, {t['created_at']})")

    elif args.command == "remove":
        if remove_tag(args.table, args.id, args.type, args.value):
            print(f"Removed tag {args.type}={args.value} from {args.table}#{args.id}")
        else:
            print(f"Tag not found")

    elif args.command == "stats":
        s = tag_stats()
        print("Tag Statistics")
        print("=" * 40)
        print(f"  Total tags:      {s['total']}")
        print(f"  Unique values:   {s['unique_values']}")
        print(f"  Records tagged:  {s['unique_records']}")
        if s["by_type"]:
            print(f"\nBy type:")
            for t, n in s["by_type"].items():
                print(f"  {t:<14} {n}")
        if s["by_table"]:
            print(f"\nBy table:")
            for t, n in s["by_table"].items():
                print(f"  {t:<14} {n}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
