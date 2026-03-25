#!/usr/bin/env python3
"""
PDF ingestion pipeline for investigation reports.

Extracts text from PDFs using PyMuPDF (fitz) and indexes pages into
datasets/investigations.db with FTS5 full-text search.

Usage:
    python tools/ingest_pdf.py ingest <path.pdf> --title "..." --source "GPO" --category congressional --year 1992
    python tools/ingest_pdf.py ingest-dir datasets/investigation_reports/
    python tools/ingest_pdf.py list
    python tools/ingest_pdf.py read <doc_id> --pages 5-10
    python tools/ingest_pdf.py stats
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: uv pip install pymupdf", file=sys.stderr)
    sys.exit(1)

DB_PATH = Path(__file__).parent.parent / "datasets" / "investigations.db"

CATEGORIES = [
    "congressional", "enforcement", "court_order", "intelligence",
    "forensic", "regulatory", "legislative", "academic", "other",
]


def get_db():
    """Get or create the investigations database."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT,
            source_url TEXT,
            category TEXT,
            year INTEGER,
            total_pages INTEGER,
            file_path TEXT,
            file_hash TEXT,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY,
            document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER,
            text TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(document_id, page_number);
    """)

    # Create FTS table if not exists
    try:
        db.execute("SELECT * FROM pages_fts LIMIT 0")
    except sqlite3.OperationalError:
        db.executescript("""
            CREATE VIRTUAL TABLE pages_fts USING fts5(
                text,
                content=pages,
                content_rowid=id,
                tokenize='porter unicode61'
            );
        """)

    db.commit()
    return db


def _rebuild_fts(db):
    """Rebuild the FTS index from pages table."""
    db.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")
    db.commit()


def _file_hash(path):
    """Quick hash of file size + first 4KB for dedup."""
    import hashlib
    p = Path(path)
    h = hashlib.md5()
    h.update(str(p.stat().st_size).encode())
    with open(p, "rb") as f:
        h.update(f.read(4096))
    return h.hexdigest()


def extract_pdf(path):
    """Extract text from a PDF, returning list of (page_num, text) tuples."""
    doc = fitz.open(str(path))
    pages = []
    for i, page in enumerate(doc, 1):
        text = page.get_text()
        if text.strip():
            pages.append((i, text))
    doc.close()
    return pages


def cmd_ingest(args):
    """Ingest a single PDF into investigations.db."""
    path = Path(args.pdf_path)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)

    db = get_db()

    # Check for duplicate
    fhash = _file_hash(path)
    existing = db.execute("SELECT id, title FROM documents WHERE file_hash = ?", [fhash]).fetchone()
    if existing and not args.force:
        print(f"Already ingested as doc #{existing['id']}: {existing['title']}")
        print("Use --force to re-ingest.")
        return

    print(f"Extracting text from: {path.name}")
    pages = extract_pdf(path)

    if not pages:
        print("WARNING: No text extracted. PDF may be scanned without OCR layer.")
        if not args.force:
            return

    title = args.title or path.stem
    print(f"  Title: {title}")
    print(f"  Pages: {len(pages)}")

    # Delete existing if force re-ingest
    if existing:
        db.execute("DELETE FROM documents WHERE id = ?", [existing['id']])

    # Insert document
    cursor = db.execute("""
        INSERT INTO documents (title, source, source_url, category, year, total_pages, file_path, file_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [title, args.source, args.url, args.category, args.year, len(pages),
          str(path.resolve()), fhash])
    doc_id = cursor.lastrowid

    # Insert pages
    page_data = [(doc_id, pnum, text) for pnum, text in pages]
    db.executemany("""
        INSERT INTO pages (document_id, page_number, text) VALUES (?, ?, ?)
    """, page_data)

    # Rebuild FTS
    _rebuild_fts(db)
    db.commit()

    print(f"  Ingested as document #{doc_id}")
    total_chars = sum(len(t) for _, t in pages)
    print(f"  Total text: {total_chars:,} characters across {len(pages)} pages")


