#!/usr/bin/env python3
"""
OpenSanctions bulk data query tool for OSINT investigations.

Downloads, ingests, and queries OpenSanctions data locally — sanctions lists
(OFAC/EU/UN), PEP databases (200+ countries), and crime/terrorism lists.

Uses bulk NDJSON downloads parsed into a local SQLite database with FTS5 for
fast search. No Docker or self-hosted app required.

Usage:
    python tools/query_opensanctions.py download
    python tools/query_opensanctions.py download --dataset sanctions
    python tools/query_opensanctions.py download --dataset peps
    python tools/query_opensanctions.py ingest
    python tools/query_opensanctions.py search "Oleg Deripaska"
    python tools/query_opensanctions.py search "Deripaska" --schema Person --topic sanction
    python tools/query_opensanctions.py search "DP World" --schema Company --country ae
    python tools/query_opensanctions.py entity ofac-12345
    python tools/query_opensanctions.py match-entities
    python tools/query_opensanctions.py pep-check "Ehud Barak"
    python tools/query_opensanctions.py stats
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "datasets" / "opensanctions.db"
DOWNLOAD_DIR = PROJECT_ROOT / "datasets" / "opensanctions"
INVESTIGATION_DB = PROJECT_ROOT / "investigation.db"

DATASET_URLS = {
    "default": "https://data.opensanctions.org/datasets/latest/default/entities.ftm.json",
    "sanctions": "https://data.opensanctions.org/datasets/latest/sanctions/entities.ftm.json",
    "peps": "https://data.opensanctions.org/datasets/latest/peps/entities.ftm.json",
}

SCHEMAS = [
    "Person", "Company", "Organization", "LegalEntity",
    "Vessel", "Aircraft", "CryptoWallet", "Security",
]

TOPICS = ["sanction", "debarment", "crime", "pep", "poi"]


def _get_db(readonly=False):
    """Open or create the OpenSanctions SQLite database."""
    if readonly and not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        print("Run 'python tools/query_opensanctions.py download' then 'ingest' first.", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def _init_db(db):
    """Create tables if they don't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS os_entities (
            id TEXT PRIMARY KEY,
            caption TEXT,
            schema TEXT,
            names TEXT,
            birth_date TEXT,
            countries TEXT,
            topics TEXT,
            datasets TEXT,
            first_seen TEXT,
            last_seen TEXT,
            properties TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_os_schema ON os_entities(schema);
        CREATE INDEX IF NOT EXISTS idx_os_first_seen ON os_entities(first_seen);
        CREATE INDEX IF NOT EXISTS idx_os_last_seen ON os_entities(last_seen);
    """)
    db.commit()


def _init_fts(db):
    """Create FTS5 virtual table for full-text search."""
    db.executescript("""
        DROP TABLE IF EXISTS os_entities_fts;
        CREATE VIRTUAL TABLE os_entities_fts USING fts5(
            id,
            caption,
            names,
            content=os_entities,
            content_rowid=rowid
        );

        INSERT INTO os_entities_fts(os_entities_fts) VALUES('rebuild');
    """)
    db.commit()


def _parse_ftm_entity(line):
    """Parse a single FTM NDJSON line into a row tuple."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    eid = obj.get("id", "")
    if not eid:
        return None

    caption = obj.get("caption", "")
    schema = obj.get("schema", "")
    props = obj.get("properties", {})

    # Collect all name variants
    all_names = []
    for name_field in ("name", "alias", "weakAlias", "previousName",
                       "tradingName", "registrationName"):
        all_names.extend(props.get(name_field, []))
    # Deduplicate while preserving order
    seen = set()
    unique_names = []
    for n in all_names:
        nl = n.lower()
        if nl not in seen:
            seen.add(nl)
            unique_names.append(n)

    names_json = json.dumps(unique_names) if unique_names else "[]"

    birth_date_list = props.get("birthDate", [])
    birth_date = birth_date_list[0] if birth_date_list else None

    countries = list(set(
        props.get("country", []) + props.get("nationality", []) +
        props.get("jurisdiction", [])
    ))
    countries_json = json.dumps(countries) if countries else "[]"

    topics = props.get("topics", [])
    topics_json = json.dumps(topics) if topics else "[]"

    datasets = obj.get("datasets", [])
    datasets_json = json.dumps(datasets) if datasets else "[]"

    first_seen = obj.get("first_seen", "")
    last_seen = obj.get("last_seen", "")

    properties_json = json.dumps(props)

    return (
        eid, caption, schema, names_json, birth_date,
        countries_json, topics_json, datasets_json,
        first_seen, last_seen, properties_json
    )


