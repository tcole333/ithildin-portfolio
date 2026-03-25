#!/usr/bin/env python3
"""
SAM.gov bulk data ingestion and local query tool.

Ingests SAM.gov Public Extract files (entities + exclusions) into a local SQLite
database for unlimited querying without API rate limits.

Data files (download from sam.gov → Data Access → Public Extracts):
  - Entity Registration: SAM_PUBLIC_UTF-8_MONTHLY_V2_*.dat (pipe-delimited, ~500MB)
  - Exclusions: SAM_Exclusions_Public_Extract_V2_*.CSV (~66MB)

Usage:
    uv run python tools/ingest_sam.py ingest-exclusions
    uv run python tools/ingest_sam.py ingest-entities
    uv run python tools/ingest_sam.py search "Palantir"
    uv run python tools/ingest_sam.py entity "Booz Allen"
    uv run python tools/ingest_sam.py exclusion "fraud"
    uv run python tools/ingest_sam.py entity-by-uei "C111ATT311C8"
    uv run python tools/ingest_sam.py entity-by-cage "53YC5"
    uv run python tools/ingest_sam.py naics "541511" --limit 20
    uv run python tools/ingest_sam.py address "1600 Pennsylvania" --limit 20
    uv run python tools/ingest_sam.py stats
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "datasets" / "sam.db"
DATA_DIR = PROJECT_ROOT / "datasets" / "sam"

BATCH_SIZE = 50_000


# --- Schema ---

def _ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sam_exclusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            classification TEXT,
            name TEXT,
            prefix TEXT,
            first_name TEXT,
            middle_name TEXT,
            last_name TEXT,
            suffix TEXT,
            address_1 TEXT,
            address_2 TEXT,
            address_3 TEXT,
            address_4 TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            zip_code TEXT,
            open_data_flag TEXT,
            uei TEXT,
            exclusion_program TEXT,
            excluding_agency TEXT,
            ct_code TEXT,
            exclusion_type TEXT,
            additional_comments TEXT,
            active_date TEXT,
            termination_date TEXT,
            record_status TEXT,
            cross_reference TEXT,
            sam_number TEXT,
            cage TEXT,
            npi TEXT,
            creation_date TEXT
        );

        CREATE TABLE IF NOT EXISTS sam_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uei TEXT UNIQUE,
            cage_code TEXT,
            dodaac TEXT,
            extract_code TEXT,
            purpose_of_registration TEXT,
            registration_date TEXT,
            expiration_date TEXT,
            last_update_date TEXT,
            activation_date TEXT,
            legal_business_name TEXT,
            dba_name TEXT,
            division_name TEXT,
            division_number TEXT,
            phys_address_1 TEXT,
            phys_address_2 TEXT,
            phys_city TEXT,
            phys_state TEXT,
            phys_zip TEXT,
            phys_zip_plus4 TEXT,
            phys_country TEXT,
            congressional_district TEXT,
            entity_start_date TEXT,
            fiscal_year_end TEXT,
            url TEXT,
            entity_structure TEXT,
            state_of_incorporation TEXT,
            country_of_incorporation TEXT,
            business_types TEXT,
            primary_naics TEXT,
            naics_codes TEXT,
            psc_codes TEXT,
            credit_card_usage TEXT,
            mail_address_1 TEXT,
            mail_address_2 TEXT,
            mail_city TEXT,
            mail_zip TEXT,
            mail_country TEXT,
            mail_state TEXT,
            govt_poc_first TEXT,
            govt_poc_last TEXT,
            govt_poc_title TEXT,
            alt_govt_poc_first TEXT,
            alt_govt_poc_last TEXT,
            past_perf_poc_first TEXT,
            past_perf_poc_last TEXT,
            elec_poc_first TEXT,
            elec_poc_last TEXT,
            alt_elec_poc_first TEXT,
            alt_elec_poc_last TEXT,
            exclusion_status TEXT,
            sba_business_types TEXT,
            entity_evs_source TEXT
        );

        CREATE TABLE IF NOT EXISTS sam_ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_type TEXT NOT NULL,
            row_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sam_entities_cage ON sam_entities(cage_code);
        CREATE INDEX IF NOT EXISTS idx_sam_entities_naics ON sam_entities(primary_naics);
        CREATE INDEX IF NOT EXISTS idx_sam_entities_state ON sam_entities(phys_state);
        CREATE INDEX IF NOT EXISTS idx_sam_entities_inc_state ON sam_entities(state_of_incorporation);
        CREATE INDEX IF NOT EXISTS idx_sam_exclusions_class ON sam_exclusions(classification);
        CREATE INDEX IF NOT EXISTS idx_sam_exclusions_agency ON sam_exclusions(excluding_agency);
        CREATE INDEX IF NOT EXISTS idx_sam_exclusions_uei ON sam_exclusions(uei);
    """)

    # FTS5 tables — created separately since IF NOT EXISTS works differently
    for tbl, cols, src in [
        ("sam_exclusions_fts",
         "name, first_name, last_name, city, state, excluding_agency, additional_comments",
         "sam_exclusions"),
        ("sam_entities_fts",
         "legal_business_name, dba_name, phys_city, phys_state, govt_poc_first, govt_poc_last, elec_poc_first, elec_poc_last",
         "sam_entities"),
    ]:
        try:
            db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {tbl} USING fts5(
                    {cols},
                    content={src}, content_rowid=id,
                    tokenize='porter unicode61'
                )
            """)
        except sqlite3.OperationalError:
            pass  # Already exists with different schema — OK


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(db)
    return db


# --- Entity field mapping (0-based indices into pipe-delimited fields) ---
# Layout: SAM Master Extract Mapping v6.1 Public File V2
# Field 1 = index 0, Field 142 = index 141

ENTITY_FIELD_MAP = {
    "uei": 0,
    "cage_code": 3,
    "dodaac": 4,
    "extract_code": 5,
    "purpose_of_registration": 6,
    "registration_date": 7,
    "expiration_date": 8,
    "last_update_date": 9,
    "activation_date": 10,
    "legal_business_name": 11,
    "dba_name": 12,
    "division_name": 13,
    "division_number": 14,
    "phys_address_1": 15,
    "phys_address_2": 16,
    "phys_city": 17,
    "phys_state": 18,
    "phys_zip": 19,
    "phys_zip_plus4": 20,
    "phys_country": 21,
    "congressional_district": 22,
    "entity_start_date": 24,
    "fiscal_year_end": 25,
    "url": 26,
    "entity_structure": 27,
    "state_of_incorporation": 28,
    "country_of_incorporation": 29,
    # Field 30 = BUSINESS TYPE COUNTER, skip
    "business_types": 31,
    "primary_naics": 32,
    # Field 33 = NAICS CODE COUNTER, skip
    "naics_codes": 34,
    # Field 35 = PSC CODE COUNTER, skip
    "psc_codes": 36,
    "credit_card_usage": 37,
    # Field 38 = CORRESPONDENCE FLAG, skip
    "mail_address_1": 39,
    "mail_address_2": 40,
    "mail_city": 41,
    "mail_zip": 42,
    "mail_country": 44,
    "mail_state": 45,
    "govt_poc_first": 46,
    "govt_poc_last": 48,
    "govt_poc_title": 49,
    "alt_govt_poc_first": 57,
    "alt_govt_poc_last": 59,
    "past_perf_poc_first": 68,
    "past_perf_poc_last": 70,
    "elec_poc_first": 90,
    "elec_poc_last": 92,
    "alt_elec_poc_first": 101,
    "alt_elec_poc_last": 103,
    "exclusion_status": 115,
    "sba_business_types": 117,
    "entity_evs_source": 121,
}

ENTITY_COLUMNS = list(ENTITY_FIELD_MAP.keys())
ENTITY_PLACEHOLDERS = ", ".join(["?"] * len(ENTITY_COLUMNS))
ENTITY_INSERT_SQL = f"INSERT OR REPLACE INTO sam_entities ({', '.join(ENTITY_COLUMNS)}) VALUES ({ENTITY_PLACEHOLDERS})"

EXCLUSION_COLUMNS = [
    "classification", "name", "prefix", "first_name", "middle_name",
    "last_name", "suffix", "address_1", "address_2", "address_3",
    "address_4", "city", "state", "country", "zip_code",
    "open_data_flag", "uei", "exclusion_program", "excluding_agency",
    "ct_code", "exclusion_type", "additional_comments", "active_date",
    "termination_date", "record_status", "cross_reference", "sam_number",
    "cage", "npi", "creation_date",
]
# CSV header → DB column mapping
EXCLUSION_CSV_MAP = {
    "Classification": "classification",
    "Name": "name",
    "Prefix": "prefix",
    "First": "first_name",
    "Middle": "middle_name",
    "Last": "last_name",
    "Suffix": "suffix",
    "Address 1": "address_1",
    "Address 2": "address_2",
    "Address 3": "address_3",
    "Address 4": "address_4",
    "City": "city",
    "State / Province": "state",
    "Country": "country",
    "Zip Code": "zip_code",
    "Open Data Flag": "open_data_flag",
    "Unique Entity ID": "uei",
    "Exclusion Program": "exclusion_program",
    "Excluding Agency": "excluding_agency",
    "CT Code": "ct_code",
    "Exclusion Type": "exclusion_type",
    "Additional Comments": "additional_comments",
    "Active Date": "active_date",
    "Termination Date": "termination_date",
    "Record Status": "record_status",
    "Cross-Reference": "cross_reference",
    "SAM Number": "sam_number",
    "CAGE": "cage",
    "NPI": "npi",
    "Creation_Date": "creation_date",
}
EXCLUSION_PLACEHOLDERS = ", ".join(["?"] * len(EXCLUSION_COLUMNS))
EXCLUSION_INSERT_SQL = f"INSERT OR IGNORE INTO sam_exclusions ({', '.join(EXCLUSION_COLUMNS)}) VALUES ({EXCLUSION_PLACEHOLDERS})"


# --- Ingest Commands ---

def cmd_ingest_exclusions(args):
    """Ingest SAM.gov exclusions CSV into local database."""
    path = Path(args.file) if args.file else _find_file(DATA_DIR, "SAM_Exclusions_Public_Extract_V2_*.CSV")
    if not path or not path.exists():
        print(f"ERROR: Exclusions file not found. Expected at {DATA_DIR}/SAM_Exclusions_Public_Extract_V2_*.CSV", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting exclusions from: {path}")
    db = get_db()

    # Clear existing data for clean reload
    db.execute("DELETE FROM sam_exclusions")
    db.commit()

    batch = []
    total = 0
    t0 = time.time()

    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        # Map CSV headers to our columns, skipping 'Blank (Deprecated)'
        for row in reader:
            values = []
            for col in EXCLUSION_COLUMNS:
                csv_key = next((k for k, v in EXCLUSION_CSV_MAP.items() if v == col), None)
                values.append(row.get(csv_key, "").strip() if csv_key else "")
            batch.append(tuple(values))
            total += 1

            if len(batch) >= BATCH_SIZE:
                db.executemany(EXCLUSION_INSERT_SQL, batch)
                db.commit()
                print(f"  {total:,} rows...", end="\r")
                batch.clear()

    if batch:
        db.executemany(EXCLUSION_INSERT_SQL, batch)
        db.commit()

    # Rebuild FTS
    print(f"\n  Rebuilding FTS index...")
    db.execute("INSERT INTO sam_exclusions_fts(sam_exclusions_fts) VALUES('rebuild')")
    db.commit()

    elapsed = time.time() - t0
    db.execute("INSERT INTO sam_ingest_log (source_file, source_type, row_count) VALUES (?, ?, ?)",
               (path.name, "exclusions", total))
    db.commit()
    db.close()

    print(f"  Done: {total:,} exclusions ingested in {elapsed:.1f}s")


def cmd_ingest_entities(args):
    """Ingest SAM.gov entity registration .dat file into local database."""
    path = Path(args.file) if args.file else _find_file(DATA_DIR, "SAM_PUBLIC_UTF-8_MONTHLY_V2_*.dat")
    if not path or not path.exists():
        print(f"ERROR: Entity file not found. Expected at {DATA_DIR}/SAM_PUBLIC_UTF-8_MONTHLY_V2_*.dat", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting entities from: {path} ({path.stat().st_size / (1024*1024):.0f} MB)")
    db = get_db()

    # Clear existing data for clean reload
    db.execute("DELETE FROM sam_entities")
    db.commit()

    batch = []
    total = 0
    skipped = 0
    t0 = time.time()

    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline()  # BOF line — skip
        if not header.startswith("BOF"):
            print(f"WARNING: First line doesn't start with BOF: {header[:60]}", file=sys.stderr)

        for line in f:
            line = line.rstrip("\n")
            if line.startswith("EOF") or line.startswith("!end"):
                continue

            fields = line.split("|")
            if len(fields) < 120:
                skipped += 1
                continue

            values = []
            for col in ENTITY_COLUMNS:
                idx = ENTITY_FIELD_MAP[col]
                val = fields[idx].strip() if idx < len(fields) else ""
                values.append(val if val else None)

            batch.append(tuple(values))
            total += 1

            if len(batch) >= BATCH_SIZE:
                db.executemany(ENTITY_INSERT_SQL, batch)
                db.commit()
                print(f"  {total:,} rows...", end="\r")
                batch.clear()

    if batch:
        db.executemany(ENTITY_INSERT_SQL, batch)
        db.commit()

    # Rebuild FTS
    print(f"\n  Rebuilding FTS index...")
    db.execute("INSERT INTO sam_entities_fts(sam_entities_fts) VALUES('rebuild')")
    db.commit()

    elapsed = time.time() - t0
    db.execute("INSERT INTO sam_ingest_log (source_file, source_type, row_count) VALUES (?, ?, ?)",
               (path.name, "entities", total))
    db.commit()
    db.close()

    print(f"  Done: {total:,} entities ingested in {elapsed:.1f}s (skipped {skipped})")


# --- Query Commands ---

def cmd_search(args):
    """Search both entities and exclusions via FTS5."""
    db = get_db()
    query = args.query
    limit = args.limit

    entities = [dict(r) for r in db.execute("""
        SELECT e.*, rank FROM sam_entities e
        JOIN sam_entities_fts ON sam_entities_fts.rowid = e.id
        WHERE sam_entities_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()]

    exclusions = [dict(r) for r in db.execute("""
        SELECT e.*, rank FROM sam_exclusions e
        JOIN sam_exclusions_fts ON sam_exclusions_fts.rowid = e.id
        WHERE sam_exclusions_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()]

    result = {"query": query, "entities": entities, "exclusions": exclusions}
    db.close()

    if not write_output(result, args, summary=f"SAM.gov: {len(entities)} entities, {len(exclusions)} exclusions for '{query}'"):
        _print_entities(entities, f"Entities matching '{query}'")
        _print_exclusions(exclusions, f"Exclusions matching '{query}'")


def cmd_entity(args):
    """Search entity registrations via FTS5."""
    db = get_db()
    query = args.query
    limit = args.limit

    rows = [dict(r) for r in db.execute("""
        SELECT e.* FROM sam_entities e
        JOIN sam_entities_fts ON sam_entities_fts.rowid = e.id
        WHERE sam_entities_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()]

    result = {"query": query, "count": len(rows), "entities": rows}
    db.close()

    if not write_output(result, args, summary=f"SAM entities: {len(rows)} results for '{query}'"):
        _print_entities(rows, f"Entities matching '{query}'")


def cmd_exclusion(args):
    """Search exclusions via FTS5."""
    db = get_db()
    query = args.query
    limit = args.limit

    rows = [dict(r) for r in db.execute("""
        SELECT e.* FROM sam_exclusions e
        JOIN sam_exclusions_fts ON sam_exclusions_fts.rowid = e.id
        WHERE sam_exclusions_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, limit)).fetchall()]

    result = {"query": query, "count": len(rows), "exclusions": rows}
    db.close()

    if not write_output(result, args, summary=f"SAM exclusions: {len(rows)} results for '{query}'"):
        _print_exclusions(rows, f"Exclusions matching '{query}'")


def cmd_entity_by_uei(args):
    """Look up entity by exact UEI."""
    db = get_db()
    row = db.execute("SELECT * FROM sam_entities WHERE uei = ?", (args.uei.upper(),)).fetchone()
    db.close()

    if not row:
        print(f"No entity found with UEI: {args.uei}")
        return

    result = dict(row)
    if not write_output(result, args, summary=f"SAM entity UEI={args.uei}: {result.get('legal_business_name', '?')}"):
        _print_entity_detail(result)


def cmd_entity_by_cage(args):
    """Look up entities by CAGE code."""
    db = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM sam_entities WHERE cage_code = ?", (args.cage.upper(),)
    ).fetchall()]
    db.close()

    result = {"cage": args.cage, "count": len(rows), "entities": rows}
    if not write_output(result, args, summary=f"SAM entities CAGE={args.cage}: {len(rows)} results"):
        _print_entities(rows, f"Entities with CAGE code {args.cage}")


def cmd_naics(args):
    """Find entities by NAICS code (primary or in tilde-separated list)."""
    db = get_db()
    code = args.code
    limit = args.limit

    # Search primary NAICS exact match and NAICS string contains
    rows = [dict(r) for r in db.execute("""
        SELECT * FROM sam_entities
        WHERE primary_naics = ? OR naics_codes LIKE ?
        LIMIT ?
    """, (code, f"%{code}%", limit)).fetchall()]

    result = {"naics": code, "count": len(rows), "entities": rows}
    db.close()

    if not write_output(result, args, summary=f"SAM entities NAICS={code}: {len(rows)} results"):
        _print_entities(rows, f"Entities with NAICS {code}")


def cmd_address(args):
    """Search entities by address (city, state, or street)."""
    db = get_db()
    query = args.query
    limit = args.limit

    rows = [dict(r) for r in db.execute("""
        SELECT * FROM sam_entities
        WHERE phys_address_1 LIKE ? OR phys_city LIKE ?
           OR mail_address_1 LIKE ? OR mail_city LIKE ?
        LIMIT ?
    """, (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()]

    result = {"address_query": query, "count": len(rows), "entities": rows}
    db.close()

    if not write_output(result, args, summary=f"SAM entities at '{query}': {len(rows)} results"):
        _print_entities(rows, f"Entities matching address '{query}'")


def cmd_stats(args):
    """Show database statistics."""
    if not DB_PATH.exists():
        print("SAM database not found. Run ingest-exclusions and ingest-entities first.")
        return

    db = get_db()
    stats = {}

    for table in ["sam_entities", "sam_exclusions"]:
        try:
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats[table] = count
        except sqlite3.OperationalError:
            stats[table] = 0

    logs = [dict(r) for r in db.execute(
        "SELECT * FROM sam_ingest_log ORDER BY ingested_at DESC LIMIT 10"
    ).fetchall()]
    stats["ingest_log"] = logs
    stats["db_size_mb"] = round(DB_PATH.stat().st_size / (1024 * 1024), 1)

    # Entity breakdown by status
    try:
        by_status = {r[0]: r[1] for r in db.execute(
            "SELECT extract_code, COUNT(*) FROM sam_entities GROUP BY extract_code"
        ).fetchall()}
        stats["entities_by_status"] = by_status
    except sqlite3.OperationalError:
        pass

    # Exclusion breakdown by classification
    try:
        by_class = {r[0]: r[1] for r in db.execute(
            "SELECT classification, COUNT(*) FROM sam_exclusions GROUP BY classification"
        ).fetchall()}
        stats["exclusions_by_classification"] = by_class
    except sqlite3.OperationalError:
        pass

    db.close()

    if not write_output(stats, args, summary=f"SAM.gov: {stats.get('sam_entities', 0):,} entities, {stats.get('sam_exclusions', 0):,} exclusions"):
        print(f"\nSAM.gov Bulk Database: {DB_PATH}")
        print(f"  Size: {stats['db_size_mb']} MB")
        print(f"  Entities: {stats.get('sam_entities', 0):,}")
        print(f"  Exclusions: {stats.get('sam_exclusions', 0):,}")
        if stats.get("entities_by_status"):
            print(f"\n  Entity status: { {_entity_status_label(k): v for k, v in stats['entities_by_status'].items()} }")
        if stats.get("exclusions_by_classification"):
            print(f"  Exclusion types: {dict(stats['exclusions_by_classification'])}")
        if logs:
            print(f"\n  Last ingest:")
            for log in logs[:3]:
                print(f"    {log['ingested_at']} — {log['source_type']}: {log['row_count']:,} rows ({log['source_file']})")


# --- Helpers ---

def _find_file(directory, pattern):
    """Find the most recent file matching a glob pattern."""
    import glob
    matches = sorted(glob.glob(str(directory / pattern)), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return Path(matches[0]) if matches else None


def _entity_status_label(code):
    return {"A": "Active", "E": "Expired", "4": "ID Assigned"}.get(code, code or "Unknown")


ENTITY_STRUCTURE_LABELS = {
    "2L": "Corporate (not tax exempt)",
    "2A": "U.S. Government Entity",
    "8H": "Limited Liability Company",
    "CY": "Country",
    "X6": "International Organization",
    "ZZ": "Other",
}


def _print_entities(rows, title):
    if not rows:
        print(f"\n{title}: No results")
        return
    print(f"\n{title} ({len(rows)} results):")
    for r in rows:
        name = r.get("legal_business_name") or r.get("dba_name") or "?"
        uei = r.get("uei", "?")
        cage = r.get("cage_code") or ""
        city = r.get("phys_city") or ""
        state = r.get("phys_state") or ""
        loc = f"{city}, {state}" if city else state
        struct = ENTITY_STRUCTURE_LABELS.get(r.get("entity_structure", ""), r.get("entity_structure") or "")
        naics = r.get("primary_naics") or ""
        status = _entity_status_label(r.get("extract_code"))
        print(f"  [{status}] {name}")
        print(f"    UEI: {uei}  CAGE: {cage}  NAICS: {naics}  Structure: {struct}")
        print(f"    Location: {loc}  Inc: {r.get('state_of_incorporation', '') or ''}/{r.get('country_of_incorporation', '') or ''}")
        if r.get("url"):
            print(f"    URL: {r['url']}")
        poc_parts = []
        if r.get("govt_poc_first") or r.get("govt_poc_last"):
            poc_parts.append(f"Govt: {(r.get('govt_poc_first') or '')} {(r.get('govt_poc_last') or '')}")
        if r.get("elec_poc_first") or r.get("elec_poc_last"):
            poc_parts.append(f"Elec: {(r.get('elec_poc_first') or '')} {(r.get('elec_poc_last') or '')}")
        if poc_parts:
            print(f"    POC: {' | '.join(poc_parts)}")
        print()


def _print_entity_detail(r):
    name = r.get("legal_business_name") or "?"
    print(f"\n=== {name} ===")
    print(f"  UEI: {r.get('uei')}  CAGE: {r.get('cage_code') or '—'}")
    print(f"  DBA: {r.get('dba_name') or '—'}")
    print(f"  Status: {_entity_status_label(r.get('extract_code'))}")
    print(f"  Structure: {ENTITY_STRUCTURE_LABELS.get(r.get('entity_structure', ''), r.get('entity_structure') or '—')}")
    print(f"  Primary NAICS: {r.get('primary_naics') or '—'}")
    print(f"  NAICS codes: {r.get('naics_codes') or '—'}")
    print(f"  PSC codes: {r.get('psc_codes') or '—'}")
    print(f"  Business types: {r.get('business_types') or '—'}")
    print(f"  SBA types: {r.get('sba_business_types') or '—'}")
    print(f"\n  Physical: {r.get('phys_address_1') or '—'}")
    if r.get("phys_address_2"):
        print(f"           {r['phys_address_2']}")
    print(f"           {r.get('phys_city') or ''}, {r.get('phys_state') or ''} {r.get('phys_zip') or ''}")
    print(f"           {r.get('phys_country') or ''}")
    if r.get("mail_address_1"):
        print(f"  Mailing: {r['mail_address_1']}")
        print(f"           {r.get('mail_city') or ''}, {r.get('mail_state') or ''} {r.get('mail_zip') or ''}")
    print(f"\n  Incorporation: {r.get('state_of_incorporation') or '—'} / {r.get('country_of_incorporation') or '—'}")
    print(f"  Entity start: {r.get('entity_start_date') or '—'}")
    print(f"  Registered: {r.get('registration_date') or '—'}  Expires: {r.get('expiration_date') or '—'}")
    print(f"  Last updated: {r.get('last_update_date') or '—'}")
    print(f"  URL: {r.get('url') or '—'}")
    print(f"  Congressional district: {r.get('congressional_district') or '—'}")
    print(f"  Exclusion status: {r.get('exclusion_status') or 'N'}")

    print(f"\n  Points of Contact:")
    for label, first_key, last_key, title_key in [
        ("Govt Business", "govt_poc_first", "govt_poc_last", "govt_poc_title"),
        ("Alt Govt", "alt_govt_poc_first", "alt_govt_poc_last", None),
        ("Past Performance", "past_perf_poc_first", "past_perf_poc_last", None),
        ("Electronic", "elec_poc_first", "elec_poc_last", None),
        ("Alt Electronic", "alt_elec_poc_first", "alt_elec_poc_last", None),
    ]:
        first = r.get(first_key) or ""
        last = r.get(last_key) or ""
        if first or last:
            title = r.get(title_key) or "" if title_key else ""
            title_str = f" ({title})" if title else ""
            print(f"    {label}: {first} {last}{title_str}")


def _print_exclusions(rows, title):
    if not rows:
        print(f"\n{title}: No results")
        return
    print(f"\n{title} ({len(rows)} results):")
    for r in rows:
        classification = r.get("classification", "?")
        if classification == "Individual":
            name = f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
        else:
            name = r.get("name") or f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
        agency = r.get("excluding_agency") or "?"
        exc_type = r.get("exclusion_type") or "?"
        active = r.get("active_date") or "?"
        term = r.get("termination_date") or "indefinite"
        status = r.get("record_status") or ""
        city = r.get("city") or ""
        state = r.get("state") or ""
        loc = f"{city}, {state}" if city else state

        print(f"  [{classification}] {name}")
        print(f"    Agency: {agency}  Type: {exc_type}  Status: {status}")
        print(f"    Active: {active} → {term}")
        if loc:
            print(f"    Location: {loc}")
        if r.get("uei"):
            print(f"    UEI: {r['uei']}")
        if r.get("npi"):
            print(f"    NPI: {r['npi']}")
        if r.get("additional_comments"):
            print(f"    Comments: {r['additional_comments'][:120]}")
        print()


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="SAM.gov bulk data ingestion and local query")
    sub = parser.add_subparsers(dest="command")

    # Ingest commands
    p_ie = sub.add_parser("ingest-exclusions", help="Ingest exclusions CSV")
    p_ie.add_argument("--file", help="Path to CSV file (default: auto-detect in datasets/sam/)")

    p_ient = sub.add_parser("ingest-entities", help="Ingest entity registration .dat file")
    p_ient.add_argument("--file", help="Path to .dat file (default: auto-detect in datasets/sam/)")

    # Query commands
    p_s = sub.add_parser("search", help="Search entities and exclusions")
    p_s.add_argument("query", help="Search term")
    p_s.add_argument("--limit", type=int, default=20)
    add_output_args(p_s)

    p_e = sub.add_parser("entity", help="Search entity registrations")
    p_e.add_argument("query", help="Entity name to search")
    p_e.add_argument("--limit", type=int, default=20)
    add_output_args(p_e)

    p_ex = sub.add_parser("exclusion", help="Search exclusions/debarments")
    p_ex.add_argument("query", help="Name or term to search")
    p_ex.add_argument("--limit", type=int, default=20)
    add_output_args(p_ex)

    p_uei = sub.add_parser("entity-by-uei", help="Look up entity by UEI")
    p_uei.add_argument("uei", help="Unique Entity Identifier")
    add_output_args(p_uei)

    p_cage = sub.add_parser("entity-by-cage", help="Look up entities by CAGE code")
    p_cage.add_argument("cage", help="CAGE code")
    add_output_args(p_cage)

    p_naics = sub.add_parser("naics", help="Find entities by NAICS code")
    p_naics.add_argument("code", help="NAICS code")
    p_naics.add_argument("--limit", type=int, default=20)
    add_output_args(p_naics)

    p_addr = sub.add_parser("address", help="Search entities by address")
    p_addr.add_argument("query", help="Address, city, or state to search")
    p_addr.add_argument("--limit", type=int, default=20)
    add_output_args(p_addr)

    p_stats = sub.add_parser("stats", help="Show database statistics")
    add_output_args(p_stats)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "ingest-exclusions": cmd_ingest_exclusions,
        "ingest-entities": cmd_ingest_entities,
        "search": cmd_search,
        "entity": cmd_entity,
        "exclusion": cmd_exclusion,
        "entity-by-uei": cmd_entity_by_uei,
        "entity-by-cage": cmd_entity_by_cage,
        "naics": cmd_naics,
        "address": cmd_address,
        "stats": cmd_stats,
    }

    handlers[args.command](args)


if __name__ == "__main__":
    main()
