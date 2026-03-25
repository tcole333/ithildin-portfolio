#!/usr/bin/env python3
"""
Israeli Corporations Authority (Rasham HaChavarot) query tool.

Searches Israel's corporate registry via data.gov.il CKAN API.
720K+ companies with Hebrew and English names.

Usage:
    python tools/query_israel.py search "Carbyne"
    python tools/query_israel.py search "Ehud Barak" --limit 50
    python tools/query_israel.py company 515106409
    python tools/query_israel.py stats
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

# CKAN API on data.gov.il
BASE_URL = "https://data.gov.il/api/3/action/datastore_search"
RESOURCE_ID = "f004176c-b85f-4542-8901-7b3176f9a054"

# Field mapping (Hebrew → English)
FIELD_MAP = {
    "מספר חברה": "company_number",
    "שם חברה": "name_hebrew",
    "שם באנגלית": "name_english",
    "סוג תאגיד": "entity_type",
    "סטטוס חברה": "status",
    "תאור חברה": "description",
    "מטרת החברה": "purpose",
    "תאריך התאגדות": "incorporation_date",
    "חברה ממשלתית": "government_company",
    "מגבלות": "limitations",
    "מפרה": "violator",
    "שנה אחרונה של דוח שנתי": "last_annual_report_year",
    "שם עיר": "city",
    "שם רחוב": "street",
    "מספר בית": "house_number",
    "מיקוד": "postal_code",
    "ת.ד.": "po_box",
    "מדינה": "country",
    "אצל": "care_of",
    "תת סטטוס": "sub_status",
    "קוד סטטוס חברה": "status_code",
    "קוד סוג חברה": "entity_type_code",
    "קוד סיווג חברה": "classification_code",
    "קוד מטרת החברה": "purpose_code",
    "קוד מגבלה": "limitation_code",
    "קוד חברה מפרה": "violator_code",
    "קוד ישוב": "settlement_code",
    "קוד רחוב": "street_code",
}


def ckan_request(params, max_retries=3):
    """Make CKAN API request with retry logic."""
    params["resource_id"] = RESOURCE_ID
    url = f"{BASE_URL}?{urlencode(params)}"

    for attempt in range(max_retries):
        try:
            req = Request(url)
            req.add_header("User-Agent", "OSINT-Research/1.0")
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("success"):
                    raise ValueError(f"API returned success=false: {data}")
                return data["result"]
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def normalize_record(record):
    """Convert Hebrew field names to English and clean up."""
    normalized = {}
    for hebrew, english in FIELD_MAP.items():
        if hebrew in record:
            normalized[english] = record[hebrew]

    # Add full address
    parts = []
    if normalized.get("street"):
        parts.append(normalized["street"])
    if normalized.get("house_number"):
        parts.append(str(normalized["house_number"]))
    if normalized.get("city"):
        parts.append(normalized["city"])
    if normalized.get("postal_code"):
        parts.append(str(normalized["postal_code"]))
    normalized["full_address"] = ", ".join(parts) if parts else None

    return normalized


def search_companies(query, limit=100, offset=0):
    """Search companies by name (Hebrew or English)."""
    params = {
        "q": query,
        "limit": limit,
        "offset": offset,
    }

    result = ckan_request(params)
    records = [normalize_record(r) for r in result.get("records", [])]

    log_search(
        query_text=query,
        source="israel_registry",
        result_count=len(records),
    )

    return {
        "total": result.get("total", 0),
        "records": records,
        "query": query,
        "limit": limit,
        "offset": offset,
    }


def get_company(company_number):
    """Get specific company by registration number."""
    params = {
        "filters": json.dumps({"מספר חברה": str(company_number)}),
        "limit": 1,
    }

    result = ckan_request(params)
    records = result.get("records", [])

    if not records:
        return None

    normalized = normalize_record(records[0])

    log_search(
        query_text=f"company:{company_number}",
        source="israel_registry",
        result_count=1,
    )

    return normalized


def get_stats():
    """Get registry statistics."""
    # Get total count
    result = ckan_request({"limit": 1})
    total = result.get("total", 0)

    # Sample recent companies
    recent = ckan_request({"limit": 5, "sort": "תאריך התאגדות desc"})
    recent_records = [normalize_record(r) for r in recent.get("records", [])]

    return {
        "total_companies": total,
        "recent_incorporations": recent_records,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Query Israeli Corporations Authority registry"
    )
    add_output_args(parser)

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # Search
    search_parser = sub.add_parser("search", help="Search companies by name")
    search_parser.add_argument("query", help="Search term (Hebrew or English)")
    search_parser.add_argument(
        "-l", "--limit", type=int, default=100, help="Max results (default 100)"
    )
    search_parser.add_argument(
        "--offset", type=int, default=0, help="Result offset (default 0)"
    )
    add_output_args(search_parser)

    # Company
    company_parser = sub.add_parser("company", help="Get company by registration number")
    company_parser.add_argument("number", help="Company registration number")
    add_output_args(company_parser)

    # Stats
    stats_parser = sub.add_parser("stats", help="Get registry statistics")
    add_output_args(stats_parser)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Execute command
    if args.command == "search":
        results = search_companies(args.query, args.limit, args.offset)

        if not write_output(results, args, summary=f"Israel registry search '{args.query}'"):
            if getattr(args, "json_out", False):
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                print(f"\n{results['total']} total companies matching '{args.query}'")
                print(f"Showing {len(results['records'])} results (offset {args.offset}):\n")

                for rec in results["records"]:
                    print(f"Company #{rec.get('company_number')}")
                    print(f"  Name (Hebrew): {rec.get('name_hebrew')}")
                    print(f"  Name (English): {rec.get('name_english')}")
                    print(f"  Type: {rec.get('entity_type')} (code {rec.get('entity_type_code')})")
                    print(f"  Status: {rec.get('status')} (code {rec.get('status_code')})")
                    print(f"  Incorporated: {rec.get('incorporation_date')}")
                    if rec.get('full_address'):
                        print(f"  Address: {rec['full_address']}")
                    if rec.get('purpose'):
                        print(f"  Purpose: {rec['purpose']}")
                    print()

    elif args.command == "company":
        company = get_company(args.number)

        if not company:
            print(f"No company found with number {args.number}", file=sys.stderr)
            sys.exit(1)

        if not write_output(company, args, summary=f"Israel company {args.number}"):
            if getattr(args, "json_out", False):
                print(json.dumps(company, indent=2, ensure_ascii=False))
            else:
                print(f"\nCompany #{company.get('company_number')}")
                print(f"Name (Hebrew): {company.get('name_hebrew')}")
                print(f"Name (English): {company.get('name_english')}")
                print(f"Type: {company.get('entity_type')} (code {company.get('entity_type_code')})")
                print(f"Status: {company.get('status')} (code {company.get('status_code')})")
                print(f"Incorporated: {company.get('incorporation_date')}")
                print(f"Last Annual Report: {company.get('last_annual_report_year')}")
                if company.get('full_address'):
                    print(f"Address: {company['full_address']}")
                if company.get('purpose'):
                    print(f"Purpose: {company['purpose']}")
                if company.get('description'):
                    print(f"Description: {company['description']}")
                if company.get('government_company'):
                    print(f"Government Company: {company['government_company']}")
                print()

    elif args.command == "stats":
        stats = get_stats()

        if not write_output(stats, args, summary="Israel registry stats"):
            if getattr(args, "json_out", False):
                print(json.dumps(stats, indent=2, ensure_ascii=False))
            else:
                print(f"\nIsraeli Corporations Authority Statistics")
                print(f"Total companies: {stats['total_companies']:,}")
                print(f"\nRecent incorporations:")
                for rec in stats.get("recent_incorporations", []):
                    print(f"  {rec.get('incorporation_date')}: {rec.get('name_english')} ({rec.get('company_number')})")
                print()


if __name__ == "__main__":
    main()
