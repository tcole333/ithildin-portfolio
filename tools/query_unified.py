#!/usr/bin/env python3
"""
Query the unified Epstein database (70K docs, 56K entities, FTS5).

Database: datasets/unified_epstein.db

Usage:
    python tools/query_unified.py emails "rod-larsen" --limit 20
    python tools/query_unified.py docs "gates foundation" --limit 20
    python tools/query_unified.py entities "Rod-Larsen" --limit 30
    python tools/query_unified.py triples --actor "Epstein" --target "Gates"
    python tools/query_unified.py cooccurrence "Rod-Larsen" --top 20
    python tools/query_unified.py stats
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DB_PATH = Path(__file__).parent.parent / "datasets" / "unified_epstein.db"


def get_db():
    if not DB_PATH.exists():
        print(f"ERROR: Unified database not found at {DB_PATH}")
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def search_emails(query, limit=20):
    """FTS5 search across emails."""
    db = get_db()
    rows = db.execute("""
        SELECT
            e.id, e.source_dataset, e.from_address, e.to_address,
            e.subject, e.timestamp_iso,
            snippet(emails_fts, 3, '>>>', '<<<', '...', 64) as snippet
        FROM emails_fts
        JOIN emails e ON e.id = emails_fts.rowid
        WHERE emails_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def search_docs(query, limit=20):
    """FTS5 search across documents."""
    db = get_db()
    rows = db.execute("""
        SELECT
            d.id, d.source_dataset, d.doc_id, d.category,
            d.date_earliest, d.date_latest,
            snippet(documents_fts, 2, '>>>', '<<<', '...', 64) as snippet
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def search_entities(name, limit=30):
    """Search entities by name."""
    db = get_db()
    rows = db.execute("""
        SELECT canonical_name, hop_distance, source, COUNT(*) as mentions
        FROM entities
        WHERE LOWER(canonical_name) LIKE ?
        GROUP BY canonical_name
        ORDER BY mentions DESC
        LIMIT ?
    """, (f"%{name.lower()}%", limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def search_triples(actor=None, action=None, target=None, topic=None, limit=30):
    """Search RDF triples (actor -> action -> target)."""
    db = get_db()
    conditions = []
    params = []

    if actor:
        conditions.append("LOWER(actor) LIKE ?")
        params.append(f"%{actor.lower()}%")
    if action:
        conditions.append("LOWER(action) LIKE ?")
        params.append(f"%{action.lower()}%")
    if target:
        conditions.append("LOWER(target) LIKE ?")
        params.append(f"%{target.lower()}%")
    if topic:
        conditions.append("(LOWER(explicit_topic) LIKE ? OR LOWER(implicit_topic) LIKE ?)")
        params.extend([f"%{topic.lower()}%", f"%{topic.lower()}%"])

    if not conditions:
        print("At least one filter (--actor, --action, --target, --topic) required.")
        sys.exit(1)

    where = " AND ".join(conditions)
    rows = db.execute(f"""
        SELECT actor, action, target, location, timestamp,
               explicit_topic, implicit_topic, doc_id
        FROM triples
        WHERE {where}
        ORDER BY timestamp
        LIMIT ?
    """, params + [limit]).fetchall()
    db.close()
    return [dict(r) for r in rows]


def cooccurrence(entity_name, top=20):
    """Get co-occurring entities."""
    db = get_db()
    rows = db.execute("""
        SELECT
            CASE
                WHEN LOWER(entity_a) LIKE ? THEN entity_b
                ELSE entity_a
            END as co_entity,
            CASE
                WHEN LOWER(entity_a) LIKE ? THEN label_b
                ELSE label_a
            END as co_label,
            file_count,
            source
        FROM entity_cooccurrence
        WHERE LOWER(entity_a) LIKE ? OR LOWER(entity_b) LIKE ?
        ORDER BY file_count DESC
        LIMIT ?
    """, (f"%{entity_name.lower()}%", f"%{entity_name.lower()}%",
          f"%{entity_name.lower()}%", f"%{entity_name.lower()}%", top)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_stats():
    """Database statistics."""
    db = get_db()
    stats = {}
    stats["emails"] = db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    stats["documents"] = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    stats["entities"] = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    stats["triples"] = db.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
    stats["cooccurrences"] = db.execute("SELECT COUNT(*) FROM entity_cooccurrence").fetchone()[0]

    # Source dataset breakdown
    rows = db.execute("SELECT source_dataset, COUNT(*) as cnt FROM emails GROUP BY source_dataset").fetchall()
    stats["emails_by_source"] = {r["source_dataset"]: r["cnt"] for r in rows}

    rows = db.execute("SELECT source_dataset, COUNT(*) as cnt FROM documents GROUP BY source_dataset").fetchall()
    stats["docs_by_source"] = {r["source_dataset"]: r["cnt"] for r in rows}

    db.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Query unified Epstein database (70K docs, 56K entities)")
    subparsers = parser.add_subparsers(dest="command")

    # emails
    e = subparsers.add_parser("emails", help="FTS search emails")
    e.add_argument("query")
    e.add_argument("--limit", "-n", type=int, default=20)
    e.add_argument("-j", "--json", action="store_true")
    add_output_args(e)

    # docs
    d = subparsers.add_parser("docs", help="FTS search documents")
    d.add_argument("query")
    d.add_argument("--limit", "-n", type=int, default=20)
    d.add_argument("-j", "--json", action="store_true")
    add_output_args(d)

    # entities
    ent = subparsers.add_parser("entities", help="Search entities")
    ent.add_argument("name")
    ent.add_argument("--limit", type=int, default=30)
    ent.add_argument("-j", "--json", action="store_true")
    add_output_args(ent)

    # triples
    t = subparsers.add_parser("triples", help="Search RDF triples")
    t.add_argument("--actor", "-a")
    t.add_argument("--action")
    t.add_argument("--target", "-t")
    t.add_argument("--topic")
    t.add_argument("--limit", type=int, default=30)
    t.add_argument("-j", "--json", action="store_true")
    add_output_args(t)

    # cooccurrence
    co = subparsers.add_parser("cooccurrence", help="Entity co-occurrence")
    co.add_argument("entity")
    co.add_argument("--top", type=int, default=20)
    co.add_argument("-j", "--json", action="store_true")
    add_output_args(co)

    # stats
    subparsers.add_parser("stats", help="Database statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "emails":
        results = search_emails(args.query, limit=args.limit)
        if write_output(results, args, summary=f"unified emails '{args.query}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nUnified DB email search: '{args.query}' — {len(results)} results")
            print("=" * 70)
            for i, r in enumerate(results, 1):
                print(f"\n[{i}] {r.get('timestamp_iso', '?')} | {r['from_address']} -> {r['to_address']}")
                print(f"    Subject: {r['subject']}")
                print(f"    Source: {r['source_dataset']}")
                if r.get("snippet"):
                    print(f"    {r['snippet'][:300]}")

    elif args.command == "docs":
        results = search_docs(args.query, limit=args.limit)
        if write_output(results, args, summary=f"unified docs '{args.query}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nUnified DB doc search: '{args.query}' — {len(results)} results")
            print("=" * 70)
            for i, r in enumerate(results, 1):
                dates = f"{r.get('date_earliest','?')} - {r.get('date_latest','?')}"
                print(f"\n[{i}] {r['doc_id']} [{r.get('category','?')}] ({dates})")
                print(f"    Source: {r['source_dataset']}")
                if r.get("snippet"):
                    print(f"    {r['snippet'][:300]}")

    elif args.command == "entities":
        results = search_entities(args.name, limit=args.limit)
        if write_output(results, args, summary=f"unified entities '{args.name}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nUnified DB entities matching '{args.name}': {len(results)}")
            for r in results:
                print(f"  {r['canonical_name']:<50} hop={r['hop_distance']} mentions={r['mentions']} src={r['source']}")

    elif args.command == "triples":
        results = search_triples(
            actor=args.actor, action=args.action,
            target=args.target, topic=args.topic, limit=args.limit
        )
        if write_output(results, args, summary="unified triples"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nTriples: {len(results)}")
            for r in results:
                ts = r.get("timestamp", "?")
                loc = f" @ {r['location']}" if r.get("location") else ""
                print(f"  [{ts}] {r['actor']} -> {r['action']} -> {r['target']}{loc}")
                if r.get("explicit_topic"):
                    print(f"         topic: {r['explicit_topic']}")

    elif args.command == "cooccurrence":
        results = cooccurrence(args.entity, top=args.top)
        if write_output(results, args, summary=f"unified cooccurrence '{args.entity}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nCo-occurring entities for '{args.entity}': {len(results)}")
            for r in results:
                print(f"  {r['co_entity']:<50} [{r.get('co_label','?')}] in {r['file_count']} files ({r.get('source','?')})")

    elif args.command == "stats":
        stats = get_stats()
        print("Unified Database Statistics:")
        print(f"  Emails: {stats['emails']}")
        print(f"  Documents: {stats['documents']}")
        print(f"  Entities: {stats['entities']}")
        print(f"  Triples: {stats['triples']}")
        print(f"  Co-occurrences: {stats['cooccurrences']}")
        if stats.get("emails_by_source"):
            print(f"\n  Emails by source:")
            for src, cnt in stats["emails_by_source"].items():
                print(f"    {src}: {cnt}")
        if stats.get("docs_by_source"):
            print(f"\n  Docs by source:")
            for src, cnt in stats["docs_by_source"].items():
                print(f"    {src}: {cnt}")


if __name__ == "__main__":
    main()
