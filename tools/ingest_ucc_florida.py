#!/usr/bin/env python3
"""
Florida Federal Lien Registration (FLR) ingester.

Downloads bulk FLR data from Florida Division of Corporations SFTP
and loads it into registry.db UCC tables. The FLR dataset contains
federal tax liens (IRS) and UCC-type lien filings.

Data source: sftp.floridados.gov (Public / PubAccess1845!)
  Quarterly: /Public/doc/Quarterly/FLR/ — flrd.zip, flrs.zip, flrf.zip, flre.zip
  Weekly:    /Public/doc/FLR/{DEBTORS,SECURED,FILINGS,EVENTS}/

Format: Fixed-width ASCII (COBOL PIC layout from FLRreadme.txt)
  Filings:  82 chars/record — doc number, dates, status, type, counters
  Debtors:  206 chars/record — filing type, doc number, name, address
  Secured:  206 chars/record — same layout as debtors
  Events:   135 chars/record (compact weekly format) — amendments, releases, terminations

NOTE: This is FLR (Federal Lien Registration), NOT commercial UCC Article 9.
~99% of secured parties are IRS. Commercial UCC filings are at floridaucc.com
(Docufree/Image API, separate system — not accessible via this SFTP).

Usage:
    python tools/ingest_ucc_florida.py download          # Download quarterly ZIPs (~2.2 MB)
    python tools/ingest_ucc_florida.py download --weekly  # Download weekly incrementals
    python tools/ingest_ucc_florida.py ingest             # Parse into registry.db
    python tools/ingest_ucc_florida.py search "Epstein"   # Quick debtor search
    python tools/ingest_ucc_florida.py link-registry      # Match debtors to registry_entities
"""

import argparse
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "datasets" / "fl_flr"

sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

# ══════════════════════════════════════════════════════════
# Fixed-width field definitions from FLRreadme.txt (COBOL PIC layout)
# Positions are 0-indexed, lengths in characters
# ══════════════════════════════════════════════════════════

# FILINGS record: 82 chars
FILING_FIELDS = [
    (0, 12, "doc_number"),           # e.g. '26FLR0000270'
    (12, 8, "filing_date"),          # MMDDYYYY
    (20, 5, "pages"),
    (25, 5, "total_pages"),
    (30, 1, "filing_status"),        # A=Active, T=Terminated, L=Lapsed
    (31, 1, "filing_type"),          # U=UCC Lien, F=FLR (Federal Lien)
    (32, 8, "assessment_date"),      # MMDDYYYY
    (40, 8, "cancellation_date"),    # MMDDYYYY (00000000 if none)
    (48, 8, "expiration_date"),      # MMDDYYYY
    (56, 1, "trans_utility"),        # Y/N
    (57, 5, "event_count"),
    (62, 5, "total_deb_ctr"),
    (67, 5, "total_sec_ctr"),
    (72, 5, "cur_deb_ctr"),
    (77, 5, "cur_sec_ctr"),
]

# DEBTORS record: 206 chars
DEBTOR_FIELDS = [
    (0, 1, "filing_type_flag"),      # F=FLR
    (1, 12, "doc_number"),
    (13, 55, "name"),
    (68, 1, "name_format"),          # C=Corporate, P=Personal
    (69, 44, "address1"),
    (113, 44, "address2"),
    (157, 28, "city"),
    (185, 2, "state"),
    (187, 9, "zip_code"),
    (196, 2, "country"),             # US
    (198, 5, "seq_ctr"),
    (203, 1, "rel_to_filing"),       # C=Current
    (204, 1, "orig_party"),
    (205, 1, "filing_status"),       # A/L/T
]

# SECURED PARTIES record: 206 chars (same layout as debtors)
SECURED_FIELDS = DEBTOR_FIELDS  # Same structure

# EVENT record (compact format): 135 chars
# Full COBOL format is 320 chars per FLRreadme, but bulk files use a compact format.
# Positions verified against actual FLRE.TXT data:
EVENT_FIELDS = [
    (0, 12, "doc_number"),           # Event's own doc number
    (12, 12, "orig_doc_number"),     # Original filing doc number (what this event applies to)
    (28, 8, "event_date"),           # MMDDYYYY (positions 24-27 are packed counters)
    (59, 3, "action_code"),          # COR, COD, A, C, T, R, BAN, PR, etc.
    (62, 70, "verbage"),             # Description
]

