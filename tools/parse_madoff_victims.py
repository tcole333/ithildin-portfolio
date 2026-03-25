#!/usr/bin/env python3
"""
Parse the Madoff Victims OCR file into structured SQLite data.

The source file is an OCR'd SDNY SIPA liquidation mailing list (Case 08-01789-BRL).
It contains customers, vendors, employees, and broker-dealers of BLMIS.

The OCR is column-oriented: each "page" has LINE1 (names), LINE2-LINE6 (address fields),
but column counts don't align due to OCR artifacts. Names in LINE1 are reliable;
address alignment is not.

Usage:
    uv run python tools/parse_madoff_victims.py parse datasets/offshore-alerts/Madoff_Victims.txt
    uv run python tools/parse_madoff_victims.py stats
    uv run python tools/parse_madoff_victims.py search "epstein"
    uv run python tools/parse_madoff_victims.py crossref          # match against investigation.db entities
    uv run python tools/parse_madoff_victims.py crossref --threshold 80
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "datasets" / "madoff.db"
INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"
SOURCE_FILE = Path(__file__).parent.parent / "datasets" / "offshore-alerts" / "Madoff_Victims.txt"

# Patterns that indicate entity types
ENTITY_PATTERNS = {
    "trust": re.compile(r"\bTRUST\b|\bTST\b|\bTSTEE\b|\bTRUSTEE\b|\bTTEE\b|\bTTEES\b|\bREV\s+TRUST\b|\bIRREVOC\b", re.I),
    "fund": re.compile(r"\bFUND\b|\bPENSION\b|\bPROFIT\s+SHARING\b|\b401K\b|\bMONEY\s+PURCH\b", re.I),
    "company": re.compile(r"\bINC\b|\bCORP\b|\bLLC\b|\bLTD\b|\bLP\b|\bLLP\b|\bPTNRSHIP\b|\bPARTNERSHIP\b|\bASSOC\b|\bCO\b\.?$|\bCOMPANY\b|\bGROUP\b|\bHOLDINGS\b|\bENTERPRISES\b|\bSERVICES\b", re.I),
    "foundation": re.compile(r"\bFOUNDATION\b|\bFDN\b|\bCHARIT\b", re.I),
    "estate": re.compile(r"\bESTATE\b", re.I),
    "cpa": re.compile(r"\bCPA\b|\bESQ\b|\bM\s*D\b|\bP\s*C\b|\bP\s*A\b", re.I),
}


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS raw_entries (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            page_num INTEGER,
            line_num INTEGER,
            exhibit TEXT DEFAULT 'A'
        );

        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL UNIQUE,
            entity_type TEXT DEFAULT 'unknown',
            name_variants TEXT,
            entry_count INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS addresses (
            id INTEGER PRIMARY KEY,
            page_num INTEGER,
            line_field TEXT,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS cross_matches (
            id INTEGER PRIMARY KEY,
            madoff_entity_id INTEGER REFERENCES entities(id),
            investigation_entity_id INTEGER,
            investigation_entity_name TEXT,
            madoff_name TEXT,
            match_type TEXT,
            score REAL,
            reviewed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_raw_name ON raw_entries(name);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_cross_score ON cross_matches(score DESC);
    """)
    db.commit()
    return db


def classify_entity(name):
    for etype, pattern in ENTITY_PATTERNS.items():
        if pattern.search(name):
            return etype
    # Heuristic: if name looks like "FIRSTNAME LASTNAME" or "FIRSTNAME M LASTNAME", likely person
    parts = name.split()
    if len(parts) >= 2 and all(p[0].isupper() for p in parts if p):
        # Check it's not a company-like name
        if not any(w in name.upper() for w in ["ADVISORY", "CAPITAL", "MANAGEMENT", "INVESTMENT", "SECURITIES", "PARTNERS"]):
            return "person"
    return "unknown"


def normalize_name(name):
    """Normalize name for deduplication."""
    n = name.upper().strip()
    # Remove common punctuation variations
    n = re.sub(r"[',.]", "", n)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n)
    return n


