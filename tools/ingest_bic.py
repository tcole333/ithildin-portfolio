#!/usr/bin/env python3
"""
SWIFT BIC Directory integration for OSINT investigations.

Downloads and indexes SWIFT Business Identifier Codes (BIC) for bank/wire routing analysis.
Uses OpenSanctions BIC dataset (32K+ entities, daily updates) and GLEIF BIC-to-LEI mapping.

Usage:
    python tools/ingest_bic.py download          # Download BIC datasets
    python tools/ingest_bic.py ingest            # Ingest into bic.db
    python tools/ingest_bic.py search "CHASE"    # Search by bank name
    python tools/ingest_bic.py bic CHASUS33      # Lookup by BIC code
    python tools/ingest_bic.py country US        # List all BICs for country
    python tools/ingest_bic.py lei <LEI>         # BIC→LEI cross-reference
    python tools/ingest_bic.py stats             # Database statistics
"""
import argparse
import json
import sqlite3
import sys
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DATASETS_DIR = Path(__file__).parent.parent / "datasets"
BIC_DIR = DATASETS_DIR / "bic"
BIC_DB = DATASETS_DIR / "bic.db"

OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/iso9362_bic/entities.ftm.json"
GLEIF_BIC_LEI_URL = "https://mapping.gleif.org/api/v2/bic-lei/b870e0d4-c05b-48cb-9de2-dcedb91ab292/download"


def init_db():
    """Initialize BIC database with schema."""
    db = sqlite3.connect(BIC_DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bic_codes (
            bic TEXT PRIMARY KEY,
            bank_name TEXT NOT NULL,
            country TEXT,
            city TEXT,
            address TEXT,
            created_at TEXT,
            modified_at TEXT,
            opensanctions_id TEXT,
            source TEXT DEFAULT 'opensanctions',
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bic_lei_mapping (
            bic TEXT NOT NULL,
            lei TEXT NOT NULL,
            source TEXT DEFAULT 'gleif',
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bic, lei)
        );

        CREATE INDEX IF NOT EXISTS idx_bic_country ON bic_codes(country);
        CREATE INDEX IF NOT EXISTS idx_bic_name ON bic_codes(bank_name);
        CREATE INDEX IF NOT EXISTS idx_lei_bic ON bic_lei_mapping(lei);

        CREATE VIRTUAL TABLE IF NOT EXISTS bic_fts USING fts5(
            bic, bank_name, country, city, address,
            content=bic_codes,
            content_rowid=rowid
        );

        CREATE TRIGGER IF NOT EXISTS bic_fts_insert AFTER INSERT ON bic_codes BEGIN
            INSERT INTO bic_fts(rowid, bic, bank_name, country, city, address)
            VALUES (new.rowid, new.bic, new.bank_name, new.country, new.city, new.address);
        END;

        CREATE TRIGGER IF NOT EXISTS bic_fts_delete AFTER DELETE ON bic_codes BEGIN
            DELETE FROM bic_fts WHERE rowid = old.rowid;
        END;

        CREATE TRIGGER IF NOT EXISTS bic_fts_update AFTER UPDATE ON bic_codes BEGIN
            DELETE FROM bic_fts WHERE rowid = old.rowid;
            INSERT INTO bic_fts(rowid, bic, bank_name, country, city, address)
            VALUES (new.rowid, new.bic, new.bank_name, new.country, new.city, new.address);
        END;
    """)
    db.commit()
    return db


def download_datasets():
    """Download BIC datasets from OpenSanctions and GLEIF."""
    BIC_DIR.mkdir(parents=True, exist_ok=True)

    # Download OpenSanctions BIC
    print(f"Downloading OpenSanctions BIC dataset...")
    opensanctions_file = BIC_DIR / "opensanctions_bic.jsonl"
    urllib.request.urlretrieve(OPENSANCTIONS_URL, opensanctions_file)
    print(f"  → {opensanctions_file} ({opensanctions_file.stat().st_size / 1024 / 1024:.1f} MB)")

    # Download GLEIF BIC-to-LEI mapping
    print(f"Downloading GLEIF BIC-to-LEI mapping...")
    gleif_zip = BIC_DIR / "gleif_bic_lei.zip"
    urllib.request.urlretrieve(GLEIF_BIC_LEI_URL, gleif_zip)
    print(f"  → {gleif_zip} ({gleif_zip.stat().st_size / 1024:.1f} KB)")

    # Extract GLEIF CSV
    with zipfile.ZipFile(gleif_zip) as zf:
        csv_names = [n for n in zf.namelist() if n.endswith('.csv')]
        if csv_names:
            csv_name = csv_names[0]
            zf.extract(csv_name, BIC_DIR)
            gleif_csv = BIC_DIR / csv_name
            print(f"  → {gleif_csv} ({gleif_csv.stat().st_size / 1024:.1f} KB)")

    print(f"\nDatasets downloaded to {BIC_DIR}/")


def ingest_opensanctions():
    """Ingest OpenSanctions BIC dataset."""
    opensanctions_file = BIC_DIR / "opensanctions_bic.jsonl"
    if not opensanctions_file.exists():
        print(f"Error: {opensanctions_file} not found. Run 'download' first.")
        return 0

    db = init_db()
    cursor = db.cursor()

    count = 0
    with open(opensanctions_file) as f:
        for line in f:
            entity = json.loads(line)
            props = entity.get('properties', {})

            # Extract BIC from referents or properties
            bic = None
            if 'swiftBic' in props and props['swiftBic']:
                bic = props['swiftBic'][0]

            if not bic:
                continue

            # Extract location info
            country = props.get('country', [None])[0]
            address_full = props.get('address', [None])[0]

            # Try to extract city from address
            city = None
            if address_full:
                # Address format: "STREET CITY POSTAL REGION COUNTRY"
                parts = address_full.split()
                if len(parts) >= 3:
                    # Simple heuristic: city is often 2-3 words before postal code
                    city = ' '.join(parts[1:3])

            cursor.execute("""
                INSERT OR REPLACE INTO bic_codes
                (bic, bank_name, country, city, address, created_at, modified_at, opensanctions_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                bic,
                entity.get('caption', ''),
                country,
                city,
                address_full,
                props.get('createdAt', [None])[0],
                props.get('modifiedAt', [None])[0],
                entity.get('id')
            ))
            count += 1

    db.commit()
    print(f"Ingested {count} BIC codes from OpenSanctions")
    return count