def cmd_ingest_dir(args):
    """Ingest all PDFs in a directory."""
    dir_path = Path(args.directory)
    if not dir_path.is_dir():
        print(f"ERROR: Not a directory: {dir_path}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(dir_path.glob("*.pdf"))
    if not pdfs:
        print(f"No PDF files found in {dir_path}")
        return

    print(f"Found {len(pdfs)} PDFs in {dir_path}")
    print()

    for pdf in pdfs:
        args.pdf_path = str(pdf)
        args.title = args.title if hasattr(args, "_custom_title") else None
        args.source = getattr(args, "source", None)
        args.url = getattr(args, "url", None)
        args.category = getattr(args, "category", None)
        args.year = getattr(args, "year", None)
        args.force = getattr(args, "force", False)
        try:
            cmd_ingest(args)
        except Exception as e:
            print(f"  ERROR: {e}")
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

    print(f"=== {doc['title']} ===")
    print(f"Source: {doc['source'] or '?'} | Year: {doc['year'] or '?'} | "
          f"Pages: {doc['total_pages']} | Category: {doc['category'] or '?'}")
    if doc['source_url']:
        print(f"URL: {doc['source_url']}")
    print()

    # Parse page range
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

    for r in rows:
        print(f"--- Page {r['page_number']} ---")
        text = r['text']
        if args.limit and len(text) > args.limit:
            text = text[:args.limit] + f"\n... [{len(r['text']) - args.limit} more chars]"
        print(text)
        print()


def cmd_stats(args):
    """Show database statistics."""
    db = get_db()

    doc_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    page_count = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    total_chars = db.execute("SELECT COALESCE(SUM(LENGTH(text)), 0) FROM pages").fetchone()[0]

    print(f"Investigations DB: {DB_PATH}")
    print(f"  Documents: {doc_count}")
    print(f"  Pages: {page_count:,}")
    print(f"  Total text: {total_chars:,} characters ({total_chars / 1024 / 1024:.1f} MB)")

    if doc_count > 0:
        print()
        cats = db.execute("""
            SELECT category, COUNT(*) as cnt FROM documents
            GROUP BY category ORDER BY cnt DESC
        """).fetchall()
        print("  By category:")
        for c in cats:
            print(f"    {c['category'] or 'uncategorized'}: {c['cnt']}")

        db_size = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0
        print(f"\n  DB size: {db_size:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="PDF ingestion for investigation reports")
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p = sub.add_parser("ingest", help="Ingest a PDF")
    p.add_argument("pdf_path", help="Path to PDF file")
    p.add_argument("--title", help="Document title (default: filename)")
    p.add_argument("--source", help="Source (e.g., GPO, Senate.gov, NYDFS)")
    p.add_argument("--url", help="Source URL")
    p.add_argument("--category", choices=CATEGORIES, help="Document category")
    p.add_argument("--year", type=int, help="Publication year")
    p.add_argument("--force", action="store_true", help="Re-ingest even if already exists")

    # ingest-dir
    p = sub.add_parser("ingest-dir", help="Ingest all PDFs in a directory")
    p.add_argument("directory", help="Directory containing PDFs")
    p.add_argument("--source", help="Source for all files")
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--force", action="store_true")

    # list
    sub.add_parser("list", help="List ingested documents")

    # read
    p = sub.add_parser("read", help="Read pages from a document")
    p.add_argument("doc_id", type=int, help="Document ID")
    p.add_argument("--pages", help="Page range (e.g., 5-10, or single page 5)")
    p.add_argument("--limit", type=int, default=0, help="Max chars per page (0=unlimited)")

    # stats
    sub.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()
    handlers = {
        "ingest": cmd_ingest,
        "ingest-dir": cmd_ingest_dir,
        "list": cmd_list,
        "read": cmd_read,
        "stats": cmd_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
