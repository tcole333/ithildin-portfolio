#!/usr/bin/env python3
"""
ProPublica Trump Team Financial Disclosures ingestion tool.

Searches and ingests data from ProPublica's database of 1,573 appointees,
3,196 documents, and 116,699 assets. Data source uses SvelteKit's compact
__data.json format with pointer-based node references.

Usage:
    uv run python tools/ingest_propublica_disclosures.py agencies
    uv run python tools/ingest_propublica_disclosures.py search "Palantir" [--output FILE]
    uv run python tools/ingest_propublica_disclosures.py appointee feinberg-stephen-andrew [--output FILE]
    uv run python tools/ingest_propublica_disclosures.py ingest feinberg-stephen-andrew
    uv run python tools/ingest_propublica_disclosures.py scan-entities [--output FILE]
    uv run python tools/ingest_propublica_disclosures.py stats
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "investigation.db"
CACHE_DIR = Path("/tmp/propublica-cache")

BASE_URL = "https://projects.propublica.org/trump-team-financial-disclosures"
SOURCE_NAME = "propublica_disclosures"

# Rate limiting: 1 second between requests
_last_request_time = 0.0


# --- SvelteKit Data Parser ---

def _resolve(data, pointer_map):
    """Resolve a SvelteKit pointer map against the flat data array.

    In SvelteKit's __data.json format, objects act as pointer maps where
    each value is an index into the flat data array. This function
    dereferences one level, producing a dict of actual values.
    """
    resolved = {}
    for key, idx in pointer_map.items():
        if isinstance(idx, int) and 0 <= idx < len(data):
            resolved[key] = data[idx]
        else:
            resolved[key] = idx
    return resolved


def _resolve_list(data, index_list, template=None):
    """Resolve a list of indices into resolved objects.

    Each index points to a pointer map in the data array.
    If template is provided, use it; otherwise the pointed-to item is used.
    """
    results = []
    for idx in index_list:
        if not isinstance(idx, int) or idx < 0 or idx >= len(data):
            continue
        item = data[idx]
        if isinstance(item, dict):
            results.append(_resolve(data, item))
        else:
            results.append(item)
    return results


def _get_sveltekit_data(raw_json):
    """Extract the flat data array from a SvelteKit __data.json response."""
    if not raw_json or raw_json.get("type") != "data":
        return None
    nodes = raw_json.get("nodes", [])
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "data":
            return node.get("data", [])
    return None


# --- HTTP / Caching ---

def _cache_key(url):
    """Generate a cache filename for a URL."""
    h = hashlib.md5(url.encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"


def _fetch_json(url, use_cache=True, cache_ttl=3600):
    """Fetch JSON from URL with caching and rate limiting.

    Args:
        url: URL to fetch
        use_cache: Whether to use file cache (default True)
        cache_ttl: Cache lifetime in seconds (default 1 hour)
    """
    global _last_request_time

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_key(url)

    # Check cache
    if use_cache and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < cache_ttl:
            with open(cache_path) as f:
                return json.load(f)

    # Rate limit
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    req = urllib.request.Request(url, headers={"User-Agent": "OSINT-Research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _last_request_time = time.time()

            # Cache the response
            with open(cache_path, "w") as f:
                json.dump(data, f)

            return data
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason} for {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason} for {url}", file=sys.stderr)
        return None


def _log(query, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, SOURCE_NAME, count)
    except Exception:
        pass


# --- Database Schema ---

def _get_db():
    """Get investigation.db connection with financial_disclosures schema."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS financial_disclosures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointee_id INTEGER,
            name TEXT NOT NULL,
            slug TEXT UNIQUE,
            title TEXT,
            agency TEXT,
            confirmation_status TEXT,
            net_worth_low INTEGER,
            net_worth_high INTEGER,
            holdover INTEGER DEFAULT 0,
            in_cabinet INTEGER DEFAULT 0,
            profile_id TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS disclosure_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            disclosure_id INTEGER REFERENCES financial_disclosures(id),
            entity_name TEXT,
            entity_id INTEGER,
            asset_type TEXT,
            value_low INTEGER,
            value_high INTEGER,
            income_type TEXT,
            income_amount TEXT,
            line_no TEXT
        );

        CREATE TABLE IF NOT EXISTS disclosure_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            disclosure_id INTEGER REFERENCES financial_disclosures(id),
            organization TEXT,
            position_title TEXT,
            start_date TEXT,
            end_date TEXT,
            line_no TEXT
        );

        CREATE TABLE IF NOT EXISTS disclosure_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            disclosure_id INTEGER REFERENCES financial_disclosures(id),
            doc_id INTEGER,
            form_type TEXT,
            doc_url TEXT,
            file TEXT
        );
    """)
    db.commit()
    return db


# --- Value Category Mapping ---

# ProPublica uses numeric IDs for OGE value ranges on Form 278
VALUE_CATEGORIES = {
    1: (0, 1_000),
    2: (1_001, 15_000),
    3: (15_001, 50_000),
    4: (50_001, 100_000),
    5: (100_001, 250_000),
    6: (250_001, 500_000),
    7: (500_001, 1_000_000),
    8: (1_000_001, 5_000_000),
    9: (5_000_001, 25_000_000),
    10: (25_000_001, 50_000_000),
    11: (50_000_001, 100_000_000),  # Added for completeness
    12: (100_000_001, 250_000_000),  # "Over $100M" bracket
    15: (250_000_001, 500_000_000),  # Upper brackets
}


def _value_range(category_id):
    """Convert a value_category_id to (low, high) dollar range."""
    if category_id is None:
        return (None, None)
    return VALUE_CATEGORIES.get(category_id, (None, None))


# --- API Functions ---

def fetch_agencies():
    """Fetch the full list of agencies."""
    url = f"{BASE_URL}/agencies/__data.json"
    raw = _fetch_json(url)
    if not raw:
        return []

    data = _get_sveltekit_data(raw)
    if not data:
        return []

    pointer_map = data[0]
    agencies_idx = pointer_map.get("agencies")
    if agencies_idx is None or not isinstance(data[agencies_idx], list):
        return []

    return _resolve_list(data, data[agencies_idx])


def fetch_search(query):
    """Search appointees by text query. Returns list of resolved result dicts."""
    encoded = urllib.parse.quote(query)
    url = f"{BASE_URL}/search/__data.json?q={encoded}"
    raw = _fetch_json(url, cache_ttl=86400)  # Cache search results for 24h
    if not raw:
        return []

    data = _get_sveltekit_data(raw)
    if not data:
        return []

    pointer_map = data[0]
    result_idx = pointer_map.get("result")
    if result_idx is None or not isinstance(data[result_idx], list):
        return []

    results = _resolve_list(data, data[result_idx])
    _log(query, len(results))
    return results


def fetch_appointee(slug):
    """Fetch full appointee record by slug. Returns (appointee_info, entries, documents)."""
    url = f"{BASE_URL}/appointees/{slug}/__data.json"
    raw = _fetch_json(url, cache_ttl=86400)
    if not raw:
        return None, [], []

    data = _get_sveltekit_data(raw)
    if not data:
        return None, [], []

    pointer_map = data[0]

    # Appointees list (usually contains the same person with different documents)
    appointees_idx = pointer_map.get("appointees")
    if appointees_idx is None or not isinstance(data[appointees_idx], list):
        return None, [], []

    appointee_records = _resolve_list(data, data[appointees_idx])
    if not appointee_records:
        return None, [], []

    # Use the first record as the canonical appointee info
    info = appointee_records[0]

    # Collect documents from all records
    documents = []
    seen_doc_ids = set()
    for rec in appointee_records:
        did = rec.get("did") or rec.get("main_document_id")
        if did and did not in seen_doc_ids:
            seen_doc_ids.add(did)
            documents.append({
                "doc_id": did,
                "form_type": rec.get("form_type"),
                "form_subtype": rec.get("form_subtype"),
                "doc_url": rec.get("url"),
                "file": rec.get("file"),
            })

    # Disclosure entries (positions + assets combined)
    entries_idx = pointer_map.get("disclosure_entries")
    entries = []
    if entries_idx is not None and isinstance(data[entries_idx], list):
        entries = _resolve_list(data, data[entries_idx])

    return info, entries, documents


def fetch_stats():
    """Fetch homepage stats (appointee/document/asset counts)."""
    url = f"{BASE_URL}/__data.json"
    raw = _fetch_json(url)
    if not raw:
        return None

    data = _get_sveltekit_data(raw)
    if not data:
        return None

    pointer_map = data[0]
    stats = {}
    for key in ("appointeeCt", "documentCt", "assetCt"):
        idx = pointer_map.get(key)
        if idx is not None:
            stats[key] = data[idx]

    # Value ranges
    vr_idx = pointer_map.get("valueRanges")
    if vr_idx is not None and isinstance(data[vr_idx], dict):
        vr = _resolve(data, data[vr_idx])
        stats["value_low_total"] = vr.get("low")
        stats["value_high_total"] = vr.get("high")

    return stats


# --- CLI Commands ---

def cmd_agencies(args):
    """List all agencies."""
    agencies = fetch_agencies()
    if not agencies:
        print("No agencies found.")
        return

    if write_output(agencies, args, summary=f"{len(agencies)} agencies"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(agencies, indent=2, default=str))
        return

    print(f"=== ProPublica Financial Disclosures: {len(agencies)} Agencies ===\n")
    for a in agencies:
        name = a.get("name", "?")
        slug = a.get("slug", "?")
        print(f"  {name}")
        print(f"    slug: {slug}")


def cmd_search(args):
    """Search appointees by text query."""
    results = fetch_search(args.query)

    if write_output(results, args, summary=f"disclosures search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"=== ProPublica Disclosures: {len(results)} results for '{args.query}' ===\n")

    for r in results:
        name = r.get("a_txt", "?")
        agency = r.get("agency_name", "")
        title = r.get("title", "")
        slug = r.get("a_slug", "")
        nw_low = r.get("net_worth_low")

        print(f"  {name}")
        if title:
            print(f"    Title: {title}")
        if agency:
            print(f"    Agency: {agency}")
        if nw_low:
            try:
                nw = int(nw_low)
                print(f"    Net Worth (low): ${nw:,}")
            except (ValueError, TypeError):
                print(f"    Net Worth (low): {nw_low}")
        if slug:
            print(f"    Slug: {slug}")

        # Show highlights (which assets matched the search)
        highlights_raw = r.get("highlights")
        if highlights_raw and isinstance(highlights_raw, str):
            try:
                hl = json.loads(highlights_raw)
                # Combine all non-empty highlight fields
                matched = []
                for k, v in hl.items():
                    if v and v.strip():
                        # Strip HTML mark tags for display
                        clean = v.replace("<mark>", "").replace("</mark>", "")
                        matched.append(clean[:120])
                if matched:
                    print(f"    Matches: {matched[0]}")
                    for m in matched[1:3]:
                        print(f"             {m}")
            except (json.JSONDecodeError, AttributeError):
                pass
        print()


def cmd_appointee(args):
    """Fetch and display full appointee record."""
    info, entries, documents = fetch_appointee(args.slug)
    if not info:
        print(f"Appointee '{args.slug}' not found.", file=sys.stderr)
        sys.exit(1)

    # Separate entries into positions (dsid=1, have metadata with role info)
    # and assets (dsid=2, have value_category_id)
    positions = []
    assets = []
    for entry in entries:
        metadata = entry.get("metadata", "")
        dsid = entry.get("dsid")

        # Entries with dsid=1 and metadata containing role/date info are positions
        # Entries with dsid=2 or value_category_id are assets
        if dsid == 1 and metadata:
            # Parse metadata: "Role, start - end" format
            pos = {
                "entity_name": entry.get("description", ""),
                "entity_id": entry.get("entity_id"),
                "metadata": metadata,
                "line_no": entry.get("line_no"),
                "doc_id": entry.get("doc_id"),
            }
            # Try to parse role and dates from metadata
            if "," in metadata:
                parts = metadata.rsplit(",", 1)
                pos["position_title"] = parts[0].strip()
                date_part = parts[1].strip()
                if " - " in date_part:
                    start, end = date_part.split(" - ", 1)
                    pos["start_date"] = start.strip()
                    pos["end_date"] = end.strip()
            else:
                pos["position_title"] = metadata

            positions.append(pos)
        else:
            val_cat = entry.get("value_category_id")
            val_low, val_high = _value_range(val_cat)
            asset = {
                "entity_name": entry.get("description", ""),
                "entity_id": entry.get("entity_id"),
                "value_category_id": val_cat,
                "value_low": val_low,
                "value_high": val_high,
                "income_type": entry.get("income_type"),
                "income_amount": entry.get("income_amount"),
                "line_no": entry.get("line_no"),
                "doc_id": entry.get("doc_id"),
                "form_type": entry.get("form_type"),
            }
            assets.append(asset)

    output_data = {
        "appointee": info,
        "positions": positions,
        "assets": assets,
        "documents": documents,
        "summary": {
            "name": info.get("name"),
            "title": info.get("title"),
            "agency": info.get("agency_name"),
            "confirmation_status": info.get("confirmation_status"),
            "net_worth_low": info.get("net_worth_low"),
            "net_worth_high": info.get("net_worth_high"),
            "position_count": len(positions),
            "asset_count": len(assets),
            "document_count": len(documents),
        },
    }

    if write_output(output_data, args, summary=f"appointee {args.slug}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(output_data, indent=2, default=str))
        return

    # Pretty-print
    name = info.get("name", "?")
    title = info.get("title", "")
    agency = info.get("agency_name", "")
    status = info.get("confirmation_status", "")
    nw_low = info.get("net_worth_low")
    nw_high = info.get("net_worth_high")
    holdover = info.get("holdover")

    print(f"=== {name} ===")
    if title:
        print(f"  Title: {title}")
    if agency:
        print(f"  Agency: {agency}")
    if status:
        print(f"  Status: {status}")
    if holdover:
        print(f"  Holdover: {'Yes' if str(holdover) == '1' else 'No'}")

    if nw_low or nw_high:
        try:
            low = int(nw_low) if nw_low else 0
            high = int(nw_high) if nw_high else 0
            print(f"  Net Worth: ${low:,} - ${high:,}")
        except (ValueError, TypeError):
            print(f"  Net Worth: {nw_low} - {nw_high}")

    # Documents
    if documents:
        print(f"\n--- Documents ({len(documents)}) ---")
        for doc in documents:
            form = doc.get("form_type", "?")
            subtype = doc.get("form_subtype", "")
            url = doc.get("doc_url", "")
            label = f"{form}"
            if subtype:
                label += f" ({subtype})"
            print(f"  [{doc.get('doc_id')}] {label}")
            if url:
                print(f"    {url}")

    # Positions
    if positions:
        print(f"\n--- Positions ({len(positions)}) ---")
        for pos in positions:
            org = pos.get("entity_name", "?")
            role = pos.get("position_title", "")
            start = pos.get("start_date", "")
            end = pos.get("end_date", "")
            line = pos.get("line_no", "")
            date_str = ""
            if start or end:
                date_str = f" ({start} - {end})"
            print(f"  [{line}] {org}")
            if role:
                print(f"       {role}{date_str}")

    # Assets (show first 30, summarize rest)
    if assets:
        print(f"\n--- Assets ({len(assets)}) ---")
        for asset in assets[:30]:
            entity = asset.get("entity_name", "?")
            val_low = asset.get("value_low")
            val_high = asset.get("value_high")
            income = asset.get("income_amount")
            income_type = asset.get("income_type")
            line = asset.get("line_no", "")

            val_str = ""
            if val_low is not None and val_high is not None:
                val_str = f" (${val_low:,} - ${val_high:,})"
            elif val_low is not None:
                val_str = f" (>${val_low:,})"

            inc_str = ""
            if income and str(income) != "0":
                inc_str = f" | income: {income}"
                if income_type:
                    inc_str += f" ({income_type})"

            print(f"  [{line}] {entity}{val_str}{inc_str}")

        if len(assets) > 30:
            print(f"  ... and {len(assets) - 30} more assets")


def cmd_ingest(args):
    """Ingest an appointee's full disclosure into investigation.db."""
    info, entries, documents = fetch_appointee(args.slug)
    if not info:
        print(f"Appointee '{args.slug}' not found.", file=sys.stderr)
        sys.exit(1)

    _ingest_one(args.slug, info, entries, documents)

    name = info.get("name", args.slug)
    position_count = sum(1 for e in entries if e.get("dsid") == 1 and e.get("metadata"))
    asset_count = len(entries) - position_count
    print(f"Ingested: {name} ({args.slug})")
    print(f"  Documents: {len(documents)}")
    print(f"  Positions: {position_count}")
    print(f"  Assets: {asset_count}")


