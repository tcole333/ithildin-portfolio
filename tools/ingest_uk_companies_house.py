#!/usr/bin/env python3
"""
UK Companies House corporate registry ingester.

Uses the Companies House REST API (https://developer.company-information.service.gov.uk/).
Auth: HTTP Basic (API key as username, empty password).
Rate limit: 600 requests per 5 minutes (~2/sec).

Get a free API key at: https://developer.company-information.service.gov.uk/
Then add to .env: COMPANIES_HOUSE_API_KEY=your_key_here

Usage:
    python tools/ingest_uk_companies_house.py search "Epstein"
    python tools/ingest_uk_companies_house.py search "Enhanced Education" --limit 50
    python tools/ingest_uk_companies_house.py company 12345678
    python tools/ingest_uk_companies_house.py officers 12345678
    python tools/ingest_uk_companies_house.py psc 12345678
    python tools/ingest_uk_companies_house.py filings 12345678
    python tools/ingest_uk_companies_house.py officer-search "Ghislaine Maxwell"
    python tools/ingest_uk_companies_house.py officer-search "Leon Black"
    python tools/ingest_uk_companies_house.py officer-appointments AbCdEf12345
    python tools/ingest_uk_companies_house.py ingest-entity 12345678
    python tools/ingest_uk_companies_house.py ingest-batch "Epstein" --limit 20

Test targets:
    - "Epstein" — UK-registered Epstein entities
    - "Ghislaine Maxwell" (officer-search) — Maxwell board seats
    - "Leon Black" (officer-search) — Black's UK directorships
    - "Enhanced Education" — UK arm of Epstein entity
    - Apollo UK subsidiaries
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip().strip('"')

BASE_URL = "https://api.company-information.service.gov.uk"

# Rate limiting: 600 requests per 5 minutes = 2/sec.
# Use 0.6s delay for ~1.67/sec with headroom.
REQUEST_DELAY = 0.6
_last_request_time = 0.0


def _get_api_key():
    """Get the Companies House API key from environment."""
    key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not key:
        print(
            "ERROR: COMPANIES_HOUSE_API_KEY not set.\n"
            "\n"
            "To get a free API key:\n"
            "  1. Register at https://developer.company-information.service.gov.uk/\n"
            "  2. Create an application (REST API, live)\n"
            "  3. Copy the API key\n"
            "  4. Add to .env: COMPANIES_HOUSE_API_KEY=your_key_here\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _make_auth_header(api_key):
    """Build HTTP Basic Auth header (key as username, empty password)."""
    credentials = f"{api_key}:"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def _request(path, params=None, retry=0):
    """Make an authenticated API request with rate limiting.

    Returns parsed JSON or None on error.
    """
    global _last_request_time

    api_key = _get_api_key()

    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)

    # Rate limiting
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    headers = {
        "Authorization": _make_auth_header(api_key),
        "Accept": "application/json",
    }
    req = Request(url, headers=headers)

    try:
        _last_request_time = time.time()
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        if e.code == 429 and retry < 3:
            # Rate limited — back off
            wait = (2 ** retry) * 5
            print(f"  Rate limited (429). Waiting {wait}s before retry {retry + 1}...", file=sys.stderr)
            time.sleep(wait)
            return _request(path, params, retry + 1)
        if e.code == 401:
            print(
                "ERROR: Authentication failed (401). Your API key may be invalid.\n"
                "Check COMPANIES_HOUSE_API_KEY in .env",
                file=sys.stderr,
            )
            return None
        if e.code == 404:
            return None
        body = ""
        try:
            body = e.read().decode()[:500]
        except Exception:
            pass
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach Companies House API: {e.reason}", file=sys.stderr)
        return None


def _paginate(path, params=None, items_key="items", max_results=100):
    """Paginate through Companies House results.

    The API uses start_index for pagination.
    """
    if params is None:
        params = {}

    all_items = []
    start_index = 0
    items_per_page = min(max_results, 100)  # API max is 100 per page

    while len(all_items) < max_results:
        params["start_index"] = start_index
        params["items_per_page"] = items_per_page

        data = _request(path, params)
        if not data:
            break

        items = data.get(items_key, [])
        if not items:
            break

        all_items.extend(items)

        total = data.get("total_results", 0)
        if start_index + len(items) >= total:
            break

        start_index += len(items)

    return all_items[:max_results]


# ══════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════

def _format_address(addr_dict):
    """Format a Companies House address object into a string."""
    if not addr_dict:
        return ""
    parts = []
    for key in ["premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country"]:
        val = addr_dict.get(key)
        if val:
            parts.append(val)
    return ", ".join(parts)


def _format_company_type(company_type):
    """Map Companies House company_type codes to readable form."""
    type_map = {
        "ltd": "Private Limited",
        "plc": "Public Limited",
        "llp": "LLP",
        "private-limited-guarant-nsc-limited-exemption": "Private Limited by Guarantee",
        "private-limited-guarant-nsc": "Private Limited by Guarantee",
        "private-unlimited": "Private Unlimited",
        "private-limited-shares-section-30-exemption": "Private Limited (s.30)",
        "private-unlimited-nsc": "Private Unlimited (NSC)",
        "royal-charter": "Royal Charter",
        "registered-society-non-jurisdictional": "Registered Society",
        "scottish-partnership": "Scottish Partnership",
        "charitable-incorporated-organisation": "CIO",
        "scottish-charitable-incorporated-organisation": "Scottish CIO",
        "industrial-and-provident-society": "Industrial & Provident Society",
        "oversea-company": "Overseas Company",
        "limited-partnership": "Limited Partnership",
        "registered-overseas-entity": "Registered Overseas Entity",
    }
    return type_map.get(company_type, company_type or "?")


def _format_company_status(status):
    """Map company_status to readable form."""
    status_map = {
        "active": "Active",
        "dissolved": "Dissolved",
        "liquidation": "In Liquidation",
        "receivership": "In Receivership",
        "administration": "In Administration",
        "voluntary-arrangement": "Voluntary Arrangement",
        "converted-closed": "Converted/Closed",
        "insolvency-proceedings": "Insolvency Proceedings",
        "registered": "Registered",
        "removed": "Removed",
        "closed": "Closed",
        "open": "Open",
    }
    return status_map.get(status, status or "?")


def _map_entity_type(company_type):
    """Map Companies House company_type to registry entity_type."""
    mapping = {
        "ltd": "ltd",
        "plc": "plc",
        "llp": "llp",
        "limited-partnership": "lp",
        "private-limited-guarant-nsc-limited-exemption": "nonprofit",
        "private-limited-guarant-nsc": "nonprofit",
        "private-unlimited": "unlimited",
        "private-unlimited-nsc": "unlimited",
        "royal-charter": "royal_charter",
        "charitable-incorporated-organisation": "nonprofit",
        "scottish-charitable-incorporated-organisation": "nonprofit",
        "oversea-company": "foreign_corp",
        "registered-overseas-entity": "foreign_corp",
    }
    return mapping.get(company_type, company_type or None)


def _map_status(company_status):
    """Map Companies House company_status to registry status."""
    mapping = {
        "active": "active",
        "dissolved": "dissolved",
        "liquidation": "inactive",
        "receivership": "inactive",
        "administration": "inactive",
        "voluntary-arrangement": "inactive",
        "converted-closed": "inactive",
        "insolvency-proceedings": "inactive",
        "registered": "active",
        "removed": "dissolved",
        "closed": "dissolved",
        "open": "active",
    }
    return mapping.get(company_status, company_status or None)


# ══════════════════════════════════════════════════════════
# COMMANDS: Display
# ══════════════════════════════════════════════════════════

def cmd_search(args):
    """Search companies by name."""
    data = _request("/search/companies", {"q": args.query, "items_per_page": args.limit})
    if not data:
        print("No results or API error.")
        return

    items = data.get("items", [])
    total = data.get("total_results", 0)
    print(f"Found {total} UK companies matching '{args.query}' (showing {len(items)})")
    print()

    for c in items:
        number = c.get("company_number", "?")
        name = c.get("title", "?")
        ctype = _format_company_type(c.get("company_type"))
        status = _format_company_status(c.get("company_status"))
        created = c.get("date_of_creation", "")
        ceased = c.get("date_of_cessation", "")
        addr = _format_address(c.get("address", {}))

        print(f"  [UK] {name} ({ctype}, {status})")
        print(f"    Company #: {number}")
        if created:
            date_line = f"    Created: {created}"
            if ceased:
                date_line += f" | Ceased: {ceased}"
            print(date_line)
        if addr:
            print(f"    Address: {addr}")

        # Show snippet if available
        snippet = c.get("snippet", "")
        if snippet:
            print(f"    Match: {snippet}")

        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_company(args):
    """Get full company profile."""
    data = _request(f"/company/{args.number}")
    if not data:
        print(f"Company {args.number} not found.")
        return

    name = data.get("company_name", "?")
    number = data.get("company_number", "?")
    ctype = _format_company_type(data.get("type"))
    status = _format_company_status(data.get("company_status"))
    created = data.get("date_of_creation", "")
    ceased = data.get("date_of_cessation", "")

    print(f"  [UK] {name} ({ctype}, {status})")
    print(f"    Company #: {number}")
    if created:
        date_line = f"    Created: {created}"
        if ceased:
            date_line += f" | Ceased: {ceased}"
        print(date_line)

    # Registered office
    office = data.get("registered_office_address", {})
    if office:
        print(f"    Registered office: {_format_address(office)}")

    # SIC codes
    sic_codes = data.get("sic_codes", [])
    if sic_codes:
        print(f"    SIC codes: {', '.join(sic_codes)}")

    # Previous names
    prev_names = data.get("previous_company_names", [])
    if prev_names:
        print("    Previous names:")
        for pn in prev_names:
            eff = pn.get("effective_from", "?")
            ceased_on = pn.get("ceased_on", "?")
            print(f"      {pn.get('name', '?')} ({eff} to {ceased_on})")

    # Accounts
    accounts = data.get("accounts", {})
    if accounts:
        next_due = accounts.get("next_due", "")
        last_made = accounts.get("last_accounts", {}).get("made_up_to", "")
        acc_type = accounts.get("accounting_reference_date", {})
        if next_due:
            print(f"    Accounts next due: {next_due}")
        if last_made:
            print(f"    Last accounts made up to: {last_made}")

    # Confirmation statement
    conf = data.get("confirmation_statement", {})
    if conf:
        next_due = conf.get("next_due", "")
        last_made = conf.get("last_made_up_to", "")
        if next_due:
            print(f"    Confirmation statement next due: {next_due}")

    # Links
    links = data.get("links", {})
    self_link = links.get("self", "")
    if self_link:
        print(f"    URL: https://find-and-update.company-information.service.gov.uk{self_link}")

    print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_officers(args):
    """List officers for a company."""
    items = _paginate(f"/company/{args.number}/officers", max_results=args.limit)

    if not items:
        print(f"No officers found for company {args.number}.")
        return

    # Also get company name for context
    company = _request(f"/company/{args.number}")
    company_name = company.get("company_name", args.number) if company else args.number

    print(f"Officers for {company_name} ({args.number}): {len(items)}")
    print()

    for o in items:
        name = o.get("name", "?")
        role = o.get("officer_role", "?")
        appointed = o.get("appointed_on", "?")
        resigned = o.get("resigned_on", "")
        status_flag = " [RESIGNED]" if resigned else ""
        nationality = o.get("nationality", "")
        occupation = o.get("occupation", "")
        country_of_residence = o.get("country_of_residence", "")

        print(f"  {name} ({role}){status_flag}")
        print(f"    Appointed: {appointed}")
        if resigned:
            print(f"    Resigned: {resigned}")
        if nationality:
            print(f"    Nationality: {nationality}")
        if occupation:
            print(f"    Occupation: {occupation}")
        if country_of_residence:
            print(f"    Country of residence: {country_of_residence}")

        addr = _format_address(o.get("address", {}))
        if addr:
            print(f"    Address: {addr}")

        # Officer ID for cross-referencing appointments
        links = o.get("links", {})
        officer_link = links.get("officer", {}).get("appointments", "")
        if officer_link:
            # Extract officer ID from path like /officers/AbCdEf/appointments
            parts = officer_link.strip("/").split("/")
            if len(parts) >= 2:
                print(f"    Officer ID: {parts[1]}")

        print()

    if args.json_out:
        print(json.dumps(items, indent=2, default=str))


def cmd_psc(args):
    """List persons with significant control for a company."""
    items = _paginate(
        f"/company/{args.number}/persons-with-significant-control",
        max_results=args.limit,
    )

    if not items:
        print(f"No PSC records found for company {args.number}.")
        return

    company = _request(f"/company/{args.number}")
    company_name = company.get("company_name", args.number) if company else args.number

    print(f"Persons with Significant Control for {company_name} ({args.number}): {len(items)}")
    print()

    for p in items:
        name = p.get("name", p.get("name_elements", {}).get("surname", "?"))
        kind = p.get("kind", "?")
        ceased = p.get("ceased_on", "")
        notified = p.get("notified_on", "?")
        status_flag = " [CEASED]" if ceased else ""

        # Name elements for individuals
        name_elements = p.get("name_elements", {})
        if name_elements and not p.get("name"):
            parts = []
            if name_elements.get("title"):
                parts.append(name_elements["title"])
            if name_elements.get("forename"):
                parts.append(name_elements["forename"])
            if name_elements.get("other_forenames"):
                parts.append(name_elements["other_forenames"])
            if name_elements.get("surname"):
                parts.append(name_elements["surname"])
            name = " ".join(parts) if parts else "?"

        print(f"  {name} ({kind}){status_flag}")
        print(f"    Notified: {notified}")
        if ceased:
            print(f"    Ceased: {ceased}")

        # Nature of control
        natures = p.get("natures_of_control", [])
        if natures:
            print(f"    Control: {'; '.join(natures)}")

        nationality = p.get("nationality", "")
        country = p.get("country_of_residence", "")
        if nationality:
            print(f"    Nationality: {nationality}")
        if country:
            print(f"    Country of residence: {country}")

        addr = _format_address(p.get("address", {}))
        if addr:
            print(f"    Address: {addr}")

        print()

    if args.json_out:
        print(json.dumps(items, indent=2, default=str))


def cmd_filings(args):
    """Get filing history for a company."""
    items = _paginate(
        f"/company/{args.number}/filing-history",
        max_results=args.limit,
    )

    if not items:
        print(f"No filing history found for company {args.number}.")
        return

    company = _request(f"/company/{args.number}")
    company_name = company.get("company_name", args.number) if company else args.number

    print(f"Filing history for {company_name} ({args.number}): {len(items)}")
    print()

    for f in items:
        date = f.get("date", "?")
        category = f.get("category", "?")
        description = f.get("description", "?")
        ftype = f.get("type", "")
        paper_filed = f.get("paper_filed", False)

        # Description values often contain placeholders like {date}
        desc_values = f.get("description_values", {})
        display_desc = description
        if desc_values:
            for k, v in desc_values.items():
                if v:
                    display_desc = display_desc.replace("{" + k + "}", str(v))

        paper_flag = " [paper]" if paper_filed else ""
        print(f"  {date}: {display_desc} ({category}){paper_flag}")
        if ftype:
            print(f"    Type: {ftype}")

        # Document link
        links = f.get("links", {})
        doc_link = links.get("document_metadata", "")
        if doc_link:
            print(f"    Document: https://find-and-update.company-information.service.gov.uk{doc_link}")

        print()

    if args.json_out:
        print(json.dumps(items, indent=2, default=str))


def cmd_officer_search(args):
    """Search for officers by name across all companies."""
    data = _request("/search/officers", {"q": args.name, "items_per_page": args.limit})
    if not data:
        print("No results or API error.")
        return

    items = data.get("items", [])
    total = data.get("total_results", 0)
    print(f"Found {total} officer records matching '{args.name}' (showing {len(items)})")
    print()

    for o in items:
        name = o.get("title", "?")
        snippet = o.get("snippet", "")
        addr = _format_address(o.get("address", {}))
        date_of_birth = o.get("date_of_birth", {})
        dob_str = ""
        if date_of_birth:
            month = date_of_birth.get("month", "")
            year = date_of_birth.get("year", "")
            if month and year:
                dob_str = f" (DOB: {month}/{year})"

        # Appointments count
        links = o.get("links", {})
        self_link = links.get("self", "")

        # Extract officer ID
        officer_id = ""
        if self_link:
            parts = self_link.strip("/").split("/")
            if len(parts) >= 2:
                officer_id = parts[1]

        appointment_count = o.get("appointment_count", 0)

        print(f"  {name}{dob_str}")
        if officer_id:
            print(f"    Officer ID: {officer_id}")
        print(f"    Appointments: {appointment_count}")
        if addr:
            print(f"    Address: {addr}")
        if snippet:
            print(f"    Match: {snippet}")

        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_officer_appointments(args):
    """List all appointments for a specific officer ID."""
    items = _paginate(
        f"/officers/{args.officer_id}/appointments",
        max_results=args.limit,
    )

    if not items:
        print(f"No appointments found for officer {args.officer_id}.")
        return

    # Get officer name from first result
    officer_name = items[0].get("name", args.officer_id) if items else args.officer_id

    print(f"Appointments for {officer_name} ({args.officer_id}): {len(items)}")
    print()

    for a in items:
        company_name = a.get("appointed_to", {}).get("company_name", "?")
        company_number = a.get("appointed_to", {}).get("company_number", "?")
        company_status = a.get("appointed_to", {}).get("company_status", "")
        role = a.get("officer_role", "?")
        appointed = a.get("appointed_on", "?")
        resigned = a.get("resigned_on", "")
        status_flag = " [RESIGNED]" if resigned else ""
        company_status_flag = f" ({company_status})" if company_status else ""

        print(f"  {company_name} #{company_number}{company_status_flag}")
        print(f"    Role: {role}{status_flag}")
        print(f"    Appointed: {appointed}")
        if resigned:
            print(f"    Resigned: {resigned}")

        addr = _format_address(a.get("address", {}))
        if addr:
            print(f"    Address: {addr}")

        print()

    if args.json_out:
        print(json.dumps(items, indent=2, default=str))


def cmd_insolvency(args):
    """Get insolvency cases for a company."""
    data = _request(f"/company/{args.number}/insolvency")
    if not data:
        print(f"No insolvency data for company {args.number} (may not have any cases).")
        return

    company = _request(f"/company/{args.number}")
    company_name = company.get("company_name", args.number) if company else args.number

    cases = data.get("cases", [])
    print(f"Insolvency cases for {company_name} ({args.number}): {len(cases)}")
    print()

    for i, case in enumerate(cases, 1):
        case_number = case.get("number", i)
        case_type = case.get("type", "?")
        print(f"  Case #{case_number} — {case_type}")

        dates = case.get("dates", [])
        for d in dates:
            print(f"    {d.get('type', '?')}: {d.get('date', '?')}")

        notes = case.get("notes", [])
        for n in notes:
            print(f"    Note: {n}")

        practitioners = case.get("practitioners", [])
        for p in practitioners:
            name = p.get("name", "?")
            role = p.get("role", "?")
            addr = _format_address(p.get("address", {}))
            print(f"    Practitioner: {name} ({role})")
            if addr:
                print(f"      Address: {addr}")

        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_ingest_insolvency(args):
    """Ingest insolvency data as findings into investigation.db."""
    data = _request(f"/company/{args.number}/insolvency")
    if not data:
        print(f"No insolvency data for company {args.number}.")
        return

    company = _request(f"/company/{args.number}")
    company_name = company.get("company_name", args.number) if company else args.number

    cases = data.get("cases", [])
    if not cases:
        print(f"No insolvency cases for {company_name}.")
        return

    # Import findings tracker
    try:
        from tools.findings_tracker import add_finding
    except ImportError:
        from findings_tracker import add_finding

    count = 0
    for case in cases:
        case_number = case.get("number", "?")
        case_type = case.get("type", "unknown")

        dates = case.get("dates", [])
        date_str = ""
        for d in dates:
            if d.get("type") in ("wound-up-on", "petitioned-on", "due-to-be-dissolved-on"):
                date_str = d.get("date", "")
                break
        if not date_str and dates:
            date_str = dates[0].get("date", "")

        practitioners = case.get("practitioners", [])
        practitioner_names = [p.get("name", "?") for p in practitioners]

        detail_parts = [f"Type: {case_type}", f"Case #{case_number}"]
        for d in dates:
            detail_parts.append(f"{d.get('type', '?')}: {d.get('date', '?')}")
        if practitioner_names:
            detail_parts.append(f"Practitioners: {', '.join(practitioner_names)}")
        for p in practitioners:
            addr = _format_address(p.get("address", {}))
            if addr:
                detail_parts.append(f"  {p.get('name', '?')} address: {addr}")

        summary = f"UK insolvency case #{case_number} ({case_type}) for {company_name}"
        detail = "; ".join(detail_parts)
        source_url = f"https://find-and-update.company-information.service.gov.uk/company/{args.number}/insolvency"

        finding_id = add_finding(
            target_name=company_name,
            finding_type="legal",
            summary=summary,
            detail=detail,
            source_datasets="UK Companies House Insolvency API",
            confidence="confirmed",
            date_of_event=date_str or None,
            evidence_ids=[source_url],
            claim_type="direct_quote",
            source_quotes=[f"Insolvency case #{case_number}, type: {case_type}"],
        )
        print(f"  Finding #{finding_id}: {summary}")
        count += 1

    print(f"\nIngested {count} insolvency cases for {company_name}")


# ══════════════════════════════════════════════════════════
# COMMANDS: Ingest into registry.db
# ══════════════════════════════════════════════════════════

def _upsert_entity(db, profile):
    """Insert or update a company from API profile data into registry.db.

    Accepts either a company profile (from /company/{number}) or a
    search result item. Returns the registry entity ID.
    """
    # Handle both profile and search result shapes
    number = profile.get("company_number", "?")
    name = profile.get("company_name", profile.get("title", "?"))
    company_type = profile.get("type", profile.get("company_type"))
    company_status = profile.get("company_status")
    created = profile.get("date_of_creation", "")
    ceased = profile.get("date_of_cessation", "")

    etype = _map_entity_type(company_type)
    status = _map_status(company_status)

    # Address — could be registered_office_address (profile) or address (search)
    addr_dict = profile.get("registered_office_address", profile.get("address", {}))
    principal_address = None
    principal_city = None
    principal_state = None
    principal_zip = None
    principal_country = None
    if addr_dict:
        addr_parts = []
        if addr_dict.get("premises"):
            addr_parts.append(addr_dict["premises"])
        if addr_dict.get("address_line_1"):
            addr_parts.append(addr_dict["address_line_1"])
        if addr_dict.get("address_line_2"):
            addr_parts.append(addr_dict["address_line_2"])
        principal_address = ", ".join(addr_parts) if addr_parts else None
        principal_city = addr_dict.get("locality")
        principal_state = addr_dict.get("region")
        principal_zip = addr_dict.get("postal_code")
        principal_country = addr_dict.get("country", "United Kingdom")

    source_url = f"https://find-and-update.company-information.service.gov.uk/company/{number}"

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, dissolution_date, principal_address, principal_city,
            principal_state, principal_zip, principal_country,
            state_of_formation, source_url, raw_data
        ) VALUES ('uk', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'United Kingdom', ?, ?)
    """, [
        number, name, etype, status, created or None, ceased or None,
        principal_address, principal_city, principal_state, principal_zip,
        principal_country, source_url, json.dumps(profile, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='uk' AND source_id=?",
        [number]
    ).fetchone()
    return row[0]


def _upsert_officers(db, entity_id, officers):
    """Insert officers from the officers API response."""
    count = 0
    for o in officers:
        name = o.get("name", "")
        if not name:
            continue

        role = o.get("officer_role", "")
        appointed = o.get("appointed_on", "")
        resigned = o.get("resigned_on", "")
        nationality = o.get("nationality", "")
        occupation = o.get("occupation", "")

        # Build title from role
        title = role.replace("-", " ").title() if role else None

        # Address
        addr_dict = o.get("address", {})
        addr_parts = []
        if addr_dict.get("premises"):
            addr_parts.append(addr_dict["premises"])
        if addr_dict.get("address_line_1"):
            addr_parts.append(addr_dict["address_line_1"])
        if addr_dict.get("address_line_2"):
            addr_parts.append(addr_dict["address_line_2"])
        address = ", ".join(addr_parts) if addr_parts else None
        city = addr_dict.get("locality")
        state = addr_dict.get("region")
        zipcode = addr_dict.get("postal_code")
        country = addr_dict.get("country")

        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip, country,
                 effective_date, end_date)
                VALUES (?, ?, ?, 'person', ?, ?, ?, ?, ?, ?, ?)
            """, [
                entity_id, name, title, address, city, state, zipcode, country,
                appointed or None, resigned or None,
            ])
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def _upsert_psc(db, entity_id, psc_items):
    """Insert PSC records as officers with title 'PSC' or similar."""
    count = 0
    for p in psc_items:
        # Build name
        name = p.get("name", "")
        if not name:
            name_elements = p.get("name_elements", {})
            parts = []
            if name_elements.get("title"):
                parts.append(name_elements["title"])
            if name_elements.get("forename"):
                parts.append(name_elements["forename"])
            if name_elements.get("other_forenames"):
                parts.append(name_elements["other_forenames"])
            if name_elements.get("surname"):
                parts.append(name_elements["surname"])
            name = " ".join(parts) if parts else ""
        if not name:
            continue

        kind = p.get("kind", "")
        ceased = p.get("ceased_on", "")
        notified = p.get("notified_on", "")
        natures = p.get("natures_of_control", [])

        # Use "PSC" as title, with nature of control detail
        title = "PSC"
        if natures:
            # Abbreviate for storage
            title = f"PSC ({'; '.join(natures[:2])})"
            if len(title) > 200:
                title = title[:197] + "..."

        addr_dict = p.get("address", {})
        addr_parts = []
        if addr_dict.get("premises"):
            addr_parts.append(addr_dict["premises"])
        if addr_dict.get("address_line_1"):
            addr_parts.append(addr_dict["address_line_1"])
        if addr_dict.get("address_line_2"):
            addr_parts.append(addr_dict["address_line_2"])
        address = ", ".join(addr_parts) if addr_parts else None
        city = addr_dict.get("locality")
        state = addr_dict.get("region")
        zipcode = addr_dict.get("postal_code")
        country = addr_dict.get("country")

        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_officers
                (entity_id, officer_name, title, officer_type, address, city, state, zip, country,
                 effective_date, end_date)
                VALUES (?, ?, ?, 'person', ?, ?, ?, ?, ?, ?, ?)
            """, [
                entity_id, name, title, address, city, state, zipcode, country,
                notified or None, ceased or None,
            ])
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def _upsert_filings(db, entity_id, filings):
    """Insert filing history records."""
    count = 0
    for f in filings:
        filing_date = f.get("date", "")
        category = f.get("category", "")
        description = f.get("description", "")
        ftype = f.get("type", "")

        # Build human-readable description
        desc_values = f.get("description_values", {})
        display_desc = description
        if desc_values:
            for k, v in desc_values.items():
                if v:
                    display_desc = display_desc.replace("{" + k + "}", str(v))

        # Map category to filing_type
        filing_type = category or ftype or description

        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_filings
                (entity_id, filing_type, filing_date, description, raw_data)
                VALUES (?, ?, ?, ?, ?)
            """, [
                entity_id, filing_type, filing_date or None,
                display_desc or None, json.dumps(f, default=str),
            ])
            count += 1
        except sqlite3.IntegrityError:
            pass

    return count


def _upsert_registered_office(db, entity_id, profile):
    """Insert the registered office as a registry_agents record."""
    addr_dict = profile.get("registered_office_address", {})
    if not addr_dict:
        return

    addr_parts = []
    if addr_dict.get("premises"):
        addr_parts.append(addr_dict["premises"])
    if addr_dict.get("address_line_1"):
        addr_parts.append(addr_dict["address_line_1"])
    if addr_dict.get("address_line_2"):
        addr_parts.append(addr_dict["address_line_2"])
    address = ", ".join(addr_parts) if addr_parts else None

    try:
        db.execute("""
            INSERT OR IGNORE INTO registry_agents
            (entity_id, agent_name, agent_type, address, city, state, zip, country)
            VALUES (?, 'Registered Office', 'address', ?, ?, ?, ?, ?)
        """, [
            entity_id, address,
            addr_dict.get("locality"),
            addr_dict.get("region"),
            addr_dict.get("postal_code"),
            addr_dict.get("country", "United Kingdom"),
        ])
    except sqlite3.IntegrityError:
        pass


def _upsert_previous_names(db, entity_id, profile):
    """Insert previous company names into name history."""
    prev_names = profile.get("previous_company_names", [])
    for pn in prev_names:
        name = pn.get("name", "")
        ceased_on = pn.get("ceased_on", "")
        if not name:
            continue
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_name_history
                (entity_id, previous_name, change_date)
                VALUES (?, ?, ?)
            """, [entity_id, name, ceased_on or None])
        except sqlite3.IntegrityError:
            pass


