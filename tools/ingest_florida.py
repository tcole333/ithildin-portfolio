#!/usr/bin/env python3
"""
Florida SunBiz corporate registry ingester.

Downloads bulk corporate data from Florida Division of Corporations SFTP
and loads it into registry.db using the unified schema.

Data source: sftp.floridados.gov (Public / PubAccess1845!)
Format: Fixed-width ASCII text (1440 chars/record for corp data, 662 chars for events)

Usage:
    python tools/ingest_florida.py download              # Download latest quarterly data
    python tools/ingest_florida.py download --daily       # Download daily incremental
    python tools/ingest_florida.py ingest                 # Parse and load downloaded files
    python tools/ingest_florida.py ingest --file data/fl/cor1.dat  # Load specific file
    python tools/ingest_florida.py search "Epstein"       # Quick search after ingest
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# We'll import paramiko lazily since it might not be installed
DATA_DIR = Path(__file__).parent.parent / "datasets" / "fl_sunbiz"

# Add parent to path for registry DB access
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

# ── Fixed-width field definitions for corporate data file ──
# (start_pos_0indexed, length, field_name)
CORP_FIELDS = [
    (0, 12, "corp_number"),
    (12, 192, "corp_name"),
    (204, 1, "status"),
    (205, 15, "filing_type"),
    # Principal address
    (220, 42, "princ_addr1"),
    (262, 42, "princ_addr2"),
    (304, 28, "princ_city"),
    (332, 2, "princ_state"),
    (334, 10, "princ_zip"),
    (344, 2, "princ_country"),
    # Mailing address
    (346, 42, "mail_addr1"),
    (388, 42, "mail_addr2"),
    (430, 28, "mail_city"),
    (458, 2, "mail_state"),
    (460, 10, "mail_zip"),
    (470, 2, "mail_country"),
    # Dates and IDs
    (472, 8, "file_date"),       # MMDDYYYY
    (480, 14, "fei_number"),     # Federal EIN
    (494, 1, "more_officers"),   # Y if >6 officers
    (495, 8, "last_transaction_date"),
    (503, 2, "state_country"),   # State/country of incorporation
    # Report years (skip for now)
    # Registered agent
    (544, 42, "ra_name"),
    (586, 1, "ra_type"),         # P=person, C=corporation
    (587, 42, "ra_addr"),
    (629, 28, "ra_city"),
    (657, 2, "ra_state"),
    (659, 9, "ra_zip"),
]

# Officers: 6 blocks, each 128 chars starting at position 668
# Each block: title(4) + type(1) + name(42) + addr(42) + city(28) + state(2) + zip(9)
OFFICER_BLOCK_START = 668
OFFICER_BLOCK_LEN = 128
OFFICER_FIELDS = [
    (0, 4, "title"),
    (4, 1, "type"),       # empty or P/C
    (5, 42, "name"),
    (47, 42, "addr"),
    (89, 28, "city"),
    (117, 2, "state"),
    (119, 9, "zip"),
]

# Event file fields
EVENT_FIELDS = [
    (0, 12, "doc_number"),
    (17, 20, "event_code"),
    (37, 40, "event_desc"),
    (77, 8, "event_eff_date"),    # MMDDYYYY
    (85, 8, "event_filed_date"),  # MMDDYYYY
    (210, 192, "event_corp_name"),
]

# Filing type mapping
FILING_TYPE_MAP = {
    "DOMP": "domestic_profit",
    "DOMNP": "domestic_nonprofit",
    "FORP": "foreign_profit",
    "FORNP": "foreign_nonprofit",
    "DOMLP": "domestic_lp",
    "FORLP": "foreign_lp",
    "FLAL": "domestic_llc",
    "FORL": "foreign_llc",
    "NPREG": "nonprofit_registered",
    "TRUST": "trust",
    "AGENT": "registered_agent",
}


def _parse_date(s):
    """Parse MMDDYYYY to ISO date."""
    s = s.strip()
    if not s or s == "00000000" or len(s) < 8:
        return None
    try:
        return f"{s[4:8]}-{s[0:2]}-{s[2:4]}"
    except (IndexError, ValueError):
        return None


def _parse_line(line, fields):
    """Parse a fixed-width line into a dict."""
    result = {}
    for start, length, name in fields:
        val = line[start:start + length].strip()
        result[name] = val if val else None
    return result


def _parse_officers(line):
    """Parse up to 6 officer blocks from a corporate data line."""
    officers = []
    for i in range(6):
        offset = OFFICER_BLOCK_START + (i * OFFICER_BLOCK_LEN)
        block = line[offset:offset + OFFICER_BLOCK_LEN]
        if len(block) < OFFICER_BLOCK_LEN:
            break
        officer = {}
        for start, length, name in OFFICER_FIELDS:
            val = block[start:start + length].strip()
            officer[name] = val if val else None
        if officer.get("name"):
            officers.append(officer)
    return officers


def cmd_download(args):
    """Download Florida bulk data via SFTP."""
    try:
        import paramiko
    except ImportError:
        print("ERROR: paramiko not installed. Run: uv pip install paramiko", file=sys.stderr)
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Connecting to sftp.floridados.gov...")
    transport = paramiko.Transport(("sftp.floridados.gov", 22))
    transport.connect(username="Public", password="PubAccess1845!")
    sftp = paramiko.SFTPClient.from_transport(transport)

    # List available directories
    root_items = sftp.listdir("/")
    print(f"Root directory contents: {root_items}")

    if args.daily:
        # Download daily files from /Public/doc/cor/
        target_dir = "/Public/doc/cor"
        local_dir = DATA_DIR / "daily"
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            items = sftp.listdir(target_dir)
            # Get the most recent daily file
            txt_files = [i for i in items if i.endswith('c.txt')]
            txt_files.sort(reverse=True)
            to_download = txt_files[:5]  # Last 5 days
            print(f"\nDownloading {len(to_download)} recent daily files from {target_dir}:")
            for item in to_download:
                remote_path = f"{target_dir}/{item}"
                local_path = local_dir / item
                if local_path.exists() and not args.force:
                    print(f"  Skipping {item} (already exists)")
                    continue
                attr = sftp.stat(remote_path)
                size_mb = attr.st_size / (1024 * 1024)
                print(f"  Downloading {item} ({size_mb:.1f} MB)...")
                sftp.get(remote_path, str(local_path))
                print(f"  Done: {local_path}")
        except FileNotFoundError:
            print(f"Directory {target_dir} not found.")
    else:
        # Download quarterly bulk data
        # Primary: /Public/doc/Quarterly/Cor/ has cordata.zip (full) and corevt.zip (events)
        # Alternative: /Public/doc/AG/corprindata.zip (principal data, smaller)
        quarterly_dir = "/Public/doc/Quarterly/Cor"
        local_dir = DATA_DIR / "quarterly"
        local_dir.mkdir(parents=True, exist_ok=True)

        try:
            items = sftp.listdir_attr(quarterly_dir)
            print(f"\nFiles in {quarterly_dir}:")
            for item in sorted(items, key=lambda x: x.filename):
                size_mb = item.st_size / (1024 * 1024)
                print(f"  {item.filename} ({size_mb:.1f} MB)")

            # Download all files
            for item in sorted(items, key=lambda x: x.filename):
                remote_path = f"{quarterly_dir}/{item.filename}"
                local_path = local_dir / item.filename
                if local_path.exists() and not args.force:
                    print(f"  Skipping {item.filename} (already exists, use --force to re-download)")
                    continue
                size_mb = item.st_size / (1024 * 1024)
                print(f"  Downloading {item.filename} ({size_mb:.1f} MB)...")
                sftp.get(remote_path, str(local_path))
                print(f"  Done: {local_path}")

        except FileNotFoundError:
            print(f"Directory {quarterly_dir} not found. Trying /Public/doc/AG/...")
            try:
                ag_dir = "/Public/doc/AG"
                remote_path = f"{ag_dir}/corprindata.zip"
                local_path = local_dir / "corprindata.zip"
                if local_path.exists() and not args.force:
                    print(f"  Skipping corprindata.zip (already exists)")
                else:
                    attr = sftp.stat(remote_path)
                    size_mb = attr.st_size / (1024 * 1024)
                    print(f"  Downloading corprindata.zip ({size_mb:.1f} MB)...")
                    sftp.get(remote_path, str(local_path))
                    print(f"  Done: {local_path}")
            except Exception as e:
                print(f"Failed to download from AG: {e}")

    try:
        sftp.close()
        transport.close()
    except Exception:
        pass

    print("\nDownload complete.")


def cmd_ingest(args):
    """Parse downloaded FL data and load into registry.db."""
    db = get_db()

    if args.file:
        files = [Path(args.file)]
    else:
        # Find all .dat or .txt files in the quarterly directory
        quarterly_dir = DATA_DIR / "quarterly"
        if not quarterly_dir.exists():
            print(f"No data found at {quarterly_dir}. Run 'download' first.", file=sys.stderr)
            sys.exit(1)
        files = sorted(quarterly_dir.glob("*"))
        # Filter to actual data files (not directories, not tiny files)
        files = [f for f in files if f.is_file() and f.stat().st_size > 1000]

    if not files:
        print("No data files found to ingest.", file=sys.stderr)
        sys.exit(1)

    total_entities = 0
    total_officers = 0
    total_events = 0

    for filepath in files:
        fname = filepath.name.lower()
        print(f"\nProcessing {filepath.name} ({filepath.stat().st_size / (1024*1024):.1f} MB)...")

        # Determine if this is a corporate data file or event file
        # Corporate data files typically named corXX.dat, event files named evntXX.dat
        is_event = "evnt" in fname or "event" in fname

        if is_event:
            count = _ingest_events(db, filepath)
            total_events += count
            print(f"  Loaded {count:,} filing events")
        else:
            ent_count, off_count = _ingest_corps(db, filepath)
            total_entities += ent_count
            total_officers += off_count
            print(f"  Loaded {ent_count:,} entities, {off_count:,} officers")

    # Rebuild FTS
    print("\nRebuilding search indexes...")
    try:
        _rebuild_fts(db)
    except Exception as e:
        print(f"  FTS rebuild warning: {e}")

    # Log the ingest
    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
        VALUES ('fl', 'sftp_bulk', ?, ?)
    """, [total_entities, f"Entities: {total_entities}, Officers: {total_officers}, Events: {total_events}"])
    db.commit()

    print(f"\nIngest complete: {total_entities:,} entities, {total_officers:,} officers, {total_events:,} events")