def cmd_scan_entities(args):
    """Search ProPublica for all investigation-tracked entities and persons.

    Builds a cross-reference table showing which appointees hold stock in
    or have positions at tracked entities.
    """
    try:
        from tools.investigation_context import get_active_profile
        profile = get_active_profile()
    except Exception as e:
        print(f"Could not load investigation profile: {e}", file=sys.stderr)
        sys.exit(1)

    if not profile.name:
        print("No active investigation profile set.", file=sys.stderr)
        print("Run: uv run python tools/investigation_context.py set <name>", file=sys.stderr)
        sys.exit(1)

    # Build search terms from profile
    # Key persons (search by last name for broader matching)
    person_terms = []
    for person in profile.key_persons:
        parts = person.strip().split()
        if len(parts) >= 2:
            # Use last name for search (more likely to match)
            person_terms.append(parts[-1])
        else:
            person_terms.append(person)

    # Entity terms from thread targets (deduplicated)
    entity_terms = set()
    for thread in profile.threads:
        if isinstance(thread, dict):
            for target in thread.get("targets", []):
                # Skip very generic terms and person names
                if target.lower() in (t.lower() for t in profile.key_persons):
                    continue
                # Skip short/generic terms that produce noise
                if len(target) <= 3 and target.lower() not in ("a16z", "xai", "oge", "mda", "rtx"):
                    continue
                entity_terms.add(target)

    # Add key company names explicitly
    key_entities = [
        "Palantir", "Anduril", "SpaceX", "Tesla", "D-Wave", "Coatue",
        "Founders Fund", "a16z", "Andreessen Horowitz", "8VC", "Lux Capital",
        "Shield AI", "Scale AI", "Rebellion Defense", "DPCM Capital",
        "Cerberus", "Thiel", "Northrop Grumman", "Raytheon", "L3Harris",
        "Lockheed Martin", "Starlink", "Neuralink", "xAI", "Boring Company",
        "Davidson Technologies",
    ]
    for e in key_entities:
        entity_terms.add(e)

    all_terms = sorted(set(entity_terms))
    print(f"Scanning {len(all_terms)} entity terms + {len(person_terms)} person last names...")
    print()

    # Track cross-references: entity -> list of appointees
    entity_matches = {}  # search_term -> [{name, slug, agency, title, net_worth_low}]
    person_matches = {}
    all_appointees = {}  # slug -> appointee info (deduplicated)

    # Search entity terms
    for i, term in enumerate(all_terms):
        print(f"  [{i+1}/{len(all_terms)}] Searching: {term}", end="", flush=True)
        results = fetch_search(term)
        if results:
            entity_matches[term] = []
            for r in results:
                slug = r.get("a_slug", "")
                name = r.get("a_txt", "?")
                match_info = {
                    "name": name,
                    "slug": slug,
                    "agency": r.get("agency_name", ""),
                    "title": r.get("title", ""),
                    "net_worth_low": r.get("net_worth_low"),
                }
                entity_matches[term].append(match_info)
                all_appointees[slug] = match_info
            print(f" -> {len(results)} matches")
        else:
            print(f" -> 0 matches")

    # Search person terms
    for i, term in enumerate(person_terms):
        print(f"  [person {i+1}/{len(person_terms)}] Searching: {term}", end="", flush=True)
        results = fetch_search(term)
        if results:
            person_matches[term] = []
            for r in results:
                slug = r.get("a_slug", "")
                name = r.get("a_txt", "?")
                match_info = {
                    "name": name,
                    "slug": slug,
                    "agency": r.get("agency_name", ""),
                    "title": r.get("title", ""),
                    "net_worth_low": r.get("net_worth_low"),
                }
                person_matches[term].append(match_info)
                all_appointees[slug] = match_info
            print(f" -> {len(results)} matches")
        else:
            print(f" -> 0 matches")

    # Build output
    output = {
        "profile": profile.name,
        "entity_matches": entity_matches,
        "person_matches": person_matches,
        "unique_appointees": len(all_appointees),
        "summary": _build_scan_summary(entity_matches, person_matches, all_appointees),
    }

    if write_output(output, args, summary=f"scan-entities ({len(all_appointees)} unique appointees)"):
        return

    # Print summary
    print(f"\n{'='*70}")
    print(f"SCAN RESULTS: {len(all_appointees)} unique appointees across {len(entity_matches)} entity terms")
    print(f"{'='*70}\n")

    # Entity cross-reference table
    print("--- Entity Cross-References (appointees with tracked entity holdings/positions) ---\n")
    for term in sorted(entity_matches.keys(), key=lambda t: len(entity_matches[t]), reverse=True):
        matches = entity_matches[term]
        if not matches:
            continue
        print(f"  {term}: {len(matches)} appointees")
        for m in matches[:5]:
            nw = ""
            if m.get("net_worth_low"):
                try:
                    nw = f" (NW: ${int(m['net_worth_low']):,})"
                except (ValueError, TypeError):
                    pass
            print(f"    - {m['name']} — {m.get('title', '')} @ {m.get('agency', '')}{nw}")
        if len(matches) > 5:
            print(f"    ... and {len(matches) - 5} more")
        print()

    # Person matches
    if person_matches:
        print("--- Person Matches ---\n")
        for term in sorted(person_matches.keys()):
            matches = person_matches[term]
            if not matches:
                continue
            # Only show if it looks like the actual person (not just a common name match)
            print(f"  {term}: {len(matches)} matches")
            for m in matches[:3]:
                print(f"    - {m['name']} — {m.get('title', '')} @ {m.get('agency', '')}")
            if len(matches) > 3:
                print(f"    ... and {len(matches) - 3} more")
            print()