def ingest_gleif():
    """Ingest GLEIF BIC-to-LEI mapping."""
    gleif_files = list(BIC_DIR.glob("lei-bic-*.csv"))
    if not gleif_files:
        print(f"Error: GLEIF CSV not found in {BIC_DIR}. Run 'download' first.")
        return 0

    gleif_file = gleif_files[0]
    db = init_db()
    cursor = db.cursor()

    count = 0
    with open(gleif_file) as f:
        header = f.readline().strip().split(',')
        for line in f:
            lei, bic = line.strip().split(',')
            cursor.execute("""
                INSERT OR IGNORE INTO bic_lei_mapping (lei, bic)
                VALUES (?, ?)
            """, (lei, bic))
            count += 1

    db.commit()
    print(f"Ingested {count} BIC→LEI mappings from GLEIF")
    return count


def search_bic(query, output=None):
    """Search BIC codes by bank name, country, city, or address."""
    if not BIC_DB.exists():
        print("Error: BIC database not found. Run 'ingest' first.")
        return []

    db = sqlite3.connect(BIC_DB)
    db.row_factory = sqlite3.Row

    # Use FTS5 for full-text search
    results = db.execute("""
        SELECT bc.* FROM bic_fts
        JOIN bic_codes bc ON bic_fts.rowid = bc.rowid
        WHERE bic_fts MATCH ?
        ORDER BY rank
        LIMIT 100
    """, (query,)).fetchall()

    output_data = [dict(r) for r in results]

    if output:
        write_output(output_data, output)
        print(f"Found {len(results)} BIC codes → {output}")
    else:
        for r in results:
            print(f"{r['bic']:11} | {r['bank_name']:40} | {r['country'] or 'N/A':2} | {r['city'] or 'N/A'}")

    return output_data


def lookup_bic(bic_code, output=None):
    """Lookup a specific BIC code."""
    if not BIC_DB.exists():
        print("Error: BIC database not found. Run 'ingest' first.")
        return None

    db = sqlite3.connect(BIC_DB)
    db.row_factory = sqlite3.Row

    # Normalize BIC (uppercase, strip XXX suffix if present)
    bic_code = bic_code.upper().strip()

    result = db.execute("""
        SELECT bc.*, GROUP_CONCAT(blm.lei) as leis
        FROM bic_codes bc
        LEFT JOIN bic_lei_mapping blm ON bc.bic = blm.bic
        WHERE bc.bic = ? OR bc.bic LIKE ?
        GROUP BY bc.bic
    """, (bic_code, f"{bic_code}%")).fetchone()

    if not result:
        print(f"BIC {bic_code} not found")
        return None

    output_data = dict(result)

    if output:
        write_output(output_data, output)
        print(f"BIC {bic_code} details → {output}")
    else:
        print(f"\nBIC:      {result['bic']}")
        print(f"Bank:     {result['bank_name']}")
        print(f"Country:  {result['country'] or 'N/A'}")
        print(f"City:     {result['city'] or 'N/A'}")
        print(f"Address:  {result['address'] or 'N/A'}")
        if result['leis']:
            print(f"LEI(s):   {result['leis']}")
        print(f"Source:   {result['source']}")

    return output_data


