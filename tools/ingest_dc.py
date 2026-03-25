#!/usr/bin/env python3
"""
District of Columbia corporate registry ingester (DLCP CorpOnline).

Uses two data sources:
  1. DC Open Data ArcGIS FeatureServer (492K+ entities, no auth, SQL-like queries)
     - Primary search interface: name, agent, address, file number
     - Fields: BUSINESS_NAME, FILE_NUMBER, ENTITY_STATUS, MODELTYPE, LOCALE,
       RA_NAME, RA_ADDRESS*, BUSNIESS_ADDRESS*, EFFECTIVE_DATE, etc.
  2. CorpOnline API (corponlineapi.dlcp.dc.gov)
     - Detail endpoint: /api/businesssearch/{uuid} — no auth/CAPTCHA
     - Returns: principals, directors, filing history, NAICS codes, foreign jurisdiction
     - Search endpoint requires reCAPTCHA — NOT used by this tool

Architecture:
  Search → ArcGIS FeatureServer (fast, no auth, 492K entities)
  Detail → CorpOnline API /api/businesssearch/{uuid} (no auth)
  Ingest → ArcGIS data + optional CorpOnline enrichment → registry.db

Usage:
    python tools/ingest_dc.py search "Capital Athletic Foundation"
    python tools/ingest_dc.py search "Epstein" --output /tmp/dc-epstein.json
    python tools/ingest_dc.py search "Abramoff" --type nonprofit
    python tools/ingest_dc.py search-agent "Corporation Service Company" --limit 50
    python tools/ingest_dc.py search-address "Dupont Circle"
    python tools/ingest_dc.py detail <entity-uuid>
    python tools/ingest_dc.py ingest-entity <file-number>
    python tools/ingest_dc.py ingest-batch "Capital Athletic" "Epstein"
    python tools/ingest_dc.py stats
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import log_search
except ImportError:
    try:
        from lead_tracker import log_search
    except ImportError:
        def log_search(*a, **kw):
            pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ArcGIS FeatureServer — DC Open Data (492K+ entities, no auth)
ARCGIS_BASE = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Business_Licensing_and_Grants_WebMercator/FeatureServer/0"
)

# CorpOnline API — detail endpoint (no auth/CAPTCHA)
CORPONLINE_API = "https://corponlineapi.dlcp.dc.gov/api"
SITE_URL = "https://corponline.dlcp.dc.gov"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) OSINT-Research/1.0"

# All fields from ArcGIS
ARCGIS_FIELDS = (
    "FILE_NUMBER,ENTITY_STATUS,LOCALE,MODELTYPE,BUSINESS_NAME,SUFFIX,"
    "BUSNIESS_ADDRESS_LINE1,BUSNIESS_ADDRESS_LINE2,BUSNIESS_ADDRESS_LINE3,"
    "BUSNIESS_ADDRESS_LINE4,BUSINESS_CITY,BUSINESS_STATE,ZIPCODE,BUSINESS_COUNTRY,"
    "RA_NAME,RA_ADDRESS1,RA_ADDRESS2,RA_ADDRESS3,RA_ADDRESS4,"
    "RA_CITY,RA_STATE,RA_ZIPCODE,"
    "EFFECTIVE_DATE,FOREIGN_DATEOF_ORGANIZATION,"
    "NEXT_REPORTYEAR_DUE,DCS_LAST_MOD_DTTM,DATE_LAST_REPORT_FILED,"
    "LATESTREPORT_YEARFILED,LATESTFILED_REPORTDATE,OBJECTID,GLOBALID"
)

# Rate limiting
REQUEST_DELAY = 0.5  # ArcGIS is generous; CorpOnline needs 2s
CORPONLINE_DELAY = 2

# Max results per ArcGIS query
MAX_RESULTS = 100


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_json(url, timeout=60, delay=None):
    """Fetch JSON from a URL with retries."""
    if delay:
        time.sleep(delay)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = (attempt + 1) * 5
                print(f"  Rate limited (429). Waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode()[:300]
            print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
            return None
        except URLError as e:
            print(f"ERROR: {e.reason}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return None


def _post_json(url, data=None, timeout=30, delay=None):
    """Make a POST request with JSON body."""
    if delay:
        time.sleep(delay)
    body = json.dumps(data or {}).encode("utf-8")
    req = Request(url, data=body, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body_text = e.read().decode()[:300]
        print(f"ERROR: HTTP {e.code}: {body_text}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# ArcGIS search (primary)
# ---------------------------------------------------------------------------

def _escape_sql(value):
    """Escape a value for ArcGIS SQL WHERE clause."""
    return value.replace("'", "''")


def _epoch_to_date(epoch_ms):
    """Convert epoch milliseconds to YYYY-MM-DD."""
    if not epoch_ms:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=None).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


def arcgis_query(where, limit=MAX_RESULTS, offset=0):
    """Query DC ArcGIS FeatureServer.

    Args:
        where: SQL WHERE clause (e.g., "BUSINESS_NAME LIKE '%EPSTEIN%'")
        limit: Max results (default 100)
        offset: Result offset for pagination

    Returns:
        List of feature attribute dicts
    """
    params = {
        "where": where,
        "outFields": ARCGIS_FIELDS,
        "f": "json",
        "resultRecordCount": limit,
        "resultOffset": offset,
        "orderByFields": "BUSINESS_NAME ASC",
    }
    url = f"{ARCGIS_BASE}/query?{urlencode(params)}"
    data = _fetch_json(url, delay=REQUEST_DELAY)
    if not data:
        return []

    if "error" in data:
        print(f"ArcGIS error: {data['error'].get('message', data['error'])}", file=sys.stderr)
        return []

    return [f["attributes"] for f in data.get("features", [])]


def arcgis_count(where):
    """Get count of matching records."""
    params = {
        "where": where,
        "returnCountOnly": "true",
        "f": "json",
    }
    url = f"{ARCGIS_BASE}/query?{urlencode(params)}"
    data = _fetch_json(url, delay=REQUEST_DELAY)
    if data:
        return data.get("count", 0)
    return 0


def search_by_name(query, entity_type=None, status=None, limit=MAX_RESULTS):
    """Search entities by business name (LIKE %query%)."""
    where = f"BUSINESS_NAME LIKE '%{_escape_sql(query.upper())}%'"
    if entity_type:
        where += f" AND MODELTYPE LIKE '%{_escape_sql(entity_type)}%'"
    if status:
        where += f" AND ENTITY_STATUS = '{_escape_sql(status)}'"
    return arcgis_query(where, limit=limit)


def search_by_agent(query, limit=MAX_RESULTS):
    """Search entities by registered agent name."""
    where = f"RA_NAME LIKE '%{_escape_sql(query.upper())}%'"
    return arcgis_query(where, limit=limit)


def search_by_address(query, limit=MAX_RESULTS):
    """Search entities by business address."""
    escaped = _escape_sql(query.upper())
    where = (
        f"BUSNIESS_ADDRESS_LINE1 LIKE '%{escaped}%' OR "
        f"BUSNIESS_ADDRESS_LINE2 LIKE '%{escaped}%'"
    )
    return arcgis_query(where, limit=limit)


def search_by_file_number(file_number):
    """Search by exact file number."""
    where = f"FILE_NUMBER = '{_escape_sql(file_number)}'"
    return arcgis_query(where, limit=1)


# ---------------------------------------------------------------------------
# CorpOnline detail API (secondary — enrichment)
# ---------------------------------------------------------------------------

def get_entity_detail(entity_uuid):
    """Fetch full entity detail by UUID from CorpOnline API.

    No CAPTCHA required. Returns principals, directors, filings, NAICS, etc.
    """
    url = f"{CORPONLINE_API}/businesssearch/{entity_uuid}"
    result = _post_json(url, {}, delay=CORPONLINE_DELAY)
    if result and isinstance(result, dict):
        return result.get("data", result)
    return None


# ---------------------------------------------------------------------------
# Data normalization
# ---------------------------------------------------------------------------

def _normalize_record(rec):
    """Normalize an ArcGIS record to a standard format."""
    return {
        "file_number": rec.get("FILE_NUMBER", ""),
        "entity_name": rec.get("BUSINESS_NAME", ""),
        "entity_status": rec.get("ENTITY_STATUS", ""),
        "entity_type": rec.get("MODELTYPE", ""),
        "locale": rec.get("LOCALE", ""),
        "suffix": rec.get("SUFFIX", ""),
        "business_address": _build_address(
            rec.get("BUSNIESS_ADDRESS_LINE1"),
            rec.get("BUSNIESS_ADDRESS_LINE2"),
            rec.get("BUSNIESS_ADDRESS_LINE3"),
            rec.get("BUSNIESS_ADDRESS_LINE4"),
        ),
        "business_city": rec.get("BUSINESS_CITY"),
        "business_state": rec.get("BUSINESS_STATE"),
        "zipcode": rec.get("ZIPCODE"),
        "business_country": rec.get("BUSINESS_COUNTRY"),
        "agent_name": rec.get("RA_NAME"),
        "agent_address": _build_address(
            rec.get("RA_ADDRESS1"),
            rec.get("RA_ADDRESS2"),
            rec.get("RA_ADDRESS3"),
            rec.get("RA_ADDRESS4"),
        ),
        "agent_city": rec.get("RA_CITY"),
        "agent_state": rec.get("RA_STATE"),
        "agent_zip": rec.get("RA_ZIPCODE"),
        "effective_date": _epoch_to_date(rec.get("EFFECTIVE_DATE")),
        "foreign_date": _epoch_to_date(rec.get("FOREIGN_DATEOF_ORGANIZATION")),
        "last_report_filed": _epoch_to_date(rec.get("DATE_LAST_REPORT_FILED")),
        "last_modified": _epoch_to_date(rec.get("DCS_LAST_MOD_DTTM")),
        "next_report_due": rec.get("NEXT_REPORTYEAR_DUE"),
        "latest_report_year": rec.get("LATESTREPORT_YEARFILED"),
        "object_id": rec.get("OBJECTID"),
        "global_id": rec.get("GLOBALID"),
    }


def _build_address(*parts):
    """Build address string from non-empty parts."""
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return ", ".join(cleaned) if cleaned else None


def _parse_date(date_str):
    """Parse various date formats to YYYY-MM-DD."""
    if not date_str:
        return None
    # ISO 8601: 2005-09-01T00:00:00
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(date_str))
    if match:
        return match.group(1)
    # MM/DD/YYYY
    match = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(date_str))
    if match:
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"
    # Epoch ms
    if isinstance(date_str, (int, float)):
        return _epoch_to_date(date_str)
    return None


# ---------------------------------------------------------------------------
# Registry DB integration
# ---------------------------------------------------------------------------

def _map_entity_type(model_type):
    """Map DC MODELTYPE to unified registry entity type."""
    if not model_type:
        return None
    name = model_type.lower()
    type_map = {
        "domestic business corporation": "corp",
        "foreign business corporation": "foreign_corp",
        "domestic nonprofit corporation": "nonprofit",
        "foreign nonprofit corporation": "foreign_nonprofit",
        "domestic limited liability company": "llc",
        "foreign limited liability company": "foreign_llc",
        "domestic limited partnership": "lp",
        "foreign limited partnership": "foreign_lp",
        "domestic limited liability partnership": "llp",
        "foreign limited liability partnership": "foreign_llp",
        "domestic statutory trust": "trust",
        "foreign statutory trust": "foreign_trust",
        "domestic general cooperative association": "cooperative",
        "foreign general cooperative association": "foreign_cooperative",
    }
    for key, val in type_map.items():
        if key in name:
            return val
    return name.replace(" ", "_")


def _map_status(status_str):
    """Map DC entity status to unified status."""
    if not status_str:
        return None
    name = status_str.lower()
    status_map = {
        "active": "active",
        "in good standing": "active",
        "revoked": "revoked",
        "dissolved": "dissolved",
        "merged": "merged",
        "converted": "converted",
        "cancelled": "cancelled",
        "inactive": "inactive",
        "withdrawal": "withdrawn",
    }
    for key, val in status_map.items():
        if key in name:
            return val
    return name


def _upsert_from_arcgis(db, rec):
    """Insert or update a DC entity from ArcGIS data into registry.db.

    Args:
        db: Database connection
        rec: Raw ArcGIS attributes dict

    Returns:
        registry entity ID
    """
    norm = _normalize_record(rec)
    source_id = norm["file_number"]
    if not source_id:
        return None

    name = norm["entity_name"]
    etype = _map_entity_type(norm["entity_type"])
    status = _map_status(norm["entity_status"])
    formation_date = norm["effective_date"]
    last_filing_date = norm["last_report_filed"]

    # State of formation
    state_of_formation = "DC"
    if norm["locale"] == "Foreign" and norm["foreign_date"]:
        state_of_formation = None  # Unknown for foreign entities from ArcGIS

    # Addresses
    principal_address = norm["business_address"]
    principal_city = norm["business_city"]
    principal_state = norm["business_state"]
    principal_zip = norm["zipcode"]
    principal_country = norm["business_country"]

    # Source URL
    source_url = f"{SITE_URL}/homepage/business-search"

    # Raw data
    raw_data = json.dumps(rec, indent=2, default=str)

    # Upsert
    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='dc' AND source_id=?",
        [source_id],
    ).fetchone()

    if existing:
        entity_id = existing[0]
        db.execute(
            """
            UPDATE registry_entities SET
                entity_name=?, entity_type=?, status=?,
                formation_date=?, last_filing_date=?,
                state_of_formation=COALESCE(?, state_of_formation),
                principal_address=COALESCE(?, principal_address),
                principal_city=COALESCE(?, principal_city),
                principal_state=COALESCE(?, principal_state),
                principal_zip=COALESCE(?, principal_zip),
                principal_country=COALESCE(?, principal_country),
                source_url=?, raw_data=?, updated_at=datetime('now')
            WHERE id=?
            """,
            [
                name, etype, status,
                formation_date, last_filing_date,
                state_of_formation,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                source_url, raw_data,
                entity_id,
            ],
        )
    else:
        db.execute(
            """
            INSERT INTO registry_entities (
                source_jurisdiction, source_id, entity_name, entity_type, status,
                formation_date, last_filing_date, state_of_formation,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                source_url, raw_data
            ) VALUES ('dc', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source_id, name, etype, status,
                formation_date, last_filing_date, state_of_formation,
                principal_address, principal_city, principal_state,
                principal_zip, principal_country,
                source_url, raw_data,
            ],
        )
        row = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='dc' AND source_id=?",
            [source_id],
        ).fetchone()
        entity_id = row[0]

    # Registered agent
    agent_name = norm["agent_name"]
    if agent_name and agent_name.strip():
        agent_address = norm["agent_address"]
        agent_city = norm["agent_city"]
        agent_state = norm["agent_state"]
        agent_zip = norm["agent_zip"]

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, agent_type, address, city, state, zip, country,
                 effective_date)
                VALUES (?, ?, 'entity', ?, ?, ?, ?, 'US', ?)
                """,
                [
                    entity_id, agent_name,
                    agent_address, agent_city, agent_state, agent_zip,
                    formation_date,
                ],
            )
        except Exception:
            pass

    return entity_id


def _enrich_from_corponline(db, entity_id, detail_data):
    """Enrich a registry entity with CorpOnline detail data.

    Adds: foreign jurisdiction, dissolution date, principals, directors,
    organizers, incorporators, filing attachments.
    """
    if not detail_data:
        return

    # Update entity with enrichment data
    dissolution_date = _parse_date(detail_data.get("dissolvedDate"))
    foreign_juris = detail_data.get("foreignJurisdiction", {}) or {}
    state_of_formation = foreign_juris.get("foreignJurisdictionState")

    updates = []
    params = []
    if dissolution_date:
        updates.append("dissolution_date=?")
        params.append(dissolution_date)
    if state_of_formation:
        updates.append("state_of_formation=?")
        params.append(state_of_formation)

    # Update raw_data to include enrichment
    raw = db.execute("SELECT raw_data FROM registry_entities WHERE id=?", [entity_id]).fetchone()
    if raw and raw[0]:
        try:
            existing_raw = json.loads(raw[0])
            existing_raw["_corponline_detail"] = detail_data
            updates.append("raw_data=?")
            params.append(json.dumps(existing_raw, indent=2, default=str))
        except json.JSONDecodeError:
            pass

    if updates:
        params.append(entity_id)
        db.execute(
            f"UPDATE registry_entities SET {', '.join(updates)}, updated_at=datetime('now') WHERE id=?",
            params,
        )

    # Officers from principals/directors
    for role_key, default_title in [
        ("principles", "principal"),
        ("businessDirectors", "director"),
        ("businessOrganizers", "organizer"),
        ("businessIncorporators", "incorporator"),
    ]:
        for person in detail_data.get(role_key, []):
            pname = person.get("name") or person.get("businessName")
            if not pname:
                continue
            ptitle = person.get("title") or default_title
            paddr = person.get("physicalAddress", {}) or {}
            try:
                db.execute(
                    """
                    INSERT OR IGNORE INTO registry_officers
                    (entity_id, officer_name, title, officer_type, address, city, state, zip, country)
                    VALUES (?, ?, ?, 'person', ?, ?, ?, ?, ?)
                    """,
                    [
                        entity_id, pname, ptitle,
                        paddr.get("fullAddress"),
                        paddr.get("city"),
                        paddr.get("state"),
                        paddr.get("zip5"),
                        paddr.get("country") or "US",
                    ],
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Search DC corporate registry by entity name."""
    type_filter = None
    if args.type:
        type_map = {
            "corp": "Business Corporation",
            "llc": "Limited Liability Company",
            "nonprofit": "Nonprofit Corporation",
            "lp": "Limited Partnership",
            "llp": "Limited Liability Partnership",
            "trust": "Statutory Trust",
        }
        type_filter = type_map.get(args.type, args.type)

    status_filter = None
    if args.status:
        status_map = {
            "active": "Active - In Good Standing",
            "revoked": "Revoked",
            "dissolved": "Dissolved",
            "inactive": "Inactive",
        }
        status_filter = status_map.get(args.status, args.status)

    results_raw = search_by_name(
        args.query,
        entity_type=type_filter,
        status=status_filter,
        limit=args.limit,
    )
    results = [_normalize_record(r) for r in results_raw]
    total = len(results)

    log_search(args.query, "dc_corp_registry", total)

    if write_output(results, args, summary=f"DC corp search '{args.query}'"):
        return

    # Count total matches
    where = f"BUSINESS_NAME LIKE '%{_escape_sql(args.query.upper())}%'"
    if type_filter:
        where += f" AND MODELTYPE LIKE '%{_escape_sql(type_filter)}%'"
    if status_filter:
        where += f" AND ENTITY_STATUS = '{_escape_sql(status_filter)}'"
    total_count = arcgis_count(where)

    print(f"Found {total_count} DC entities matching '{args.query}' (showing {total})")
    print()
    for r in results:
        _print_result(r)


