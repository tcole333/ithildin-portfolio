#!/usr/bin/env python3
"""
FAA aircraft registry ingester and search tool.

Downloads the ReleasableAircraft bulk data from FAA, parses MASTER and DEREG
files, and loads into datasets/faa_registry.db with FTS5 search.

Key investigation targets: N908JE (727 "Lolita Express"), N212JE (Gulfstream),
N120JE, JEGE INC, PLAN D LLC.

Source: https://registry.faa.gov/database/ReleasableAircraft.zip (~60MB, daily refresh)

Usage:
    python tools/ingest_faa.py download
    python tools/ingest_faa.py ingest
    python tools/ingest_faa.py search "JEGE"
    python tools/ingest_faa.py search "Epstein"
    python tools/ingest_faa.py n-number N212JE
    python tools/ingest_faa.py address "457 Madison"
    python tools/ingest_faa.py stats
"""

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DATA_DIR = Path(__file__).parent.parent / "datasets" / "faa_registry"
DB_PATH = Path(__file__).parent.parent / "datasets" / "faa_registry.db"
ZIP_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"

# Type registrant codes
REGISTRANT_TYPES = {
    "1": "Individual",
    "2": "Partnership",
    "3": "Corporation",
    "4": "Co-Owned",
    "5": "Government",
    "7": "LLC",
    "8": "Non-Citizen Corporation",
    "9": "Non-Citizen Co-Owned",
}

# Status codes
STATUS_CODES = {
    "V": "Valid",
    "M": "Multiple",
    "D": "Duplicate",
    "N": "Cancelled",
    "R": "Revoked",
    "S": "Suspended",
    "T": "Transfer",
    "X": "Expired",
    "Z": "Unassigned",
}


def get_db():
    """Get or create the FAA registry database."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS aircraft (
            n_number TEXT PRIMARY KEY,
            serial_number TEXT,
            mfr_mdl_code TEXT,
            year_mfr INTEGER,
            type_registrant TEXT,
            registrant_type_desc TEXT,
            name TEXT,
            street TEXT,
            street2 TEXT,
            city TEXT,
            state TEXT,
            zip_code TEXT,
            country TEXT,
            region TEXT,
            county TEXT,
            status_code TEXT,
            status_desc TEXT,
            is_deregistered INTEGER DEFAULT 0,
            type_aircraft TEXT,
            type_engine TEXT,
            make TEXT,
            model TEXT,
            cert_issue_date TEXT,
            expiration_date TEXT,
            last_action_date TEXT,
            air_worth_date TEXT,
            other_names_1 TEXT,
            other_names_2 TEXT,
            other_names_3 TEXT,
            other_names_4 TEXT,
            other_names_5 TEXT
        );

        CREATE TABLE IF NOT EXISTS acft_ref (
            code TEXT PRIMARY KEY,
            mfr TEXT,
            model TEXT,
            type_acft TEXT,
            type_eng TEXT,
            ac_cat TEXT,
            build_cert_ind TEXT,
            no_eng TEXT,
            no_seats TEXT,
            ac_weight TEXT,
            speed TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_aircraft_name ON aircraft(name);
        CREATE INDEX IF NOT EXISTS idx_aircraft_status ON aircraft(status_code);
    """)

    # Create FTS table if not exists
    try:
        db.execute("SELECT * FROM aircraft_fts LIMIT 0")
    except sqlite3.OperationalError:
        db.executescript("""
            CREATE VIRTUAL TABLE aircraft_fts USING fts5(
                name, street, city, other_names_1, other_names_2,
                other_names_3, other_names_4, other_names_5,
                content=aircraft,
                content_rowid=rowid,
                tokenize='porter unicode61'
            );
        """)

    db.commit()
    return db