def _ingest_corps(db, filepath):
    """Ingest a corporate data file."""
    entity_count = 0
    officer_count = 0
    batch_entities = []
    batch_officers = []
    batch_agents = []

    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            if len(line) < 500:  # Skip short/header lines
                continue

            try:
                rec = _parse_line(line, CORP_FIELDS)
            except Exception:
                continue

            if not rec.get("corp_number") or not rec.get("corp_name"):
                continue

            # Map status
            status_map = {"A": "active", "I": "inactive"}
            status = status_map.get(rec["status"], rec["status"])

            # Map filing type
            filing_type = FILING_TYPE_MAP.get(
                (rec["filing_type"] or "").strip(),
                (rec["filing_type"] or "").strip().lower()
            )

            # Build address
            princ_addr = rec["princ_addr1"] or ""
            if rec["princ_addr2"]:
                princ_addr += " " + rec["princ_addr2"]
            princ_addr = princ_addr.strip() or None

            mail_addr = rec["mail_addr1"] or ""
            if rec["mail_addr2"]:
                mail_addr += " " + rec["mail_addr2"]
            mail_addr = mail_addr.strip() or None

            batch_entities.append((
                "fl",
                rec["corp_number"],
                rec["corp_name"],
                filing_type,
                status,
                _parse_date(rec["file_date"] or ""),
                None,  # dissolution_date (from events)
                _parse_date(rec["last_transaction_date"] or ""),
                rec["fei_number"],
                rec["state_country"],
                None,  # purpose
                princ_addr,
                rec["princ_city"],
                rec["princ_state"],
                rec["princ_zip"],
                rec["princ_country"],
                mail_addr,
                rec["mail_city"],
                rec["mail_state"],
                rec["mail_zip"],
                rec["mail_country"],
                f"https://search.sunbiz.org/Inquiry/CorporationSearch/SearchByNumber?searchNumber={rec['corp_number']}",
            ))

            # Parse officers
            officers = _parse_officers(line)
            for o in officers:
                batch_officers.append((
                    rec["corp_number"],  # Will be resolved to entity_id
                    o["name"],
                    o["title"],
                    {"P": "person", "C": "corporation"}.get(o["type"], None),
                    o["addr"],
                    o["city"],
                    o["state"],
                    o["zip"],
                ))
                officer_count += 1

            # Registered agent
            if rec["ra_name"]:
                batch_agents.append((
                    rec["corp_number"],
                    rec["ra_name"],
                    {"P": "person", "C": "corporation"}.get(rec["ra_type"], None),
                    rec["ra_addr"],
                    rec["ra_city"],
                    rec["ra_state"],
                    rec["ra_zip"],
                ))

            entity_count += 1

            # Batch insert every 10000 records
            if entity_count % 10000 == 0:
                _flush_batch(db, batch_entities, batch_officers, batch_agents)
                batch_entities.clear()
                batch_officers.clear()
                batch_agents.clear()
                if entity_count % 100000 == 0:
                    print(f"    {entity_count:,} entities processed...")

    # Flush remaining
    _flush_batch(db, batch_entities, batch_officers, batch_agents)
    db.commit()

    return entity_count, officer_count