def cmd_search_agent(args):
    """Search DC corporate registry by registered agent name."""
    results_raw = search_by_agent(args.query, limit=args.limit)
    results = [_normalize_record(r) for r in results_raw]

    log_search(f"agent:{args.query}", "dc_corp_registry", len(results))

    if write_output(results, args, summary=f"DC agent search '{args.query}'"):
        return

    print(f"Found {len(results)} DC entities with agent matching '{args.query}'")
    print()
    for r in results:
        _print_result(r)


def cmd_search_address(args):
    """Search DC corporate registry by business address."""
    results_raw = search_by_address(args.query, limit=args.limit)
    results = [_normalize_record(r) for r in results_raw]

    log_search(f"address:{args.query}", "dc_corp_registry", len(results))

    if write_output(results, args, summary=f"DC address search '{args.query}'"):
        return

    print(f"Found {len(results)} DC entities at address matching '{args.query}'")
    print()
    for r in results:
        _print_result(r)


def cmd_detail(args):
    """Fetch full entity detail by UUID from CorpOnline API."""
    entity = get_entity_detail(args.entity_uuid)
    if not entity:
        print(f"Entity {args.entity_uuid} not found")
        return

    if write_output(entity, args, summary=f"DC entity detail {args.entity_uuid}"):
        return

    _print_detail(entity)


