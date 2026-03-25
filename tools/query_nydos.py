#!/usr/bin/env python3
"""
New York Department of State (NY DOS) Public Inquiry tool.

Queries the NY DOS Division of Corporations REST API for business entity
information. Covers 4.1M+ NY entities including corporations, LLCs, LPs,
and LLPs — active, inactive, and suspended.

Complements the SODA-based ingest_newyork.py with direct entity lookup,
detailed entity pages, filing history, and name history.

Usage:
    python tools/query_nydos.py search "HOME CARE" --status Active
    python tools/query_nydos.py search "EPSTEIN" --match Contains --types Corporation LimitedLiabilityCompany
    python tools/query_nydos.py entity 873065
    python tools/query_nydos.py entity 873065 --filings --names
    python tools/query_nydos.py filings 873065
    python tools/query_nydos.py names 873065
    python tools/query_nydos.py ingest 873065
    python tools/query_nydos.py ingest-search "HOME CARE" --status Active --limit 50
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
    from query_registry import get_db, _rebuild_fts


# ══════════════════════════════════════════════════════════
# API CONFIGURATION
# ══════════════════════════════════════════════════════════

BASE_URL = "https://apps.dos.ny.gov/PublicInquiryWeb/api/PublicInquiry"
REFERER = "https://apps.dos.ny.gov/publicInquiry/"

ALL_ENTITY_TYPES = [
    "Corporation",
    "LimitedLiabilityCompany",
    "LimitedPartnership",
    "LimitedLiabilityPartnership",
]

# Rate limit: be respectful — 1 request per second
RATE_LIMIT_SECONDS = 1.0
_last_request_time = 0.0


# ══════════════════════════════════════════════════════════
# HTTP HELPERS
# ══════════════════════════════════════════════════════════

def _api_post(endpoint, payload, retries=3):
    """Make a POST request to the NY DOS API with rate limiting and retries."""
    global _last_request_time
    url = f"{BASE_URL}/{endpoint}"

    # Rate limit
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Referer": REFERER,
    }
    req = Request(url, data=body, headers=headers, method="POST")

    for attempt in range(retries):
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            err_body = e.read().decode()[:500]
            if e.code == 429 or e.code >= 500:
                wait = (attempt + 1) * 3
                print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
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
    """Search NY DOS entities by name or DOS ID."""
    search_type = "EntityName"
    if args.by_id:
        search_type = "DosId"

    types = args.types if args.types else ALL_ENTITY_TYPES

    payload = {
        "searchValue": args.query,
        "searchByTypeIndicator": search_type,
        "searchExpressionIndicator": args.match,
        "entityStatusIndicator": args.status,
        "entityTypeIndicator": types,
        "listPaginationInfo": {
            "listStartRecord": 1,
            "listEndRecord": args.limit,
        },
    }

    data = _api_post("GetComplexSearchMatchingEntities", payload)
    if not data:
        print("Search failed")
        return

    if data.get("requestStatus") != "Success":
        print(f"Search returned: {data.get('requestStatus', '?')}")
        return

    results = data.get("entitySearchResultList", [])
    total = data.get("totalMatchingCount", len(results))

    log_search(args.query, "nydos", len(results))

    if write_output(results, args, summary=f"NY DOS search '{args.query}' ({len(results)} of {total})"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {len(results)} of {total} NY entities matching '{args.query}'")
    print()
    for r in results:
        status = r.get("entityStatus", "?")
        etype = r.get("entityType", "?")
        dos_id = r.get("dosID", "?")
        county = r.get("county", "")
        filed = r.get("initialFilingDate", "")[:10] if r.get("initialFilingDate") else ""
        juris = r.get("jurisdiction", "")

        print(f"  {r.get('entityName', '?')} ({etype})")
        print(f"    DOS ID: {dos_id} | Status: {status} | Filed: {filed}")
        if county:
            print(f"    County: {county}")
        if juris and juris != "New York, United States":
            print(f"    Jurisdiction: {juris}")
        if r.get("assumedName"):
            print(f"    Assumed name: {r['assumedName']} (ID: {r.get('assumedNameID', '?')})")
        print()


# ══════════════════════════════════════════════════════════
# ENTITY DETAIL COMMAND
# ══════════════════════════════════════════════════════════

def _fetch_entity_detail(dos_id, entity_name=None):
    """Fetch full entity detail from DOS API."""
    payload = {
        "SearchID": str(dos_id),
        "EntityName": entity_name or "",
        "AssumedNameFlag": "false",
    }
    return _api_post("GetEntityRecordByID", payload)


def _fetch_filing_history(dos_id, entity_name=None, limit=100):
    """Fetch filing history from DOS API."""
    payload = {
        "SearchID": str(dos_id),
        "AssumedNameFlag": "false",
        "EntityName": entity_name or "",
        "ListSortedBy": "ALL",
        "listPaginationInfo": {
            "listStartRecord": 1,
            "listEndRecord": limit,
        },
    }
    return _api_post("GetFilingHistoryByID", payload)


def _fetch_name_history(dos_id, entity_name=None, limit=50):
    """Fetch name history from DOS API."""
    payload = {
        "SearchID": str(dos_id),
        "AssumedNameFlag": "false",
        "EntityName": entity_name or "",
        "ListSortedBy": "ALL",
        "listPaginationInfo": {
            "listStartRecord": 1,
            "listEndRecord": limit,
        },
    }
    return _api_post("GetNameHistoryByID", payload)


def _format_address(addr_obj):
    """Format an address object into a single string."""
    if not addr_obj:
        return None
    addr = addr_obj.get("address", addr_obj)
    parts = []
    street = (addr.get("streetAddress1") or addr.get("streetAddress") or "").strip()
    line2 = (addr.get("addressLine2") or addr.get("streetAddress2") or "").strip()
    if street:
        parts.append(street)
    if line2:
        parts.append(line2)
    city = (addr.get("city") or "").strip()
    state = (addr.get("state") or "").strip()
    zipcode = (addr.get("zipCode") or "").strip()
    country = (addr.get("country") or "").strip()
    if city:
        parts.append(city)
    if state:
        combined = state
        if zipcode:
            combined += f" {zipcode}"
        parts.append(combined)
    if country and country not in ("United States", ""):
        parts.append(country)
    return ", ".join(parts) if parts else None


def cmd_entity(args):
    """Get full entity details by DOS ID."""
    # First look up entity name if not known
    entity_name = ""

    # Try search by DOS ID to get entity name
    search_data = _api_post("GetComplexSearchMatchingEntities", {
        "searchValue": str(args.dos_id),
        "searchByTypeIndicator": "DosId",
        "searchExpressionIndicator": "BeginsWith",
        "entityStatusIndicator": "AllStatuses",
        "entityTypeIndicator": ALL_ENTITY_TYPES,
        "listPaginationInfo": {"listStartRecord": 1, "listEndRecord": 5},
    })
    if search_data and search_data.get("entitySearchResultList"):
        for r in search_data["entitySearchResultList"]:
            if r.get("dosID") == str(args.dos_id):
                entity_name = r.get("entityName", "")
                break

    data = _fetch_entity_detail(args.dos_id, entity_name)
    if not data or data.get("requestStatus") != "Success":
        print(f"Entity {args.dos_id} not found or API error")
        return

    result = {"entity": data}

    # Optionally fetch filing history
    if args.filings:
        fh = _fetch_filing_history(args.dos_id, entity_name)
        if fh and fh.get("requestStatus") == "Success":
            result["filings"] = fh.get("filingHistoryResultList", [])

    # Optionally fetch name history
    if args.names:
        nh = _fetch_name_history(args.dos_id, entity_name)
        if nh and nh.get("requestStatus") == "Success":
            result["name_history"] = nh.get("nameHistoryResultList", [])

    if write_output(result, args, summary=f"NY DOS entity {args.dos_id}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    # Pretty print
    gi = data.get("entityGeneralInfo", {})
    print(f"\n  [NY DOS] {gi.get('entityName', '?')}")
    print(f"    DOS ID: {gi.get('dosID', '?')}")
    print(f"    Type: {gi.get('entityType', '?')}")
    print(f"    Status: {gi.get('entityStatus', '?')}")
    if gi.get("reasonForStatus"):
        print(f"    Reason: {gi['reasonForStatus']}")
    if gi.get("dateOfInitialDosFiling"):
        print(f"    Filed: {gi['dateOfInitialDosFiling'][:10]}")
    if gi.get("effectiveDateInitialFiling") and gi["effectiveDateInitialFiling"] != gi.get("dateOfInitialDosFiling"):
        print(f"    Effective: {gi['effectiveDateInitialFiling'][:10]}")
    if gi.get("inactiveDate"):
        print(f"    Inactive date: {gi['inactiveDate'][:10]}")
    if gi.get("county"):
        print(f"    County: {gi['county']}")
    if gi.get("jurisdiction"):
        print(f"    Jurisdiction: {gi['jurisdiction']}")
    if gi.get("foreignLegalName"):
        print(f"    Foreign legal name: {gi['foreignLegalName']}")
    if gi.get("fictitiousName"):
        print(f"    Fictitious name: {gi['fictitiousName']}")
    if gi.get("nfpCategory"):
        print(f"    NFP Category: {gi['nfpCategory']}")

    # Service of Process
    sop = data.get("sopAddress")
    if sop:
        sop_name = sop.get("name") or ""
        sop_addr = _format_address(sop)
        if sop_name or sop_addr:
            print(f"\n    Service of Process:")
            if sop_name:
                print(f"      Name: {sop_name}")
            if sop_addr:
                print(f"      Address: {sop_addr}")

    # CEO
    ceo = data.get("ceo")
    if ceo and ceo.get("name"):
        ceo_addr = _format_address(ceo)
        print(f"\n    CEO: {ceo['name']}")
        if ceo_addr:
            print(f"      Address: {ceo_addr}")

    # Registered Agent
    ra = data.get("registeredAgent")
    if ra and ra.get("name"):
        ra_addr = _format_address(ra)
        print(f"\n    Registered Agent: {ra['name']}")
        if ra_addr:
            print(f"      Address: {ra_addr}")

    # Principal Office
    po = data.get("poExecAddress")
    if po:
        po_addr = _format_address(po)
        if po_addr:
            print(f"\n    Principal Office: {po_addr}")

    # Location
    loc = data.get("locationAddress")
    if loc and loc.get("name"):
        loc_addr = _format_address(loc)
        print(f"\n    Location: {loc.get('name', '')}")
        if loc_addr:
            print(f"      Address: {loc_addr}")

    # Stock Info
    stocks = data.get("stockShareInfoList", [])
    if stocks:
        print(f"\n    Stock Information:")
        for s in stocks:
            print(f"      {s.get('stockTypeDescriptor', '?')}: {s.get('quantity', '?')} shares @ ${s.get('stockValue', '?')}")

    # Filing History
    if "filings" in result:
        filings = result["filings"]
        print(f"\n    Filing History ({len(filings)} records):")
        for f in filings:
            date = f.get("fileDate", "")[:10] if f.get("fileDate") else "?"
            doc_type = f.get("documentType", "?")
            file_num = f.get("fileNumber", "")
            print(f"      {date}: {doc_type}" + (f" [{file_num}]" if file_num else ""))
            if f.get("amendmentDescription"):
                print(f"        Amendment: {f['amendmentDescription']}")

    # Name History
    if "name_history" in result:
        names = result["name_history"]
        print(f"\n    Name History ({len(names)} records):")
        for n in names:
            date = n.get("fileDate", "")[:10] if n.get("fileDate") else "?"
            doc_type = n.get("documentType", "?")
            ename = n.get("entityName", "?")
            print(f"      {date}: {ename} ({doc_type})")

    print()


# ══════════════════════════════════════════════════════════
# FILING HISTORY COMMAND
# ══════════════════════════════════════════════════════════

def cmd_filings(args):
    """Get filing history for a DOS ID."""
    data = _fetch_filing_history(args.dos_id, limit=args.limit)
    if not data or data.get("requestStatus") != "Success":
        print(f"No filing history for DOS ID {args.dos_id}")
        return

    filings = data.get("filingHistoryResultList", [])
    total = data.get("totalMatchingCount", len(filings))

    if write_output(filings, args, summary=f"NY DOS filings for {args.dos_id} ({len(filings)} of {total})"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(filings, indent=2, default=str))
        return

    entity_name = data.get("entityName", "?")
    print(f"Filing history for {entity_name} (DOS ID: {args.dos_id})")
    print(f"  {len(filings)} of {total} filings")
    print()
    for f in filings:
        date = f.get("fileDate", "")[:10] if f.get("fileDate") else "?"
        doc_type = f.get("documentType", "?")
        file_num = f.get("fileNumber", "")
        pages = f.get("pageCount", "")
        print(f"  {date}: {doc_type}" + (f" [{file_num}]" if file_num else ""))
        if f.get("amendmentDescription"):
            print(f"    Amendment: {f['amendmentDescription']}")
        if f.get("assumedName"):
            print(f"    Assumed name: {f['assumedName']} (Status: {f.get('assumedNameStatus', '?')})")
    print()


# ══════════════════════════════════════════════════════════
# NAME HISTORY COMMAND
# ══════════════════════════════════════════════════════════

def cmd_names(args):
    """Get name history for a DOS ID."""
    data = _fetch_name_history(args.dos_id, limit=args.limit)
    if not data or data.get("requestStatus") != "Success":
        print(f"No name history for DOS ID {args.dos_id}")
        return

    names = data.get("nameHistoryResultList", [])
    total = data.get("totalMatchingCount", len(names))

    if write_output(names, args, summary=f"NY DOS name history for {args.dos_id} ({len(names)} of {total})"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(names, indent=2, default=str))
        return

    print(f"Name history for DOS ID {args.dos_id}")
    print(f"  {len(names)} of {total} records")
    print()
    for n in names:
        date = n.get("fileDate", "")[:10] if n.get("fileDate") else "?"
        doc_type = n.get("documentType", "?")
        ename = n.get("entityName", "?")
        file_num = n.get("fileNumber", "")
        print(f"  {date}: {ename}")
        print(f"    Document: {doc_type}" + (f" [{file_num}]" if file_num else ""))
    print()


# ══════════════════════════════════════════════════════════
# REGISTRY INGESTION COMMANDS
# ══════════════════════════════════════════════════════════

# Entity type mapping
ENTITY_TYPE_MAP = {
    "DOMESTIC BUSINESS CORPORATION": "corp",
    "FOREIGN BUSINESS CORPORATION": "foreign_corp",
    "DOMESTIC LIMITED LIABILITY COMPANY": "llc",
    "FOREIGN LIMITED LIABILITY COMPANY": "foreign_llc",
    "DOMESTIC NOT-FOR-PROFIT CORPORATION": "nonprofit",
    "FOREIGN NOT-FOR-PROFIT CORPORATION": "foreign_nonprofit",
    "DOMESTIC LIMITED PARTNERSHIP": "lp",
    "FOREIGN LIMITED PARTNERSHIP": "foreign_lp",
    "DOMESTIC LIMITED LIABILITY PARTNERSHIP": "llp",
    "FOREIGN LIMITED LIABILITY PARTNERSHIP": "foreign_llp",
    "DOMESTIC PROFESSIONAL SERVICE CORPORATION": "prof_corp",
    "FOREIGN PROFESSIONAL SERVICE CORPORATION": "foreign_prof_corp",
    "FOREIGN PROFESSIONAL LIMITED LIABILITY COMPANY": "foreign_prof_llc",
    "DOMESTIC PROFESSIONAL LIMITED LIABILITY COMPANY": "prof_llc",
}

STATUS_MAP = {
    "Active": "active",
    "Inactive": "inactive",
    "Suspended": "suspended",
}


def _ingest_entity_to_registry(db, dos_id, entity_name=None):
    """Fetch entity from DOS API and ingest into registry.db. Returns entity_id or None."""
    # Fetch detail
    detail = _fetch_entity_detail(dos_id, entity_name)
    if not detail or detail.get("requestStatus") != "Success":
        return None

    gi = detail.get("entityGeneralInfo", {})
    name = gi.get("entityName", entity_name or "?")
    etype_raw = gi.get("entityType", "")
    etype = ENTITY_TYPE_MAP.get(etype_raw, etype_raw.lower().replace(" ", "_") if etype_raw else None)
    status_raw = gi.get("entityStatus", "")
    status = STATUS_MAP.get(status_raw, status_raw.lower() if status_raw else None)

    filed_date = gi.get("dateOfInitialDosFiling", "")
    if filed_date:
        filed_date = filed_date[:10]

    inactive_date = gi.get("inactiveDate", "")
    if inactive_date:
        inactive_date = inactive_date[:10]

    # Service of Process address as principal address
    sop = detail.get("sopAddress", {})
    sop_addr = sop.get("address", {}) if sop else {}
    street = (sop_addr.get("streetAddress1") or sop_addr.get("streetAddress") or "").strip()
    line2 = (sop_addr.get("addressLine2") or sop_addr.get("streetAddress2") or "").strip()
    princ_addr = f"{street} {line2}".strip() if street else None

    # Principal Executive Office as alternate
    po = detail.get("poExecAddress", {})
    po_addr = po.get("address", {}) if po else {}
    po_street = (po_addr.get("streetAddress1") or po_addr.get("streetAddress") or "").strip()
    if po_street and not princ_addr:
        po_line2 = (po_addr.get("addressLine2") or po_addr.get("streetAddress2") or "").strip()
        princ_addr = f"{po_street} {po_line2}".strip()
        sop_addr = po_addr  # use PO address fields

    # State of formation from jurisdiction
    state_of_formation = gi.get("jurisdiction", "")

    source_url = f"https://apps.dos.ny.gov/publicInquiry/EntityDisplay?dosID={dos_id}"

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, dissolution_date, principal_address, principal_city,
            principal_state, principal_zip, principal_country,
            state_of_formation, purpose, source_url, raw_data
        ) VALUES ('ny', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        str(dos_id), name, etype, status, filed_date,
        inactive_date or None,
        princ_addr,
        (sop_addr.get("city") or "").strip() or None,
        (sop_addr.get("state") or "").strip() or None,
        (sop_addr.get("zipCode") or "").strip() or None,
        (sop_addr.get("country") or "US").strip() or "US",
        state_of_formation or None,
        gi.get("nfpCategory") or None,
        source_url,
        json.dumps(detail, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='ny' AND source_id=?",
        [str(dos_id)]
    ).fetchone()
    entity_id = row[0]

    # CEO as officer
    ceo = detail.get("ceo", {})
    if ceo and ceo.get("name"):
        ceo_addr = ceo.get("address", {})
        ceo_street = (ceo_addr.get("streetAddress1") or ceo_addr.get("streetAddress") or "").strip()
        ceo_line2 = (ceo_addr.get("addressLine2") or "").strip()
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip)
                VALUES (?, ?, 'CEO', 'person', ?, ?, ?, ?)
            """, [
                entity_id, ceo["name"],
                f"{ceo_street} {ceo_line2}".strip() or None,
                (ceo_addr.get("city") or "").strip() or None,
                (ceo_addr.get("state") or "").strip() or None,
                (ceo_addr.get("zipCode") or "").strip() or None,
            ])
        except sqlite3.IntegrityError:
            pass

    # Registered Agent
    ra = detail.get("registeredAgent", {})
    if ra and ra.get("name"):
        ra_addr = ra.get("address", {})
        ra_street = (ra_addr.get("streetAddress1") or ra_addr.get("streetAddress") or "").strip()
        ra_line2 = (ra_addr.get("addressLine2") or "").strip()
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                entity_id, ra["name"],
                f"{ra_street} {ra_line2}".strip() or None,
                (ra_addr.get("city") or "").strip() or None,
                (ra_addr.get("state") or "").strip() or None,
                (ra_addr.get("zipCode") or "").strip() or None,
            ])
        except sqlite3.IntegrityError:
            pass

    # Fetch and ingest filing history
    fh = _fetch_filing_history(dos_id, name)
    if fh and fh.get("requestStatus") == "Success":
        for f in fh.get("filingHistoryResultList", []):
            file_date = f.get("fileDate", "")[:10] if f.get("fileDate") else None
            doc_type = f.get("documentType", "")
            try:
                db.execute("""
                    INSERT OR IGNORE INTO registry_filings
                    (entity_id, filing_type, filing_date, description, entity_name_at_time, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [
                    entity_id, doc_type, file_date, doc_type,
                    name, json.dumps(f, default=str),
                ])
            except sqlite3.IntegrityError:
                pass

    # Fetch and ingest name history
    nh = _fetch_name_history(dos_id, name)
    if nh and nh.get("requestStatus") == "Success":
        for n in nh.get("nameHistoryResultList", []):
            hist_name = n.get("entityName", "")
            change_date = n.get("fileDate", "")[:10] if n.get("fileDate") else None
            if hist_name and hist_name != name:
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO registry_name_history
                        (entity_id, previous_name, change_date)
                        VALUES (?, ?, ?)
                    """, [entity_id, hist_name, change_date])
                except sqlite3.IntegrityError:
                    pass

    return entity_id


