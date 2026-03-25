#!/usr/bin/env python3
"""
Hong Kong corporate registry via OpenCorporates API (ICRIS data).

Requires OPENCORPORATES_API_KEY environment variable.
Free research API keys: https://opencorporates.com/api_accounts/new
Paid plans start at £2,250/year.

Usage:
    python tools/query_hongkong.py search "ENTITY NAME"
    python tools/query_hongkong.py search "ENTITY NAME" --inactive
    python tools/query_hongkong.py entity <company_number>
    python tools/query_hongkong.py filings <company_number>
    python tools/query_hongkong.py batch-entities <entity1> <entity2> ...
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
JURISDICTION = "hk"  # Hong Kong
RATE_LIMIT_DELAY = 0.5  # 500ms between requests to avoid rate limits


def get_api_key():
    """Get OpenCorporates API key from environment."""
    key = os.getenv("OPENCORPORATES_API_KEY")
    if not key:
        print("ERROR: OPENCORPORATES_API_KEY environment variable not set", file=sys.stderr)
        print("Get a free research key: https://opencorporates.com/api_accounts/new", file=sys.stderr)
        print("Or set: export OPENCORPORATES_API_KEY=your_key_here", file=sys.stderr)
        sys.exit(1)
    return key


def api_request(endpoint, params=None):
    """Make API request with authentication and rate limiting."""
    api_key = get_api_key()

    if params is None:
        params = {}
    params["api_token"] = api_key

    url = f"{API_BASE}/{endpoint}"

    try:
        time.sleep(RATE_LIMIT_DELAY)  # Rate limit
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print(f"ERROR: Invalid API token. Check your OPENCORPORATES_API_KEY", file=sys.stderr)
        elif e.response.status_code == 429:
            print(f"ERROR: Rate limit exceeded. Free tier: 200/month, 50/day", file=sys.stderr)
        else:
            print(f"HTTP {e.response.status_code}: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}", file=sys.stderr)
        sys.exit(1)


def search_companies(query, inactive=False, per_page=30, page=1):
    """
    Search Hong Kong companies by name.

    Args:
        query: Company name search term
        inactive: Include inactive companies
        per_page: Results per page (max 100, default 30)
        page: Page number (max 100)

    Returns:
        List of matching companies with metadata
    """
    params = {
        "q": query,
        "jurisdiction_code": JURISDICTION,
        "per_page": per_page,
        "page": page
    }

    if inactive:
        params["inactive"] = "true"

    data = api_request("companies/search", params)

    companies = data.get("results", {}).get("companies", [])

    # Log search after we have results
    result_count = data.get("results", {}).get("total_count", 0)
    try:
        log_search("hongkong_opencorporates", query, {"result_count": result_count})
    except Exception:
        pass  # Don't fail on logging errors

    results = []
    for item in companies:
        company = item.get("company", {})
        results.append({
            "name": company.get("name"),
            "company_number": company.get("company_number"),
            "jurisdiction_code": company.get("jurisdiction_code"),
            "incorporation_date": company.get("incorporation_date"),
            "company_type": company.get("company_type"),
            "registry_url": company.get("registry_url"),
            "opencorporates_url": company.get("opencorporates_url"),
            "current_status": company.get("current_status"),
            "registered_address_in_full": company.get("registered_address_in_full"),
            "agent_name": company.get("agent_name"),
            "agent_address": company.get("agent_address")
        })

    return {
        "query": query,
        "jurisdiction": JURISDICTION,
        "total_count": data.get("results", {}).get("total_count", 0),
        "page": data.get("results", {}).get("page", 1),
        "per_page": data.get("results", {}).get("per_page", 30),
        "total_pages": data.get("results", {}).get("total_pages", 0),
        "companies": results
    }


def get_company(company_number):
    """
    Get detailed company information.

    Args:
        company_number: Hong Kong company registration number

    Returns:
        Complete company data including officers, filings, etc.
    """
    data = api_request(f"companies/{JURISDICTION}/{company_number}")

    try:
        log_search("hongkong_opencorporates", f"entity:{company_number}", {"found": True})
    except Exception:
        pass

    company = data.get("results", {}).get("company", {})

    return {
        "name": company.get("name"),
        "company_number": company.get("company_number"),
        "jurisdiction_code": company.get("jurisdiction_code"),
        "incorporation_date": company.get("incorporation_date"),
        "dissolution_date": company.get("dissolution_date"),
        "company_type": company.get("company_type"),
        "registry_url": company.get("registry_url"),
        "opencorporates_url": company.get("opencorporates_url"),
        "current_status": company.get("current_status"),
        "registered_address_in_full": company.get("registered_address_in_full"),
        "registered_address": company.get("registered_address"),
        "agent_name": company.get("agent_name"),
        "agent_address": company.get("agent_address"),
        "industry_codes": company.get("industry_codes"),
        "previous_names": company.get("previous_names"),
        "alternative_names": company.get("alternative_names"),
        "officers": company.get("officers", []),
        "filings_count": len(company.get("filings", []))
    }


def get_filings(company_number, per_page=100, page=1):
    """
    Get company filings.

    Args:
        company_number: Hong Kong company registration number
        per_page: Results per page (max 100)
        page: Page number

    Returns:
        List of filings with metadata
    """
    params = {
        "per_page": per_page,
        "page": page
    }

    data = api_request(f"companies/{JURISDICTION}/{company_number}/filings", params)

    try:
        result_count = data.get("results", {}).get("total_count", 0)
        log_search("hongkong_opencorporates", f"filings:{company_number}", {"result_count": result_count})
    except Exception:
        pass

    filings = data.get("results", {}).get("filings", [])

    results = []
    for item in filings:
        filing = item.get("filing", {})
        results.append({
            "title": filing.get("title"),
            "filing_type": filing.get("filing_type"),
            "date": filing.get("date"),
            "description": filing.get("description"),
            "uid": filing.get("uid"),
            "url": filing.get("url"),
            "opencorporates_url": filing.get("opencorporates_url")
        })

    return {
        "company_number": company_number,
        "jurisdiction": JURISDICTION,
        "total_count": data.get("results", {}).get("total_count", 0),
        "page": data.get("results", {}).get("page", 1),
        "per_page": data.get("results", {}).get("per_page", 100),
        "filings": results
    }


def batch_entities(company_numbers, output_file=None):
    """
    Fetch multiple entities in batch.

    Args:
        company_numbers: List of Hong Kong company numbers
        output_file: Optional output file path

    Returns:
        Dict mapping company_number -> company data
    """
    results = {}

    for i, number in enumerate(company_numbers, 1):
        print(f"Fetching {i}/{len(company_numbers)}: {number}", file=sys.stderr)
        try:
            results[number] = get_company(number)
        except Exception as e:
            print(f"Error fetching {number}: {e}", file=sys.stderr)
            results[number] = {"error": str(e)}

    if output_file:
        write_output(results, output_file)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Hong Kong corporate registry via OpenCorporates API (ICRIS)"
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # Search
    search_p = sub.add_parser("search", help="Search Hong Kong companies by name")
    search_p.add_argument("query", help="Company name")
    search_p.add_argument("--inactive", action="store_true", help="Include inactive companies")
    search_p.add_argument("--per-page", type=int, default=30, help="Results per page (max 100)")
    search_p.add_argument("--page", type=int, default=1, help="Page number")
    add_output_args(search_p)

    # Entity detail
    entity_p = sub.add_parser("entity", help="Get company details by number")
    entity_p.add_argument("company_number", help="Hong Kong company registration number")
    add_output_args(entity_p)

    # Filings
    filings_p = sub.add_parser("filings", help="Get company filings")
    filings_p.add_argument("company_number", help="Hong Kong company registration number")
    filings_p.add_argument("--per-page", type=int, default=100, help="Results per page (max 100)")
    filings_p.add_argument("--page", type=int, default=1, help="Page number")
    add_output_args(filings_p)

    # Batch
    batch_p = sub.add_parser("batch-entities", help="Fetch multiple entities")
    batch_p.add_argument("company_numbers", nargs="+", help="Company numbers to fetch")
    add_output_args(batch_p)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    output_file = getattr(args, 'output', None)

    if args.command == "search":
        results = search_companies(
            args.query,
            inactive=args.inactive,
            per_page=args.per_page,
            page=args.page
        )
        write_output(results, output_file)
        if not output_file:
            print(f"\nFound {results['total_count']} companies")
            for company in results['companies']:
                print(f"\n{company['name']} ({company['company_number']})")
                print(f"  Type: {company['company_type']}")
                print(f"  Incorporated: {company['incorporation_date']}")
                if company['current_status']:
                    print(f"  Status: {company['current_status']}")
                if company['agent_name']:
                    print(f"  Agent: {company['agent_name']}")

    elif args.command == "entity":
        results = get_company(args.company_number)
        write_output(results, output_file)
        if not output_file:
            print(f"\n{results['name']}")
            print(f"Number: {results['company_number']}")
            print(f"Type: {results['company_type']}")
            print(f"Incorporated: {results['incorporation_date']}")
            if results['current_status']:
                print(f"Status: {results['current_status']}")
            if results['agent_name']:
                print(f"Agent: {results['agent_name']}")
            print(f"Officers: {len(results.get('officers', []))}")
            print(f"Filings: {results.get('filings_count', 0)}")

    elif args.command == "filings":
        results = get_filings(
            args.company_number,
            per_page=args.per_page,
            page=args.page
        )
        write_output(results, output_file)
        if not output_file:
            print(f"\nFound {results['total_count']} filings")
            for filing in results['filings']:
                print(f"\n{filing['date']}: {filing['title']}")
                if filing['filing_type']:
                    print(f"  Type: {filing['filing_type']}")

    elif args.command == "batch-entities":
        results = batch_entities(args.company_numbers, output_file)
        if not output_file:
            print(f"\nFetched {len(results)} entities")


if __name__ == "__main__":
    main()
