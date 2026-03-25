#!/usr/bin/env python3
"""
OpenCorporates API — global corporate registry search.

Searches the full OpenCorporates dataset (200M+ companies, 160+ jurisdictions).
Supports company search, officer search, address search, and entity detail.

Requires OPENCORPORATES_API_KEY environment variable.

Usage:
    python tools/query_opencorporates.py search "Excession LLC"
    python tools/query_opencorporates.py search "Excession LLC" --jurisdiction us_tx
    python tools/query_opencorporates.py search "Excession LLC" --country us
    python tools/query_opencorporates.py officers "Elon Musk"
    python tools/query_opencorporates.py officers "Jared Birchall" --jurisdiction us_tx
    python tools/query_opencorporates.py address "865 FM 1209, Bastrop"
    python tools/query_opencorporates.py entity us_tx 0804842786
    python tools/query_opencorporates.py filings us_tx 0804842786
    python tools/query_opencorporates.py statements us_de 12345678
    python tools/query_opencorporates.py account-status
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

API_BASE = "https://api.opencorporates.com/v0.4"
RATE_LIMIT_DELAY = 0.5


def get_api_key():
    """Get OpenCorporates API key from environment."""
    key = os.getenv("OPENCORPORATES_API_KEY")
    if not key:
        print("ERROR: OPENCORPORATES_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    return key


def api_request(endpoint, params=None):
    """Make authenticated API request with rate limiting."""
    api_key = get_api_key()
    if params is None:
        params = {}
    params["api_token"] = api_key
    url = f"{API_BASE}/{endpoint}"

    try:
        time.sleep(RATE_LIMIT_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("ERROR: Invalid API token", file=sys.stderr)
        elif e.response.status_code == 403:
            print("ERROR: Access denied — may need a paid plan for this endpoint", file=sys.stderr)
        elif e.response.status_code == 429:
            print("ERROR: Rate limit exceeded", file=sys.stderr)
        elif e.response.status_code == 404:
            print(f"ERROR: Not found: {endpoint}", file=sys.stderr)
            return None
        else:
            print(f"HTTP {e.response.status_code}: {e.response.text[:200]}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_company(company):
    """Extract key fields from a company object."""
    return {
        "name": company.get("name"),
        "company_number": company.get("company_number"),
        "jurisdiction_code": company.get("jurisdiction_code"),
        "incorporation_date": company.get("incorporation_date"),
        "dissolution_date": company.get("dissolution_date"),
        "company_type": company.get("company_type"),
        "current_status": company.get("current_status"),
        "registered_address": company.get("registered_address_in_full"),
        "agent_name": company.get("agent_name"),
        "agent_address": company.get("agent_address"),
        "registry_url": company.get("registry_url"),
        "opencorporates_url": company.get("opencorporates_url"),
    }


def _parse_officer(officer):
    """Extract key fields from an officer object."""
    company = officer.get("company", {})
    return {
        "name": officer.get("name"),
        "position": officer.get("position"),
        "start_date": officer.get("start_date"),
        "end_date": officer.get("end_date"),
        "occupation": officer.get("occupation"),
        "nationality": officer.get("nationality"),
        "address": officer.get("address"),
        "date_of_birth": officer.get("date_of_birth"),
        "company_name": company.get("name"),
        "company_number": company.get("company_number"),
        "jurisdiction_code": company.get("jurisdiction_code"),
        "opencorporates_url": officer.get("opencorporates_url"),
    }


# --- Search commands ---

def search_companies(query, jurisdiction=None, country=None, inactive=False,
                     address=None, per_page=30, page=1):
    """Search companies by name, optionally filtered by jurisdiction or address."""
    params = {"q": query, "per_page": per_page, "page": page}
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction
    if country:
        params["country_code"] = country
    if inactive:
        params["inactive"] = "true"
    if address:
        params["registered_address"] = address

    data = api_request("companies/search", params)
    companies = data.get("results", {}).get("companies", [])
    total = data.get("results", {}).get("total_count", 0)

    try:
        log_search("opencorporates", query, {
            "result_count": total,
            "jurisdiction": jurisdiction or country or "global",
        })
    except Exception:
        pass

    return {
        "query": query,
        "jurisdiction": jurisdiction,
        "country": country,
        "total_count": total,
        "page": data.get("results", {}).get("page", 1),
        "per_page": per_page,
        "total_pages": data.get("results", {}).get("total_pages", 0),
        "companies": [_parse_company(c.get("company", {})) for c in companies],
    }


def search_officers(query, jurisdiction=None, per_page=30, page=1):
    """Search officers/directors by name."""
    params = {"q": query, "per_page": per_page, "page": page, "order": "score"}
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction

    data = api_request("officers/search", params)
    officers = data.get("results", {}).get("officers", [])
    total = data.get("results", {}).get("total_count", 0)

    try:
        log_search("opencorporates_officers", query, {
            "result_count": total,
            "jurisdiction": jurisdiction or "global",
        })
    except Exception:
        pass

    return {
        "query": query,
        "jurisdiction": jurisdiction,
        "total_count": total,
        "page": data.get("results", {}).get("page", 1),
        "per_page": per_page,
        "total_pages": data.get("results", {}).get("total_pages", 0),
        "officers": [_parse_officer(o.get("officer", {})) for o in officers],
    }


def search_by_address(address, jurisdiction=None, per_page=30, page=1):
    """Search companies by registered address (loose match)."""
    return search_companies("", jurisdiction=jurisdiction, address=address,
                            per_page=per_page, page=page)


# --- Entity detail commands ---

def get_company(jurisdiction, company_number, sparse=False):
    """Get full company details including officers."""
    params = {}
    if sparse:
        params["sparse"] = "true"

    data = api_request(f"companies/{jurisdiction}/{company_number}", params)
    if not data:
        return None

    company = data.get("results", {}).get("company", {})

    try:
        log_search("opencorporates", f"entity:{jurisdiction}/{company_number}", {"found": True})
    except Exception:
        pass

    result = _parse_company(company)
    result["previous_names"] = company.get("previous_names", [])
    result["alternative_names"] = company.get("alternative_names", [])
    result["industry_codes"] = company.get("industry_codes", [])

    # Parse officers
    raw_officers = company.get("officers", [])
    result["officers"] = []
    for item in raw_officers:
        off = item.get("officer", {})
        result["officers"].append({
            "name": off.get("name"),
            "position": off.get("position"),
            "start_date": off.get("start_date"),
            "end_date": off.get("end_date"),
            "address": off.get("address"),
            "uid": off.get("uid"),
        })

    result["filings_count"] = len(company.get("filings", []))
    return result


def get_filings(jurisdiction, company_number, per_page=100, page=1):
    """Get company filings."""
    params = {"per_page": per_page, "page": page}
    data = api_request(f"companies/{jurisdiction}/{company_number}/filings", params)
    if not data:
        return None

    filings = data.get("results", {}).get("filings", [])

    try:
        log_search("opencorporates", f"filings:{jurisdiction}/{company_number}",
                    {"result_count": len(filings)})
    except Exception:
        pass

    return {
        "jurisdiction": jurisdiction,
        "company_number": company_number,
        "total_count": data.get("results", {}).get("total_count", 0),
        "filings": [{
            "title": f.get("filing", {}).get("title"),
            "filing_type": f.get("filing", {}).get("filing_type"),
            "date": f.get("filing", {}).get("date"),
            "description": f.get("filing", {}).get("description"),
            "url": f.get("filing", {}).get("url"),
        } for f in filings],
    }


def get_statements(jurisdiction, company_number):
    """Get control statements (beneficial ownership) for a company."""
    data = api_request(f"companies/{jurisdiction}/{company_number}/statements")
    if not data:
        return None

    statements = data.get("results", {}).get("statements", [])
    return {
        "jurisdiction": jurisdiction,
        "company_number": company_number,
        "total_count": len(statements),
        "statements": statements,
    }


def get_account_status():
    """Check API usage and rate limit status."""
    data = api_request("account_status")
    if not data:
        return None
    return data.get("results", {}).get("account_status", data)


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(
        description="OpenCorporates API — global corporate registry search"
    )
    sub = parser.add_subparsers(dest="command")

    # search
    sp = sub.add_parser("search", help="Search companies by name")
    sp.add_argument("query", help="Company name")
    sp.add_argument("--jurisdiction", "-j", help="Jurisdiction code (e.g. us_tx, gb, de)")
    sp.add_argument("--country", help="Country code (e.g. us, gb)")
    sp.add_argument("--inactive", action="store_true", help="Include inactive")
    sp.add_argument("--per-page", type=int, default=30)
    sp.add_argument("--page", type=int, default=1)
    add_output_args(sp)

    # officers
    op = sub.add_parser("officers", help="Search officers/directors by name")
    op.add_argument("query", help="Officer name")
    op.add_argument("--jurisdiction", "-j", help="Jurisdiction code")
    op.add_argument("--per-page", type=int, default=30)
    op.add_argument("--page", type=int, default=1)
    add_output_args(op)

    # address
    ap = sub.add_parser("address", help="Search companies by registered address")
    ap.add_argument("address", help="Address (loose match)")
    ap.add_argument("--jurisdiction", "-j", help="Jurisdiction code")
    ap.add_argument("--per-page", type=int, default=30)
    ap.add_argument("--page", type=int, default=1)
    add_output_args(ap)

    # entity
    ep = sub.add_parser("entity", help="Get company details")
    ep.add_argument("jurisdiction", help="Jurisdiction code (e.g. us_tx)")
    ep.add_argument("company_number", help="Company number")
    ep.add_argument("--sparse", action="store_true", help="Reduced response")
    add_output_args(ep)

    # filings
    fp = sub.add_parser("filings", help="Get company filings")
    fp.add_argument("jurisdiction", help="Jurisdiction code")
    fp.add_argument("company_number", help="Company number")
    fp.add_argument("--per-page", type=int, default=100)
    fp.add_argument("--page", type=int, default=1)
    add_output_args(fp)

    # statements
    stp = sub.add_parser("statements", help="Get control/ownership statements")
    stp.add_argument("jurisdiction", help="Jurisdiction code")
    stp.add_argument("company_number", help="Company number")
    add_output_args(stp)

    # account status
    sub.add_parser("account-status", help="Check API usage")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    output_file = getattr(args, "output", None)

    if args.command == "search":
        results = search_companies(
            args.query, jurisdiction=args.jurisdiction, country=args.country,
            inactive=args.inactive, per_page=args.per_page, page=args.page,
        )
        if not write_output(results, args, summary=f"company search '{args.query}': {results['total_count']} results"):
            print(f"\nFound {results['total_count']} companies")
            for c in results["companies"]:
                status = f" [{c['current_status']}]" if c.get("current_status") else ""
                print(f"\n  {c['name']} ({c['jurisdiction_code']} / {c['company_number']}){status}")
                if c.get("incorporation_date"):
                    print(f"    Incorporated: {c['incorporation_date']}")
                if c.get("registered_address"):
                    print(f"    Address: {c['registered_address']}")
                if c.get("agent_name"):
                    print(f"    Agent: {c['agent_name']}")

    elif args.command == "officers":
        results = search_officers(
            args.query, jurisdiction=args.jurisdiction,
            per_page=args.per_page, page=args.page,
        )
        if not write_output(results, args, summary=f"officer search '{args.query}': {results['total_count']} results"):
            print(f"\nFound {results['total_count']} officers")
            for o in results["officers"]:
                end = f" (ended {o['end_date']})" if o.get("end_date") else ""
                print(f"\n  {o['name']} — {o.get('position', '?')} at {o.get('company_name', '?')}{end}")
                print(f"    {o.get('jurisdiction_code', '?')} / {o.get('company_number', '?')}")
                if o.get("address"):
                    print(f"    Address: {o['address']}")

    elif args.command == "address":
        results = search_by_address(
            args.address, jurisdiction=args.jurisdiction,
            per_page=args.per_page, page=args.page,
        )
        if not write_output(results, args, summary=f"address search '{args.address}': {results['total_count']} results"):
            print(f"\nFound {results['total_count']} companies at '{args.address}'")
            for c in results["companies"]:
                print(f"\n  {c['name']} ({c['jurisdiction_code']} / {c['company_number']})")
                if c.get("registered_address"):
                    print(f"    Address: {c['registered_address']}")

    elif args.command == "entity":
        result = get_company(args.jurisdiction, args.company_number, sparse=args.sparse)
        if not result:
            print("Not found", file=sys.stderr)
            sys.exit(1)
        if not write_output(result, args, summary=f"entity {args.jurisdiction}/{args.company_number}"):
            print(f"\n{result['name']}")
            print(f"  Number: {result['company_number']}")
            print(f"  Type: {result.get('company_type')}")
            print(f"  Status: {result.get('current_status')}")
            print(f"  Incorporated: {result.get('incorporation_date')}")
            if result.get("registered_address"):
                print(f"  Address: {result['registered_address']}")
            if result.get("agent_name"):
                print(f"  Agent: {result['agent_name']}")
            if result.get("officers"):
                print(f"  Officers ({len(result['officers'])}):")
                for o in result["officers"]:
                    print(f"    {o['name']} — {o.get('position', '?')}")
            if result.get("previous_names"):
                print(f"  Previous names: {[n.get('company_name') for n in result['previous_names']]}")

    elif args.command == "filings":
        result = get_filings(args.jurisdiction, args.company_number,
                             per_page=args.per_page, page=args.page)
        if not result:
            print("Not found", file=sys.stderr)
            sys.exit(1)
        if not write_output(result, args, summary=f"filings: {result['total_count']} results"):
            print(f"\nFound {result['total_count']} filings")
            for f in result["filings"]:
                print(f"  {f['date']}: {f['title']}")

    elif args.command == "statements":
        result = get_statements(args.jurisdiction, args.company_number)
        if not result:
            print("Not found or no statements", file=sys.stderr)
            sys.exit(1)
        if not write_output(result, args, summary=f"statements: {result['total_count']}"):
            print(f"\n{result['total_count']} control statements")
            print(json.dumps(result["statements"], indent=2))

    elif args.command == "account-status":
        result = get_account_status()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