def cmd_download(args):
    """Download FAA ReleasableAircraft.zip."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DATA_DIR / "ReleasableAircraft.zip"

    if zip_path.exists() and not args.force:
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"ZIP already exists: {zip_path} ({size_mb:.1f} MB)")
        print("Use --force to re-download.")
        # Still extract if needed
    else:
        print(f"Downloading {ZIP_URL}...")
        headers = {"User-Agent": "OSINT-Research/1.0"}
        req = Request(ZIP_URL, headers=headers)
        try:
            with urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                data = b""
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    data += chunk
                    if total:
                        pct = len(data) / total * 100
                        print(f"  {len(data) / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB ({pct:.0f}%)", end="\r")
                print()

                with open(zip_path, "wb") as f:
                    f.write(data)

            size_mb = zip_path.stat().st_size / (1024 * 1024)
            print(f"Downloaded: {zip_path} ({size_mb:.1f} MB)")
        except (HTTPError, URLError) as e:
            print(f"ERROR: Download failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Extract
    print("Extracting...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            print(f"  {name}")
        zf.extractall(DATA_DIR)
    print("Done.")


def _read_csv(filepath):
    """Read a comma-delimited FAA data file."""
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        # FAA files are comma-delimited with headers
        reader = csv.DictReader(f)
        for row in reader:
            # Strip whitespace from all values and keys, skip None keys
            yield {k.strip(): (v.strip() if v else None) for k, v in row.items() if k is not None}


def cmd_ingest(args):
    """Parse downloaded FAA data and load into SQLite."""
    db = get_db()

    # Load aircraft reference (make/model lookup)
    acft_ref_path = DATA_DIR / "ACFTREF.txt"
    if acft_ref_path.exists():
        print("Loading aircraft reference data...")
        count = 0
        for row in _read_csv(acft_ref_path):
            code = row.get("CODE")
            if not code:
                continue
            db.execute("""
                INSERT OR REPLACE INTO acft_ref (code, mfr, model, type_acft, type_eng,
                    ac_cat, build_cert_ind, no_eng, no_seats, ac_weight, speed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [code, row.get("MFR"), row.get("MODEL"), row.get("TYPE-ACFT"),
                  row.get("TYPE-ENG"), row.get("AC-CAT"), row.get("BUILD-CERT-IND"),
                  row.get("NO-ENG"), row.get("NO-SEATS"), row.get("AC-WEIGHT"),
                  row.get("SPEED")])
            count += 1
        db.commit()
        print(f"  Loaded {count:,} aircraft reference entries")

    # Build reference lookup
    ref_map = {}
    for row in db.execute("SELECT code, mfr, model FROM acft_ref").fetchall():
        ref_map[row["code"]] = (row["mfr"], row["model"])

    # Load MASTER file(s)
    master_files = sorted(DATA_DIR.glob("MASTER*.txt"))
    if not master_files:
        # Try alternate names
        master_files = sorted(DATA_DIR.glob("master*.txt"))
    if not master_files:
        print("ERROR: No MASTER files found. Run 'download' first.", file=sys.stderr)
        sys.exit(1)

    total_aircraft = 0
    for master_path in master_files:
        print(f"Processing {master_path.name}...")
        count = 0
        batch = []

        for row in _read_csv(master_path):
            n_num = row.get("N-NUMBER")
            if not n_num:
                continue

            mfr_code = row.get("MFR MDL CODE", "")
            make, model = ref_map.get(mfr_code, (None, None))
            type_reg = row.get("TYPE REGISTRANT", "")

            batch.append((
                n_num,
                row.get("SERIAL NUMBER"),
                mfr_code,
                int(row["YEAR MFR"]) if row.get("YEAR MFR") and row["YEAR MFR"].strip().isdigit() else None,
                type_reg,
                REGISTRANT_TYPES.get(type_reg, type_reg),
                row.get("NAME"),
                row.get("STREET"),
                row.get("STREET2"),
                row.get("CITY"),
                row.get("STATE"),
                row.get("ZIP CODE"),
                row.get("COUNTRY"),
                row.get("REGION"),
                row.get("COUNTY"),
                row.get("STATUS CODE", ""),
                STATUS_CODES.get(row.get("STATUS CODE", ""), row.get("STATUS CODE", "")),
                0,  # not deregistered
                row.get("TYPE AIRCRAFT"),
                row.get("TYPE ENGINE"),
                make,
                model,
                row.get("CERT ISSUE DATE"),
                row.get("EXPIRATION DATE"),
                row.get("LAST ACTION DATE"),
                row.get("AIR WORTH DATE"),
                row.get("OTHER NAMES(1)"),
                row.get("OTHER NAMES(2)"),
                row.get("OTHER NAMES(3)"),
                row.get("OTHER NAMES(4)"),
                row.get("OTHER NAMES(5)"),
            ))
            count += 1

            if count % 50000 == 0:
                db.executemany("""
                    INSERT OR REPLACE INTO aircraft VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                """, batch)
                batch.clear()
                print(f"  {count:,} records...")

        if batch:
            db.executemany("""
                INSERT OR REPLACE INTO aircraft VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, batch)

        db.commit()
        total_aircraft += count
        print(f"  Loaded {count:,} aircraft from {master_path.name}")

    # Load DEREG file
    dereg_path = DATA_DIR / "DEREG.txt"
    if not dereg_path.exists():
        dereg_path = DATA_DIR / "dereg.txt"
    if dereg_path.exists():
        print(f"Processing {dereg_path.name} (deregistered aircraft)...")
        count = 0
        batch = []

        for row in _read_csv(dereg_path):
            n_num = row.get("N-NUMBER")
            if not n_num:
                continue

            # DEREG file uses hyphenated column names
            mfr_code = row.get("MFR-MDL-CODE", row.get("MFR MDL CODE", ""))
            make, model = ref_map.get(mfr_code, (None, None))
            type_reg = row.get("TYPE REGISTRANT", "")
            year_mfr = row.get("YEAR-MFR", row.get("YEAR MFR", ""))

            batch.append((
                n_num,
                row.get("SERIAL-NUMBER", row.get("SERIAL NUMBER")),
                mfr_code,
                int(year_mfr) if year_mfr and year_mfr.strip().isdigit() else None,
                type_reg,
                REGISTRANT_TYPES.get(type_reg, type_reg),
                row.get("NAME"),
                row.get("STREET-MAIL", row.get("STREET")),
                row.get("STREET2-MAIL", row.get("STREET2")),
                row.get("CITY-MAIL", row.get("CITY")),
                row.get("STATE-ABBREV-MAIL", row.get("STATE")),
                row.get("ZIP-CODE-MAIL", row.get("ZIP CODE")),
                row.get("COUNTRY-MAIL", row.get("COUNTRY")),
                row.get("REGION"),
                row.get("COUNTY-MAIL", row.get("COUNTY")),
                row.get("STATUS-CODE", "N"),  # cancelled/deregistered
                "Deregistered",
                1,  # is_deregistered
                row.get("TYPE AIRCRAFT"),
                row.get("TYPE ENGINE"),
                make,
                model,
                row.get("CERT-ISSUE-DATE", row.get("CERT ISSUE DATE")),
                row.get("EXPIRATION DATE"),
                row.get("LAST-ACT-DATE", row.get("LAST ACTION DATE")),
                row.get("AIR-WORTH-DATE", row.get("AIR WORTH DATE")),
                row.get("OTHER-NAMES(1)", row.get("OTHER NAMES(1)")),
                row.get("OTHER-NAMES(2)", row.get("OTHER NAMES(2)")),
                row.get("OTHER-NAMES(3)", row.get("OTHER NAMES(3)")),
                row.get("OTHER-NAMES(4)", row.get("OTHER NAMES(4)")),
                row.get("OTHER-NAMES(5)", row.get("OTHER NAMES(5)")),
            ))
            count += 1

            if count % 50000 == 0:
                db.executemany("""
                    INSERT OR REPLACE INTO aircraft VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                """, batch)
                batch.clear()
                print(f"  {count:,} deregistered records...")

        if batch:
            db.executemany("""
                INSERT OR REPLACE INTO aircraft VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, batch)
        db.commit()
        print(f"  Loaded {count:,} deregistered aircraft")
        total_aircraft += count

    # Rebuild FTS
    print("Rebuilding search index...")
    db.execute("INSERT INTO aircraft_fts(aircraft_fts) VALUES('rebuild')")
    db.commit()

    print(f"\nIngest complete: {total_aircraft:,} total aircraft records")