def _format_entity(row, verbose=False):
    """Format an entity row for display."""
    lines = []

    schema = row["schema"] or "?"
    caption = row["caption"] or "?"
    topics_raw = row["topics"] or "[]"
    try:
        topics = json.loads(topics_raw)
    except (json.JSONDecodeError, TypeError):
        topics = []

    topic_tags = ""
    if topics:
        tag_map = {
            "sanction": "SANCTIONED",
            "pep": "PEP",
            "crime": "CRIME",
            "debarment": "DEBARRED",
            "poi": "POI",
        }
        tags = [tag_map.get(t, t.upper()) for t in topics]
        topic_tags = " [" + ", ".join(tags) + "]"

    lines.append(f"  [{schema}] {caption}{topic_tags}")
    lines.append(f"    ID: {row['id']}")

    # Names/aliases
    try:
        names = json.loads(row["names"] or "[]")
    except (json.JSONDecodeError, TypeError):
        names = []
    if len(names) > 1:
        aliases = [n for n in names[1:]]
        if aliases:
            display = aliases[:5]
            suffix = f" (+{len(aliases)-5} more)" if len(aliases) > 5 else ""
            lines.append(f"    Aliases: {'; '.join(display)}{suffix}")

    if row["birth_date"]:
        lines.append(f"    DOB: {row['birth_date']}")

    try:
        countries = json.loads(row["countries"] or "[]")
    except (json.JSONDecodeError, TypeError):
        countries = []
    if countries:
        lines.append(f"    Countries: {', '.join(sorted(countries))}")

    try:
        datasets = json.loads(row["datasets"] or "[]")
    except (json.JSONDecodeError, TypeError):
        datasets = []
    if datasets:
        display_ds = datasets[:8]
        suffix = f" (+{len(datasets)-8} more)" if len(datasets) > 8 else ""
        lines.append(f"    Datasets: {', '.join(display_ds)}{suffix}")

    if row["first_seen"]:
        lines.append(f"    First seen: {row['first_seen']}")
    if row["last_seen"]:
        lines.append(f"    Last seen: {row['last_seen']}")

    if verbose:
        try:
            props = json.loads(row["properties"] or "{}")
        except (json.JSONDecodeError, TypeError):
            props = {}
        skip_keys = {"name", "alias", "weakAlias", "previousName",
                     "tradingName", "registrationName", "birthDate",
                     "country", "nationality", "jurisdiction", "topics"}
        for key in sorted(props.keys()):
            if key in skip_keys:
                continue
            vals = props[key]
            if vals:
                display_vals = vals[:5]
                suffix = f" (+{len(vals)-5} more)" if len(vals) > 5 else ""
                joined = "; ".join(str(v) for v in display_vals)
                lines.append(f"    {key}: {joined}{suffix}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_download(args):
    """Download the bulk NDJSON file from OpenSanctions."""
    dataset = args.dataset or "default"
    url = DATASET_URLS.get(dataset)
    if not url:
        print(f"ERROR: Unknown dataset '{dataset}'. Choose from: {', '.join(DATASET_URLS.keys())}")
        sys.exit(1)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"entities.{dataset}.ftm.json"
    out_path = DOWNLOAD_DIR / filename

    print(f"Downloading OpenSanctions '{dataset}' dataset...")
    print(f"  URL: {url}")
    print(f"  Destination: {out_path}")
    print()

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0)",
    }
    req = Request(url, headers=headers)

    try:
        resp = urlopen(req, timeout=60)
    except HTTPError as e:
        print(f"ERROR: HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Cannot reach OpenSanctions: {e.reason}", file=sys.stderr)
        sys.exit(1)

    content_length = resp.headers.get("Content-Length")
    total_size = int(content_length) if content_length else None
    if total_size:
        print(f"  File size: {total_size / 1024 / 1024:.1f} MB")

    downloaded = 0
    last_report = 0
    start_time = time.time()

    with open(out_path, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)

            # Progress every 10MB
            if downloaded - last_report >= 10 * 1024 * 1024:
                elapsed = time.time() - start_time
                speed = downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                if total_size:
                    pct = downloaded / total_size * 100
                    print(f"  {downloaded / 1024 / 1024:.1f} MB / {total_size / 1024 / 1024:.1f} MB ({pct:.0f}%) — {speed:.1f} MB/s")
                else:
                    print(f"  {downloaded / 1024 / 1024:.1f} MB downloaded — {speed:.1f} MB/s")
                last_report = downloaded

    elapsed = time.time() - start_time
    print()
    print(f"Download complete: {downloaded / 1024 / 1024:.1f} MB in {elapsed:.0f}s")
    print(f"  Saved to: {out_path}")

    # Count lines for user info
    print("  Counting entities...", end=" ", flush=True)
    line_count = 0
    with open(out_path, "r", encoding="utf-8") as f:
        for _ in f:
            line_count += 1
    print(f"{line_count:,} lines")


