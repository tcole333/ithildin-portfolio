#!/usr/bin/env python3
"""
Ingest and query the teyler/epstein-files-20k dataset from HuggingFace.

25,800 clean OCR'd texts from the House Oversight Epstein Estate Documents.
All documents use HOUSE_OVERSIGHT_XXXXXX identifiers (distinct from DOJ Vol 11 EFTA IDs).

Database: datasets/epstein_files_20k.db (SQLite with FTS5)

Usage:
    python tools/ingest_epstein_20k.py download
    python tools/ingest_epstein_20k.py ingest
    python tools/ingest_epstein_20k.py search "Jeffrey Epstein" --limit 20
    python tools/ingest_epstein_20k.py doc HOUSE_OVERSIGHT_020367
    python tools/ingest_epstein_20k.py stats
    python tools/ingest_epstein_20k.py overlap
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Increase CSV field size limit for large document texts
csv.field_size_limit(sys.maxsize)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "datasets" / "epstein_files_20k.db"
DATA_DIR = BASE_DIR / "datasets" / "epstein_files_20k"
CSV_FILENAME = "EPS_FILES_20K_NOV2025.txt"

HF_REPO = "teyler/epstein-files-20k"


def get_db(create=False):
    """Connect to the epstein_files_20k database."""
    if not create and not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        print("Run 'python tools/ingest_epstein_20k.py ingest' first.", file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_db(db):
    """Create tables and FTS5 index."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            house_oversight_id TEXT,
            source_prefix TEXT,
            text TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            word_count INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_docs_ho_id ON documents(house_oversight_id);
        CREATE INDEX IF NOT EXISTS idx_docs_prefix ON documents(source_prefix);
    """)

    # Check if FTS5 table exists
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()
    if not row:
        db.execute("""
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                filename,
                text,
                content=documents,
                content_rowid=id,
                tokenize='porter unicode61'
            );
        """)
        # Triggers to keep FTS in sync
        db.executescript("""
            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, filename, text)
                VALUES (new.id, new.filename, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, filename, text)
                VALUES ('delete', old.id, old.filename, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, filename, text)
                VALUES ('delete', old.id, old.filename, old.text);
                INSERT INTO documents_fts(rowid, filename, text)
                VALUES (new.id, new.filename, new.text);
            END;
        """)

    db.commit()


def parse_filename(filename):
    """Extract HOUSE_OVERSIGHT ID and source prefix from filename.

    Examples:
        IMAGES-005-HOUSE_OVERSIGHT_020367.txt -> ('HOUSE_OVERSIGHT_020367', 'IMAGES-005')
        TEXT-001-HOUSE_OVERSIGHT_031683.txt   -> ('HOUSE_OVERSIGHT_031683', 'TEXT-001')
    """
    ho_match = re.search(r'(HOUSE_OVERSIGHT_\d+)', filename)
    ho_id = ho_match.group(1) if ho_match else None

    prefix_match = re.match(r'([A-Z]+-\d+)-', filename)
    prefix = prefix_match.group(1) if prefix_match else None

    return ho_id, prefix


# --- Commands ---


def cmd_download(args):
    """Download the dataset from HuggingFace."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub not installed.", file=sys.stderr)
        print("Install with: .venv/bin/uv pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / CSV_FILENAME

    if target.exists():
        size_mb = target.stat().st_size / 1024 / 1024
        print(f"File already exists: {target} ({size_mb:.1f} MB)")
        if not args.force:
            print("Use --force to re-download.")
            return
        print("Re-downloading...")

    print(f"Downloading {HF_REPO}/{CSV_FILENAME}...")
    downloaded = hf_hub_download(
        HF_REPO,
        CSV_FILENAME,
        repo_type="dataset",
        local_dir=str(DATA_DIR),
    )
    size_mb = os.path.getsize(downloaded) / 1024 / 1024
    print(f"Downloaded: {downloaded} ({size_mb:.1f} MB)")

    # Also check the HF cache for an already-downloaded copy
    if not Path(downloaded).resolve().is_relative_to(DATA_DIR.resolve()):
        # hf_hub_download may have stored it in cache; copy to our data dir
        import shutil
        shutil.copy2(downloaded, target)
        print(f"Copied to: {target}")


def cmd_ingest(args):
    """Parse the CSV into SQLite with FTS5 index."""
    # Find the CSV file
    csv_path = DATA_DIR / CSV_FILENAME
    if not csv_path.exists():
        # Check HF cache
        cache_pattern = Path.home() / ".cache" / "huggingface" / "hub" / f"datasets--{HF_REPO.replace('/', '--')}"
        candidates = list(cache_pattern.glob(f"**/{CSV_FILENAME}"))
        if candidates:
            csv_path = candidates[0]
            print(f"Using cached file: {csv_path}")
        else:
            print(f"ERROR: CSV not found at {csv_path}", file=sys.stderr)
            print("Run 'python tools/ingest_epstein_20k.py download' first.", file=sys.stderr)
            sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = get_db(create=True)
    init_db(db)

    # Check existing count
    existing = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing > 0 and not args.force:
        print(f"Database already has {existing:,} documents.")
        print("Use --force to re-ingest (will drop and recreate).")
        db.close()
        return

    if existing > 0 and args.force:
        print("Dropping existing data...")
        db.execute("DELETE FROM documents")
        db.execute("DELETE FROM documents_fts")
        db.commit()

    print(f"Ingesting from: {csv_path}")
    size_mb = csv_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")

    count = 0
    skipped = 0
    batch = []
    batch_size = 500

    with open(csv_path, "r", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != ["filename", "text"]:
            print(f"WARNING: Unexpected header: {header}", file=sys.stderr)

        for row in reader:
            if len(row) < 2:
                skipped += 1
                continue

            filename = row[0]
            text = row[1]
            ho_id, prefix = parse_filename(filename)
            char_count = len(text)
            word_count = len(text.split())

            batch.append((filename, ho_id, prefix, text, char_count, word_count))
            count += 1

            if len(batch) >= batch_size:
                db.executemany(
                    """INSERT OR IGNORE INTO documents
                       (filename, house_oversight_id, source_prefix, text, char_count, word_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    batch,
                )
                db.commit()
                batch = []

                if count % 5000 == 0:
                    print(f"  ... {count:,} documents ingested")

    # Final batch
    if batch:
        db.executemany(
            """INSERT OR IGNORE INTO documents
               (filename, house_oversight_id, source_prefix, text, char_count, word_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            batch,
        )
        db.commit()

    # Verify
    final_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    db.close()

    print(f"\nIngestion complete:")
    print(f"  Documents: {final_count:,}")
    print(f"  Skipped: {skipped}")
    print(f"  Database: {DB_PATH}")
    print(f"  Size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")


def cmd_search(args):
    """FTS5 full-text search across all documents."""
    db = get_db()

    # Build FTS5 query
    query = args.query

    sql = """
        SELECT
            d.id,
            d.filename,
            d.house_oversight_id,
            d.source_prefix,
            d.char_count,
            d.word_count,
            snippet(documents_fts, 1, '>>>', '<<<', '...', 64) as snippet
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
    """
    params = [query]

    if args.prefix:
        sql += " AND d.source_prefix = ?"
        params.append(args.prefix)

    if args.min_chars:
        sql += " AND d.char_count >= ?"
        params.append(args.min_chars)

    sql += " ORDER BY rank LIMIT ?"
    params.append(args.limit)

    rows = db.execute(sql, params).fetchall()

    # Get total count
    count_sql = """
        SELECT COUNT(*)
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
    """
    count_params = [query]
    if args.prefix:
        count_sql += " AND d.source_prefix = ?"
        count_params.append(args.prefix)
    if args.min_chars:
        count_sql += " AND d.char_count >= ?"
        count_params.append(args.min_chars)

    total = db.execute(count_sql, count_params).fetchone()[0]

    filters = []
    if args.prefix:
        filters.append(f"prefix={args.prefix}")
    if args.min_chars:
        filters.append(f"min_chars={args.min_chars}")
    filter_str = f" ({', '.join(filters)})" if filters else ""

    print(f"Search: '{args.query}'{filter_str} -- {total} matches (showing {len(rows)})")
    print()

    for r in rows:
        ho_id = r["house_oversight_id"] or "?"
        prefix = r["source_prefix"] or "?"
        print(f"  {ho_id} [{prefix}] ({r['char_count']:,} chars / {r['word_count']:,} words)")
        print(f"    File: {r['filename']}")
        snippet = r["snippet"] or ""
        # Clean up snippet for display
        snippet = snippet.replace("\n", " ").strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        print(f"    {snippet}")
        print()

    if args.json_out:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))

    db.close()


def cmd_doc(args):
    """Retrieve a specific document by HOUSE_OVERSIGHT ID."""
    db = get_db()

    # Accept both "HOUSE_OVERSIGHT_020367" and "020367"
    doc_id = args.doc_id
    if not doc_id.startswith("HOUSE_OVERSIGHT_"):
        doc_id = f"HOUSE_OVERSIGHT_{doc_id}"

    rows = db.execute(
        "SELECT * FROM documents WHERE house_oversight_id = ? ORDER BY source_prefix",
        [doc_id],
    ).fetchall()

    if not rows:
        # Try filename match
        rows = db.execute(
            "SELECT * FROM documents WHERE filename LIKE ?",
            [f"%{args.doc_id}%"],
        ).fetchall()

    if not rows:
        print(f"Document not found: {args.doc_id}")
        db.close()
        sys.exit(1)

    print(f"=== {doc_id} ({len(rows)} version(s)) ===\n")

    for r in rows:
        print(f"File: {r['filename']}")
        print(f"Prefix: {r['source_prefix']} | {r['char_count']:,} chars | {r['word_count']:,} words")
        print("-" * 60)

        text = r["text"]
        if args.full:
            print(text)
        else:
            # Show first N chars
            limit = args.chars
            if len(text) > limit:
                print(text[:limit])
                print(f"\n... [{len(text) - limit:,} more chars, use --full for complete text]")
            else:
                print(text)
        print()

    if args.json_out:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))

    db.close()


