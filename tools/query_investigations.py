#!/usr/bin/env python3
"""
Query ingested investigation reports (FTS5 full-text search).

Database: datasets/investigations.db (created by ingest_pdf.py)

Usage:
    python tools/query_investigations.py search "BCCI" --limit 20
    python tools/query_investigations.py search "Deutsche Bank" --category enforcement
    python tools/query_investigations.py list
    python tools/query_investigations.py read <doc_id> --pages 5-10
    python tools/query_investigations.py stats
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

DB_PATH = Path(__file__).parent.parent / "datasets" / "investigations.db"


def get_db():
    """Connect to investigations database."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        print("Run 'python tools/ingest_pdf.py ingest <pdf>' first.", file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def cmd_search(args):
    """FTS5 search across all ingested investigation reports."""
    db = get_db()

    query_parts = [args.query]
    params = [args.query, args.limit]

    # Base search query with snippets
    sql = """
        SELECT
            p.document_id,
            p.page_number,
            d.title,
            d.source,
            d.year,
            d.category,
            snippet(pages_fts, 0, '>>>', '<<<', '...', 64) as snippet
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        JOIN documents d ON d.id = p.document_id
        WHERE pages_fts MATCH ?
    """

    if args.category:
        sql += " AND d.category = ?"
        params = [args.query, args.category, args.limit]
        sql += " ORDER BY rank LIMIT ?"
    else:
        sql += " ORDER BY rank LIMIT ?"

    if args.category:
        rows = db.execute(sql, [args.query, args.category, args.limit]).fetchall()
    else:
        rows = db.execute(sql, [args.query, args.limit]).fetchall()

    data = [dict(r) for r in rows]

    # Check write_output FIRST
    if write_output(data, args, summary=f"investigations search '{args.query}'"):
        return

    # Then check json_out
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Finally do pretty-print
    print(f"Investigation reports search: '{args.query}' — {len(rows)} page matches")
    if args.category:
        print(f"  Category filter: {args.category}")
    print()

    # Group by document
    by_doc = {}
    for r in rows:
        doc_id = r["document_id"]
        if doc_id not in by_doc:
            by_doc[doc_id] = {
                "title": r["title"],
                "source": r["source"],
                "year": r["year"],
                "category": r["category"],
                "pages": [],
            }
        by_doc[doc_id]["pages"].append({
            "page": r["page_number"],
            "snippet": r["snippet"],
        })

    for doc_id, info in by_doc.items():
        year_str = f" ({info['year']})" if info['year'] else ""
        source_str = f" [{info['source']}]" if info['source'] else ""
        print(f"Doc #{doc_id}: {info['title']}{year_str}{source_str}")
        print(f"  Category: {info['category'] or '?'} | {len(info['pages'])} page matches")
        for p in info["pages"][:5]:
            snippet = p["snippet"][:300] if p["snippet"] else ""
            print(f"  p.{p['page']}: {snippet}")
        if len(info["pages"]) > 5:
            print(f"  ... and {len(info['pages']) - 5} more pages")
        print()


def cmd_list(args):
    """List all ingested documents."""
    db = get_db()
    rows = db.execute("""
        SELECT d.*, COUNT(p.id) as page_count
        FROM documents d
        LEFT JOIN pages p ON p.document_id = d.id
        GROUP BY d.id
        ORDER BY d.id
    """).fetchall()

    data = [dict(r) for r in rows]

    # Check write_output FIRST
    if write_output(data, args, summary="investigations document listing"):
        return

    # Then check json_out
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Finally do pretty-print
    if not rows:
        print("No documents ingested yet.")
        return

    print(f"{'ID':>4}  {'Year':>4}  {'Pages':>5}  {'Category':<14}  {'Source':<12}  Title")
    print("-" * 90)
    for r in rows:
        print(f"{r['id']:>4}  {r['year'] or '?':>4}  {r['page_count']:>5}  "
              f"{(r['category'] or '?'):<14}  {(r['source'] or '?'):<12}  {r['title']}")


