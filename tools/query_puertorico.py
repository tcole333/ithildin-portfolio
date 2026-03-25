#!/usr/bin/env python3
"""
Puerto Rico Department of State corporate registry tool.

Queries the PR Department of State Registry of Corporations and Other Legal
Entities via the rceapi.estado.pr.gov REST API. Covers corporations, LLCs,
LLPs, and other entity types registered in Puerto Rico.

Key for Act 60 (crypto/tax incentive) entity tracing. PR entities often have
rich data: officers (incorporators/authorized persons), resident agent,
addresses, filing history, and downloadable documents.

API endpoints (all free, no auth):
  - Search:   POST rceapi.estado.pr.gov/api/corporation/search
  - Entity:   GET  rceapi.estado.pr.gov/api/corporation/info/{regIndex}
  - Related:  GET  rceapi.estado.pr.gov/api/corporation/relatedentities/{regIndex}
  - Articles: GET  rceapi.estado.pr.gov/api/corporation/docs/articles/{type}/{regIndex}
  - Filings:  GET  rceapi.estado.pr.gov/api/corporation/docs/filings/annualreport/{regIndex}/summary

Usage:
    python tools/query_puertorico.py search "AXIOM MANAGEMENT"
    python tools/query_puertorico.py search "FOLKMAN" --match starting-with --active
    python tools/query_puertorico.py search --registry-number 420115
    python tools/query_puertorico.py entity 420115-1511
    python tools/query_puertorico.py entity 420115-1511 --filings --articles
    python tools/query_puertorico.py ingest 420115-1511
    python tools/query_puertorico.py ingest-search "WORLD LIBERTY" --limit 50
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import log_search
except ImportError:
    try:
        from lead_tracker import log_search
    except ImportError:
        def log_search(*a, **kw):
            pass

try:
    from tools.query_registry import get_db, _rebuild_fts
except ImportError:
    try:
        from query_registry import get_db, _rebuild_fts
    except ImportError:
        def get_db():
            raise RuntimeError("query_registry not available")
        def _rebuild_fts(db):
            pass


# ══════════════════════════════════════════════════════════
# API CONFIGURATION
# ══════════════════════════════════════════════════════════

API_BASE = "https://rceapi.estado.pr.gov/api"
PORTAL_URL = "https://rcp.estado.pr.gov/en"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Search matchType values (Corporation Name dropdown)
MATCH_TYPES = {
    "all-words": 4,      # All Words (default)
    "any-word": 3,        # Any Word
    "starting-with": 1,   # Starting With
    "exact": 2,           # Exact Match
}

# Rate limit: 1 request per second
RATE_LIMIT_SECONDS = 1.0
_last_request_time = 0.0


# ══════════════════════════════════════════════════════════
# HTTP HELPERS
# ══════════════════════════════════════════════════════════

def _api_get(path, retries=3):
    """Make a GET request to the PR DoS API with rate limiting and retries."""
    global _last_request_time
    url = f"{API_BASE}/{path}"

    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
    }
    req = Request(url, headers=headers, method="GET")

    for attempt in range(retries):
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success") is False and data.get("code") != 1:
                    print(f"  API returned success=false, code={data.get('code')}", file=sys.stderr)
                    return None
                return data
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = (attempt + 1) * 3
                print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            err_body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code}: {err_body}", file=sys.stderr)
            return None
        except (URLError, TimeoutError) as e:
            wait = (attempt + 1) * 3
            print(f"  Connection error: {e}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

    print("ERROR: Max retries exceeded", file=sys.stderr)
    return None


def _api_post(path, payload, retries=3):
    """Make a POST request to the PR DoS API with rate limiting and retries."""
    global _last_request_time
    url = f"{API_BASE}/{path}"

    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/problem+json; charset=UTF-8",
        "User-Agent": USER_AGENT,
    }
    req = Request(url, data=body, headers=headers, method="POST")

    for attempt in range(retries):
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success") is False and data.get("code") != 1:
                    print(f"  API returned success=false, code={data.get('code')}", file=sys.stderr)
                    return None
                return data
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = (attempt + 1) * 3
                print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            err_body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code}: {err_body}", file=sys.stderr)
            return None
        except (URLError, TimeoutError) as e:
            wait = (attempt + 1) * 3
            print(f"  Connection error: {e}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

    print("ERROR: Max retries exceeded", file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════════
# SEARCH COMMAND
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search PR corporate registry by name or registry number."""
    payload = {
        "cancellationMode": False,
        "comparisonType": 1,
        "corpName": None,
        "isWorkFlowSearch": False,
        "limit": args.limit,
        "matchType": MATCH_TYPES.get(args.match, 4),
        "method": None,
        "onlyActive": args.active,
        "registryNumber": None,
        "advanceSearch": None,
    }

    if args.registry_number:
        payload["registryNumber"] = int(args.registry_number)
        payload["corpName"] = None
    else:
        payload["corpName"] = args.query
        payload["registryNumber"] = None

    data = _api_post("corporation/search", payload)
    if not data or not data.get("response"):
        print("Search failed or no results")
        return

    records = data["response"].get("records", [])
    total = data["response"].get("totalRecords", len(records))

    search_term = args.query or f"registry#{args.registry_number}"
    log_search(search_term, "pr_dos", len(records))

    if write_output(records, args, summary=f"PR DoS search '{search_term}' ({len(records)} of {total})"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(records, indent=2, default=str))
        return

    print(f"Found {len(records)} of {total} PR entities matching '{search_term}'")
    if args.active:
        print("  (active entities only)")
    print()
    for r in records:
        reg_num = r.get("registrationNumber", "?")
        reg_idx = r.get("registrationIndex", "?")
        name = r.get("corpName", "?")
        class_en = r.get("classEn", "?")
        profit = r.get("profitTypeEn", "")
        status = r.get("statusEn", "?")

        print(f"  {name}")
        print(f"    Registry: {reg_num} (Index: {reg_idx}) | {class_en} | {profit}")
        print(f"    Status: {status}")
        print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL COMMAND