# Status mapping
STATUS_MAP = {"A": "active", "T": "terminated", "L": "lapsed"}

# Filing type mapping
TYPE_MAP = {"U": "ucc_lien", "F": "federal_lien"}

# Event action codes
ACTION_MAP = {
    "COR": "certificate_of_release",
    "COD": "certificate_of_discharge",
    "A": "amendment",
    "C": "continuation",
    "T": "termination",
    "R": "release",
    "BAN": "bankruptcy",
    "PR": "partial_release",
    "SUB": "subordination",
    "ASG": "assignment",
}


def _parse_date(s):
    """Parse MMDDYYYY to ISO date."""
    if not s:
        return None
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
        if start + length <= len(line):
            val = line[start:start + length].strip()
            result[name] = val if val else None
        else:
            result[name] = None
    return result


def cmd_download(args):
    """Download Florida FLR bulk data via SFTP."""
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

    if args.weekly:
        # Download weekly incremental files from /Public/doc/FLR/
        for subdir in ["DEBTORS", "SECURED", "FILINGS", "EVENTS"]:
            remote_dir = f"/Public/doc/FLR/{subdir}"
            local_dir = DATA_DIR / "weekly" / subdir.lower()
            local_dir.mkdir(parents=True, exist_ok=True)

            try:
                items = sftp.listdir(remote_dir)
                # Get only recent files (top-level, not year subdirectories)
                txt_files = [f for f in items if f.endswith(".txt")]
                print(f"\n{subdir}: {len(txt_files)} weekly files")

                for fname in sorted(txt_files)[-10:]:  # Last 10 weeks
                    remote_path = f"{remote_dir}/{fname}"
                    local_path = local_dir / fname
                    if local_path.exists() and not args.force:
                        continue
                    try:
                        attr = sftp.stat(remote_path)
                        size_kb = attr.st_size / 1024
                        print(f"  Downloading {fname} ({size_kb:.0f} KB)...")
                        sftp.get(remote_path, str(local_path))
                    except Exception as e:
                        print(f"  Error: {fname}: {e}")
            except Exception as e:
                print(f"  Cannot access {remote_dir}: {e}")
    else:
        # Download quarterly bulk ZIPs from /Public/doc/Quarterly/FLR/
        remote_dir = "/Public/doc/Quarterly/FLR"
        local_dir = DATA_DIR / "quarterly"
        local_dir.mkdir(parents=True, exist_ok=True)

        try:
            items = sftp.listdir(remote_dir)
            print(f"\nQuarterly FLR files: {items}")

            for fname in sorted(items):
                remote_path = f"{remote_dir}/{fname}"
                local_path = local_dir / fname
                if local_path.exists() and not args.force:
                    print(f"  Skipping {fname} (exists, use --force)")
                    continue
                try:
                    attr = sftp.stat(remote_path)
                    size_kb = attr.st_size / 1024
                    print(f"  Downloading {fname} ({size_kb:.0f} KB)...")
                    sftp.get(remote_path, str(local_path))
                except Exception as e:
                    print(f"  Error: {fname}: {e}")
        except Exception as e:
            print(f"Cannot access {remote_dir}: {e}")

    sftp.close()
    transport.close()

    # Extract ZIPs
    zip_dir = DATA_DIR / "quarterly"
    if zip_dir.exists():
        for zf in zip_dir.glob("*.zip"):
            print(f"\nExtracting {zf.name}...")
            extract_dir = DATA_DIR / "extracted"
            extract_dir.mkdir(exist_ok=True)
            with zipfile.ZipFile(zf) as z:
                z.extractall(extract_dir)
                for name in z.namelist():
                    print(f"  {name}")

    print("\nDownload complete. Run 'ingest' to parse the files.")


