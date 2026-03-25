#!/usr/bin/env python3
"""
Query LMSBAND Epstein Files database (60,806 files, 851K entities, 110K co-occurrences).

Database: datasets/lmsband_epstein_files.db

Usage:
    python tools/query_lmsband.py search "rod-larsen" --limit 20
    python tools/query_lmsband.py entities "Rod-Larsen" --min-count 3
    python tools/query_lmsband.py cooccurrence "Rod-Larsen" --top 20
    python tools/query_lmsband.py file 12345
    python tools/query_lmsband.py stats
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

DB_PATH = Path(__file__).parent.parent / "datasets" / "lmsband_epstein_files.db"


def get_db():
    if not DB_PATH.exists():
        print(f"ERROR: LMSBAND database not found at {DB_PATH}")
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def _has_fts(db):
    """Check if FTS5 index exists."""
    row = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'text_fts'"
    ).fetchone()
    return row[0] > 0


def _fts_query(query):
    """Convert a natural language query to FTS5 syntax.

    Handles phrase queries (quoted strings pass through) and
    multi-word queries (converted to AND terms).
    """
    query = query.strip()
    if query.startswith('"') and query.endswith('"'):
        return query
    # Multi-word: join with AND for FTS5
    words = query.split()
    if len(words) > 1:
        return " AND ".join(words)
    return query


def text_search(query, limit=20, dataset=None):
    """Search extracted text content using FTS5 (with LIKE fallback)."""
    db = get_db()

    if _has_fts(db):
        fts_q = _fts_query(query)
        ds_filter = ""
        params = [fts_q, limit]
        if dataset:
            ds_filter = "AND dataset = ?"
            params = [fts_q, int(dataset), limit]
        try:
            sql = f"""
                SELECT
                    rowid as file_id,
                    filename,
                    dataset,
                    snippet(text_fts, 2, '>>>', '<<<', '...', 50) as context,
                    rank
                FROM text_fts
                WHERE text_fts MATCH ?
                {ds_filter}
                ORDER BY rank
                LIMIT ?
            """
            rows = db.execute(sql, params).fetchall()
            # Enrich with char_count and method from source tables
            results = []
            for r in rows:
                row = dict(r)
                meta = db.execute(
                    "SELECT char_count, method FROM text_cache WHERE file_id = ?",
                    (row["file_id"],)
                ).fetchone()
                if meta:
                    row["char_count"] = meta[0]
                    row["method"] = meta[1]
                else:
                    row["char_count"] = 0
                    row["method"] = "?"
                results.append(row)
            db.close()
            return results
        except Exception:
            pass  # Fall through to LIKE

    # Fallback: LIKE search (slow but works without FTS)
    ds_clause = ""
    params = [query, f"%{query.lower()}%", limit]
    if dataset:
        ds_clause = "AND f.dataset = ?"
        params = [query, f"%{query.lower()}%", int(dataset), limit]
    rows = db.execute(f"""
        SELECT
            tc.file_id,
            f.filename,
            f.dataset,
            tc.char_count,
            tc.method,
            SUBSTR(tc.extracted_text,
                   MAX(1, INSTR(LOWER(tc.extracted_text), LOWER(?)) - 100),
                   300) as context
        FROM text_cache tc
        JOIN files f ON f.id = tc.file_id
        WHERE LOWER(tc.extracted_text) LIKE ?
        {ds_clause}
        ORDER BY tc.char_count DESC
        LIMIT ?
    """, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def entity_search(name, min_count=1, limit=50):
    """Search entities by name."""
    db = get_db()
    rows = db.execute("""
        SELECT entity_text, entity_label, normalized, SUM(count) as total_count,
               COUNT(DISTINCT file_id) as file_count
        FROM entities
        WHERE LOWER(entity_text) LIKE ? OR LOWER(normalized) LIKE ?
        GROUP BY COALESCE(normalized, entity_text)
        HAVING total_count >= ?
        ORDER BY total_count DESC
        LIMIT ?
    """, (f"%{name.lower()}%", f"%{name.lower()}%", min_count, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def cooccurrence(entity_name, top=20):
    """Get entities that co-occur with the given entity."""
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
            file_count
        FROM entity_cooccurrence
        WHERE LOWER(entity_a) LIKE ? OR LOWER(entity_b) LIKE ?
        ORDER BY file_count DESC
        LIMIT ?
    """, (f"%{entity_name.lower()}%", f"%{entity_name.lower()}%",
          f"%{entity_name.lower()}%", f"%{entity_name.lower()}%", top)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_file(file_id):
    """Get file details and extracted text."""
    db = get_db()
    file_row = db.execute(
        "SELECT * FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if not file_row:
        db.close()
        return None

    text_row = db.execute(
        "SELECT * FROM text_cache WHERE file_id = ?", (file_id,)
    ).fetchone()

    entities = db.execute(
        "SELECT entity_text, entity_label, count FROM entities WHERE file_id = ? ORDER BY count DESC LIMIT 30",
        (file_id,)
    ).fetchall()

    result = dict(file_row)
    result["text"] = dict(text_row) if text_row else None
    result["entities"] = [dict(e) for e in entities]
    db.close()
    return result


def get_stats():
    """Get database statistics."""
    db = get_db()
    stats = {}
    stats["files"] = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    stats["text_cached"] = db.execute("SELECT COUNT(*) FROM text_cache WHERE char_count > 0").fetchone()[0]
    stats["entities"] = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    stats["unique_entities"] = db.execute("SELECT COUNT(DISTINCT COALESCE(normalized, entity_text)) FROM entities").fetchone()[0]
    stats["cooccurrences"] = db.execute("SELECT COUNT(*) FROM entity_cooccurrence").fetchone()[0]

    # Top entity labels
    rows = db.execute("""
        SELECT entity_label, COUNT(*) as cnt
        FROM entities
        GROUP BY entity_label
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    stats["top_labels"] = {r["entity_label"]: r["cnt"] for r in rows}

    db.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Query LMSBAND Epstein Files (60K files, 851K entities)")
    subparsers = parser.add_subparsers(dest="command")

    # search
    s = subparsers.add_parser("search", help="Text search across extracted content")
    s.add_argument("query", help="Search query")
    s.add_argument("--limit", "-n", type=int, default=20)
    s.add_argument("--dataset", "-d", type=int, help="Filter by dataset number (1-12)")
    s.add_argument("-j", "--json", action="store_true")
    add_output_args(s)

    # entities
    e = subparsers.add_parser("entities", help="Search entities")
    e.add_argument("name", help="Entity name")
    e.add_argument("--min-count", type=int, default=1)
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("-j", "--json", action="store_true")
    add_output_args(e)

    # cooccurrence
    c = subparsers.add_parser("cooccurrence", help="Entity co-occurrence")
    c.add_argument("entity", help="Entity name")
    c.add_argument("--top", type=int, default=20)
    c.add_argument("-j", "--json", action="store_true")
    add_output_args(c)

    # file
    f = subparsers.add_parser("file", help="Get file details")
    f.add_argument("file_id", type=int)
    f.add_argument("-j", "--json", action="store_true")
    f.add_argument("--text", action="store_true", help="Show full text")

    # stats
    subparsers.add_parser("stats", help="Database statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        results = text_search(args.query, limit=args.limit, dataset=args.dataset)
        if write_output(results, args, summary=f"LMSBAND search '{args.query}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            ds_label = f" [DS{args.dataset}]" if args.dataset else ""
            print(f"\nLMSBAND text search: '{args.query}'{ds_label} — {len(results)} results")
            print("=" * 70)
            for i, r in enumerate(results, 1):
                ds = r.get("dataset", "?")
                chars = r.get("char_count", 0)
                method = r.get("method", "?")
                print(f"\n[{i}] File #{r['file_id']}: {r['filename']} (DS{ds}, {chars} chars, {method})")
                ctx = r.get("context", "")
                if ctx:
                    print(f"    ...{ctx.strip()[:300]}...")

    elif args.command == "entities":
        results = entity_search(args.name, min_count=args.min_count, limit=args.limit)
        if write_output(results, args, summary=f"LMSBAND entities '{args.name}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nLMSBAND entities matching '{args.name}': {len(results)}")
            print("=" * 70)
            for r in results:
                label = r.get("entity_label", "?")
                print(f"  {r['entity_text']:<50} [{label}] count={r['total_count']} files={r['file_count']}")

    elif args.command == "cooccurrence":
        results = cooccurrence(args.entity, top=args.top)
        if write_output(results, args, summary=f"LMSBAND cooccurrence '{args.entity}'"):
            pass
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\nEntities co-occurring with '{args.entity}': {len(results)}")
            print("=" * 70)
            for r in results:
                label = r.get("co_label", "?")
                print(f"  {r['co_entity']:<50} [{label}] in {r['file_count']} files")

    elif args.command == "file":
        result = get_file(args.file_id)
        if not result:
            print(f"File #{args.file_id} not found.")
            sys.exit(1)
        if args.json:
            if not args.text and result.get("text"):
                result["text"].pop("extracted_text", None)
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"\nFile #{result['id']}: {result['filename']}")
            print(f"  Path: {result['rel_path']}")
            print(f"  Size: {result.get('file_size', 0)} bytes")
            if result.get("text"):
                print(f"  Text: {result['text']['char_count']} chars ({result['text']['method']})")
                if args.text:
                    print(f"\n--- Text ---\n{result['text'].get('extracted_text', '')[:5000]}")
            if result.get("entities"):
                print(f"\n  Top entities:")
                for e in result["entities"][:15]:
                    print(f"    {e['entity_text']:<40} [{e['entity_label']}] x{e['count']}")

    elif args.command == "stats":
        stats = get_stats()
        print(f"LMSBAND Database Statistics:")
        print(f"  Files: {stats['files']}")
        print(f"  Text cached: {stats['text_cached']}")
        print(f"  Entity mentions: {stats['entities']}")
        print(f"  Unique entities: {stats['unique_entities']}")
        print(f"  Co-occurrences: {stats['cooccurrences']}")
        print(f"\n  Entity labels:")
        for label, cnt in stats["top_labels"].items():
            print(f"    {label}: {cnt}")


if __name__ == "__main__":
    main()