# ══════════════════════════════════════════════════════════

def _format_address(addr_obj):
    """Format a PR DoS address object into a readable string."""
    if not addr_obj:
        return None
    parts = []
    a1 = (addr_obj.get("address1") or "").strip()
    a2 = (addr_obj.get("address2") or "").strip()
    if a1:
        parts.append(a1)
    if a2:
        parts.append(a2)
    city = (addr_obj.get("city") or "").strip()
    state_abbr = (addr_obj.get("stateAbbreviation") or addr_obj.get("state") or "").strip()
    zipcode = (addr_obj.get("zip") or "").strip()
    if city:
        loc = city
        if state_abbr:
            loc += f", {state_abbr}"
        if zipcode:
            loc += f" {zipcode}"
        parts.append(loc)
    elif state_abbr:
        loc = state_abbr
        if zipcode:
            loc += f" {zipcode}"
        parts.append(loc)
    return ", ".join(parts) if parts else None


def _extract_person_name(person):
    """Extract a display name from a PR DoS person/party object."""
    if person.get("individualName"):
        ind = person["individualName"]
        first = (ind.get("firstName") or "").strip()
        middle = (ind.get("middleName") or "").strip()
        last = (ind.get("lastName") or "").strip()
        surname = (ind.get("surName") or "").strip()
        parts = [p for p in [first, middle, last, surname] if p]
        return " ".join(parts)
    elif person.get("organizationName"):
        return (person["organizationName"].get("name") or "").strip()
    elif person.get("name"):
        return person["name"].strip()
    return None


def _fetch_entity_info(reg_index):
    """Fetch full entity info from PR DoS API."""
    return _api_get(f"corporation/info/{reg_index}")