def cmd_ingest(args):
    """Parse downloaded NDJSON into SQLite with FTS5 index."""
    dataset = args.dataset or "default"
    filename = f"entities.{dataset}.ftm.json"
    src_path = DOWNLOAD_DIR / filename

    if not src_path.exists():
        print(f"ERROR: File not found: {src_path}")
        print(f"Run 'python tools/query_opensanctions.py download --dataset {dataset}' first.")
        sys.exit(1)

    print(f"Ingesting {src_path}...")
    print(f"  Database: {DB_PATH}")
    print()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = _get_db()
    _init_db(db)

    # If ingesting from scratch, clear existing data
    if not args.append:
        db.execute("DELETE FROM os_entities")
        db.commit()
        print("  Cleared existing data (use --append to add incrementally)")

    inserted = 0
    skipped = 0
    errors = 0
    schema_counts = {}
    topic_counts = {}
    start_time = time.time()

    batch = []
    batch_size = 5000

    with open(src_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            parsed = _parse_ftm_entity(line)
            if parsed is None:
                errors += 1
                continue

            batch.append(parsed)

            # Track stats
            schema = parsed[2]
            schema_counts[schema] = schema_counts.get(schema, 0) + 1
            try:
                topics = json.loads(parsed[6])
            except (json.JSONDecodeError, TypeError):
                topics = []
            for t in topics:
                topic_counts[t] = topic_counts.get(t, 0) + 1

            if len(batch) >= batch_size:
                _insert_batch(db, batch)
                inserted += len(batch)
                batch = []

                if inserted % 50000 == 0:
                    elapsed = time.time() - start_time
                    rate = inserted / elapsed if elapsed > 0 else 0
                    print(f"  {inserted:>9,} entities ingested ({rate:,.0f}/s) — line {line_num:,}")

    # Final batch
    if batch:
        _insert_batch(db, batch)
        inserted += len(batch)

    elapsed = time.time() - start_time
    print()
    print(f"Ingestion complete: {inserted:,} entities in {elapsed:.1f}s")
    if errors:
        print(f"  Parse errors: {errors:,}")

    # Build FTS5 index
    print("  Building FTS5 index...", end=" ", flush=True)
    fts_start = time.time()
    _init_fts(db)
    fts_elapsed = time.time() - fts_start
    print(f"done ({fts_elapsed:.1f}s)")

    db.close()

    # Print stats
    print()
    print("By schema:")
    for schema, count in sorted(schema_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  {schema:<25} {count:>10,}")

    print()
    print("By topic:")
    for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
        print(f"  {topic:<25} {count:>10,}")


def _insert_batch(db, batch):
    """Insert a batch of entities using INSERT OR REPLACE."""
    db.executemany("""
        INSERT OR REPLACE INTO os_entities
            (id, caption, schema, names, birth_date,
             countries, topics, datasets, first_seen, last_seen, properties)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)
    db.commit()


def cmd_search(args):
    """FTS5 search across all entities."""
    db = _get_db(readonly=True)

    # Build the FTS5 query. Escape double quotes in user input.
    query_escaped = args.query.replace('"', '""')
    fts_query = f'"{query_escaped}"'

    sql = """
        SELECT e.* FROM os_entities e
        JOIN os_entities_fts f ON e.rowid = f.rowid
        WHERE os_entities_fts MATCH ?
    """
    params = [fts_query]

    # Apply filters in Python (SQLite JSON functions may not be available)
    # For schema, we can filter directly
    if args.schema:
        sql += " AND e.schema = ?"
        params.append(args.schema)

    sql += " LIMIT ?"
    # Fetch extra to allow post-filtering
    fetch_limit = args.limit * 5 if (args.topic or args.country) else args.limit
    params.append(fetch_limit)

    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print("ERROR: FTS index not found. Run 'ingest' first.", file=sys.stderr)
            sys.exit(1)
        raise

    # Post-filter by topic and country
    filtered = []
    for row in rows:
        if args.topic:
            try:
                topics = json.loads(row["topics"] or "[]")
            except (json.JSONDecodeError, TypeError):
                topics = []
            if args.topic not in topics:
                continue
        if args.country:
            try:
                countries = json.loads(row["countries"] or "[]")
            except (json.JSONDecodeError, TypeError):
                countries = []
            if args.country.lower() not in [c.lower() for c in countries]:
                continue
        filtered.append(row)
        if len(filtered) >= args.limit:
            break

    # Print results
    filters = []
    if args.schema:
        filters.append(f"schema={args.schema}")
    if args.topic:
        filters.append(f"topic={args.topic}")
    if args.country:
        filters.append(f"country={args.country}")
    filter_str = f" ({', '.join(filters)})" if filters else ""

    if write_output([dict(r) for r in filtered], args, summary=f"OpenSanctions search '{args.query}'"):
        db.close()
        return

    print(f"Search: '{args.query}'{filter_str} -- {len(filtered)} results (showing up to {args.limit})")
    print()

    for row in filtered:
        print(_format_entity(row, verbose=args.verbose))
        print()

    db.close()


def cmd_entity(args):
    """Get full entity details by ID."""
    db = _get_db(readonly=True)

    row = db.execute("SELECT * FROM os_entities WHERE id = ?", (args.entity_id,)).fetchone()
    if not row:
        print(f"Entity '{args.entity_id}' not found.")
        db.close()
        return

    d = dict(row)
    for field in ("names", "countries", "topics", "datasets", "properties"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass

    if write_output(d, args, summary=f"OpenSanctions entity {args.entity_id}"):
        db.close()
        return

    print(f"=== Entity: {args.entity_id} ===")
    print()
    print(_format_entity(row, verbose=True))
    print()

    if args.json_out:
        print(json.dumps(d, indent=2, ensure_ascii=False))

    db.close()


def cmd_match_entities(args):
    """Cross-reference investigation entities and persons against OpenSanctions."""
    db = _get_db(readonly=True)

    if not INVESTIGATION_DB.exists():
        print(f"ERROR: investigation.db not found at {INVESTIGATION_DB}", file=sys.stderr)
        sys.exit(1)

    inv_db = sqlite3.connect(str(INVESTIGATION_DB))
    inv_db.row_factory = sqlite3.Row

    # Gather names to check from investigation.db
    names_to_check = {}

    # 1. Entities table
    try:
        entities = inv_db.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
        for e in entities:
            name = e["name"].strip()
            if len(name) >= 3:
                names_to_check[name] = f"entity #{e['id']}"
    except sqlite3.OperationalError:
        entities = []

    # 2. Connections table — unique person names
    try:
        persons_a = inv_db.execute("SELECT DISTINCT person_a FROM connections").fetchall()
        persons_b = inv_db.execute("SELECT DISTINCT person_b FROM connections").fetchall()
        all_persons = set()
        for r in persons_a:
            if r["person_a"]:
                all_persons.add(r["person_a"].strip())
        for r in persons_b:
            if r["person_b"]:
                all_persons.add(r["person_b"].strip())
        for name in sorted(all_persons):
            if len(name) >= 3 and name not in names_to_check:
                names_to_check[name] = "connection"
    except sqlite3.OperationalError:
        pass

    inv_db.close()

    print(f"Cross-referencing {len(names_to_check)} names against OpenSanctions")
    print("=" * 80)
    print()

    sanctioned = []
    peps = []
    crime = []
    other_matches = []
    no_match = 0

    for i, (name, source) in enumerate(sorted(names_to_check.items()), 1):
        if i % 50 == 0:
            print(f"  ... checked {i}/{len(names_to_check)}", flush=True)

        query_escaped = name.replace('"', '""')
        fts_query = f'"{query_escaped}"'

        try:
            rows = db.execute("""
                SELECT e.* FROM os_entities e
                JOIN os_entities_fts f ON e.rowid = f.rowid
                WHERE os_entities_fts MATCH ?
                LIMIT 10
            """, (fts_query,)).fetchall()
        except sqlite3.OperationalError:
            rows = []

        # Filter to close caption matches (avoid false positives)
        matches = []
        name_lower = name.lower()
        name_parts = set(name_lower.split())
        for row in rows:
            caption_lower = (row["caption"] or "").lower()
            # Match if the caption contains all significant words of the query name
            # (at least 2 chars each) or vice versa
            significant_parts = {p for p in name_parts if len(p) >= 3}
            if not significant_parts:
                significant_parts = name_parts

            # Check word overlap
            caption_parts = set(caption_lower.replace(",", " ").replace(".", " ").split())
            overlap = significant_parts & caption_parts
            if len(overlap) >= min(len(significant_parts), 2) or name_lower in caption_lower or caption_lower in name_lower:
                matches.append(row)

        if not matches:
            no_match += 1
            continue

        for m in matches:
            try:
                topics = json.loads(m["topics"] or "[]")
            except (json.JSONDecodeError, TypeError):
                topics = []

            entry = {
                "search_name": name,
                "source": source,
                "match_id": m["id"],
                "match_caption": m["caption"],
                "schema": m["schema"],
                "topics": topics,
                "countries": m["countries"],
                "datasets": m["datasets"],
            }

            if "sanction" in topics:
                sanctioned.append(entry)
            elif "pep" in topics:
                peps.append(entry)
            elif "crime" in topics:
                crime.append(entry)
            else:
                other_matches.append(entry)

    # Output to file if requested
    match_data = {
        "sanctioned": sanctioned,
        "peps": peps,
        "crime": crime,
        "other": other_matches,
        "no_match_count": no_match,
        "total_checked": len(names_to_check),
    }
    if write_output(match_data, args, summary=f"OpenSanctions match-entities ({len(sanctioned)} sanctioned, {len(peps)} PEPs)"):
        db.close()
        return

    # Print results
    if sanctioned:
        print(f"SANCTIONED ENTITIES ({len(sanctioned)} matches)")
        print("-" * 60)
        for e in sanctioned:
            try:
                ds = json.loads(e["datasets"]) if isinstance(e["datasets"], str) else e["datasets"]
            except (json.JSONDecodeError, TypeError):
                ds = []
            print(f"  {e['search_name']} ({e['source']})")
            print(f"    -> {e['match_caption']} [{e['schema']}]")
            print(f"    ID: {e['match_id']}")
            if ds:
                print(f"    Lists: {', '.join(ds[:6])}")
            print()

    if peps:
        print(f"POLITICALLY EXPOSED PERSONS ({len(peps)} matches)")
        print("-" * 60)
        for e in peps:
            try:
                countries = json.loads(e["countries"]) if isinstance(e["countries"], str) else e["countries"]
            except (json.JSONDecodeError, TypeError):
                countries = []
            print(f"  {e['search_name']} ({e['source']})")
            print(f"    -> {e['match_caption']} [{e['schema']}]")
            if countries:
                print(f"    Countries: {', '.join(countries)}")
            print()

    if crime:
        print(f"CRIME-RELATED ({len(crime)} matches)")
        print("-" * 60)
        for e in crime:
            print(f"  {e['search_name']} ({e['source']})")
            print(f"    -> {e['match_caption']} [{e['schema']}]")
            print()

    if other_matches:
        print(f"OTHER MATCHES ({len(other_matches)} matches)")
        print("-" * 60)
        for e in other_matches[:30]:
            topics_str = ", ".join(e["topics"]) if e["topics"] else "none"
            print(f"  {e['search_name']} -> {e['match_caption']} [topics: {topics_str}]")
        if len(other_matches) > 30:
            print(f"  ... and {len(other_matches) - 30} more")
        print()

    # Summary
    total_checked = len(names_to_check)
    total_hits = len(sanctioned) + len(peps) + len(crime) + len(other_matches)
    print("=" * 80)
    print(f"SUMMARY: {total_checked} names checked")
    print(f"  Sanctioned:   {len(sanctioned)}")
    print(f"  PEPs:         {len(peps)}")
    print(f"  Crime-listed: {len(crime)}")
    print(f"  Other:        {len(other_matches)}")
    print(f"  No match:     {no_match}")

    db.close()


def cmd_pep_check(args):
    """Quick PEP check for a specific person."""
    db = _get_db(readonly=True)

    query_escaped = args.name.replace('"', '""')
    fts_query = f'"{query_escaped}"'

    try:
        rows = db.execute("""
            SELECT e.* FROM os_entities e
            JOIN os_entities_fts f ON e.rowid = f.rowid
            WHERE os_entities_fts MATCH ?
            AND e.schema = 'Person'
            LIMIT 50
        """, (fts_query,)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print("ERROR: FTS index not found. Run 'ingest' first.", file=sys.stderr)
            sys.exit(1)
        raise

    # Filter to PEP-tagged results with name overlap
    name_lower = args.name.lower()
    name_parts = set(name_lower.split())
    significant_parts = {p for p in name_parts if len(p) >= 3}
    if not significant_parts:
        significant_parts = name_parts

    pep_results = []
    other_results = []
    for row in rows:
        caption_lower = (row["caption"] or "").lower()
        caption_parts = set(caption_lower.replace(",", " ").replace(".", " ").split())
        overlap = significant_parts & caption_parts

        if len(overlap) < min(len(significant_parts), 2) and name_lower not in caption_lower and caption_lower not in name_lower:
            continue

        try:
            topics = json.loads(row["topics"] or "[]")
        except (json.JSONDecodeError, TypeError):
            topics = []

        if "pep" in topics:
            pep_results.append(row)
        elif topics:
            other_results.append(row)

    pep_data = {
        "name": args.name,
        "pep_results": [dict(r) for r in pep_results],
        "other_results": [dict(r) for r in other_results],
    }
    if write_output(pep_data, args, summary=f"OpenSanctions PEP check '{args.name}' ({len(pep_results)} PEP matches)"):
        db.close()
        return

    print(f"PEP check: '{args.name}'")
    print("=" * 60)

    if pep_results:
        print(f"\nPOLITICALLY EXPOSED PERSON -- {len(pep_results)} match(es)")
        print()
        for row in pep_results:
            print(_format_entity(row, verbose=args.verbose))
            print()
    else:
        print(f"\nNo PEP designation found for '{args.name}'")

    if other_results:
        print(f"\nOther listings ({len(other_results)}):")
        for row in other_results:
            print(_format_entity(row, verbose=False))
            print()

    db.close()


def cmd_stats(args):
    """Print database statistics."""
    db = _get_db(readonly=True)

    total = db.execute("SELECT COUNT(*) FROM os_entities").fetchone()[0]
    print(f"OpenSanctions database: {DB_PATH}")
    print(f"Total entities: {total:,}")
    print()

    # By schema
    print("By schema:")
    rows = db.execute("""
        SELECT schema, COUNT(*) as cnt FROM os_entities
        GROUP BY schema ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['schema']:<30} {r['cnt']:>10,}")

    print()

    # By topic (requires parsing JSON; sample approach)
    print("By topic:")
    topic_counts = {}
    # Use a cursor to stream through all rows
    cursor = db.execute("SELECT topics FROM os_entities")
    while True:
        batch = cursor.fetchmany(10000)
        if not batch:
            break
        for row in batch:
            try:
                topics = json.loads(row["topics"] or "[]")
            except (json.JSONDecodeError, TypeError):
                topics = []
            for t in topics:
                topic_counts[t] = topic_counts.get(t, 0) + 1

    for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
        print(f"  {topic:<30} {count:>10,}")

    print()

    # By dataset (top 20)
    print("Top datasets:")
    dataset_counts = {}
    cursor = db.execute("SELECT datasets FROM os_entities")
    while True:
        batch = cursor.fetchmany(10000)
        if not batch:
            break
        for row in batch:
            try:
                datasets = json.loads(row["datasets"] or "[]")
            except (json.JSONDecodeError, TypeError):
                datasets = []
            for d in datasets:
                dataset_counts[d] = dataset_counts.get(d, 0) + 1

    for ds, count in sorted(dataset_counts.items(), key=lambda x: -x[1])[:25]:
        print(f"  {ds:<45} {count:>10,}")

    # File info
    print()
    if DB_PATH.exists():
        size_mb = DB_PATH.stat().st_size / 1024 / 1024
        print(f"Database size: {size_mb:.1f} MB")

    src_dir = DOWNLOAD_DIR
    if src_dir.exists():
        for f in sorted(src_dir.iterdir()):
            if f.suffix == ".json":
                fsize = f.stat().st_size / 1024 / 1024
                print(f"Source file: {f.name} ({fsize:.1f} MB)")

    db.close()


def cmd_reconcile_ftm(args):
    """Fuzzy reconciliation of investigation.db entities against OpenSanctions.

    Uses FTS for candidate retrieval + rapidfuzz for scoring, producing
    ranked matches with confidence scores. Optionally creates leads for
    sanctioned/PEP matches above threshold.
    """
    from rapidfuzz import fuzz

    try:
        from tools.entity_resolution import normalize_entity_name
    except ImportError:
        from entity_resolution import normalize_entity_name

    db = _get_db(readonly=True)

    if not INVESTIGATION_DB.exists():
        print(f"ERROR: investigation.db not found at {INVESTIGATION_DB}", file=sys.stderr)
        sys.exit(1)

    inv_db = sqlite3.connect(str(INVESTIGATION_DB))
    inv_db.row_factory = sqlite3.Row

    # Gather all entities + connection persons from investigation.db
    names_to_check = {}
    try:
        for e in inv_db.execute("SELECT id, name, entity_type FROM entities").fetchall():
            name = e["name"].strip()
            if len(name) >= 3:
                names_to_check[name] = {"source": f"entity #{e['id']}", "type": e["entity_type"]}
    except sqlite3.OperationalError:
        pass

    try:
        for col in ("person_a", "person_b"):
            for r in inv_db.execute(f"SELECT DISTINCT {col} FROM connections").fetchall():
                name = (r[0] or "").strip()
                if len(name) >= 3 and name not in names_to_check:
                    names_to_check[name] = {"source": "connection", "type": "person"}
    except sqlite3.OperationalError:
        pass

    inv_db.close()

    print(f"Reconciling {len(names_to_check)} names against OpenSanctions (threshold={args.threshold})")
    print("=" * 90)
    print()

    matches = []

    for i, (name, info) in enumerate(sorted(names_to_check.items()), 1):
        if i % 50 == 0:
            print(f"  ... checked {i}/{len(names_to_check)}", flush=True)

        name_normalized = normalize_entity_name(name)

        # Use FTS for candidate retrieval (fast)
        query_escaped = name.replace('"', '""')
        # Use individual words for broader FTS coverage
        words = [w for w in name.split() if len(w) >= 3]
        if not words:
            continue
        fts_query = " OR ".join(f'"{w}"' for w in words[:4])

        try:
            candidates = db.execute("""
                SELECT e.* FROM os_entities e
                JOIN os_entities_fts f ON e.rowid = f.rowid
                WHERE os_entities_fts MATCH ?
                LIMIT 30
            """, (fts_query,)).fetchall()
        except sqlite3.OperationalError:
            continue

        # Score each candidate with rapidfuzz
        for cand in candidates:
            caption = (cand["caption"] or "").strip()
            if not caption:
                continue
            cand_normalized = normalize_entity_name(caption)
            score = fuzz.token_sort_ratio(name_normalized, cand_normalized)

            if score >= args.threshold:
                try:
                    topics = json.loads(cand["topics"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    topics = []
                try:
                    datasets = json.loads(cand["datasets"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    datasets = []
                try:
                    countries = json.loads(cand["countries"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    countries = []

                matches.append({
                    "our_name": name,
                    "our_source": info["source"],
                    "os_id": cand["id"],
                    "os_caption": caption,
                    "os_schema": cand["schema"],
                    "score": score,
                    "topics": topics,
                    "datasets": datasets[:5],
                    "countries": countries,
                })

    # Sort by score descending
    matches.sort(key=lambda m: m["score"], reverse=True)
    if args.limit:
        matches = matches[:args.limit]

    # Categorize
    sanctioned = [m for m in matches if "sanction" in m["topics"]]
    peps = [m for m in matches if "pep" in m["topics"]]
    crime = [m for m in matches if "crime" in m["topics"]]
    other = [m for m in matches if not (set(m["topics"]) & {"sanction", "pep", "crime"})]

    # Create leads for sanctioned/PEP matches if requested
    leads_created = 0
    if args.create_leads and (sanctioned or peps):
        try:
            from tools.lead_tracker import get_db as get_inv_db
        except ImportError:
            from lead_tracker import get_db as get_inv_db

        lead_db = get_inv_db()
        for m in sanctioned + peps:
            topic_str = ", ".join(m["topics"])
            title = f"OpenSanctions match: {m['our_name']} -> {m['os_caption']} ({topic_str})"
            # Check for duplicate lead
            existing = lead_db.execute(
                "SELECT id FROM leads WHERE title = ?", (title,)
            ).fetchone()
            if existing:
                continue
            lead_db.execute("""
                INSERT INTO leads (title, status, target_name, priority, notes, created_at)
                VALUES (?, 'pending_triage', ?, 3, ?, datetime('now'))
            """, (title, m["our_name"],
                  f"Score: {m['score']}. OS ID: {m['os_id']}. Schema: {m['os_schema']}. "
                  f"Datasets: {', '.join(m['datasets'])}"))
            leads_created += 1

        lead_db.commit()
        lead_db.close()

    # Output
    if write_output({"sanctioned": sanctioned, "peps": peps, "crime": crime,
                      "other": other, "leads_created": leads_created},
                    args, summary=f"FtM reconciliation ({len(matches)} matches)"):
        db.close()
        return

    if sanctioned:
        print(f"SANCTIONED ({len(sanctioned)} matches)")
        print("-" * 90)
        for m in sanctioned:
            print(f"  {m['score']:>3}  {m['our_name']:<35} -> {m['os_caption'][:35]:<35} [{m['os_schema']}]")
            if m["datasets"]:
                print(f"       Lists: {', '.join(m['datasets'])}")
        print()

    if peps:
        print(f"POLITICALLY EXPOSED ({len(peps)} matches)")
        print("-" * 90)
        for m in peps:
            print(f"  {m['score']:>3}  {m['our_name']:<35} -> {m['os_caption'][:35]:<35} [{m['os_schema']}]")
        print()

    if crime:
        print(f"CRIME-RELATED ({len(crime)} matches)")
        print("-" * 90)
        for m in crime:
            print(f"  {m['score']:>3}  {m['our_name']:<35} -> {m['os_caption'][:35]:<35} [{m['os_schema']}]")
        print()

    if other:
        print(f"OTHER ({len(other)} matches)")
        print("-" * 90)
        for m in other[:30]:
            print(f"  {m['score']:>3}  {m['our_name']:<35} -> {m['os_caption'][:35]:<35} [{m['os_schema']}]")
        if len(other) > 30:
            print(f"  ... and {len(other) - 30} more")
        print()

    print("=" * 90)
    print(f"SUMMARY: {len(names_to_check)} names checked, {len(matches)} matches above threshold {args.threshold}")
    print(f"  Sanctioned:   {len(sanctioned)}")
    print(f"  PEPs:         {len(peps)}")
    print(f"  Crime-listed: {len(crime)}")
    print(f"  Other:        {len(other)}")
    if args.create_leads:
        print(f"  Leads created: {leads_created}")

    db.close()


def main():
    parser = argparse.ArgumentParser(
        description="OpenSanctions bulk data query tool for OSINT investigation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # download
    p = sub.add_parser("download", help="Download bulk NDJSON from OpenSanctions")
    p.add_argument("--dataset", choices=["default", "sanctions", "peps"],
                   default="default",
                   help="Dataset to download (default: all data)")

    # ingest
    p = sub.add_parser("ingest", help="Parse NDJSON into SQLite + FTS5")
    p.add_argument("--dataset", choices=["default", "sanctions", "peps"],
                   default="default",
                   help="Which downloaded file to ingest")
    p.add_argument("--append", action="store_true",
                   help="Append to existing data instead of replacing")

    # search
    p = sub.add_parser("search", help="Full-text search across entities")
    p.add_argument("query", help="Search query")
    p.add_argument("--schema", choices=SCHEMAS,
                   help="Filter by entity schema (Person, Company, etc.)")
    p.add_argument("--topic", choices=TOPICS,
                   help="Filter by topic (sanction, pep, crime, etc.)")
    p.add_argument("--country", help="Filter by country code (e.g., ru, us, il)")
    p.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show all entity properties")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity details by ID")
    p.add_argument("entity_id", help="OpenSanctions entity ID")
    add_output_args(p)

    # match-entities
    p = sub.add_parser("match-entities",
                       help="Cross-ref investigation entities/persons against OpenSanctions")
    add_output_args(p)

    # pep-check
    p = sub.add_parser("pep-check", help="Quick PEP check for a person")
    p.add_argument("name", help="Person name to check")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show all entity properties")
    add_output_args(p)

    # stats
    p = sub.add_parser("stats", help="Database statistics")
    add_output_args(p)

    # reconcile-ftm
    p = sub.add_parser("reconcile-ftm",
                       help="Fuzzy reconciliation with confidence scores (uses rapidfuzz)")
    p.add_argument("--threshold", type=int, default=85,
                   help="Minimum fuzzy match score (default: 85)")
    p.add_argument("--limit", type=int, default=100,
                   help="Max matches to return (default: 100)")
    p.add_argument("--create-leads", action="store_true",
                   help="Create pending_triage leads for sanctioned/PEP matches")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
        "entity": cmd_entity,
        "match-entities": cmd_match_entities,
        "pep-check": cmd_pep_check,
        "stats": cmd_stats,
        "reconcile-ftm": cmd_reconcile_ftm,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