def _format_aircraft(row):
    """Format an aircraft record for display."""
    n = row["n_number"]
    name = row["name"] or "?"
    status = row["status_desc"] or row["status_code"] or "?"
    make = row["make"] or "?"
    model = row["model"] or "?"
    year = row["year_mfr"] or "?"
    reg_type = row["registrant_type_desc"] or row["type_registrant"] or "?"

    lines = [f"  N{n} — {make} {model} ({year})"]
    lines.append(f"    Owner: {name} [{reg_type}]")

    addr_parts = [row["street"] or ""]
    if row["street2"]:
        addr_parts.append(row["street2"])
    city_state = f"{row['city'] or ''}, {row['state'] or ''} {row['zip_code'] or ''}".strip(", ")
    if any(addr_parts) or city_state:
        full_addr = ", ".join(p for p in addr_parts if p)
        if city_state:
            full_addr += f", {city_state}" if full_addr else city_state
        lines.append(f"    Address: {full_addr}")

    lines.append(f"    Status: {status}")
    if row["is_deregistered"]:
        lines.append(f"    ** DEREGISTERED **")

    if row["serial_number"]:
        lines.append(f"    Serial: {row['serial_number']}")
    if row["last_action_date"]:
        lines.append(f"    Last action: {row['last_action_date']}")

    # Other names
    others = []
    for i in range(1, 6):
        val = row[f"other_names_{i}"]
        if val:
            others.append(val)
    if others:
        lines.append(f"    Other names: {'; '.join(others)}")

    return "\n".join(lines)