def cmd_read(args):
    """Read pages from an ingested document."""
    db = get_db()

    doc = db.execute("SELECT * FROM documents WHERE id = ?", [args.doc_id]).fetchone()
    if not doc:
        print(f"Document #{args.doc_id} not found.")
        sys.exit(1)

    query_params = [args.doc_id]
    page_filter = ""
    if args.pages:
        if "-" in args.pages:
            start, end = args.pages.split("-", 1)
            page_filter = " AND page_number BETWEEN ? AND ?"
            query_params.extend([int(start), int(end)])
        else:
            page_filter = " AND page_number = ?"
            query_params.append(int(args.pages))

    rows = db.execute(f"""
        SELECT page_number, text FROM pages
        WHERE document_id = ?{page_filter}
        ORDER BY page_number
    """, query_params).fetchall()

    data = {
        "document_id": doc["id"],
        "title": doc["title"],
        "source": doc["source"],
        "year": doc["year"],
        "category": doc["category"],
        "source_url": doc["source_url"],
        "total_pages": doc["total_pages"],
        "pages": [dict(r) for r in rows],
    }

    # Check write_output FIRST
    if write_output(data, args, summary=f"document #{args.doc_id}"):
        return

    # Then check json_out
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Finally do pretty-print
    print(f"=== {doc['title']} ===")
    print(f"Source: {doc['source'] or '?'} | Year: {doc['year'] or '?'} | "
          f"Pages: {doc['total_pages']} | Category: {doc['category'] or '?'}")
    if doc['source_url']:
        print(f"URL: {doc['source_url']}")
    print()

    for r in rows:
        print(f"--- Page {r['page_number']} ---")
        print(r['text'])
        print()


def cmd_stats(args):
    """Show database statistics."""
    db = get_db()

    doc_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    page_count = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    total_chars = db.execute("SELECT COALESCE(SUM(LENGTH(text)), 0) FROM pages").fetchone()[0]

    cats = []
    sources = []
    if doc_count > 0:
        cats = [dict(r) for r in db.execute("""
            SELECT category, COUNT(*) as cnt FROM documents
            GROUP BY category ORDER BY cnt DESC
        """).fetchall()]
        sources = [dict(r) for r in db.execute("""
            SELECT source, COUNT(*) as cnt FROM documents
            GROUP BY source ORDER BY cnt DESC
        """).fetchall()]

    data = {
        "database": str(DB_PATH),
        "doc_count": doc_count,
        "page_count": page_count,
        "total_chars": total_chars,
        "total_mb": round(total_chars / 1024 / 1024, 1),
        "by_category": cats,
        "by_source": sources,
    }

    # Check write_output FIRST
    if write_output(data, args, summary="investigations database statistics"):
        return

    # Then check json_out
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Finally do pretty-print
    print(f"Investigations DB: {DB_PATH}")
    print(f"  Documents: {doc_count}")
    print(f"  Pages: {page_count:,}")
    print(f"  Total text: {total_chars:,} characters ({total_chars / 1024 / 1024:.1f} MB)")

    if doc_count > 0:
        print()
        print("  By category:")
        for c in cats:
            print(f"    {c['category'] or 'uncategorized'}: {c['cnt']}")

        print("  By source:")
        for s in sources:
            print(f"    {s['source'] or 'unknown'}: {s['cnt']}")


def main():
    parser = argparse.ArgumentParser(description="Search ingested investigation reports")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Full-text search")
    p.add_argument("query", help="FTS5 search query")
    p.add_argument("--category", help="Filter by category")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    # list
    p = sub.add_parser("list", help="List ingested documents")
    add_output_args(p)

    # read
    p = sub.add_parser("read", help="Read pages from a document")
    p.add_argument("doc_id", type=int, help="Document ID")
    p.add_argument("--pages", help="Page range (e.g., 5-10)")
    add_output_args(p)

    # stats
    p = sub.add_parser("stats", help="Show statistics")
    add_output_args(p)

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "list": cmd_list,
        "read": cmd_read,
        "stats": cmd_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
