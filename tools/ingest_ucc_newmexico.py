#!/usr/bin/env python3
"""
New Mexico UCC/Lien filing ingester.

Uses the REST API behind enterprise.sos.nm.gov (same platform as corporate registry).
Rate limited by Azure WAF — needs 4s delays between requests.

API Endpoints (discovered via Playwright SPA inspection, Feb 2026):
  Search:  GET /api/uccEntitySearch/webSearch?SearchType=Debtor&NameTypeId=2&OrganizationName=...
  Detail:  GET /api/FilingDetail/ucc/<internal_id>/false
  History: GET /api/History/ucc/<record_num>
  Images:  GET /api/report/GetImageByNum/<hash>

Search types: UCCNum (lien number), Debtor (debtor name), SecuredParty (secured party name)
Name types:   1=Individual (firstName/lastName), 2=Organization (OrganizationName)

Note: The portal labels this "Lien Search" but the API uses "ucc" in all paths.
      Case matters: 'ucc' and 'UCC' return JSON; 'lien' returns HTML fallback.

Usage:
    python tools/ingest_ucc_newmexico.py search "Zorro Ranch"
    python tools/ingest_ucc_newmexico.py search "Epstein" --individual --first-name Jeffrey --last-name Epstein
    python tools/ingest_ucc_newmexico.py search "Wells Fargo" --type SecuredParty
    python tools/ingest_ucc_newmexico.py search "010417005" --type UCCNum
    python tools/ingest_ucc_newmexico.py detail <internal_id>
    python tools/ingest_ucc_newmexico.py history <record_num>
    python tools/ingest_ucc_newmexico.py ingest-batch "Epstein"
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

BASE_URL = "https://enterprise.sos.nm.gov/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://enterprise.sos.nm.gov/search/ucc",
    "Origin": "https://enterprise.sos.nm.gov",
}

MAX_RETRIES = 3
BASE_DELAY = 4  # seconds between requests


def _request(url, retry=0):
    """Make API request with WAF-aware rate limiting."""
    req = Request(url, headers=HEADERS)
    try:
        time.sleep(BASE_DELAY)
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        if e.code == 403 and retry < MAX_RETRIES:
            wait = (2 ** retry) * 10
            print(f"  WAF rate limit hit (403). Waiting {wait}s before retry {retry+1}...", file=sys.stderr)
            time.sleep(wait)
            return _request(url, retry + 1)
        body = e.read().decode()[:300]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach NM SoS: {e.reason}", file=sys.stderr)
        return None


def _search_ucc(query, search_type="Debtor", name_type=2, include_lapsed=True,
                first_name="", last_name="", city="", state=""):
    """Search NM UCC/Lien filings.

    The NM SoS UCC search endpoint is /api/uccEntitySearch/webSearch.
    This is a separate endpoint from the business entity search.

    search_type: UCCNum (lien number), Debtor (debtor name), SecuredParty (secured party name)
    name_type: 1=Individual Name, 2=Organization Name
    include_lapsed: Include lapsed records

    For UCCNum search: pass query as SEARCH_VALUE (the lien/filing number).
    For Debtor/SecuredParty search:
      - name_type=1 (Individual): use first_name + last_name
      - name_type=2 (Organization): use query as OrganizationName

    Response format:
      {"template": [...], "rows": {"<id>": {"TITLE": ["<filenum>", ""], "ID": <int>,
       "DEBTOR": "NAME - CITY, ST", "RECORD_NUM": "...", "RECORD_TYPE": "Lien ...",
       "SEC_PARTY": "NAME - CITY, ST", "STATUS": "Active|Terminated|Lapsed",
       "FILING_DATE": "M/D/YYYY H:MM AM", "LAPSE_DATE": "...", "PAGE_COUNT": <int>}, ...}}
    """
    params = {"SEARCH_VALUE": "", "SearchType": search_type, "SearchLapsed": str(include_lapsed).lower()}

    if search_type == "UCCNum":
        params["SEARCH_VALUE"] = query
    elif name_type == 1:
        # Individual name search
        params["NameTypeId"] = "1"
        params["firstName"] = first_name or (query.split()[0] if " " in query else "")
        params["middleName"] = ""
        params["lastName"] = last_name or (query.split()[-1] if " " in query else query)
        params["suffix"] = ""
        params["OrganizationName"] = ""
        params["SearchCity"] = city
        params["SearchState"] = state
    else:
        # Organization name search (default)
        params["NameTypeId"] = "2"
        params["firstName"] = ""
        params["middleName"] = ""
        params["lastName"] = ""
        params["suffix"] = ""
        params["OrganizationName"] = query
        params["SearchCity"] = city
        params["SearchState"] = state

    url = f"{BASE_URL}/uccEntitySearch/webSearch?{urlencode(params)}"
    data = _request(url)
    if data is not None:
        return data, url
    return None, None


def _get_ucc_detail(filing_id):
    """Get UCC filing detail by internal ID (the 'ID' field from search results).

    Endpoint: /api/FilingDetail/ucc/<internal_id>/false
    Note: case-sensitive — 'ucc' works, 'lien' returns HTML, 'UCC' also works.

    Response format:
      {"DRAWER_DETAIL_LIST": [
        {"LABEL": "Document Type", "VALUE": "Record Information", ...},
        {"LABEL": "Record Number", "VALUE": "010417005", ...},
        {"LABEL": "Debtor Name", "VALUE": "...", ...},
        {"LABEL": "Debtor Address", "VALUE": "...", ...},
        {"LABEL": "Secured Party Name", "VALUE": "...", ...},
        {"LABEL": "Secured Party Address", "VALUE": "...", ...},
      ], ...}
    """
    url = f"{BASE_URL}/FilingDetail/ucc/{filing_id}/false"
    return _request(url)


def _get_ucc_history(record_num):
    """Get UCC filing history by record number (RECORD_NUM from search results).

    Endpoint: /api/History/ucc/<record_num>
    Note: case-sensitive — 'ucc' works, 'lien' returns HTML, 'UCC' also works.

    Response format:
      {"AMENDMENT_LIST": [
        {"AMENDMENT_TYPE": "Lien Amendment - Termination", "AMENDMENT_NUM": "20239782727B",
         "AMENDMENT_DATE": "01/09/2023", "AMENDMENT_ID": 122,
         "DOWNLOAD_LINK": "/api/report/GetImageByNum/..."},
        ...
      ],
      "HISTORY_LIST": [],
      "TEMPLATE": [{"label": "UCC Type", "id": "AMENDMENT_TYPE"}, ...]}
    """
    url = f"{BASE_URL}/History/ucc/{record_num}"
    return _request(url)


def _parse_date(s):
    """Parse date string to ISO format."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None

    # MM/DD/YYYY
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"

    # YYYY-MM-DDT... (ISO with time)
    if "T" in s:
        return s[:10]

    # YYYY-MM-DD
    if len(s) == 10 and s[4] == "-":
        return s

    return None