def _build_scan_summary(entity_matches, person_matches, all_appointees):
    """Build a structured summary of scan results for output files."""
    # Find appointees who appear across multiple entity searches (potential conflicts)
    appointee_entities = {}  # slug -> list of entity terms they matched
    for term, matches in entity_matches.items():
        for m in matches:
            slug = m.get("slug", "")
            if slug not in appointee_entities:
                appointee_entities[slug] = {
                    "name": m.get("name"),
                    "agency": m.get("agency"),
                    "title": m.get("title"),
                    "net_worth_low": m.get("net_worth_low"),
                    "entities": [],
                }
            appointee_entities[slug]["entities"].append(term)

    # Sort by number of entity matches (most conflicted first)
    multi_entity = {
        slug: info for slug, info in appointee_entities.items()
        if len(info["entities"]) >= 2
    }
    conflicts = sorted(
        multi_entity.items(),
        key=lambda x: len(x[1]["entities"]),
        reverse=True,
    )

    return {
        "total_entity_terms": len(entity_matches),
        "total_person_terms": len(person_matches),
        "unique_appointees": len(all_appointees),
        "multi_entity_conflicts": [
            {
                "slug": slug,
                "name": info["name"],
                "agency": info["agency"],
                "title": info["title"],
                "net_worth_low": info["net_worth_low"],
                "entity_count": len(info["entities"]),
                "entities": info["entities"],
            }
            for slug, info in conflicts[:50]
        ],
    }