def cmd_ingest(args):
    """Parse downloaded FL FLR data and load into registry.db."""
    db = get_db()

    # Find all extracted/downloaded text files
    if args.file:
        files = [Path(args.file)]
    else:
        files = []
        for search_dir in [DATA_DIR / "extracted", DATA_DIR / "weekly"]:
            if search_dir.exists():
                files.extend(sorted(search_dir.rglob("*.txt")))
                files.extend(sorted(search_dir.rglob("*.TXT")))
                files.extend(sorted(search_dir.rglob("*.dat")))
                files.extend(sorted(search_dir.rglob("*.DAT")))
        files = [f for f in files if f.is_file() and f.stat().st_size > 100
                 and "readme" not in f.name.lower()]

    if not files:
        print(f"No data files found. Run 'download' first.", file=sys.stderr)
        sys.exit(1)

    total_filings = 0
    total_debtors = 0
    total_secured = 0
    total_events = 0

    # Sort files: filings first, then debtors/secured, then events last
    # (events reference filings, debtors create stub filings, so order matters)
    def _sort_key(f):
        fn = f.name.lower()
        if "flrf" in fn or "filing" in fn:
            return 0
        if "flrd" in fn or "debtor" in fn:
            return 1
        if "flrs" in fn or "secured" in fn:
            return 2
        if "flre" in fn or "event" in fn:
            return 3
        return 4
    files.sort(key=_sort_key)

    for filepath in files:
        fname = filepath.name.lower()
        size_kb = filepath.stat().st_size / 1024
        print(f"\nProcessing {filepath.name} ({size_kb:.0f} KB)...")

        # Determine file type from name
        if "flrf" in fname or "filing" in fname:
            count = _ingest_filings(db, filepath)
            total_filings += count
            print(f"  Loaded {count:,} filing records")
        elif "flrd" in fname or "debtor" in fname:
            count = _ingest_debtors(db, filepath)
            total_debtors += count
            print(f"  Loaded {count:,} debtor records")
        elif "flrs" in fname or "secured" in fname:
            count = _ingest_secured(db, filepath)
            total_secured += count
            print(f"  Loaded {count:,} secured party records")
        elif "flre" in fname or "event" in fname:
            count = _ingest_events(db, filepath)
            total_events += count
            print(f"  Loaded {count:,} event records")
        else:
            # Try to detect from line length
            with open(filepath, "r", encoding="latin-1", errors="replace") as f:
                sample = f.readline()
            if len(sample.strip()) == 82:
                count = _ingest_filings(db, filepath)
                total_filings += count
                print(f"  Detected as filings, loaded {count:,}")
            elif len(sample.strip()) == 206:
                # Could be debtor or secured — check position 0
                flag = sample[0:1]
                count = _ingest_debtors(db, filepath)
                total_debtors += count
                print(f"  Detected as debtors/secured, loaded {count:,}")
            else:
                print(f"  Unknown format (line length={len(sample.strip())}), skipping")

    # Rebuild FTS
    print("\nRebuilding search indexes...")
    try:
        _rebuild_fts(db)
    except Exception as e:
        print(f"  FTS rebuild warning: {e}")

    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
        VALUES ('fl', 'sftp_flr', ?, ?)
    """, [total_filings, f"Filings: {total_filings}, Debtors: {total_debtors}, "
          f"Secured: {total_secured}, Events: {total_events}"])
    db.commit()

    print(f"\nIngest complete: {total_filings:,} filings, {total_debtors:,} debtors, "
          f"{total_secured:,} secured parties, {total_events:,} events")


def _ingest_filings(db, filepath):
    """Ingest a filings file (82 chars/record)."""
    count = 0
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line.strip()) < 30:
                continue
            rec = _parse_line(line, FILING_FIELDS)
            if not rec.get("doc_number"):
                continue

            status = STATUS_MAP.get(rec["filing_status"], rec["filing_status"])
            ftype = TYPE_MAP.get(rec["filing_type"], rec["filing_type"])

            try:
                db.execute("""
                    INSERT OR IGNORE INTO ucc_filings
                    (source_jurisdiction, filing_number, filing_type, filing_date,
                     lapse_date, status, raw_data)
                    VALUES ('fl', ?, ?, ?, ?, ?, ?)
                """, [
                    rec["doc_number"], ftype,
                    _parse_date(rec["filing_date"]),
                    _parse_date(rec["expiration_date"]),
                    status,
                    json.dumps(rec, default=str),
                ])
            except sqlite3.IntegrityError:
                pass

            count += 1
            if count % 5000 == 0:
                db.commit()

    db.commit()
    return count


def _ingest_debtors(db, filepath):
    """Ingest a debtors file (206 chars/record)."""
    count = 0
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line.strip()) < 60:
                continue
            rec = _parse_line(line, DEBTOR_FIELDS)
            if not rec.get("doc_number") or not rec.get("name"):
                continue

            # Find or create the filing
            filing_row = db.execute(
                "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                [rec["doc_number"]]
            ).fetchone()

            if not filing_row:
                # Create a stub filing if one doesn't exist yet
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO ucc_filings
                        (source_jurisdiction, filing_number, filing_type, status)
                        VALUES ('fl', ?, 'unknown', ?)
                    """, [rec["doc_number"], STATUS_MAP.get(rec["filing_status"], "active")])
                except sqlite3.IntegrityError:
                    pass
                filing_row = db.execute(
                    "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                    [rec["doc_number"]]
                ).fetchone()
                if not filing_row:
                    continue

            filing_id = filing_row[0]
            debtor_type = "individual" if rec.get("name_format") == "P" else "organization"

            addr = rec["address1"] or ""
            if rec["address2"]:
                addr += " " + rec["address2"]
            addr = addr.strip() or None

            try:
                db.execute("""
                    INSERT OR IGNORE INTO ucc_debtors
                    (filing_id, debtor_name, debtor_type, address, city, state, zip, country)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [filing_id, rec["name"], debtor_type, addr,
                      rec["city"], rec["state"], rec["zip_code"], rec["country"]])
            except sqlite3.IntegrityError:
                pass

            count += 1
            if count % 5000 == 0:
                db.commit()

    db.commit()
    return count


def _ingest_secured(db, filepath):
    """Ingest a secured parties file (206 chars/record, same layout as debtors)."""
    count = 0
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line.strip()) < 60:
                continue
            rec = _parse_line(line, SECURED_FIELDS)
            if not rec.get("doc_number") or not rec.get("name"):
                continue

            filing_row = db.execute(
                "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                [rec["doc_number"]]
            ).fetchone()

            if not filing_row:
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO ucc_filings
                        (source_jurisdiction, filing_number, filing_type, status)
                        VALUES ('fl', ?, 'unknown', ?)
                    """, [rec["doc_number"], STATUS_MAP.get(rec["filing_status"], "active")])
                except sqlite3.IntegrityError:
                    pass
                filing_row = db.execute(
                    "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                    [rec["doc_number"]]
                ).fetchone()
                if not filing_row:
                    continue

            filing_id = filing_row[0]
            party_type = "individual" if rec.get("name_format") == "P" else "organization"

            addr = rec["address1"] or ""
            if rec["address2"]:
                addr += " " + rec["address2"]
            addr = addr.strip() or None

            try:
                db.execute("""
                    INSERT OR IGNORE INTO ucc_secured_parties
                    (filing_id, party_name, party_type, address, city, state, zip, country)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [filing_id, rec["name"], party_type, addr,
                      rec["city"], rec["state"], rec["zip_code"], rec["country"]])
            except sqlite3.IntegrityError:
                pass

            count += 1
            if count % 5000 == 0:
                db.commit()

    db.commit()
    return count


def _ingest_events(db, filepath):
    """Ingest an events file (135 chars/record compact format)."""
    count = 0
    skipped = 0
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line.strip()) < 30:
                continue
            rec = _parse_line(line, EVENT_FIELDS)
            if not rec.get("orig_doc_number"):
                continue

            # Look up the ORIGINAL filing this event applies to
            filing_row = db.execute(
                "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                [rec["orig_doc_number"]]
            ).fetchone()
            if not filing_row:
                # Create a stub filing for old/terminated filings not in FLRF.TXT
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO ucc_filings
                        (source_jurisdiction, filing_number, filing_type, status)
                        VALUES ('fl', ?, 'unknown', 'historical')
                    """, [rec["orig_doc_number"]])
                except sqlite3.IntegrityError:
                    pass
                filing_row = db.execute(
                    "SELECT id FROM ucc_filings WHERE source_jurisdiction='fl' AND filing_number=?",
                    [rec["orig_doc_number"]]
                ).fetchone()
                if not filing_row:
                    skipped += 1
                    continue

            action = ACTION_MAP.get(rec.get("action_code", ""), rec.get("action_code", ""))

            try:
                db.execute("""
                    INSERT OR IGNORE INTO ucc_filing_history
                    (filing_id, action_type, action_date, action_filing_number, description)
                    VALUES (?, ?, ?, ?, ?)
                """, [filing_row[0], action,
                      _parse_date(rec["event_date"]),
                      rec.get("doc_number"),
                      rec.get("verbage")])
            except sqlite3.IntegrityError:
                pass

            count += 1
            if count % 5000 == 0:
                db.commit()

    db.commit()
    if skipped > 0:
        print(f"  ({skipped:,} events skipped — no matching filing)")
    return count