def cmd_search(args):
    """Search NM UCC filings."""
    search_type = getattr(args, "search_type", "Debtor")
    name_type = 1 if getattr(args, "individual", False) else 2
    first_name = getattr(args, "first_name", "")
    last_name = getattr(args, "last_name", "")
    city = getattr(args, "city", "") or ""
    state = getattr(args, "state", "") or ""
    include_lapsed = not getattr(args, "active_only", False)

    data, endpoint = _search_ucc(
        args.query, search_type=search_type, name_type=name_type,
        include_lapsed=include_lapsed, first_name=first_name,
        last_name=last_name, city=city, state=state,
    )
    if not data:
        print("No results or error.")
        return

    print(f"Endpoint: {endpoint}")
    print()

    rows = data.get("rows", {})
    print(f"Found {len(rows)} NM UCC results for '{args.query}'")
    print()

    for key, r in rows.items():
        if not isinstance(r, dict):
            continue
        # TITLE is [filing_number, ""]
        title = r.get("TITLE", ["?"])
        if isinstance(title, list):
            title = title[0]
        record_num = r.get("RECORD_NUM", "?")
        internal_id = r.get("ID", key)
        debtor = r.get("DEBTOR", "")
        secured_party = r.get("SEC_PARTY", "")
        record_type = r.get("RECORD_TYPE", "?")
        status = r.get("STATUS", "?")
        filing_date = r.get("FILING_DATE", "")
        lapse_date = r.get("LAPSE_DATE", "")
        page_count = r.get("PAGE_COUNT", "")

        print(f"  [NM UCC] {title}")
        print(f"    Record #: {record_num} | Internal ID: {internal_id}")
        print(f"    Type: {record_type} | Status: {status}")
        if debtor:
            print(f"    Debtor: {debtor}")
        if secured_party:
            print(f"    Secured Party: {secured_party}")
        if filing_date:
            print(f"    Filed: {filing_date}")
        if lapse_date:
            print(f"    Lapse: {lapse_date}")
        if page_count:
            print(f"    Pages: {page_count}")
        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_detail(args):
    """Get full UCC filing details.

    Accepts internal ID (from search results) for detail,
    or record number for history.
    """
    internal_id = args.filing_id

    # Get detail
    data = _get_ucc_detail(internal_id)
    if not data:
        print(f"No detail found for internal ID {internal_id}")
        return

    detail_list = data.get("DRAWER_DETAIL_LIST", [])
    print(f"=== UCC Filing Detail (ID: {internal_id}) ===")
    record_num = None
    for item in detail_list:
        label = item.get("LABEL", "")
        value = item.get("VALUE", "")
        clean = value.replace("\r\n", " | ").replace("\n", " | ").strip()
        if clean:
            print(f"  {label}: {clean}")
        if label == "Record Number":
            record_num = value

    # Also get history if we found the record number
    if record_num:
        print()
        history = _get_ucc_history(record_num)
        if history:
            amendments = history.get("AMENDMENT_LIST", [])
            print(f"  Filing History ({len(amendments)} entries):")
            for a in amendments:
                atype = a.get("AMENDMENT_TYPE", "?")
                anum = a.get("AMENDMENT_NUM", "")
                adate = a.get("AMENDMENT_DATE", "?")
                dl = a.get("DOWNLOAD_LINK", "")
                print(f"    {adate}: {atype} (#{anum})")
                if dl:
                    print(f"      Image: https://enterprise.sos.nm.gov{dl}")

    if args.json_out:
        print()
        print(json.dumps(data, indent=2, default=str))
        if record_num and history:
            print(json.dumps(history, indent=2, default=str))