def parse_file(filepath):
    """Parse the Madoff victims OCR file, extracting names from LINE1 blocks."""
    text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    entries = []
    addresses = []
    current_section = None
    page_num = 0
    in_preamble = True

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip until we hit the first exhibit
        if in_preamble:
            if stripped.startswith("EXHIBIT"):
                in_preamble = False
            continue

        # Track page numbers
        page_match = re.match(r"Page\s+(\d+)\s+of\s+\d+", stripped)
        if page_match:
            page_num = int(page_match.group(1))
            continue

        # Track section markers
        if stripped in ("LINE1", "LINE2", "LINE3", "LINE4", "LINE5", "LINE6"):
            current_section = stripped
            continue

        # Track exhibit transitions
        if re.match(r"^EXHIBIT\s+[A-Z]$", stripped):
            current_section = None
            continue

        if stripped in ("Customers", "Vendors", "Employees", "Broker Dealers", "Other Parties"):
            continue

        # Skip page markers like "A-1", "B-1"
        if re.match(r"^[A-Z]-\d+$", stripped):
            continue

        # Empty lines reset nothing
        if not stripped:
            continue

        # Extract data based on current section
        if current_section == "LINE1":
            entries.append({
                "name": stripped,
                "page_num": page_num,
                "line_num": i,
            })
        elif current_section in ("LINE2", "LINE3", "LINE4", "LINE5", "LINE6"):
            addresses.append({
                "page_num": page_num,
                "line_field": current_section,
                "value": stripped,
            })

    return entries, addresses


def deduplicate_names(entries):
    """Group entries by normalized name, pick canonical form, classify."""
    from collections import defaultdict

    groups = defaultdict(list)
    for e in entries:
        norm = normalize_name(e["name"])
        groups[norm].append(e["name"])

    entities = []
    for norm, variants in groups.items():
        # Pick the longest variant as canonical (usually most complete)
        canonical = max(set(variants), key=lambda v: (len(v), variants.count(v)))
        etype = classify_entity(canonical)
        unique_variants = list(set(variants))
        entities.append({
            "canonical_name": canonical,
            "entity_type": etype,
            "name_variants": "|".join(unique_variants) if len(unique_variants) > 1 else None,
            "entry_count": len(variants),
        })

    return entities


def cmd_parse(args):
    filepath = args.file or str(SOURCE_FILE)
    if not Path(filepath).exists():
        print(f"ERROR: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing: {filepath}")
    entries, addresses = parse_file(filepath)
    print(f"  Raw LINE1 entries: {len(entries):,}")
    print(f"  Raw address entries: {len(addresses):,}")

    entities = deduplicate_names(entries)
    print(f"  Unique entities after dedup: {len(entities):,}")

    # Type breakdown
    from collections import Counter
    type_counts = Counter(e["entity_type"] for e in entities)
    for t, c in type_counts.most_common():
        print(f"    {t}: {c:,}")

    db = get_db()

    # Clear existing data
    db.execute("DELETE FROM raw_entries")
    db.execute("DELETE FROM entities")
    db.execute("DELETE FROM addresses")

    # Insert raw entries
    db.executemany(
        "INSERT INTO raw_entries (name, page_num, line_num) VALUES (?, ?, ?)",
        [(e["name"], e["page_num"], e["line_num"]) for e in entries]
    )

    # Insert deduplicated entities
    db.executemany(
        "INSERT OR IGNORE INTO entities (canonical_name, entity_type, name_variants, entry_count) VALUES (?, ?, ?, ?)",
        [(e["canonical_name"], e["entity_type"], e["name_variants"], e["entry_count"]) for e in entities]
    )

    # Insert addresses (for later manual lookup)
    db.executemany(
        "INSERT INTO addresses (page_num, line_field, value) VALUES (?, ?, ?)",
        [(a["page_num"], a["line_field"], a["value"]) for a in addresses]
    )

    db.commit()
    print(f"\n  Saved to: {DB_PATH}")
    print(f"  Raw entries: {len(entries):,}")
    print(f"  Unique entities: {len(entities):,}")
    print(f"  Address records: {len(addresses):,}")


def cmd_stats(args):
    db = get_db()
    raw = db.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0]
    ent = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    addr = db.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    cross = db.execute("SELECT COUNT(*) FROM cross_matches").fetchone()[0]

    print(f"Madoff Victims DB: {DB_PATH}")
    print(f"  Raw entries: {raw:,}")
    print(f"  Unique entities: {ent:,}")
    print(f"  Address records: {addr:,}")
    print(f"  Cross-matches: {cross:,}")

    if ent > 0:
        print("\n  By type:")
        for row in db.execute("SELECT entity_type, COUNT(*) as cnt FROM entities GROUP BY entity_type ORDER BY cnt DESC"):
            print(f"    {row['entity_type']}: {row['cnt']:,}")

        print("\n  Top repeated names:")
        for row in db.execute("SELECT canonical_name, entry_count FROM entities ORDER BY entry_count DESC LIMIT 10"):
            print(f"    {row['canonical_name']}: {row['entry_count']}x")