def cmd_ingest_entity(args):
    """Ingest a specific company by number into registry.db."""
    db = get_db()

    # Fetch full company profile
    profile = _request(f"/company/{args.number}")
    if not profile:
        print(f"Company {args.number} not found.")
        return

    entity_id = _upsert_entity(db, profile)
    name = profile.get("company_name", "?")
    print(f"Ingested entity: {name} (registry ID: {entity_id})")

    # Registered office as agent
    _upsert_registered_office(db, entity_id, profile)

    # Previous names
    _upsert_previous_names(db, entity_id, profile)

    # Officers
    officers = _paginate(f"/company/{args.number}/officers", max_results=500)
    officer_count = _upsert_officers(db, entity_id, officers)
    print(f"  Loaded {officer_count} officers")

    # PSC (beneficial owners)
    psc_items = _paginate(
        f"/company/{args.number}/persons-with-significant-control",
        max_results=100,
    )
    psc_count = _upsert_psc(db, entity_id, psc_items)
    print(f"  Loaded {psc_count} PSC records")

    # Filing history
    filings = _paginate(f"/company/{args.number}/filing-history", max_results=500)
    filing_count = _upsert_filings(db, entity_id, filings)
    print(f"  Loaded {filing_count} filings")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    # Log the ingest
    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, file_name, record_count, notes)
        VALUES ('uk', 'api', ?, ?, ?)
    """, [
        args.number,
        1 + officer_count + psc_count + filing_count,
        f"Company {name} ({args.number}): {officer_count} officers, {psc_count} PSC, {filing_count} filings",
    ])
    db.commit()


def cmd_ingest_batch(args):
    """Search and ingest all matching companies."""
    db = get_db()

    data = _request("/search/companies", {"q": args.query, "items_per_page": args.limit})
    if not data:
        print("No results or API error.")
        return

    items = data.get("items", [])
    total = data.get("total_results", 0)
    print(f"Ingesting {len(items)} of {total} companies matching '{args.query}'")
    print()

    total_officers = 0
    total_psc = 0
    total_filings = 0

    for i, c in enumerate(items):
        number = c.get("company_number", "")
        name = c.get("title", "?")
        if not number:
            continue

        # Fetch full profile for each company
        profile = _request(f"/company/{number}")
        if not profile:
            print(f"  [{i + 1}/{len(items)}] SKIP {name} ({number}) — could not fetch profile")
            continue

        entity_id = _upsert_entity(db, profile)
        print(f"  [{i + 1}/{len(items)}] {name} ({number}) -> registry ID {entity_id}")

        # Registered office + previous names
        _upsert_registered_office(db, entity_id, profile)
        _upsert_previous_names(db, entity_id, profile)

        # Officers
        officers = _paginate(f"/company/{number}/officers", max_results=200)
        oc = _upsert_officers(db, entity_id, officers)
        total_officers += oc

        # PSC
        psc_items = _paginate(
            f"/company/{number}/persons-with-significant-control",
            max_results=50,
        )
        pc = _upsert_psc(db, entity_id, psc_items)
        total_psc += pc

        # Filing history (limit to 50 per entity in batch mode)
        filings = _paginate(f"/company/{number}/filing-history", max_results=50)
        fc = _upsert_filings(db, entity_id, filings)
        total_filings += fc

        if oc or pc or fc:
            print(f"    + {oc} officers, {pc} PSC, {fc} filings")

        # Commit periodically
        if (i + 1) % 5 == 0:
            db.commit()

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass

    # Log the batch ingest
    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, file_name, record_count, notes)
        VALUES ('uk', 'api_batch', ?, ?, ?)
    """, [
        f"search:{args.query}",
        len(items),
        f"Batch '{args.query}': {len(items)} companies, {total_officers} officers, {total_psc} PSC, {total_filings} filings",
    ])
    db.commit()

    print(f"\nBatch ingest complete: {len(items)} companies, {total_officers} officers, {total_psc} PSC, {total_filings} filings")


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="UK Companies House corporate registry via REST API"
    )
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search companies by name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    # company
    p = sub.add_parser("company", help="Get full company profile")
    p.add_argument("number", help="Company number (e.g. 12345678)")

    # officers
    p = sub.add_parser("officers", help="List officers for a company")
    p.add_argument("number", help="Company number")
    p.add_argument("--limit", type=int, default=100)

    # psc
    p = sub.add_parser("psc", help="List persons with significant control")
    p.add_argument("number", help="Company number")
    p.add_argument("--limit", type=int, default=50)

    # filings
    p = sub.add_parser("filings", help="Filing history for a company")
    p.add_argument("number", help="Company number")
    p.add_argument("--limit", type=int, default=50)

    # officer-search
    p = sub.add_parser("officer-search", help="Search officers by name across all companies")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)

    # officer-appointments
    p = sub.add_parser("officer-appointments", help="All appointments for one officer")
    p.add_argument("officer_id", help="Officer ID (from officers or officer-search output)")
    p.add_argument("--limit", type=int, default=100)

    # ingest-entity
    p = sub.add_parser("ingest-entity", help="Ingest a company + officers + PSC into registry.db")
    p.add_argument("number", help="Company number")

    # insolvency
    p = sub.add_parser("insolvency", help="Get insolvency cases for a company")
    p.add_argument("number", help="Company number")

    # ingest-insolvency
    p = sub.add_parser("ingest-insolvency", help="Ingest insolvency data as findings")
    p.add_argument("number", help="Company number")

    # ingest-batch
    p = sub.add_parser("ingest-batch", help="Search and ingest all matching companies")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "company": cmd_company,
        "officers": cmd_officers,
        "psc": cmd_psc,
        "filings": cmd_filings,
        "officer-search": cmd_officer_search,
        "officer-appointments": cmd_officer_appointments,
        "insolvency": cmd_insolvency,
        "ingest-insolvency": cmd_ingest_insolvency,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