def cmd_ingest(args):
    """Ingest a single entity by DOS ID into registry.db."""
    db = get_db()
    entity_id = _ingest_entity_to_registry(db, args.dos_id)
    if entity_id:
        db.commit()
        try:
            _rebuild_fts(db)
        except Exception:
            pass
        row = db.execute("SELECT entity_name FROM registry_entities WHERE id=?", [entity_id]).fetchone()
        name = row[0] if row else "?"
        print(f"Ingested: {name} (DOS ID: {args.dos_id}, registry ID: {entity_id})")
    else:
        print(f"Failed to ingest DOS ID {args.dos_id}")


def cmd_ingest_search(args):
    """Search for entities and ingest all results into registry.db."""
    types = args.types if args.types else ALL_ENTITY_TYPES

    payload = {
        "searchValue": args.query,
        "searchByTypeIndicator": "EntityName",
        "searchExpressionIndicator": args.match,
        "entityStatusIndicator": args.status,
        "entityTypeIndicator": types,
        "listPaginationInfo": {
            "listStartRecord": 1,
            "listEndRecord": args.limit,
        },
    }

    data = _api_post("GetComplexSearchMatchingEntities", payload)
    if not data or data.get("requestStatus") != "Success":
        print("Search failed")
        return

    results = data.get("entitySearchResultList", [])
    total = data.get("totalMatchingCount", len(results))
    print(f"Found {len(results)} of {total} entities. Ingesting...")

    db = get_db()
    ingested = 0
    for i, r in enumerate(results):
        dos_id = r.get("dosID", "")
        name = r.get("entityName", "?")
        if not dos_id:
            continue

        entity_id = _ingest_entity_to_registry(db, dos_id, name)
        if entity_id:
            ingested += 1
            print(f"  [{i+1}/{len(results)}] {name} (DOS: {dos_id}, reg ID: {entity_id})")
        else:
            print(f"  [{i+1}/{len(results)}] FAILED: {name} (DOS: {dos_id})")

        # Commit every 10 entities
        if ingested % 10 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    # Log ingest
    try:
        db.execute("""
            INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
            VALUES ('ny', 'api', ?, ?)
        """, [ingested, f"NY DOS Public Inquiry search: '{args.query}' ({args.status})"])
        db.commit()
    except Exception:
        pass

    log_search(args.query, "nydos-ingest", ingested)
    print(f"\nIngest complete: {ingested} of {len(results)} entities ingested")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="NY DOS Public Inquiry — Division of Corporations entity lookup"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name or DOS ID")
    p.add_argument("query", help="Entity name or DOS ID")
    p.add_argument("--by-id", action="store_true", help="Search by DOS ID instead of name")
    p.add_argument("--match", choices=["BeginsWith", "Contains", "BaseWord"], default="Contains",
                   help="Match type (default: Contains)")
    p.add_argument("--status", choices=["AllStatuses", "Active", "Inactive", "Suspended"],
                   default="AllStatuses", help="Filter by status")
    p.add_argument("--types", nargs="+",
                   choices=["Corporation", "LimitedLiabilityCompany", "LimitedPartnership", "LimitedLiabilityPartnership"],
                   help="Entity types to include (default: all)")
    p.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity detail by DOS ID")
    p.add_argument("dos_id", help="DOS ID number")
    p.add_argument("--filings", action="store_true", help="Include filing history")
    p.add_argument("--names", action="store_true", help="Include name history")
    add_output_args(p)

    # filings
    p = sub.add_parser("filings", help="Get filing history by DOS ID")
    p.add_argument("dos_id", help="DOS ID number")
    p.add_argument("--limit", type=int, default=100, help="Max filings to return")
    add_output_args(p)

    # names
    p = sub.add_parser("names", help="Get name history by DOS ID")
    p.add_argument("dos_id", help="DOS ID number")
    p.add_argument("--limit", type=int, default=50, help="Max name records")
    add_output_args(p)

    # ingest
    p = sub.add_parser("ingest", help="Ingest a single entity into registry.db")
    p.add_argument("dos_id", help="DOS ID to ingest")

    # ingest-search
    p = sub.add_parser("ingest-search", help="Search and ingest all matching entities")
    p.add_argument("query", help="Entity name search query")
    p.add_argument("--match", choices=["BeginsWith", "Contains", "BaseWord"], default="Contains")
    p.add_argument("--status", choices=["AllStatuses", "Active", "Inactive", "Suspended"],
                   default="AllStatuses")
    p.add_argument("--types", nargs="+",
                   choices=["Corporation", "LimitedLiabilityCompany", "LimitedPartnership", "LimitedLiabilityPartnership"])
    p.add_argument("--limit", type=int, default=50, help="Max entities to ingest")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "filings": cmd_filings,
        "names": cmd_names,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