def cmd_ingest_entity(args):
    """Ingest a specific entity by file number into registry.db.

    1. Looks up entity in ArcGIS by file number
    2. Inserts basic data from ArcGIS
    3. Optionally enriches with CorpOnline detail (if --enrich flag)
    """
    results = search_by_file_number(args.file_number)
    if not results:
        print(f"No entity found with file number '{args.file_number}'")
        return

    rec = results[0]
    db = get_db()
    entity_id = _upsert_from_arcgis(db, rec)

    if entity_id and args.enrich:
        # Try to find UUID from CorpOnline
        # The GLOBALID from ArcGIS is NOT the same as the CorpOnline UUID
        # We need to search CorpOnline to get the UUID
        print(f"  Enrichment from CorpOnline not available without UUID.")
        print(f"  Use 'detail <uuid>' after finding UUID from corponline.dlcp.dc.gov")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    name = rec.get("BUSINESS_NAME", "?")
    print(f"Ingested: {name} ({args.file_number}) -> registry ID {entity_id}")


def cmd_ingest_batch(args):
    """Search and ingest all entities matching multiple queries."""
    db = get_db()
    total_ingested = 0

    for query in args.queries:
        print(f"\n--- Searching DC: '{query}' ---")
        results = search_by_name(query, limit=args.limit)

        if not results:
            print(f"  No results for '{query}'")
            continue

        print(f"  Found {len(results)} results")

        for i, rec in enumerate(results):
            file_num = rec.get("FILE_NUMBER", "")
            name = rec.get("BUSINESS_NAME", "?")

            if not file_num:
                print(f"  [{i+1}/{len(results)}] SKIP (no file number): {name}")
                continue

            # Check if already ingested
            existing = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='dc' AND source_id=?",
                [file_num],
            ).fetchone()
            if existing and not args.force:
                print(f"  [{i+1}/{len(results)}] SKIP (already ingested): {name} ({file_num})")
                continue

            rid = _upsert_from_arcgis(db, rec)
            if rid:
                total_ingested += 1
                print(f"  [{i+1}/{len(results)}] Ingested: {name} ({file_num}) -> ID {rid}")

        log_search(query, "dc_corp_registry", len(results))

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    print(f"\nBatch ingest complete: {total_ingested} entities ingested")