def cmd_search(args):
    """Quick debtor search after ingest."""
    db = get_db()
    rows = db.execute("""
        SELECT d.debtor_name, d.debtor_type, d.address, d.city, d.state,
               f.filing_number, f.filing_type, f.filing_date, f.status
        FROM ucc_debtors d
        JOIN ucc_filings f ON d.filing_id = f.id
        WHERE f.source_jurisdiction = 'fl' AND d.debtor_name LIKE ?
        ORDER BY d.debtor_name LIMIT 20
    """, [f"%{args.query}%"]).fetchall()

    print(f"Found {len(rows)} FL FLR debtors matching '{args.query}'")
    for r in rows:
        dtype = f" [{r['debtor_type']}]" if r["debtor_type"] else ""
        ftype = r["filing_type"] or "?"
        print(f"  {r['debtor_name']}{dtype}")
        print(f"    Filing #{r['filing_number']} ({ftype}, {r['status'] or '?'}) filed {r['filing_date'] or '?'}")
        addr = r["address"] or ""
        if r["city"]:
            addr += f", {r['city']}"
        if r["state"]:
            addr += f", {r['state']}"
        if addr:
            print(f"    Address: {addr}")
        print()


def cmd_link_registry(args):
    """Match UCC/FLR debtors to registry_entities for cross-linking."""
    db = get_db()

    unlinked = db.execute("""
        SELECT d.id, d.debtor_name, f.source_jurisdiction
        FROM ucc_debtors d
        JOIN ucc_filings f ON d.filing_id = f.id
        WHERE d.registry_entity_id IS NULL AND f.source_jurisdiction = 'fl'
    """).fetchall()

    print(f"Found {len(unlinked)} unlinked FL debtors")
    matched = 0

    for d in unlinked:
        # Exact match (case-insensitive)
        entity = db.execute("""
            SELECT id FROM registry_entities
            WHERE source_jurisdiction = 'fl' AND UPPER(entity_name) = UPPER(?)
        """, [d["debtor_name"]]).fetchone()

        if not entity:
            # Normalized match: strip suffixes
            normalized = d["debtor_name"].upper().replace(",", "").strip()
            for suffix in [" LLC", " INC", " CORP", " LTD", " LP", " LLP", " CO"]:
                normalized = normalized.rstrip(suffix).rstrip(".")
            entity = db.execute("""
                SELECT id FROM registry_entities
                WHERE source_jurisdiction = 'fl' AND UPPER(entity_name) LIKE ?
                LIMIT 1
            """, [f"%{normalized}%"]).fetchone()

        if entity:
            db.execute("UPDATE ucc_debtors SET registry_entity_id = ? WHERE id = ?",
                       [entity[0], d["id"]])
            matched += 1

    db.commit()
    print(f"Linked {matched} debtors to registry entities ({len(unlinked) - matched} unmatched)")


def main():
    parser = argparse.ArgumentParser(
        description="Florida Federal Lien Registration (FLR) ingester",
        epilog="NOTE: This ingests FLR (tax liens). Commercial UCC Article 9 "
               "filings are at floridaucc.com (separate system)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download", help="Download FLR data from SFTP")
    p.add_argument("--weekly", action="store_true",
                   help="Download weekly incrementals (default: quarterly bulk)")
    p.add_argument("--force", action="store_true", help="Re-download existing files")

    p = sub.add_parser("ingest", help="Parse and load downloaded data")
    p.add_argument("--file", help="Specific file to ingest")

    p = sub.add_parser("search", help="Quick debtor search")
    p.add_argument("query")

    sub.add_parser("link-registry", help="Match debtors to registry_entities")

    args = parser.parse_args()
    handlers = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
        "link-registry": cmd_link_registry,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