def cmd_stats(args):
    """Database statistics."""
    db = get_db()

    total = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    total_chars = db.execute("SELECT SUM(char_count) FROM documents").fetchone()[0] or 0
    total_words = db.execute("SELECT SUM(word_count) FROM documents").fetchone()[0] or 0
    avg_chars = db.execute("SELECT AVG(char_count) FROM documents").fetchone()[0] or 0
    max_chars = db.execute("SELECT MAX(char_count) FROM documents").fetchone()[0] or 0
    min_chars = db.execute("SELECT MIN(char_count) FROM documents").fetchone()[0] or 0
    empty = db.execute("SELECT COUNT(*) FROM documents WHERE char_count = 0").fetchone()[0]

    unique_ho = db.execute(
        "SELECT COUNT(DISTINCT house_oversight_id) FROM documents WHERE house_oversight_id IS NOT NULL"
    ).fetchone()[0]

    ho_range = db.execute("""
        SELECT
            MIN(CAST(REPLACE(house_oversight_id, 'HOUSE_OVERSIGHT_', '') AS INTEGER)),
            MAX(CAST(REPLACE(house_oversight_id, 'HOUSE_OVERSIGHT_', '') AS INTEGER))
        FROM documents
        WHERE house_oversight_id IS NOT NULL
    """).fetchone()

    print(f"=== Epstein Files 20K Database ===")
    print(f"  Source: {HF_REPO}")
    print(f"  Database: {DB_PATH}")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print()
    print(f"Documents: {total:,}")
    print(f"  Unique HOUSE_OVERSIGHT IDs: {unique_ho:,}")
    print(f"  ID range: HOUSE_OVERSIGHT_{ho_range[0]:06d} - HOUSE_OVERSIGHT_{ho_range[1]:06d}")
    print(f"  Empty documents: {empty}")
    print()
    print(f"Text statistics:")
    print(f"  Total chars: {total_chars:,} ({total_chars / 1024 / 1024:.1f} MB)")
    print(f"  Total words: {total_words:,}")
    print(f"  Avg chars/doc: {avg_chars:,.0f}")
    print(f"  Max chars: {max_chars:,}")
    print(f"  Min chars: {min_chars}")
    print()

    # Prefix distribution
    print("Source prefix distribution:")
    rows = db.execute("""
        SELECT source_prefix, COUNT(*) as cnt, SUM(char_count) as total_chars
        FROM documents
        GROUP BY source_prefix
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        prefix = r["source_prefix"] or "?"
        print(f"  {prefix}: {r['cnt']:,} docs ({r['total_chars'] / 1024 / 1024:.1f} MB)")

    # Size distribution
    print()
    print("Document size distribution:")
    brackets = [
        (0, 0, "empty"),
        (1, 100, "tiny (<100 chars)"),
        (100, 500, "small (100-500)"),
        (500, 2000, "medium (500-2K)"),
        (2000, 10000, "large (2K-10K)"),
        (10000, 50000, "very large (10K-50K)"),
        (50000, None, "huge (50K+)"),
    ]
    for lo, hi, label in brackets:
        if hi is None:
            cnt = db.execute("SELECT COUNT(*) FROM documents WHERE char_count >= ?", [lo]).fetchone()[0]
        else:
            cnt = db.execute(
                "SELECT COUNT(*) FROM documents WHERE char_count >= ? AND char_count < ?",
                [lo, hi],
            ).fetchone()[0]
        if cnt > 0:
            print(f"  {label}: {cnt:,}")

    db.close()


def cmd_overlap(args):
    """Check overlap with existing investigation databases."""
    db = get_db()

    print("=== Cross-Reference with Existing Databases ===\n")

    # Get all HOUSE_OVERSIGHT IDs from this dataset
    ho_ids = set()
    rows = db.execute(
        "SELECT DISTINCT house_oversight_id FROM documents WHERE house_oversight_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        ho_ids.add(r["house_oversight_id"])
    print(f"This dataset: {len(ho_ids):,} unique HOUSE_OVERSIGHT IDs")

    # Check DOJ Vol 11 (uses EFTA IDs - no direct overlap expected)
    doj_db_path = Path("/Users/travcole/projects/epstein-docs/output/documents.db")
    if doj_db_path.exists():
        doj = sqlite3.connect(str(doj_db_path))
        doj_count = doj.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        # Check if any DOJ bates_ids reference HOUSE_OVERSIGHT
        doj_ho = doj.execute(
            "SELECT COUNT(*) FROM documents WHERE bates_id LIKE '%HOUSE%'"
        ).fetchone()[0]
        print(f"\nDOJ Vol 11: {doj_count:,} documents (EFTA IDs)")
        print(f"  HOUSE_OVERSIGHT IDs in DOJ: {doj_ho}")
        print(f"  NOTE: DOJ uses EFTA IDs, this dataset uses HOUSE_OVERSIGHT IDs")
        print(f"        These are from DIFFERENT releases (DOJ vs House Oversight)")
        doj.close()
    else:
        print("\nDOJ Vol 11: not found")

    # Check LMSBAND
    lms_path = BASE_DIR / "datasets" / "lmsband_epstein_files.db"
    if lms_path.exists():
        lms = sqlite3.connect(str(lms_path))
        lms_count = lms.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        lms_ho = lms.execute(
            "SELECT COUNT(*) FROM files WHERE filename LIKE '%HOUSE_OVERSIGHT%'"
        ).fetchone()[0]
        print(f"\nLMSBAND: {lms_count:,} files")
        print(f"  HOUSE_OVERSIGHT files: {lms_ho}")

        if lms_ho > 0:
            # Check specific overlap
            lms_ho_ids = set()
            for r in lms.execute(
                "SELECT filename FROM files WHERE filename LIKE '%HOUSE_OVERSIGHT%'"
            ).fetchall():
                m = re.search(r'(HOUSE_OVERSIGHT_\d+)', r[0])
                if m:
                    lms_ho_ids.add(m.group(1))
            overlap = ho_ids & lms_ho_ids
            only_20k = ho_ids - lms_ho_ids
            only_lms = lms_ho_ids - ho_ids
            print(f"  Overlap with this dataset: {len(overlap):,}")
            print(f"  Only in 20K dataset: {len(only_20k):,}")
            print(f"  Only in LMSBAND: {len(only_lms):,}")
        lms.close()
    else:
        print("\nLMSBAND: not found")

    # Check Unified DB
    uni_path = BASE_DIR / "datasets" / "unified_epstein.db"
    if uni_path.exists():
        uni = sqlite3.connect(str(uni_path))
        # Look for tables with document text
        tables = [
            r[0]
            for r in uni.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        print(f"\nUnified DB: tables = {', '.join(tables[:10])}...")

        for t in ["documents", "docs"]:
            if t in tables:
                cnt = uni.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                # Check columns for HOUSE references
                cols = [
                    r[1]
                    for r in uni.execute(f"PRAGMA table_info({t})").fetchall()
                ]
                print(f"  {t}: {cnt:,} rows, columns: {cols[:5]}")
        uni.close()
    else:
        print("\nUnified DB: not found")

    # Check HF Emails Parquet
    parquet_path = BASE_DIR / "datasets" / "epstein-emails-hf" / "emails.parquet"
    if parquet_path.exists():
        print(f"\nHF Emails Parquet: exists ({parquet_path.stat().st_size / 1024:.0f} KB)")
        print("  NOTE: Parquet contains emails, not documents — different data type")
    else:
        print("\nHF Emails Parquet: not found")

    # Sample text comparison for quality
    print("\n--- OCR Quality Sample ---")
    sample = db.execute(
        "SELECT house_oversight_id, text FROM documents WHERE char_count > 500 ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if sample:
        text = sample["text"][:500]
        # Count garbage characters (common OCR artifacts)
        garbage = sum(1 for c in text if ord(c) > 127 and c not in "''""—–")
        alpha = sum(1 for c in text if c.isalpha())
        ratio = alpha / max(len(text), 1) * 100
        print(f"  Sample doc: {sample['house_oversight_id']}")
        print(f"  Alpha ratio: {ratio:.1f}% (higher = cleaner OCR)")
        print(f"  Non-ASCII artifacts: {garbage}")
        print(f"  First 300 chars: {text[:300]}")

    db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Ingest and query the Epstein Files 20K dataset from HuggingFace"
    )
    subs = parser.add_subparsers(dest="command", help="Command to run")

    # download
    p_dl = subs.add_parser("download", help="Download dataset from HuggingFace")
    p_dl.add_argument("--force", action="store_true", help="Re-download if exists")

    # ingest
    p_in = subs.add_parser("ingest", help="Parse CSV into SQLite with FTS5")
    p_in.add_argument("--force", action="store_true", help="Drop and re-ingest")

    # search
    p_s = subs.add_parser("search", help="FTS5 full-text search")
    p_s.add_argument("query", help="Search query (FTS5 syntax)")
    p_s.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_s.add_argument("--prefix", help="Filter by source prefix (e.g. IMAGES-005, TEXT-001)")
    p_s.add_argument("--min-chars", type=int, help="Minimum document size in chars")
    p_s.add_argument("--json", dest="json_out", action="store_true", help="Output JSON")

    # doc
    p_d = subs.add_parser("doc", help="Retrieve a specific document")
    p_d.add_argument("doc_id", help="HOUSE_OVERSIGHT ID (e.g. HOUSE_OVERSIGHT_020367 or 020367)")
    p_d.add_argument("--full", action="store_true", help="Show full text")
    p_d.add_argument("--chars", type=int, default=2000, help="Max chars to show (default: 2000)")
    p_d.add_argument("--json", dest="json_out", action="store_true", help="Output JSON")

    # stats
    p_st = subs.add_parser("stats", help="Database statistics")

    # overlap
    p_ov = subs.add_parser("overlap", help="Cross-reference with existing databases")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
        "doc": cmd_doc,
        "stats": cmd_stats,
        "overlap": cmd_overlap,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