def _flush_batch(db, entities, officers, agents):
    """Insert a batch of records into the database."""
    # Disable FK checks during bulk insert (INSERT OR REPLACE triggers DELETE first)
    db.execute("PRAGMA foreign_keys=OFF")
    # Insert entities
    db.executemany("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, dissolution_date, last_filing_date, ein,
            state_of_formation, purpose,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip, mailing_country,
            source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, entities)

    # For officers and agents, we need the entity_id from the source_id
    # Build a mapping from corp_number to entity_id
    if officers or agents:
        corp_numbers = set()
        for o in officers:
            corp_numbers.add(o[0])
        for a in agents:
            corp_numbers.add(a[0])

        id_map = {}
        for cn in corp_numbers:
            row = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='fl' AND source_id=?",
                [cn]
            ).fetchone()
            if row:
                id_map[cn] = row[0]

    # Insert officers
    for o in officers:
        entity_id = id_map.get(o[0])
        if not entity_id:
            continue
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (entity_id, o[1], o[2], o[3], o[4], o[5], o[6], o[7]))
        except sqlite3.IntegrityError:
            pass

    # Insert agents
    for a in agents:
        entity_id = id_map.get(a[0])
        if not entity_id:
            continue
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, agent_type, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (entity_id, a[1], a[2], a[3], a[4], a[5], a[6]))
        except sqlite3.IntegrityError:
            pass

    # Re-enable FK checks
    db.execute("PRAGMA foreign_keys=ON")