def cmd_history(args):
    """Get UCC filing history by record number."""
    data = _get_ucc_history(args.record_num)
    if not data:
        print(f"No history found for record {args.record_num}")
        return

    amendments = data.get("AMENDMENT_LIST", [])
    print(f"=== UCC Filing History (Record #: {args.record_num}) ===")
    print(f"  {len(amendments)} filings:")
    for a in amendments:
        atype = a.get("AMENDMENT_TYPE", "?")
        anum = a.get("AMENDMENT_NUM", "")
        adate = a.get("AMENDMENT_DATE", "?")
        aid = a.get("AMENDMENT_ID", "")
        dl = a.get("DOWNLOAD_LINK", "")
        print(f"    {adate}: {atype}")
        print(f"      Control ID: {anum} | Internal ID: {aid}")
        if dl:
            print(f"      Image: https://enterprise.sos.nm.gov{dl}")

    if args.json_out:
        print()
        print(json.dumps(data, indent=2, default=str))


def _ingest_filing(db, search_row, detail_data=None, history_data=None):
    """Ingest a single UCC filing into registry.db.

    search_row: row from /api/uccEntitySearch/webSearch response
    detail_data: optional response from /api/FilingDetail/ucc/<id>/false
    history_data: optional response from /api/History/ucc/<record_num>
    """
    # Extract from search row
    title = search_row.get("TITLE", ["?"])
    if isinstance(title, list):
        title = title[0]
    record_num = search_row.get("RECORD_NUM", title)
    internal_id = search_row.get("ID", "?")
    record_type = search_row.get("RECORD_TYPE", "initial").lower()
    status = search_row.get("STATUS", "active").lower()
    filing_date_raw = search_row.get("FILING_DATE", "")
    lapse_date_raw = search_row.get("LAPSE_DATE", "")
    debtor_str = search_row.get("DEBTOR", "")
    sec_party_str = search_row.get("SEC_PARTY", "")

    # Parse dates (format: "M/D/YYYY H:MM AM" or "M/D/YYYY")
    f_date = _parse_date(filing_date_raw.split(" ")[0] if filing_date_raw else "")
    lapse = _parse_date(lapse_date_raw.split(" ")[0] if lapse_date_raw else "")

    # Insert filing
    try:
        db.execute("""
            INSERT OR IGNORE INTO ucc_filings
            (source_jurisdiction, filing_number, filing_type, filing_date, lapse_date, status, raw_data)
            VALUES ('nm', ?, ?, ?, ?, ?, ?)
        """, [record_num, record_type, f_date, lapse, status,
              json.dumps({"search": search_row, "detail": detail_data, "history": history_data}, default=str)])
    except Exception as e:
        print(f"    Error inserting filing: {e}", file=sys.stderr)
        return

    row = db.execute(
        "SELECT id FROM ucc_filings WHERE source_jurisdiction='nm' AND filing_number=?",
        [record_num]
    ).fetchone()
    if not row:
        return
    filing_id = row[0]

    # Extract debtors - from detail (preferred) or search row
    if detail_data:
        detail_fields = {item.get("LABEL", ""): item.get("VALUE", "")
                        for item in detail_data.get("DRAWER_DETAIL_LIST", [])}
        debtor_name = detail_fields.get("Debtor Name", "")
        debtor_addr = detail_fields.get("Debtor Address", "")
        party_name = detail_fields.get("Secured Party Name", "")
        party_addr = detail_fields.get("Secured Party Address", "")
    else:
        # Parse from search row format: "NAME - CITY, ST"
        debtor_name = debtor_str.split(" - ")[0].strip() if debtor_str else ""
        debtor_addr = debtor_str.split(" - ")[1].strip() if " - " in debtor_str else ""
        party_name = sec_party_str.split(" - ")[0].strip() if sec_party_str else ""
        party_addr = sec_party_str.split(" - ")[1].strip() if " - " in sec_party_str else ""

    if debtor_name:
        try:
            db.execute("""
                INSERT OR IGNORE INTO ucc_debtors (filing_id, debtor_name, debtor_type, address)
                VALUES (?, ?, 'organization', ?)
            """, [filing_id, debtor_name, debtor_addr or None])
        except Exception:
            pass

    if party_name:
        try:
            db.execute("""
                INSERT OR IGNORE INTO ucc_secured_parties (filing_id, party_name, party_type, address)
                VALUES (?, ?, 'organization', ?)
            """, [filing_id, party_name, party_addr or None])
        except Exception:
            pass

    # Ingest history/amendments
    if history_data:
        for a in history_data.get("AMENDMENT_LIST", []):
            atype = a.get("AMENDMENT_TYPE", "")
            anum = a.get("AMENDMENT_NUM", "")
            adate = _parse_date(a.get("AMENDMENT_DATE", ""))
            dl = a.get("DOWNLOAD_LINK", "")
            try:
                db.execute("""
                    INSERT OR IGNORE INTO ucc_filing_history
                    (filing_id, action_type, action_filing_number, action_date, description)
                    VALUES (?, ?, ?, ?, ?)
                """, [filing_id, atype, anum, adate,
                      f"Image: https://enterprise.sos.nm.gov{dl}" if dl else None])
            except Exception:
                pass

    print(f"    Ingested: {record_num} ({record_type}, {status})")