def cmd_search(args):
    db = get_db()
    pattern = f"%{args.query}%"
    rows = db.execute(
        "SELECT canonical_name, entity_type, entry_count FROM entities WHERE canonical_name LIKE ? ORDER BY entry_count DESC LIMIT 50",
        [pattern]
    ).fetchall()

    if not rows:
        print(f"No matches for '{args.query}'")
        return

    print(f"Matches for '{args.query}' ({len(rows)} results):")
    for r in rows:
        print(f"  [{r['entity_type']}] {r['canonical_name']} ({r['entry_count']}x)")


def cmd_crossref(args):
    """Cross-reference Madoff entities against investigation.db entities."""
    if not INVESTIGATION_DB.exists():
        print(f"ERROR: investigation.db not found at {INVESTIGATION_DB}", file=sys.stderr)
        sys.exit(1)

    db = get_db()

    # Attach investigation.db
    db.execute(f"ATTACH DATABASE ? AS inv", [str(INVESTIGATION_DB)])

    # Get all investigation entities
    inv_entities = db.execute("SELECT id, name FROM inv.entities").fetchall()
    madoff_entities = db.execute("SELECT id, canonical_name, entity_type FROM entities").fetchall()

    print(f"Cross-referencing {len(madoff_entities):,} Madoff entities against {len(inv_entities):,} investigation entities...")

    threshold = args.threshold / 100.0 if hasattr(args, 'threshold') else 0.8

    # Clear previous matches
    db.execute("DELETE FROM cross_matches")

    matches = []

    # Build normalized lookup for investigation entities
    inv_lookup = {}
    for ie in inv_entities:
        norm = normalize_name(ie["name"])
        inv_lookup[norm] = (ie["id"], ie["name"])

    # Also build word-set for fuzzy matching
    def name_words(n):
        return set(normalize_name(n).split())

    inv_word_sets = [(ie["id"], ie["name"], name_words(ie["name"])) for ie in inv_entities]

    for me in madoff_entities:
        me_norm = normalize_name(me["canonical_name"])
        me_words = set(me_norm.split())

        # Exact match
        if me_norm in inv_lookup:
            inv_id, inv_name = inv_lookup[me_norm]
            matches.append((me["id"], inv_id, inv_name, me["canonical_name"], "exact", 1.0))
            continue

        # Substring containment (either direction)
        for inv_id, inv_name, inv_words in inv_word_sets:
            inv_norm = normalize_name(inv_name)

            # Check if one name contains the other
            if me_norm in inv_norm or inv_norm in me_norm:
                matches.append((me["id"], inv_id, inv_name, me["canonical_name"], "contains", 0.9))
                continue

            # Jaccard similarity on word sets
            if me_words and inv_words:
                intersection = me_words & inv_words
                union = me_words | inv_words
                jaccard = len(intersection) / len(union)
                if jaccard >= threshold:
                    matches.append((me["id"], inv_id, inv_name, me["canonical_name"], "fuzzy", round(jaccard, 3)))

    # Insert matches
    db.executemany(
        "INSERT INTO cross_matches (madoff_entity_id, investigation_entity_id, investigation_entity_name, madoff_name, match_type, score) VALUES (?, ?, ?, ?, ?, ?)",
        matches
    )
    db.commit()

    print(f"\n  Found {len(matches)} cross-matches:")

    # Show results grouped by match type
    for mtype in ("exact", "contains", "fuzzy"):
        typed = [m for m in matches if m[4] == mtype]
        if typed:
            print(f"\n  {mtype.upper()} matches ({len(typed)}):")
            for m in sorted(typed, key=lambda x: -x[5]):
                print(f"    [{m[5]:.0%}] Madoff: {m[3]}  <->  Investigation: {m[2]} (entity #{m[1]})")

    db.execute("DETACH DATABASE inv")


def main():
    parser = argparse.ArgumentParser(description="Parse Madoff Victims list into structured data")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="Parse the OCR file into SQLite")
    p.add_argument("file", nargs="?", help="Path to Madoff_Victims.txt (default: datasets/offshore-alerts/)")

    sub.add_parser("stats", help="Show database statistics")

    p = sub.add_parser("search", help="Search Madoff entities by name")
    p.add_argument("query", help="Search term")

    p = sub.add_parser("crossref", help="Cross-reference against investigation.db")
    p.add_argument("--threshold", type=int, default=80, help="Minimum match score 0-100 (default: 80)")

    args = parser.parse_args()
    {"parse": cmd_parse, "stats": cmd_stats, "search": cmd_search, "crossref": cmd_crossref}[args.command](args)


if __name__ == "__main__":
    main()