def _ingest_events(db, filepath):
    """Ingest an event/filing history file."""
    count = 0
    batch = []

    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 100:
                continue

            try:
                rec = _parse_line(line, EVENT_FIELDS)
            except Exception:
                continue

            if not rec.get("doc_number"):
                continue

            batch.append((
                rec["doc_number"],
                rec["event_code"],
                _parse_date(rec["event_filed_date"] or ""),
                _parse_date(rec["event_eff_date"] or ""),
                rec["event_desc"],
                rec["event_corp_name"],
            ))
            count += 1

            if count % 10000 == 0:
                _flush_events(db, batch)
                batch.clear()
                if count % 100000 == 0:
                    print(f"    {count:,} events processed...")

    _flush_events(db, batch)
    db.commit()
    return count


def _flush_events(db, events):
    """Insert event records, resolving entity_id from corp_number."""
    for e in events:
        corp_number, filing_type, filing_date, effective_date, description, name_at_time = e
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='fl' AND source_id=?",
            [corp_number]
        ).fetchone()
        if not row:
            continue
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, effective_date, description, entity_name_at_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (row[0], filing_type, filing_date, effective_date, description, name_at_time))
        except sqlite3.IntegrityError:
            pass


def cmd_search(args):
    """Quick search after ingest."""
    db = get_db()
    rows = db.execute("""
        SELECT * FROM registry_entities
        WHERE source_jurisdiction = 'fl' AND entity_name LIKE ?
        ORDER BY entity_name LIMIT 20
    """, [f"%{args.query}%"]).fetchall()

    print(f"Found {len(rows)} FL entities matching '{args.query}'")
    for r in rows:
        from query_registry import format_entity
        print(format_entity(r))
        print()


def main():
    parser = argparse.ArgumentParser(description="Florida SunBiz corporate registry ingester")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download", help="Download bulk data from SFTP")
    p.add_argument("--daily", action="store_true", help="Download daily incremental instead of quarterly")
    p.add_argument("--force", action="store_true", help="Re-download even if files exist")

    p = sub.add_parser("ingest", help="Parse and load downloaded data")
    p.add_argument("--file", help="Specific file to ingest")

    p = sub.add_parser("search", help="Quick search FL entities")
    p.add_argument("query")

    args = parser.parse_args()
    handlers = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