def cmd_ingest_batch(args):
    """Search and ingest all matching UCC filings."""
    search_type = getattr(args, "search_type", "Debtor")
    name_type = 1 if getattr(args, "individual", False) else 2

    data, endpoint = _search_ucc(args.query, search_type=search_type, name_type=name_type)
    if not data:
        print("No results or error.")
        return

    rows = data.get("rows", {})
    print(f"Ingesting {len(rows)} UCC filings for '{args.query}'")

    db = get_db()

    for i, (key, r) in enumerate(rows.items()):
        if not isinstance(r, dict):
            continue

        title = r.get("TITLE", ["?"])
        if isinstance(title, list):
            title = title[0]
        internal_id = r.get("ID", key)
        record_num = r.get("RECORD_NUM", title)
        print(f"\n  [{i+1}/{len(rows)}] {title} (record #{record_num}, ID {internal_id})")

        # Get detailed data
        detail = _get_ucc_detail(internal_id)

        # Get history
        history = _get_ucc_history(record_num)

        _ingest_filing(db, r, detail_data=detail, history_data=history)

        if i < len(rows) - 1:
            time.sleep(2)

    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
        VALUES ('nm', 'api_ucc', ?, ?)
    """, [len(rows), f"UCC batch search: {args.query}"])
    db.commit()

    try:
        _rebuild_fts(db)
    except Exception:
        pass

    print(f"\nBatch ingest complete: {len(rows)} UCC filings")


def main():
    parser = argparse.ArgumentParser(description="New Mexico UCC/Lien filing ingester")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="Search UCC/Lien filings")
    p.add_argument("query", help="Search term (org name, person name, or lien number)")
    p.add_argument("--type", dest="search_type", default="Debtor",
                   choices=["Debtor", "SecuredParty", "UCCNum"],
                   help="Search type: Debtor name (default), SecuredParty name, or UCCNum (lien number)")
    p.add_argument("--individual", action="store_true",
                   help="Search by individual name (default is organization). Query='First Last'")
    p.add_argument("--first-name", default="", help="First name (for individual search)")
    p.add_argument("--last-name", default="", help="Last name (for individual search)")
    p.add_argument("--city", default="", help="Filter by city")
    p.add_argument("--state", default="", help="Filter by state")
    p.add_argument("--active-only", action="store_true", help="Exclude lapsed records")

    p = sub.add_parser("detail", help="Get UCC filing detail by internal ID")
    p.add_argument("filing_id", help="Internal ID from search results (the 'ID' field)")

    p = sub.add_parser("history", help="Get UCC filing history by record number")
    p.add_argument("record_num", help="Record number (RECORD_NUM from search results)")

    p = sub.add_parser("ingest-batch", help="Search and ingest matching filings")
    p.add_argument("query", help="Search term")
    p.add_argument("--type", dest="search_type", default="Debtor",
                   choices=["Debtor", "SecuredParty", "UCCNum"])
    p.add_argument("--individual", action="store_true")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "detail": cmd_detail,
        "history": cmd_history,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
