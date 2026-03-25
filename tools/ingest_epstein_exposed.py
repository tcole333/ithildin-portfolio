#!/usr/bin/env python3
"""
EpsteinExposed.com API wrapper for the Epstein OSINT investigation.

Public REST API (no auth required) providing:
  - 1,271+ persons with bios, aliases, categories, connections
  - 1,522,061 documents with FTS5 search
  - 1,708 flight records
  - Cross-type search across documents + emails

API base: https://epsteinexposed.com/api/v1
Rate limits: 60 req/min (standard), 30 req/min (search)
Docs: https://epsteinexposed.com/api-docs

Usage:
    python tools/ingest_epstein_exposed.py download              # Download all persons + connections to local DB
    python tools/ingest_epstein_exposed.py ingest                 # Parse downloaded data into investigation.db
    python tools/ingest_epstein_exposed.py search "query"         # Cross-type search (docs + emails)
    python tools/ingest_epstein_exposed.py persons                # List all persons (paginated)
    python tools/ingest_epstein_exposed.py persons --category business
    python tools/ingest_epstein_exposed.py person "bill-gates"    # Full person detail by slug
    python tools/ingest_epstein_exposed.py documents "epstein wexner" --source doj
    python tools/ingest_epstein_exposed.py flights --passenger "clinton" --year 2002
    python tools/ingest_epstein_exposed.py match-entities         # Cross-ref with investigation.db
    python tools/ingest_epstein_exposed.py stats                  # Show local DB stats
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://epsteinexposed.com/api/v1"
DB_PATH = Path(__file__).parent.parent / "datasets" / "epstein_exposed.db"
INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"

# Rate limiting: 60 req/min standard, 30 req/min search
# = 1 req/s standard, 2s between search requests
STANDARD_DELAY = 1.0
SEARCH_DELAY = 2.0

CATEGORIES = [
    "politician", "business", "royalty", "celebrity", "associate",
    "legal", "academic", "socialite", "military-intelligence", "other"
]

DOC_SOURCES = ["court-filing", "doj-release", "fbi", "efta", "doj"]
DOC_CATEGORIES = ["deposition", "testimony", "correspondence", "legal-filing",
                   "fbi-report"]


def _request(path, params=None, timeout=30):
    """Make an API request to EpsteinExposed."""
    url = f"{BASE_URL}{path}"
    if params:
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        if params:
            url += "?" + urlencode(params, doseq=True)

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0; +https://github.com/epstein-index)",
    }

    req = Request(url, headers=headers)
    retries = 3
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "error":
                    err = data.get("error", {})
                    print(f"API error: {err.get('message', 'Unknown')} ({err.get('code', '?')})",
                          file=sys.stderr)
                    return None
                return data
        except HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"Rate limited (429). Waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
        except URLError as e:
            print(f"ERROR: Cannot reach EpsteinExposed: {e.reason}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return None


def _paginate(path, params, max_results=100, per_page=100, delay=STANDARD_DELAY):
    """Paginate through API results. Returns (results_list, total_count)."""
    results = []
    page = 1
    params["per_page"] = min(per_page, 100)
    total = None

    while True:
        params["page"] = page
        data = _request(path, params)
        if not data:
            break

        batch = data.get("data", [])
        meta = data.get("meta", {})
        if total is None:
            total = meta.get("total", 0)

        if not batch:
            break

        results.extend(batch)

        if len(results) >= total or len(results) >= max_results:
            break

        page += 1
        time.sleep(delay)

    return results[:max_results], total or len(results)


# ------------- Local database setup ------------- #

def _init_db():
    """Create the local epstein_exposed.db with schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            aliases TEXT,           -- JSON array
            short_bio TEXT,
            bio TEXT,
            image_url TEXT,
            status TEXT,            -- JSON array
            black_book_entry INTEGER DEFAULT 0,
            flight_count INTEGER DEFAULT 0,
            document_count INTEGER DEFAULT 0,
            connection_count INTEGER DEFAULT 0,
            email_count INTEGER DEFAULT 0,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            detail_fetched INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS person_connections (
            person_id TEXT NOT NULL REFERENCES persons(id),
            connected_person_id TEXT NOT NULL,
            strength TEXT,
            co_flights INTEGER DEFAULT 0,
            co_documents INTEGER DEFAULT 0,
            PRIMARY KEY (person_id, connected_person_id)
        );

        CREATE TABLE IF NOT EXISTS download_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(name);
        CREATE INDEX IF NOT EXISTS idx_persons_category ON persons(category);
        CREATE INDEX IF NOT EXISTS idx_person_connections_connected ON person_connections(connected_person_id);
    """)

    conn.commit()
    return conn


# ------------- Commands ------------- #

def cmd_download(args):
    """Download all persons + detail records from API into local DB."""
    conn = _init_db()
    print("=== Downloading all persons from EpsteinExposed.com ===")
    print()

    # Phase 1: Download all person list entries
    print("Phase 1: Fetching person list...")
    all_persons, total = _paginate("/persons", {}, max_results=5000, per_page=100)
    print(f"  Retrieved {len(all_persons)} of {total} persons")

    inserted = 0
    updated = 0
    for p in all_persons:
        try:
            # Handle duplicate slugs from API by appending ID suffix
            slug = p.get("slug")
            pid = p.get("id")
            existing_slug = conn.execute(
                "SELECT id FROM persons WHERE slug = ? AND id != ?", (slug, pid)
            ).fetchone()
            if existing_slug:
                slug = f"{slug}-{pid}"

            conn.execute("""
                INSERT INTO persons (id, slug, name, category, aliases, short_bio,
                    image_url, status, flight_count, document_count,
                    connection_count, email_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    slug=excluded.slug, name=excluded.name, category=excluded.category,
                    aliases=excluded.aliases, short_bio=excluded.short_bio,
                    image_url=excluded.image_url, status=excluded.status,
                    flight_count=excluded.flight_count, document_count=excluded.document_count,
                    connection_count=excluded.connection_count, email_count=excluded.email_count,
                    downloaded_at=CURRENT_TIMESTAMP
            """, (
                pid, slug, p.get("name"), p.get("category"),
                json.dumps(p.get("aliases", [])),
                p.get("shortBio"),
                p.get("imageUrl"),
                json.dumps(p.get("status") or []),
                p.get("flightCount", 0), p.get("documentCount", 0),
                p.get("connectionCount", 0), p.get("emailCount", 0),
            ))
            inserted += 1
        except Exception as e:
            print(f"  WARN: Failed to insert {p.get('name')}: {e}", file=sys.stderr)
            updated += 1

    conn.commit()
    print(f"  Stored {inserted} persons")
    print()

    # Phase 2: Fetch detail for each person (bio, connections, blackBookEntry)
    if not args.skip_detail:
        print("Phase 2: Fetching person details + connections...")
        print(f"  {len(all_persons)} persons to fetch (1s delay each = ~{len(all_persons) // 60}min)")
        print()

        for i, p in enumerate(all_persons):
            slug = p.get("slug")
            if not slug:
                continue

            # Check if already fetched
            row = conn.execute(
                "SELECT detail_fetched FROM persons WHERE slug = ?", (slug,)
            ).fetchone()
            if row and row[0] and not args.force:
                if (i + 1) % 100 == 0:
                    print(f"  [{i+1}/{len(all_persons)}] Skipping {slug} (already fetched)")
                continue

            data = _request(f"/persons/{quote(slug, safe='')}")
            if not data:
                print(f"  [{i+1}/{len(all_persons)}] FAILED: {slug}", file=sys.stderr)
                time.sleep(STANDARD_DELAY)
                continue

            detail = data.get("data", data)

            # Update person with full bio
            conn.execute("""
                UPDATE persons SET
                    bio = ?,
                    black_book_entry = ?,
                    detail_fetched = 1,
                    downloaded_at = CURRENT_TIMESTAMP
                WHERE slug = ?
            """, (
                detail.get("bio"),
                1 if detail.get("blackBookEntry") else 0,
                slug,
            ))

            # Also update stats from detail if available
            stats = detail.get("stats", {})
            if stats:
                conn.execute("""
                    UPDATE persons SET
                        flight_count = ?,
                        document_count = ?,
                        connection_count = ?,
                        email_count = ?
                    WHERE slug = ?
                """, (
                    stats.get("flights", 0),
                    stats.get("documents", 0),
                    stats.get("connections", 0),
                    stats.get("emails", 0),
                    slug,
                ))

            # Store connections
            connections = detail.get("connections", [])
            for c in connections:
                try:
                    conn.execute("""
                        INSERT INTO person_connections
                            (person_id, connected_person_id, strength, co_flights, co_documents)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(person_id, connected_person_id) DO UPDATE SET
                            strength=excluded.strength,
                            co_flights=excluded.co_flights,
                            co_documents=excluded.co_documents
                    """, (
                        detail.get("id"),
                        c.get("personId"),
                        c.get("strength"),
                        c.get("coFlights", 0),
                        c.get("coDocuments", 0),
                    ))
                except Exception as e:
                    pass  # Skip duplicate/error silently

            conn.commit()

            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{len(all_persons)}] Fetched {slug} "
                      f"({len(connections)} connections)")

            time.sleep(STANDARD_DELAY)

    # Record metadata
    conn.execute("""
        INSERT INTO download_meta (key, value, updated_at)
        VALUES ('last_download', datetime('now'), datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=datetime('now'), updated_at=datetime('now')
    """)
    conn.execute("""
        INSERT INTO download_meta (key, value, updated_at)
        VALUES ('total_persons', ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
    """, (str(total),))
    conn.commit()
    conn.close()

    print()
    print(f"=== Download complete. DB: {DB_PATH} ===")
    print(f"  Persons: {len(all_persons)}")


def cmd_ingest(args):
    """Parse downloaded data into investigation.db — entities, connections, findings."""
    if not DB_PATH.exists():
        print("ERROR: No local data. Run 'download' first.", file=sys.stderr)
        sys.exit(1)

    if not INVESTIGATION_DB.exists():
        print(f"ERROR: {INVESTIGATION_DB} not found.", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(str(DB_PATH))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(INVESTIGATION_DB))
    dst.execute("PRAGMA journal_mode=WAL")

    # Get all persons with some meaningful data
    rows = src.execute("""
        SELECT * FROM persons
        WHERE document_count > 0 OR flight_count > 0 OR connection_count > 0 OR email_count > 0
        ORDER BY document_count + flight_count + connection_count + email_count DESC
    """).fetchall()

    print(f"=== Ingesting {len(rows)} persons into investigation.db ===")
    print()

    connections_added = 0
    persons_processed = 0

    # Get all person connections
    all_connections = src.execute("""
        SELECT pc.*, p1.name as source_name, p2.name as target_name
        FROM person_connections pc
        JOIN persons p1 ON pc.person_id = p1.id
        JOIN persons p2 ON pc.connected_person_id = p2.id
        WHERE pc.strength IN ('strong', 'moderate')
    """).fetchall()

    # Build a lookup from ID to name
    id_to_name = {}
    for row in src.execute("SELECT id, name FROM persons").fetchall():
        id_to_name[row["id"]] = row["name"]

    # Ingest connections as connections in investigation.db
    for c in all_connections:
        source_name = c["source_name"]
        target_name = c["target_name"]
        strength = c["strength"]

        # Map strength
        strength_map = {"strong": "strong", "moderate": "medium", "weak": "weak"}
        mapped_strength = strength_map.get(strength, "medium")

        # Check if already exists
        existing = dst.execute("""
            SELECT id FROM connections
            WHERE (person_a = ? AND person_b = ?)
               OR (person_a = ? AND person_b = ?)
        """, (source_name, target_name, target_name, source_name)).fetchone()

        if not existing:
            desc_parts = []
            co_flights = c["co_flights"]
            co_docs = c["co_documents"]
            if co_flights:
                desc_parts.append(f"{co_flights} co-flights")
            if co_docs:
                desc_parts.append(f"{co_docs} co-documents")
            desc = f"EpsteinExposed: {', '.join(desc_parts)}" if desc_parts else "EpsteinExposed connection"

            try:
                dst.execute("""
                    INSERT INTO connections (person_a, person_b, relationship_type,
                        description, strength)
                    VALUES (?, ?, 'social', ?, ?)
                """, (source_name, target_name, desc, mapped_strength))
                connections_added += 1
            except Exception:
                pass  # Skip if constraint violation

    dst.commit()
    persons_processed = len(rows)

    src.close()
    dst.close()

    print(f"  Persons processed: {persons_processed}")
    print(f"  New connections added: {connections_added}")
    print(f"  Source: {DB_PATH}")
    print(f"  Target: {INVESTIGATION_DB}")


def cmd_search(args):
    """Cross-type search across documents and emails."""
    params = {"q": args.query, "limit": args.limit}
    if args.type:
        params["type"] = args.type

    data = _request("/search", params)
    if not data:
        return

    payload = data.get("data", data)

    # Documents
    docs = payload.get("documents", {})
    doc_results = docs.get("results", [])
    doc_total = docs.get("total", len(doc_results))

    if doc_results:
        print(f"=== Documents ({doc_total} total, showing {len(doc_results)}) ===")
        print()
        for d in doc_results:
            did = d.get("id", "?")
            title = d.get("title", "Untitled")
            snippet = d.get("snippet", "")
            print(f"  [{did}] {title}")
            if snippet:
                # Clean up HTML marks
                clean = snippet.replace("<mark>", "**").replace("</mark>", "**")
                # Truncate long snippets
                if len(clean) > 200:
                    clean = clean[:200] + "..."
                print(f"    {clean}")
            print()

    # Emails
    emails = payload.get("emails", {})
    email_results = emails.get("results", [])
    email_total = emails.get("total", len(email_results))

    if email_results:
        print(f"=== Emails ({email_total} total, showing {len(email_results)}) ===")
        print()
        for e in email_results:
            eid = e.get("id", "?")
            subject = e.get("subject", "No subject")
            snippet = e.get("snippet", "")
            print(f"  [{eid}] {subject}")
            if snippet:
                clean = snippet.replace("<mark>", "**").replace("</mark>", "**")
                if len(clean) > 200:
                    clean = clean[:200] + "..."
                print(f"    {clean}")
            print()

    if not doc_results and not email_results:
        print(f"No results for '{args.query}'")

    if args.json_out:
        print(json.dumps(payload, indent=2, default=str))


def cmd_persons(args):
    """List persons with optional category filter."""
    params = {}
    if args.query:
        params["q"] = args.query
    if args.category:
        params["category"] = args.category

    results, total = _paginate("/persons", params, max_results=args.limit)

    cat_label = f" (category={args.category})" if args.category else ""
    q_label = f" matching '{args.query}'" if args.query else ""
    print(f"=== Persons: {total} total{cat_label}{q_label} (showing {len(results)}) ===")
    print()

    for p in results:
        pid = p.get("id", "?")
        name = p.get("name", "?")
        slug = p.get("slug", "")
        cat = p.get("category", "?")
        bio = p.get("shortBio", "")
        aliases = p.get("aliases", [])
        status = p.get("status") or []
        flights = p.get("flightCount", 0)
        docs = p.get("documentCount", 0)
        conns = p.get("connectionCount", 0)
        emails = p.get("emailCount", 0)

        status_str = f" [{', '.join(status)}]" if status else ""
        alias_str = f" (aka {', '.join(aliases)})" if aliases else ""

        print(f"  {name}{alias_str}{status_str}")
        print(f"    Category: {cat} | Slug: {slug}")
        print(f"    Flights: {flights} | Docs: {docs:,} | Connections: {conns} | Emails: {emails}")
        if bio:
            print(f"    Bio: {bio[:150]}")
        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_person(args):
    """Get full detail for a single person by slug."""
    slug = args.slug.lower().strip()
    # If they passed a name with spaces, convert to slug
    if " " in slug:
        slug = slug.replace(" ", "-")

    data = _request(f"/persons/{quote(slug, safe='')}")
    if not data:
        print(f"Person not found: {slug}")
        return

    p = data.get("data", data)

    name = p.get("name", "?")
    cat = p.get("category", "?")
    aliases = p.get("aliases", [])
    status = p.get("status") or []
    bio = p.get("bio", "")
    short_bio = p.get("shortBio", "")
    bb = p.get("blackBookEntry", False)
    stats = p.get("stats", {})
    connections = p.get("connections", [])

    print(f"=== {name} ===")
    print(f"  Category: {cat}")
    if aliases:
        print(f"  Aliases: {', '.join(aliases)}")
    if status:
        print(f"  Status: {', '.join(status)}")
    print(f"  Black Book: {'Yes' if bb else 'No'}")
    print(f"  Slug: {slug}")
    print(f"  URL: https://epsteinexposed.com/persons/{slug}")
    print()

    if stats:
        print(f"  Stats:")
        print(f"    Flights: {stats.get('flights', 0)}")
        print(f"    Documents: {stats.get('documents', 0):,}")
        print(f"    Connections: {stats.get('connections', 0)}")
        print(f"    Emails: {stats.get('emails', 0)}")
    print()

    if bio:
        print(f"  Bio:")
        # Word-wrap at 80 chars
        words = bio.split()
        line = "    "
        for w in words:
            if len(line) + len(w) + 1 > 100:
                print(line)
                line = "    " + w
            else:
                line += " " + w if line.strip() else "    " + w
        if line.strip():
            print(line)
        print()

    if connections:
        print(f"  Connections ({len(connections)}):")
        for c in connections:
            cpid = c.get("personId", "?")
            strength = c.get("strength", "?")
            co_f = c.get("coFlights", 0)
            co_d = c.get("coDocuments", 0)
            parts = []
            if co_f:
                parts.append(f"{co_f} co-flights")
            if co_d:
                parts.append(f"{co_d} co-docs")
            detail = f" ({', '.join(parts)})" if parts else ""
            print(f"    [{strength}] {cpid}{detail}")
        print()

    if args.json_out:
        print(json.dumps(p, indent=2, default=str))


def cmd_documents(args):
    """Search documents with full-text search."""
    params = {"q": args.query}
    if args.source:
        params["source"] = args.source
    if args.category:
        params["category"] = args.category

    results, total = _paginate("/documents", params, max_results=args.limit, delay=SEARCH_DELAY)

    src_label = f" (source={args.source})" if args.source else ""
    print(f"=== Documents: {total:,} total for '{args.query}'{src_label} (showing {len(results)}) ===")
    print()

    for d in results:
        did = d.get("id", "?")
        title = d.get("title", "Untitled")
        date = d.get("date", "?")
        source = d.get("source", "?")
        cat = d.get("category", "?")
        summary = d.get("summary", "")
        url = d.get("sourceUrl", "")
        tags = d.get("tags", [])

        print(f"  [{did}] {title}")
        print(f"    Date: {date} | Source: {source} | Category: {cat}")
        if tags:
            print(f"    Tags: {', '.join(tags[:10])}")
        if summary:
            print(f"    Summary: {summary[:200]}{'...' if len(summary) > 200 else ''}")
        if url:
            print(f"    URL: {url}")
        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_flights(args):
    """Search flight records."""
    params = {}
    if args.passenger:
        params["passenger"] = args.passenger
    if args.year:
        params["year"] = args.year
    if args.origin:
        params["origin"] = args.origin
    if args.destination:
        params["destination"] = args.destination

    results, total = _paginate("/flights", params, max_results=args.limit)

    filters = []
    if args.passenger:
        filters.append(f"passenger={args.passenger}")
    if args.year:
        filters.append(f"year={args.year}")
    filter_str = f" ({', '.join(filters)})" if filters else ""
    print(f"=== Flights: {total} total{filter_str} (showing {len(results)}) ===")
    print()

    for f in results:
        fid = f.get("id", "?")
        date = f.get("date", "?")
        origin = f.get("origin", "?")
        dest = f.get("destination", "?")
        aircraft = f.get("aircraft", "?")
        pilot = f.get("pilot", "")
        passengers = f.get("passengerNames", [])
        pcount = f.get("passengerCount", 0)

        print(f"  [{fid}] {date}: {origin} -> {dest}")
        print(f"    Aircraft: {aircraft}")
        if pilot:
            print(f"    Pilot: {pilot}")
        if passengers:
            print(f"    Passengers ({pcount}): {', '.join(passengers[:10])}")
        elif pcount:
            print(f"    Passengers: {pcount} (names not available)")
        print()

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def cmd_match_entities(args):
    """Cross-reference EpsteinExposed persons with investigation.db entities/connections."""
    if not DB_PATH.exists():
        print("ERROR: No local data. Run 'download' first.", file=sys.stderr)
        sys.exit(1)

    if not INVESTIGATION_DB.exists():
        print(f"ERROR: {INVESTIGATION_DB} not found.", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(str(DB_PATH))
    src.row_factory = sqlite3.Row
    inv = sqlite3.connect(str(INVESTIGATION_DB))
    inv.row_factory = sqlite3.Row

    # Get all EpsteinExposed persons
    ee_persons = src.execute("""
        SELECT id, name, slug, category, aliases, short_bio,
               flight_count, document_count, connection_count, email_count
        FROM persons ORDER BY name
    """).fetchall()

    # Get all investigation.db connection persons
    inv_persons_a = set(r["person_a"] for r in inv.execute(
        "SELECT DISTINCT person_a FROM connections").fetchall())
    inv_persons_b = set(r["person_b"] for r in inv.execute(
        "SELECT DISTINCT person_b FROM connections").fetchall())
    inv_persons = inv_persons_a | inv_persons_b

    # Get findings targets
    inv_targets = set(r["target_name"] for r in inv.execute(
        "SELECT DISTINCT target_name FROM findings").fetchall())

    all_inv = inv_persons | inv_targets

    # Normalize for matching
    def normalize(name):
        return name.lower().strip().replace(".", "").replace(",", "")

    inv_normalized = {}
    for n in all_inv:
        inv_normalized[normalize(n)] = n

    matched = []
    unmatched = []
    new_intel = []  # Persons in EE with data that we don't have in investigation.db

    for p in ee_persons:
        name = p["name"]
        norm = normalize(name)
        aliases = json.loads(p["aliases"] or "[]")

        # Check direct match
        inv_match = inv_normalized.get(norm)

        # Check aliases
        if not inv_match:
            for alias in aliases:
                inv_match = inv_normalized.get(normalize(alias))
                if inv_match:
                    break

        # Check last-name + first-initial match
        if not inv_match:
            parts = name.split()
            if len(parts) >= 2:
                last = parts[-1].lower()
                first_init = parts[0][0].lower() if parts[0] else ""
                for inv_name, orig in inv_normalized.items():
                    inv_parts = inv_name.split()
                    if len(inv_parts) >= 2:
                        if (inv_parts[-1] == last and
                                inv_parts[0] and inv_parts[0][0] == first_init):
                            inv_match = orig
                            break

        total_data = (p["flight_count"] + p["document_count"] +
                      p["connection_count"] + p["email_count"])

        if inv_match:
            matched.append({
                "ee_name": name,
                "inv_name": inv_match,
                "category": p["category"],
                "flights": p["flight_count"],
                "docs": p["document_count"],
                "connections": p["connection_count"],
                "emails": p["email_count"],
                "slug": p["slug"],
            })
        elif total_data > 5:
            new_intel.append({
                "name": name,
                "category": p["category"],
                "flights": p["flight_count"],
                "docs": p["document_count"],
                "connections": p["connection_count"],
                "emails": p["email_count"],
                "bio": p["short_bio"],
                "slug": p["slug"],
            })
        else:
            unmatched.append(name)

    print(f"=== Entity Matching: EpsteinExposed vs investigation.db ===")
    print(f"  EE persons: {len(ee_persons)}")
    print(f"  Investigation persons: {len(all_inv)}")
    print(f"  Matched: {len(matched)}")
    print(f"  New intel (data > 5): {len(new_intel)}")
    print(f"  Unmatched (sparse): {len(unmatched)}")
    print()

    if matched:
        print(f"--- Matched ({len(matched)}) ---")
        # Sort by total data descending
        matched.sort(key=lambda x: x["docs"] + x["flights"] + x["connections"] + x["emails"],
                      reverse=True)
        for m in matched[:50]:
            inv_note = f" = {m['inv_name']}" if m["ee_name"] != m["inv_name"] else ""
            print(f"  {m['ee_name']}{inv_note} [{m['category']}]")
            print(f"    Flights: {m['flights']} | Docs: {m['docs']:,} | "
                  f"Connections: {m['connections']} | Emails: {m['emails']}")
        if len(matched) > 50:
            print(f"  ... and {len(matched) - 50} more")
        print()

    if new_intel:
        print(f"--- NEW Intelligence Targets ({len(new_intel)}) ---")
        new_intel.sort(key=lambda x: x["docs"] + x["flights"] + x["connections"] + x["emails"],
                        reverse=True)
        for n in new_intel[:30]:
            print(f"  {n['name']} [{n['category']}] — {n['slug']}")
            print(f"    Flights: {n['flights']} | Docs: {n['docs']:,} | "
                  f"Connections: {n['connections']} | Emails: {n['emails']}")
            if n["bio"]:
                print(f"    Bio: {n['bio'][:120]}")
        if len(new_intel) > 30:
            print(f"  ... and {len(new_intel) - 30} more")
        print()

    src.close()
    inv.close()

    if args.json_out:
        print(json.dumps({
            "matched": matched,
            "new_intel": new_intel[:50],
            "unmatched_count": len(unmatched),
        }, indent=2, default=str))


def cmd_stats(args):
    """Show local database stats."""
    if not DB_PATH.exists():
        print(f"No local database at {DB_PATH}")
        print("Run 'download' to fetch data from EpsteinExposed.com")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) as c FROM persons").fetchone()["c"]
    detailed = conn.execute("SELECT COUNT(*) as c FROM persons WHERE detail_fetched = 1").fetchone()["c"]
    with_bio = conn.execute("SELECT COUNT(*) as c FROM persons WHERE bio IS NOT NULL AND bio != ''").fetchone()["c"]
    black_book = conn.execute("SELECT COUNT(*) as c FROM persons WHERE black_book_entry = 1").fetchone()["c"]
    connections = conn.execute("SELECT COUNT(*) as c FROM person_connections").fetchone()["c"]

    # Category breakdown
    categories = conn.execute("""
        SELECT category, COUNT(*) as c FROM persons
        GROUP BY category ORDER BY c DESC
    """).fetchall()

    # Top persons by data volume
    top = conn.execute("""
        SELECT name, category, flight_count, document_count, connection_count, email_count,
               (flight_count + document_count + connection_count + email_count) as total
        FROM persons ORDER BY total DESC LIMIT 20
    """).fetchall()

    # Download meta
    meta = conn.execute("SELECT key, value, updated_at FROM download_meta").fetchall()

    print(f"=== EpsteinExposed Local DB: {DB_PATH} ===")
    print(f"  Total persons: {total}")
    print(f"  Detail fetched: {detailed}")
    print(f"  With bios: {with_bio}")
    print(f"  Black book entries: {black_book}")
    print(f"  Connections: {connections}")
    print()

    if meta:
        print("  Metadata:")
        for m in meta:
            print(f"    {m['key']}: {m['value']} (updated: {m['updated_at']})")
        print()

    print("  Categories:")
    for cat in categories:
        print(f"    {cat['category'] or 'none'}: {cat['c']}")
    print()

    print("  Top 20 by data volume:")
    for t in top:
        print(f"    {t['name']} [{t['category']}]: "
              f"F={t['flight_count']} D={t['document_count']:,} "
              f"C={t['connection_count']} E={t['email_count']}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="EpsteinExposed.com API — persons, documents, flights, connections"
    )
    parser.add_argument("--json", action="store_true", dest="json_out",
                        help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # download
    p = sub.add_parser("download", help="Download all persons + connections to local DB")
    p.add_argument("--skip-detail", action="store_true",
                   help="Skip fetching individual person details (faster, less data)")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch details even if already downloaded")

    # ingest
    p = sub.add_parser("ingest", help="Parse downloaded data into investigation.db")

    # search
    p = sub.add_parser("search", help="Cross-type search (documents + emails)")
    p.add_argument("query")
    p.add_argument("--type", choices=["documents", "emails"],
                   help="Limit to one type")
    p.add_argument("--limit", type=int, default=20)

    # persons
    p = sub.add_parser("persons", help="List persons")
    p.add_argument("--query", "-q", help="Search by name")
    p.add_argument("--category", choices=CATEGORIES, help="Filter by category")
    p.add_argument("--limit", type=int, default=50)

    # person (detail)
    p = sub.add_parser("person", help="Get full person detail by slug")
    p.add_argument("slug", help="Person slug (e.g., 'bill-gates') or name")

    # documents
    p = sub.add_parser("documents", help="Search documents (FTS5)")
    p.add_argument("query")
    p.add_argument("--source", choices=DOC_SOURCES, help="Filter by source")
    p.add_argument("--category", choices=DOC_CATEGORIES, help="Filter by category")
    p.add_argument("--limit", type=int, default=20)

    # flights
    p = sub.add_parser("flights", help="Search flight records")
    p.add_argument("--passenger", help="Filter by passenger name")
    p.add_argument("--year", type=int, help="Filter by year")
    p.add_argument("--origin", help="Filter by origin")
    p.add_argument("--destination", help="Filter by destination")
    p.add_argument("--limit", type=int, default=50)

    # match-entities
    p = sub.add_parser("match-entities",
                        help="Cross-reference with investigation.db")

    # stats
    p = sub.add_parser("stats", help="Show local DB stats")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "download": cmd_download,
        "ingest": cmd_ingest,
        "search": cmd_search,
        "persons": cmd_persons,
        "person": cmd_person,
        "documents": cmd_documents,
        "flights": cmd_flights,
        "match-entities": cmd_match_entities,
        "stats": cmd_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