def cmd_entity(args):
    """Get full entity detail by registration index."""
    reg_index = args.reg_index

    data = _fetch_entity_info(reg_index)
    if not data or not data.get("response"):
        print(f"Entity {reg_index} not found or API error")
        return

    resp = data["response"]
    corp = resp.get("corporation", {})
    result = {"entity": resp}

    # Optionally fetch articles
    if args.articles:
        articles = {}
        for art_type in ("regandorg", "amendments", "namechange", "correspondence"):
            art_data = _api_get(f"corporation/docs/articles/{art_type}/{reg_index}")
            if art_data and art_data.get("response"):
                articles[art_type] = art_data["response"]
        if articles:
            result["articles"] = articles

    # Optionally fetch annual filings summary
    if args.filings:
        filings_data = _api_get(f"corporation/docs/filings/annualreport/{reg_index}/summary")
        if filings_data and filings_data.get("response"):
            result["annual_filings"] = filings_data["response"]

    # Optionally fetch related entities
    if args.related:
        rel_data = _api_get(f"corporation/relatedentities/{reg_index}")
        if rel_data and rel_data.get("response"):
            result["related_entities"] = rel_data["response"]

    if write_output(result, args, summary=f"PR DoS entity {reg_index}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    # Pretty print
    print(f"\n  [PR DoS] {corp.get('corpName', '?')}")
    print(f"    Registry: {corp.get('corpRegisterNumber', '?')} (Index: {corp.get('corpRegisterIndex', '?')})")
    print(f"    Class: {corp.get('classEn', '?')}")
    print(f"    Type: {corp.get('typeEn', '?')}")
    print(f"    Jurisdiction: {corp.get('jurisdictionEn', '?')}")
    print(f"    Status: {corp.get('statusEn', '?')}")
    if corp.get("designation"):
        print(f"    Designation: {corp['designation']}")
    if corp.get("dateFormed"):
        print(f"    Formation Date: {corp['dateFormed'][:10]}")
    if corp.get("effectiveDate") and corp["effectiveDate"] != corp.get("dateFormed"):
        print(f"    Effective Date: {corp['effectiveDate'][:10]}")
    if corp.get("isPerpetual"):
        print(f"    Expiration: Perpetual")
    elif corp.get("expirationDate"):
        print(f"    Expiration: {corp['expirationDate'][:10]}")
    if corp.get("purpose"):
        purpose = corp["purpose"][:200]
        print(f"    Purpose: {purpose}{'...' if len(corp['purpose']) > 200 else ''}")

    # Main Office Address
    street_addr = resp.get("corpStreetAddress")
    if street_addr:
        addr_str = _format_address(street_addr)
        if addr_str:
            print(f"\n    Main Office (Street): {addr_str}")

    mailing_addr = resp.get("mailingAddress")
    if mailing_addr:
        addr_str = _format_address(mailing_addr)
        if addr_str:
            print(f"    Main Office (Mailing): {addr_str}")

    # Resident Agent
    ra = resp.get("residentAgent")
    if ra:
        ra_name = _extract_person_name(ra)
        if ra_name:
            is_entity = not ra.get("isIndividual", True)
            agent_type = " (Entity)" if is_entity else ""
            print(f"\n    Resident Agent: {ra_name}{agent_type}")
            if ra.get("streetAddress"):
                addr_str = _format_address(ra["streetAddress"])
                if addr_str:
                    print(f"      Address: {addr_str}")

    # Officers
    officers = resp.get("officers")
    if officers:
        print(f"\n    Officers ({len(officers)}):")
        for off in officers:
            off_name = _extract_person_name(off)
            title = off.get("titleEn") or off.get("title") or "Officer"
            print(f"      {off_name or '?'} — {title}")
            if off.get("streetAddress"):
                addr_str = _format_address(off["streetAddress"])
                if addr_str:
                    print(f"        Address: {addr_str}")

    # Incorporators / Authorized Persons
    incorporators = resp.get("incorporators", [])
    if incorporators:
        print(f"\n    Authorized Persons / Incorporators ({len(incorporators)}):")
        for inc in incorporators:
            inc_name = _extract_person_name(inc)
            print(f"      {inc_name or '?'}")
            if inc.get("streetAddress"):
                addr_str = _format_address(inc["streetAddress"])
                if addr_str:
                    print(f"        Address: {addr_str}")

    # Public Benefit Executives
    pbe = resp.get("publicBenefitExecutives")
    if pbe:
        print(f"\n    Public Benefit Executives ({len(pbe)}):")
        for p in pbe:
            p_name = _extract_person_name(p)
            print(f"      {p_name or '?'}")

    # Articles (if fetched)
    if "articles" in result:
        for art_type, docs in result["articles"].items():
            if docs:
                print(f"\n    Articles — {art_type} ({len(docs)} documents):")
                for doc in docs[:10]:
                    label = doc.get("documentLabelEn", "?")
                    eff = doc.get("effectiveDate", "")[:10] if doc.get("effectiveDate") else "?"
                    has_link = doc.get("enableLink", False)
                    print(f"      {eff}: {label}" + (" [PDF]" if has_link else ""))

    # Annual Filings (if fetched)
    if "annual_filings" in result:
        filings = result["annual_filings"]
        print(f"\n    Annual Filings ({len(filings)} years):")
        for f in filings:
            year = f.get("filingYear", "?")
            status = f.get("statusEn") or ("MISSING" if f.get("missing") else "Filed")
            gs = "" if not f.get("notGoodStanding") else " [NOT IN GOOD STANDING]"
            print(f"      {year}: {status}{gs}")

    # Related Entities (if fetched)
    if "related_entities" in result and result["related_entities"]:
        related = result["related_entities"]
        print(f"\n    Related Entities ({len(related)}):")
        for rel in related:
            print(f"      {rel.get('corpName', '?')} (Registry: {rel.get('registrationNumber', '?')})")

    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION
# ══════════════════════════════════════════════════════════

# Entity class mapping -> unified schema
CLASS_TYPE_MAP = {
    "Corporation": "corp",
    "L.L.C.": "llc",
    "Limited Liability Company": "llc",
    "Limited Partnership": "lp",
    "L.L.P.": "llp",
    "Limited Liability Partnership": "llp",
    "Professional Corporation": "prof_corp",
    "Non-Profit Corporation": "nonprofit",
    "General Partnership": "gp",
}

STATUS_MAP = {
    "ACTIVE": "active",
    "CANCELLED": "cancelled",
    "REVOKED": "revoked",
    "SUSPENDED": "suspended",
    "MERGED": "merged",
    "CONVERTED": "converted",
    "DISSOLVED": "dissolved",
}


def _ingest_entity_to_registry(db, reg_index):
    """Fetch entity from PR DoS API and ingest into registry.db. Returns entity_id or None."""
    data = _fetch_entity_info(reg_index)
    if not data or not data.get("response"):
        return None

    resp = data["response"]
    corp = resp.get("corporation", {})

    name = corp.get("corpName", "?")
    reg_num = str(corp.get("corpRegisterNumber", reg_index.split("-")[0]))

    # Entity type
    class_en = corp.get("classEn", "")
    etype = CLASS_TYPE_MAP.get(class_en, class_en.lower().replace(" ", "_").replace(".", "") if class_en else None)
    if corp.get("jurisdictionEn") == "Foreign" and etype and not etype.startswith("foreign_"):
        etype = f"foreign_{etype}"

    # Status
    status_en = corp.get("statusEn", "")
    status = STATUS_MAP.get(status_en, status_en.lower() if status_en else None)

    # Dates
    formed = corp.get("dateFormed", "")
    if formed:
        formed = formed[:10]
    terminated = corp.get("terminationDate", "")
    if terminated:
        terminated = terminated[:10]

    # Addresses
    street_addr = resp.get("corpStreetAddress", {})
    mailing_addr = resp.get("mailingAddress", {})
    addr = street_addr or mailing_addr or {}
    princ_addr = None
    if addr:
        a1 = (addr.get("address1") or "").strip()
        a2 = (addr.get("address2") or "").strip()
        princ_addr = f"{a1} {a2}".strip() if a1 else None

    # Jurisdiction / state of formation
    jurisdiction = corp.get("jurisdictionEn", "")
    state_of_formation = "PR" if jurisdiction == "Domestic" else (corp.get("homeState") or "")

    # Purpose
    purpose = corp.get("purpose", "")

    source_url = f"{PORTAL_URL}/entity-information?c={reg_index}"

    # Delete child records before REPLACE to avoid FK constraint violations
    existing = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='pr' AND source_id=?",
        [reg_num]
    ).fetchone()
    if existing:
        old_id = existing[0]
        for child_table in ("registry_officers", "registry_agents", "registry_filings", "registry_name_history"):
            db.execute(f"DELETE FROM {child_table} WHERE entity_id=?", [old_id])

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, dissolution_date, principal_address, principal_city,
            principal_state, principal_zip, principal_country,
            state_of_formation, purpose, source_url, raw_data
        ) VALUES ('pr', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        reg_num, name, etype, status, formed,
        terminated or None,
        princ_addr,
        (addr.get("city") or "").strip() or None,
        (addr.get("stateAbbreviation") or addr.get("state") or "PR").strip() or "PR",
        (addr.get("zip") or "").strip() or None,
        (addr.get("countryAbbreviation") or "USA").strip() or "USA",
        state_of_formation or "PR",
        purpose[:500] if purpose else None,
        source_url,
        json.dumps(data, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='pr' AND source_id=?",
        [reg_num]
    ).fetchone()
    entity_id = row[0]

    # Officers
    for off in (resp.get("officers") or []):
        off_name = _extract_person_name(off)
        if not off_name:
            continue
        title = off.get("titleEn") or off.get("title") or "Officer"
        is_individual = off.get("isIndividual", True)
        off_addr = off.get("streetAddress", {})
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                entity_id, off_name, title,
                "person" if is_individual else "entity",
                _format_address(off_addr) if off_addr else None,
                (off_addr.get("city") or "").strip() or None,
                (off_addr.get("stateAbbreviation") or "").strip() or None,
                (off_addr.get("zip") or "").strip() or None,
            ])
        except sqlite3.IntegrityError:
            pass

    # Incorporators / Authorized Persons -> officers with title "Authorized Person"
    for inc in (resp.get("incorporators") or []):
        inc_name = _extract_person_name(inc)
        if not inc_name:
            continue
        is_individual = inc.get("isIndividual", True)
        inc_addr = inc.get("streetAddress", {})
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                entity_id, inc_name, "Authorized Person",
                "person" if is_individual else "entity",
                _format_address(inc_addr) if inc_addr else None,
                (inc_addr.get("city") or "").strip() or None,
                (inc_addr.get("stateAbbreviation") or "").strip() or None,
                (inc_addr.get("zip") or "").strip() or None,
            ])
        except sqlite3.IntegrityError:
            pass

    # Resident Agent
    ra = resp.get("residentAgent")
    if ra:
        ra_name = _extract_person_name(ra)
        if ra_name:
            ra_addr = ra.get("streetAddress", {})
            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_agents
                    (entity_id, agent_name, address, city, state, zip)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [
                    entity_id, ra_name,
                    _format_address(ra_addr) if ra_addr else None,
                    (ra_addr.get("city") or "").strip() or None,
                    (ra_addr.get("stateAbbreviation") or "").strip() or None,
                    (ra_addr.get("zip") or "").strip() or None,
                ])
            except sqlite3.IntegrityError:
                pass

    # Annual filings summary
    filings_data = _api_get(f"corporation/docs/filings/annualreport/{reg_index}/summary")
    if filings_data and filings_data.get("response"):
        for f in filings_data["response"]:
            year = f.get("filingYear")
            status_en_f = f.get("statusEn") or ("Missing" if f.get("missing") else "Filed")
            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_filings
                    (entity_id, filing_type, filing_date, description, entity_name_at_time, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [
                    entity_id,
                    "Annual Filing",
                    f"{year}-01-01" if year else None,
                    f"Annual Filing {year}: {status_en_f}",
                    name,
                    json.dumps(f, default=str),
                ])
            except sqlite3.IntegrityError:
                pass

    # Name changes from articles
    namechange_data = _api_get(f"corporation/docs/articles/namechange/{reg_index}")
    if namechange_data and namechange_data.get("response"):
        for nc in namechange_data["response"]:
            prev_name = nc.get("documentLabelEn", "")
            eff_date = nc.get("effectiveDate", "")[:10] if nc.get("effectiveDate") else None
            if prev_name:
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO registry_name_history
                        (entity_id, previous_name, change_date)
                        VALUES (?, ?, ?)
                    """, [entity_id, prev_name, eff_date])
                except sqlite3.IntegrityError:
                    pass

    return entity_id


def cmd_ingest(args):
    """Ingest a single entity by registration index into registry.db."""
    db = get_db()
    entity_id = _ingest_entity_to_registry(db, args.reg_index)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        row = db.execute("SELECT entity_name FROM registry_entities WHERE id=?", [entity_id]).fetchone()
        name = row[0] if row else "?"
        print(f"Ingested: {name} (Index: {args.reg_index}, registry ID: {entity_id})")
    else:
        print(f"Failed to ingest entity {args.reg_index}")


def cmd_ingest_search(args):
    """Search for entities and ingest all results into registry.db."""
    payload = {
        "cancellationMode": False,
        "comparisonType": 1,
        "corpName": args.query,
        "isWorkFlowSearch": False,
        "limit": args.limit,
        "matchType": MATCH_TYPES.get(args.match, 4),
        "method": None,
        "onlyActive": args.active,
        "registryNumber": None,
        "advanceSearch": None,
    }

    data = _api_post("corporation/search", payload)
    if not data or not data.get("response"):
        print("Search failed")
        return

    records = data["response"].get("records", [])
    total = data["response"].get("totalRecords", len(records))
    print(f"Found {len(records)} of {total} entities. Ingesting...")

    db = get_db()
    ingested = 0
    for i, r in enumerate(records):
        reg_idx = r.get("registrationIndex", "")
        name = r.get("corpName", "?")
        if not reg_idx:
            continue

        entity_id = _ingest_entity_to_registry(db, reg_idx)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(records)}] {name} (Index: {reg_idx}, reg ID: {entity_id})")
        else:
            print(f"  [{i+1}/{len(records)}] FAILED: {name} (Index: {reg_idx})")

        if ingested % 10 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    try:
        db.execute("""
            INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('pr', 'api', ?, ?)
        """, [ingested, f"PR DoS search: '{args.query}' (active_only={args.active})"])
        db.commit()
    except Exception:
        pass

    log_search(args.query, "pr_dos-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(records)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Puerto Rico Department of State — Registry of Corporations entity lookup"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name or registry number")
    p.add_argument("query", nargs="?", default=None, help="Corporation name to search")
    p.add_argument("--registry-number", type=int, help="Search by registry number instead of name")
    p.add_argument("--match", choices=list(MATCH_TYPES.keys()), default="all-words",
                   help="Name match type (default: all-words)")
    p.add_argument("--active", action="store_true", help="Only show active entities")
    p.add_argument("--limit", type=int, default=250, help="Max results (default/max: 250)")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity detail by registration index")
    p.add_argument("reg_index", help="Registration index (e.g., 420115-1511)")
    p.add_argument("--filings", action="store_true", help="Include annual filing history")
    p.add_argument("--articles", action="store_true", help="Include articles/documents")
    p.add_argument("--related", action="store_true", help="Include related entities")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest a single entity into registry.db")
    p.add_argument("reg_index", help="Registration index to ingest (e.g., 420115-1511)")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Corporation name search query")
    p.add_argument("--match", choices=list(MATCH_TYPES.keys()), default="all-words")
    p.add_argument("--active", action="store_true", help="Only ingest active entities")
    p.add_argument("--limit", type=int, default=50, help="Max entities to ingest (default: 50)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    # Validate search args
    if args.command == "search" and not args.query and not args.registry_number:
        parser.error("Either provide a search query or --registry-number")

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