def cmd_ingest_all(args):
    """Bulk ingest appointees from a file of slugs (one per line)."""
    slug_file = Path(args.slug_file)
    if not slug_file.exists():
        print(f"File not found: {slug_file}", file=sys.stderr)
        sys.exit(1)

    slugs = [line.strip() for line in slug_file.read_text().splitlines() if line.strip()]
    if not slugs:
        print("No slugs found in file.", file=sys.stderr)
        sys.exit(1)

    # Check what's already ingested to skip
    db = _get_db()
    existing = set(
        row[0] for row in db.execute("SELECT slug FROM financial_disclosures").fetchall()
    )
    db.close()

    if not args.force:
        to_ingest = [s for s in slugs if s not in existing]
        skipped = len(slugs) - len(to_ingest)
        if skipped:
            print(f"Skipping {skipped} already-ingested appointees (use --force to re-ingest)")
    else:
        to_ingest = slugs
        skipped = 0

    print(f"Ingesting {len(to_ingest)} appointees...")

    success = 0
    errors = []
    for i, slug in enumerate(to_ingest):
        try:
            info, entries, documents = fetch_appointee(slug)
            if not info:
                errors.append((slug, "not found"))
                print(f"  [{i+1}/{len(to_ingest)}] {slug}: NOT FOUND")
                continue

            # Re-use the ingest logic
            _ingest_one(slug, info, entries, documents)
            name = info.get("name", slug)
            asset_ct = len([e for e in entries if e.get("dsid") != 1])
            success += 1
            if (i + 1) % 50 == 0 or i == 0:
                print(f"  [{i+1}/{len(to_ingest)}] {name}: {asset_ct} assets")
        except Exception as e:
            errors.append((slug, str(e)))
            print(f"  [{i+1}/{len(to_ingest)}] {slug}: ERROR - {e}")

    print(f"\nDone: {success} ingested, {skipped} skipped, {len(errors)} errors")
    if errors:
        print("Errors:")
        for slug, err in errors[:20]:
            print(f"  {slug}: {err}")
        if len(errors) > 20:
            print(f"  ... +{len(errors) - 20} more")