def cmd_stats(args):
    """Show DC registry statistics."""
    total = arcgis_count("1=1")
    active = arcgis_count("ENTITY_STATUS = 'Active - In Good Standing'")
    revoked = arcgis_count("ENTITY_STATUS = 'Revoked'")
    dissolved = arcgis_count("ENTITY_STATUS = 'Dissolved'")

    stats = {
        "total_entities": total,
        "active": active,
        "revoked": revoked,
        "dissolved": dissolved,
        "source": "DC DLCP Open Data ArcGIS FeatureServer",
        "url": ARCGIS_BASE,
    }

    if write_output(stats, args, summary="DC registry stats"):
        return

    print(f"DC Corporate Registry Statistics")
    print(f"  Total entities:  {total:,}")
    print(f"  Active:          {active:,}")
    print(f"  Revoked:         {revoked:,}")
    print(f"  Dissolved:       {dissolved:,}")
    print(f"  Other:           {total - active - revoked - dissolved:,}")
    print(f"\n  Source: DC DLCP Open Data ArcGIS FeatureServer")

    # Registry.db stats
    try:
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) FROM registry_entities WHERE source_jurisdiction='dc'"
        ).fetchone()
        print(f"  Ingested in registry.db: {row[0]:,}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_result(r):
    """Print a normalized search result."""
    name = r["entity_name"]
    num = r["file_number"]
    status = r["entity_status"]
    etype = r["entity_type"]
    date = r["effective_date"] or "?"
    agent = r["agent_name"]
    addr = r["business_address"]

    print(f"  {name}")
    print(f"    File No: {num}  |  Type: {etype}")
    print(f"    Status: {status}  |  Effective: {date}")
    if agent:
        print(f"    Agent: {agent}")
    if addr:
        city = r["business_city"] or ""
        state = r["business_state"] or ""
        zip_code = r["zipcode"] or ""
        full = f"{addr}, {city}, {state} {zip_code}".strip(", ")
        print(f"    Address: {full}")
    print()


def _print_detail(entity):
    """Print full detail from CorpOnline API."""
    name = entity.get("businessName", "?")
    num = entity.get("entityNumber", "?")
    print(f"=== DC Entity Detail ===")
    print(f"  Name: {name}")
    print(f"  File No: {num}")
    print(f"  UUID: {entity.get('idBusiness', '?')}")

    rt = entity.get("recordType", {}) or {}
    if rt:
        print(f"  Type: {rt.get('name', '?')} ({rt.get('regionType', '?')})")

    bs = entity.get("businessStatus", {}) or {}
    if bs:
        print(f"  Status: {bs.get('name', '?')}")

    for label, key in [
        ("Formation Date", "dateOfIncorporation"),
        ("Effective Date", "effectiveDate"),
        ("Dissolved Date", "dissolvedDate"),
        ("Last AR Filed", "lastAnnualReportFiledDate"),
        ("AR Due Date", "arDueDate"),
        ("Last Modified", "lastModifiedEntityStatusDate"),
    ]:
        val = _parse_date(entity.get(key))
        if val:
            print(f"  {label}: {val}")

    ar_status = entity.get("arStatus")
    if ar_status:
        print(f"  AR Status: {ar_status}")

    fj = entity.get("foreignJurisdiction", {}) or {}
    if fj.get("foreignJurisdictionState"):
        print(f"  Foreign Jurisdiction: {fj['foreignJurisdictionState']}, "
              f"{fj.get('foreignJurisdictionCountry', '?')}")

    for label, key in [
        ("Physical Address", "physicalAddress"),
        ("Mailing Address", "mailingAddress"),
    ]:
        addr = entity.get(key, {}) or {}
        full = addr.get("fullAddress", "")
        if full and full not in ("None", "N/A"):
            print(f"  {label}: {full}")

    ra = entity.get("registerAgent")
    if ra and isinstance(ra, dict):
        ra_name = ra.get("businessName") or ra.get("name", "?")
        print(f"  Registered Agent: {ra_name}")

    for label, key in [
        ("Principals", "principles"),
        ("Directors", "businessDirectors"),
        ("Organizers", "businessOrganizers"),
    ]:
        items = entity.get(key, [])
        if items:
            print(f"\n  {label}:")
            for item in items:
                pname = item.get("name") or item.get("businessName", "?")
                ptitle = item.get("title", "")
                print(f"    - {pname}" + (f" ({ptitle})" if ptitle else ""))

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="District of Columbia corporate registry (DLCP / DC Open Data)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name")
    p.add_argument("query", help="Entity name to search for")
    p.add_argument("--type", choices=["corp", "llc", "nonprofit", "lp", "llp", "trust"],
                    help="Filter by entity type")
    p.add_argument("--status", choices=["active", "revoked", "dissolved", "inactive"],
                    help="Filter by entity status")
    p.add_argument("--limit", type=int, default=MAX_RESULTS,
                    help=f"Max results (default {MAX_RESULTS})")
    add_output_args(p)

    # search-agent
    p = sub.add_parser("search-agent", help="Search by registered agent name")
    p.add_argument("query", help="Agent name to search for")
    p.add_argument("--limit", type=int, default=MAX_RESULTS,
                    help=f"Max results (default {MAX_RESULTS})")
    add_output_args(p)

    # search-address
    p = sub.add_parser("search-address", help="Search by business address")
    p.add_argument("query", help="Address text to search for")
    p.add_argument("--limit", type=int, default=MAX_RESULTS,
                    help=f"Max results (default {MAX_RESULTS})")
    add_output_args(p)

    # detail
    p = sub.add_parser("detail", help="Fetch full entity detail by CorpOnline UUID")
    p.add_argument("entity_uuid", help="Entity UUID from CorpOnline")
    add_output_args(p)

    # ingest-entity
    p = sub.add_parser("ingest-entity", help="Ingest entity by file number into registry.db")
    p.add_argument("file_number", help="DC file number (e.g., L04091)")
    p.add_argument("--enrich", action="store_true",
                    help="Attempt to enrich with CorpOnline detail")

    # ingest-batch
    p = sub.add_parser("ingest-batch", help="Search and ingest all matching entities")
    p.add_argument("queries", nargs="+", help="Search queries")
    p.add_argument("--force", action="store_true",
                    help="Re-ingest even if already in registry.db")
    p.add_argument("--limit", type=int, default=MAX_RESULTS,
                    help=f"Max results per query (default {MAX_RESULTS})")

    # stats
    p = sub.add_parser("stats", help="Show DC registry statistics")
    add_output_args(p)

    args = parser.parse_args()
    handlers = {
        "search": cmd_search,
        "search-agent": cmd_search_agent,
        "search-address": cmd_search_address,
        "detail": cmd_detail,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
        "stats": cmd_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