def list_by_country(country_code, output=None):
    """List all BIC codes for a country."""
    if not BIC_DB.exists():
        print("Error: BIC database not found. Run 'ingest' first.")
        return []

    db = sqlite3.connect(BIC_DB)
    db.row_factory = sqlite3.Row

    results = db.execute("""
        SELECT * FROM bic_codes
        WHERE country = ?
        ORDER BY bank_name
    """, (country_code.lower(),)).fetchall()

    output_data = [dict(r) for r in results]

    if output:
        write_output(output_data, output)
        print(f"Found {len(results)} BIC codes for {country_code} → {output}")
    else:
        print(f"\n{len(results)} BIC codes for {country_code}:\n")
        for r in results:
            print(f"{r['bic']:11} | {r['bank_name']:50} | {r['city'] or 'N/A'}")

    return output_data


def lookup_lei(lei_code, output=None):
    """Cross-reference LEI to BIC codes."""
    if not BIC_DB.exists():
        print("Error: BIC database not found. Run 'ingest' first.")
        return []

    db = sqlite3.connect(BIC_DB)
    db.row_factory = sqlite3.Row

    # LEI codes are uppercase
    results = db.execute("""
        SELECT bc.*, blm.lei
        FROM bic_lei_mapping blm
        JOIN bic_codes bc ON blm.bic = bc.bic
        WHERE blm.lei = ?
    """, (lei_code.upper(),)).fetchall()

    output_data = [dict(r) for r in results]

    if output:
        write_output(output_data, output)
        print(f"Found {len(results)} BIC codes for LEI {lei_code} → {output}")
    else:
        if not results:
            print(f"No BIC codes found for LEI {lei_code}")
        else:
            print(f"\nBIC codes for LEI {lei_code}:\n")
            for r in results:
                print(f"{r['bic']:11} | {r['bank_name']:50} | {r['country'] or 'N/A'}")

    return output_data


def show_stats(output=None):
    """Show database statistics."""
    if not BIC_DB.exists():
        print("Error: BIC database not found. Run 'ingest' first.")
        return {}

    db = sqlite3.connect(BIC_DB)

    stats = {}
    stats['total_bic_codes'] = db.execute("SELECT COUNT(*) FROM bic_codes").fetchone()[0]
    stats['total_lei_mappings'] = db.execute("SELECT COUNT(*) FROM bic_lei_mapping").fetchone()[0]
    stats['countries'] = db.execute("SELECT COUNT(DISTINCT country) FROM bic_codes WHERE country IS NOT NULL").fetchone()[0]

    # Top countries
    top_countries = db.execute("""
        SELECT country, COUNT(*) as count
        FROM bic_codes
        WHERE country IS NOT NULL
        GROUP BY country
        ORDER BY count DESC
        LIMIT 10
    """).fetchall()
    stats['top_countries'] = [{'country': r[0], 'count': r[1]} for r in top_countries]

    if output:
        write_output(stats, output)
        print(f"BIC database statistics → {output}")
    else:
        print(f"\nBIC Database Statistics:")
        print(f"  Total BIC codes:     {stats['total_bic_codes']:,}")
        print(f"  BIC→LEI mappings:    {stats['total_lei_mappings']:,}")
        print(f"  Countries:           {stats['countries']}")
        print(f"\nTop 10 countries by BIC count:")
        for item in stats['top_countries']:
            print(f"    {item['country']:2} | {item['count']:,}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="SWIFT BIC Directory tool")
    sub = parser.add_subparsers(dest="command")

    # Download
    sub.add_parser("download", help="Download BIC datasets")

    # Ingest
    sub.add_parser("ingest", help="Ingest BIC data into database")

    # Search
    search_cmd = sub.add_parser("search", help="Search BIC codes by name/location")
    search_cmd.add_argument("query", help="Search query")
    add_output_args(search_cmd)

    # BIC lookup
    bic_cmd = sub.add_parser("bic", help="Lookup specific BIC code")
    bic_cmd.add_argument("code", help="BIC code (e.g., CHASUS33)")
    add_output_args(bic_cmd)

    # Country list
    country_cmd = sub.add_parser("country", help="List BIC codes by country")
    country_cmd.add_argument("code", help="2-letter country code (e.g., US)")
    add_output_args(country_cmd)

    # LEI cross-reference
    lei_cmd = sub.add_parser("lei", help="Cross-reference LEI to BIC codes")
    lei_cmd.add_argument("code", help="LEI code")
    add_output_args(lei_cmd)

    # Stats
    stats_cmd = sub.add_parser("stats", help="Show database statistics")
    add_output_args(stats_cmd)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "download":
        download_datasets()
    elif args.command == "ingest":
        download_datasets()
        ingest_opensanctions()
        ingest_gleif()
    elif args.command == "search":
        search_bic(args.query, getattr(args, 'output', None))
    elif args.command == "bic":
        lookup_bic(args.code, getattr(args, 'output', None))
    elif args.command == "country":
        list_by_country(args.code, getattr(args, 'output', None))
    elif args.command == "lei":
        lookup_lei(args.code, getattr(args, 'output', None))
    elif args.command == "stats":
        show_stats(getattr(args, 'output', None))


if __name__ == "__main__":
    main()