def cmd_search(args):
    """Search aircraft by owner name."""
    db = get_db()

    # Try FTS first
    try:
        rows = db.execute("""
            SELECT a.* FROM aircraft_fts
            JOIN aircraft a ON a.rowid = aircraft_fts.rowid
            WHERE aircraft_fts MATCH ?
            LIMIT ?
        """, [args.query, args.limit]).fetchall()
    except sqlite3.OperationalError:
        # Fall back to LIKE search
        rows = db.execute("""
            SELECT * FROM aircraft
            WHERE name LIKE ? OR other_names_1 LIKE ? OR other_names_2 LIKE ?
            LIMIT ?
        """, [f"%{args.query}%"] * 3 + [args.limit]).fetchall()

    results = [dict(r) for r in rows]

    if write_output(results, args, summary=f"FAA search '{args.query}' ({len(results)} results)"):
        return

    print(f"FAA Registry search: '{args.query}' — {len(rows)} results")
    print()

    for r in rows:
        print(_format_aircraft(r))
        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_n_number(args):
    """Look up aircraft by N-number."""
    db = get_db()

    # Strip leading N if present
    n_num = args.n_number.upper().lstrip("N")

    row = db.execute("SELECT * FROM aircraft WHERE n_number = ?", [n_num]).fetchone()
    if not row:
        print(f"N{n_num} not found in FAA registry.")
        sys.exit(1)

    print(f"=== N{n_num} ===")
    print(_format_aircraft(row))

    if args.json_out:
        print(json.dumps(dict(row), indent=2, default=str))


def cmd_address(args):
    """Search by address."""
    db = get_db()

    rows = db.execute("""
        SELECT * FROM aircraft
        WHERE street LIKE ? OR street2 LIKE ?
        ORDER BY name
        LIMIT ?
    """, [f"%{args.query}%", f"%{args.query}%", args.limit]).fetchall()

    print(f"FAA address search: '{args.query}' — {len(rows)} results")
    print()

    for r in rows:
        print(_format_aircraft(r))
        print()


def cmd_stats(args):
    """Show database statistics."""
    db = get_db()

    total = db.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM aircraft WHERE status_code = 'V'").fetchone()[0]
    dereg = db.execute("SELECT COUNT(*) FROM aircraft WHERE is_deregistered = 1").fetchone()[0]

    print(f"FAA Registry DB: {DB_PATH}")
    print(f"  Total records: {total:,}")
    print(f"  Active (Valid): {active:,}")
    print(f"  Deregistered: {dereg:,}")

    if total > 0:
        by_type = db.execute("""
            SELECT registrant_type_desc, COUNT(*) as cnt
            FROM aircraft WHERE registrant_type_desc IS NOT NULL
            GROUP BY registrant_type_desc ORDER BY cnt DESC
        """).fetchall()
        print("\n  By registrant type:")
        for r in by_type:
            print(f"    {r['registrant_type_desc']}: {r['cnt']:,}")

        db_size = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0
        print(f"\n  DB size: {db_size:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="FAA aircraft registry for OSINT investigation")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # download
    p = sub.add_parser("download", help="Download FAA bulk data")
    p.add_argument("--force", action="store_true", help="Re-download even if exists")

    # ingest
    sub.add_parser("ingest", help="Parse and load into SQLite")

    # search
    p = sub.add_parser("search", help="Search by owner name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # n-number
    p = sub.add_parser("n-number", help="Lookup by N-number")
    p.add_argument("n_number", help="N-number (e.g., N212JE or 212JE)")

    # address
    p = sub.add_parser("address", help="Search by address")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    # stats
    sub.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False
    if not hasattr(args, "output"):
        args.output = None

    handlers = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
        "n-number": cmd_n_number,
        "address": cmd_address,
        "stats": cmd_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