def _ingest_one(slug, info, entries, documents):
    """Ingest a single appointee's data into investigation.db (shared logic)."""
    db = _get_db()

    try:
        from tools.investigation_context import get_active_profile_id
        profile_id = get_active_profile_id()
    except Exception:
        profile_id = None

    name = info.get("name", "")
    appointee_id = info.get("id")
    if isinstance(appointee_id, str):
        try:
            appointee_id = int(appointee_id)
        except ValueError:
            pass

    nw_low = info.get("net_worth_low")
    nw_high = info.get("net_worth_high")
    if isinstance(nw_low, str):
        try:
            nw_low = int(nw_low)
        except (ValueError, TypeError):
            nw_low = None
    if isinstance(nw_high, str):
        try:
            nw_high = int(nw_high)
        except (ValueError, TypeError):
            nw_high = None

    holdover = info.get("holdover")
    if isinstance(holdover, str):
        holdover = 1 if holdover == "1" else 0
    in_cabinet = info.get("in_cabinet")
    if isinstance(in_cabinet, str):
        in_cabinet = 1 if in_cabinet == "1" else 0

    db.execute("""
        INSERT INTO financial_disclosures
            (appointee_id, name, slug, title, agency, confirmation_status,
             net_worth_low, net_worth_high, holdover, in_cabinet, profile_id, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(slug) DO UPDATE SET
            appointee_id=excluded.appointee_id, name=excluded.name,
            title=excluded.title, agency=excluded.agency,
            confirmation_status=excluded.confirmation_status,
            net_worth_low=excluded.net_worth_low, net_worth_high=excluded.net_worth_high,
            holdover=excluded.holdover, in_cabinet=excluded.in_cabinet,
            profile_id=excluded.profile_id, fetched_at=CURRENT_TIMESTAMP
    """, (
        appointee_id, name, slug, info.get("title"), info.get("agency_name"),
        info.get("confirmation_status"), nw_low, nw_high,
        holdover or 0, in_cabinet or 0, profile_id,
    ))
    db.commit()

    disclosure_id = db.execute(
        "SELECT id FROM financial_disclosures WHERE slug = ?", (slug,)
    ).fetchone()["id"]

    db.execute("DELETE FROM disclosure_assets WHERE disclosure_id = ?", (disclosure_id,))
    db.execute("DELETE FROM disclosure_positions WHERE disclosure_id = ?", (disclosure_id,))
    db.execute("DELETE FROM disclosure_documents WHERE disclosure_id = ?", (disclosure_id,))

    for doc in documents:
        db.execute("""
            INSERT INTO disclosure_documents (disclosure_id, doc_id, form_type, doc_url, file)
            VALUES (?, ?, ?, ?, ?)
        """, (disclosure_id, doc.get("doc_id"), doc.get("form_type"),
              doc.get("doc_url"), doc.get("file")))

    for entry in entries:
        metadata = entry.get("metadata", "")
        dsid = entry.get("dsid")

        if dsid == 1 and metadata:
            pos_title = metadata
            start_date = None
            end_date = None
            if "," in metadata:
                parts = metadata.rsplit(",", 1)
                pos_title = parts[0].strip()
                date_part = parts[1].strip()
                if " - " in date_part:
                    start, end = date_part.split(" - ", 1)
                    start_date = start.strip()
                    end_date = end.strip()

            db.execute("""
                INSERT INTO disclosure_positions
                    (disclosure_id, organization, position_title, start_date, end_date, line_no)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (disclosure_id, entry.get("description", ""), pos_title,
                  start_date, end_date, entry.get("line_no")))
        else:
            val_cat = entry.get("value_category_id")
            val_low, val_high = _value_range(val_cat)
            income_amt = entry.get("income_amount")
            if income_amt == 0:
                income_amt = None

            db.execute("""
                INSERT INTO disclosure_assets
                    (disclosure_id, entity_name, entity_id, asset_type,
                     value_low, value_high, income_type, income_amount, line_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (disclosure_id, entry.get("description", ""), entry.get("entity_id"),
                  entry.get("form_type"), val_low, val_high,
                  entry.get("income_type"), str(income_amt) if income_amt else None,
                  entry.get("line_no")))

    db.commit()
    db.close()


def cmd_stats(args):
    """Show ProPublica database stats and local ingestion stats."""
    # Remote stats
    stats = fetch_stats()
    if stats:
        print("=== ProPublica Financial Disclosures Database ===\n")
        print(f"  Appointees: {stats.get('appointeeCt', '?'):,}")
        print(f"  Documents:  {stats.get('documentCt', '?'):,}")
        print(f"  Assets:     {stats.get('assetCt', '?'):,}")
        low = stats.get("value_low_total")
        high = stats.get("value_high_total")
        if low and high:
            print(f"  Value Range: ${low:,} - ${high:,}")
        print()

    # Local stats
    db = _get_db()

    disc_count = db.execute("SELECT COUNT(*) FROM financial_disclosures").fetchone()[0]
    asset_count = db.execute("SELECT COUNT(*) FROM disclosure_assets").fetchone()[0]
    pos_count = db.execute("SELECT COUNT(*) FROM disclosure_positions").fetchone()[0]
    doc_count = db.execute("SELECT COUNT(*) FROM disclosure_documents").fetchone()[0]

    print("=== Local Ingestion Stats ===\n")
    print(f"  Appointees ingested: {disc_count}")
    print(f"  Assets stored:       {asset_count}")
    print(f"  Positions stored:    {pos_count}")
    print(f"  Documents stored:    {doc_count}")

    if disc_count > 0:
        rows = db.execute("""
            SELECT name, agency, net_worth_low, fetched_at
            FROM financial_disclosures
            ORDER BY fetched_at DESC
            LIMIT 10
        """).fetchall()
        print(f"\n  Recently ingested:")
        for row in rows:
            nw = ""
            if row["net_worth_low"]:
                try:
                    nw = f" (NW: ${int(row['net_worth_low']):,})"
                except (ValueError, TypeError):
                    pass
            print(f"    {row['name']} — {row['agency']}{nw}")

    db.close()

    # Cache stats
    if CACHE_DIR.exists():
        cache_files = list(CACHE_DIR.glob("*.json"))
        total_size = sum(f.stat().st_size for f in cache_files)
        print(f"\n  Cache: {len(cache_files)} files, {total_size / 1024:.0f} KB")


def main():
    parser = argparse.ArgumentParser(
        description="ProPublica Trump Team Financial Disclosures"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # agencies
    p_agencies = sub.add_parser("agencies", help="List all agencies")
    add_output_args(p_agencies)

    # search
    p_search = sub.add_parser("search", help="Search appointees by term")
    p_search.add_argument("query", help="Search term (e.g., 'Palantir')")
    add_output_args(p_search)

    # appointee
    p_appointee = sub.add_parser("appointee", help="Get full appointee record")
    p_appointee.add_argument("slug", help="Appointee slug (e.g., 'feinberg-stephen-andrew')")
    add_output_args(p_appointee)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest appointee into investigation.db")
    p_ingest.add_argument("slug", help="Appointee slug")

    # ingest-all
    p_ingest_all = sub.add_parser("ingest-all", help="Bulk ingest from file of slugs")
    p_ingest_all.add_argument("slug_file", help="File with one slug per line")
    p_ingest_all.add_argument("--force", action="store_true",
                              help="Re-ingest already-ingested appointees")

    # scan-entities
    p_scan = sub.add_parser("scan-entities", help="Scan for all tracked entities/persons")
    add_output_args(p_scan)

    # stats
    p_stats = sub.add_parser("stats", help="Show database and ingestion stats")

    args = parser.parse_args()

    if args.command == "agencies":
        cmd_agencies(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "appointee":
        cmd_appointee(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "ingest-all":
        cmd_ingest_all(args)
    elif args.command == "scan-entities":
        cmd_scan_entities(args)
    elif args.command == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
