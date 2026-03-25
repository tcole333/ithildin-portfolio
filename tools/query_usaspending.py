#!/usr/bin/env python3
"""
USAspending.gov API wrapper for government contract research.

Searches for federal award spending, recipient profiles, and entity hierarchies
to identify wealth flows from the US Treasury to investigation targets.

API: https://api.usaspending.gov/
Auth: None required for public API.
Rate Limits: 10 requests per second (approx).

Usage:
    uv run python tools/query_usaspending.py search "Palantir"
    uv run python tools/query_usaspending.py recipient "Palantir Technologies"
    uv run python tools/query_usaspending.py awards "PALANTIR TECHNOLOGIES INC." --limit 10
    uv run python tools/query_usaspending.py uei "RN99S3S7N977"
"""

import argparse
import json
import os
import ssl
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Global SSL context — use certifi bundle (system store may be stale)
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
if os.environ.get("OSINT_INSECURE_SSL") == "true" or os.environ.get("PYTHONHTTPSVERIFY") == "0":
    SSL_CONTEXT.check_hostname = False
    SSL_CONTEXT.verify_mode = ssl.CERT_NONE

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://api.usaspending.gov/api/v2"

# Award types for contracts (A: B: C: D: )
CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]
# Other award types if needed (Grants: 02, 03, 04, 05; Loans: 07, 08; Insurance: 09; Direct Payments: 10, 11)
GRANT_AWARD_TYPES = ["02", "03", "04", "05"]
LOAN_AWARD_TYPES = ["07", "08"]
IDV_AWARD_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]

# COVID-19 Disaster Emergency Fund Codes (DEFC)
COVID_DEFC = ["L", "M", "N", "O", "P", "U", "V"]

def _fetch_post(endpoint, data):
    """Fetch from USAspending API using POST."""
    url = f"{BASE_URL}{endpoint}"
    req = Request(url, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })
    
    try:
        with urlopen(req, data=json.dumps(data).encode(), timeout=60, context=SSL_CONTEXT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None

def _fetch_get(endpoint, params=None):
    """Fetch from USAspending API using GET."""
    query = f"?{urlencode(params)}" if params else ""
    url = f"{BASE_URL}{endpoint}{query}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })
    
    try:
        with urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None

def cmd_search(args):
    """Search for recipients using the autocomplete endpoint."""
    data = {"search_text": args.query}
    result = _fetch_post("/autocomplete/recipient/", data)
    
    if not result:
        return

    results = result.get("results", [])
    
    if write_output(results, args, summary=f"USAspending search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"Found {len(results)} recipient matches for '{args.query}':")
    for r in results:
        uei = r.get("uei") or "N/A"
        duns = r.get("duns") or "N/A"
        print(f"  {r.get('recipient_name')} (UEI: {uei}, DUNS: {duns})")

def cmd_awards(args):
    """Search for specific awards by recipient name or UEI."""
    # Group types: contracts (A, B, C, D), grants (02, 03, 04, 05), loans (07, 08), insurance (09), direct payments (10, 11)
    # The API throws a 422 if you mix these groups.
    if args.grants:
        award_types = GRANT_AWARD_TYPES
    else:
        award_types = CONTRACT_AWARD_TYPES

    filters = {
        "award_type_codes": award_types
    }
    
    if args.uei:
        filters["recipient_search_text"] = [args.uei]
    else:
        filters["recipient_search_text"] = [args.query]

    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]

    data = {
        "filters": filters,
        "fields": [
            "Award ID", "Recipient Name", "Start Date", "End Date", 
            "Award Amount", "Description", "Awarding Agency", 
            "Awarding Sub Agency", "Contract Award Type"
        ],
        "limit": args.limit,
        "page": args.page
    }
    
    result = _fetch_post("/search/spending_by_award/", data)
    if not result:
        return

    results = result.get("results", [])
    
    if write_output(results, args, summary=f"USAspending awards for '{args.query or args.uei}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    print(f"Found {len(results)} awards (Limit: {args.limit}):")
    for r in results:
        amt = f"${r.get('Award Amount', 0):,.2f}"
        print(f"  {r.get('Award ID')} | {amt} | {r.get('Recipient Name')}")
        print(f"    Agency: {r.get('Awarding Agency')} / {r.get('Awarding Sub Agency')}")
        print(f"    Dates: {r.get('Start Date')} to {r.get('End Date')}")
        print(f"    Desc: {r.get('Description')[:100]}...")
        print()

def cmd_recipient(args):
    """Get detailed recipient profile and children/parent info."""
    # First find the recipient to get the hash (required for profile endpoint)
    search_data = {"search_text": args.query}
    search_result = _fetch_post("/autocomplete/recipient/", search_data)
    
    if not search_result or not search_result.get("results"):
        print(f"No recipient found matching '{args.query}'")
        return

    # Use the first match
    recipient = search_result["results"][0]
    # The profile API often needs a hash, but sometimes works with UEI
    # Let's try to get more details via search/recipient
    
    recipient_name = recipient.get("recipient_name")
    uei = recipient.get("uei")
    print(f"Recipient: {recipient_name}")
    print(f"UEI: {uei}")
    print("-" * 40)
    
    # Get spending over time for this recipient
    spending_data = {
        "filters": {
            "recipient_search_text": [uei if uei else recipient_name],
            "award_type_codes": CONTRACT_AWARD_TYPES
        }
    }
    
    # Spending by agency
    agency_spending = _fetch_post("/search/spending_by_category/awarding_agency/", spending_data)
    if agency_spending and agency_spending.get("results"):
        print("Spending by Agency:")
        for r in agency_spending["results"][:5]:
            print(f"  {r.get('name')}: ${r.get('amount', 0):,.2f}")
    
    print("\nUse 'awards' command to see individual contract details.")

def cmd_covid(args):
    """Search for COVID-19 relief awards (PPP, EIDL, etc) using DEFC.
    Searches multiple groups (contracts, grants, loans) separately to avoid API 422.
    """
    groups = {
        "Contracts": CONTRACT_AWARD_TYPES,
        "Grants": GRANT_AWARD_TYPES,
        "Loans": LOAN_AWARD_TYPES,
        "IDVs": IDV_AWARD_TYPES
    }
    
    all_results = []
    
    for group_name, types in groups.items():
        filters = {
            "def_codes": COVID_DEFC,
            "recipient_search_text": [args.query],
            "award_type_codes": types
        }
        
        data = {
            "filters": filters,
            "fields": [
                "Award ID", "Recipient Name", "Start Date", "End Date", 
                "Award Amount", "Description", "Awarding Agency", 
                "Awarding Sub Agency", "Contract Award Type"
            ],
            "limit": args.limit
        }
        
        result = _fetch_post("/search/spending_by_award/", data)
        if result and result.get("results"):
            all_results.extend(result.get("results"))
            if len(all_results) >= args.limit:
                break

    results = all_results[:args.limit]
    if write_output(results, args, summary=f"USAspending COVID awards for '{args.query}'"):
        return
    
    if not results:
        print(f"No COVID-19 relief awards found for '{args.query}'")
        return

    print(f"Found {len(results)} COVID-19 relief awards:")
    for r in results:
        amount_val = r.get('Award Amount')
        if amount_val is None: amount_val = 0
        amt = f"${float(amount_val):,.2f}"
        print(f"  {r.get('Award ID')} | {amt} | {r.get('Recipient Name')}")
        print(f"    Agency: {r.get('Awarding Agency')}")
        desc = r.get('Description') or "No description"
        print(f"    Desc: {desc[:100]}...")
        print()

def cmd_loans(args):
    """Search specifically for loan awards (including PPP/EIDL)."""
    filters = {
        "award_type_codes": LOAN_AWARD_TYPES,
        "recipient_search_text": [args.query]
    }
    
    data = {
        "filters": filters,
        "fields": [
            "Award ID", "Recipient Name", "Start Date", "End Date", 
            "Award Amount", "Description", "Awarding Agency", 
            "Awarding Sub Agency"
        ],
        "limit": args.limit
    }
    
    result = _fetch_post("/search/spending_by_award/", data)
    if not result:
        return

    results = result.get("results", [])
    if write_output(results, args, summary=f"USAspending loans for '{args.query}'"):
        return
    
    print(f"Found {len(results)} loan awards:")
    for r in results:
        amount_val = r.get('Award Amount')
        if amount_val is None: amount_val = 0
        amt = f"${float(amount_val):,.2f}"
        print(f"  {r.get('Award ID')} | {amt} | {r.get('Recipient Name')}")
        print(f"    Agency: {r.get('Awarding Agency')}")
        print()

def _fmt_money(val):
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if abs(v) >= 1_000_000_000:
            return f"${v/1e9:.1f}B"
        if abs(v) >= 1_000_000:
            return f"${v/1e6:.1f}M"
        if abs(v) >= 1_000:
            return f"${v/1e3:.0f}K"
        return f"${v:,.2f}"
    except (ValueError, TypeError):
        return str(val)


def cmd_award_detail(args):
    """Get full detail for a specific award by generated_unique_award_id or internal ID."""
    result = _fetch_get(f"/awards/{args.award_id}/")
    if not result:
        return

    if write_output(result, args, summary=f"USAspending award detail {args.award_id}"):
        return

    print(f"Award: {result.get('generated_unique_award_id', 'N/A')}")
    print(f"  Type: {result.get('type_description', '?')}")
    print(f"  Recipient: {result.get('recipient', {}).get('recipient_name', '?')}")
    print(f"  UEI: {result.get('recipient', {}).get('recipient_uei', 'N/A')}")
    print(f"  Total obligation: {_fmt_money(result.get('total_obligation'))}")
    print(f"  Base + exercised options: {_fmt_money(result.get('base_exercised_options'))}")
    print(f"  Base + all options: {_fmt_money(result.get('base_and_all_options_value'))}")
    print(f"  Period: {result.get('period_of_performance_start_date', '?')} to {result.get('period_of_performance_current_end_date', '?')}")
    print(f"  Agency: {result.get('awarding_agency', {}).get('toptier_agency', {}).get('name', '?')}")
    print(f"  Sub-agency: {result.get('awarding_agency', {}).get('subtier_agency', {}).get('name', '?')}")
    desc = result.get('description', '')
    if desc:
        print(f"  Description: {desc[:200]}")

    # Subawards count
    subaward_count = result.get('subaward_count', 0)
    if subaward_count:
        print(f"\n  Subawards: {subaward_count} (total {_fmt_money(result.get('total_subaward_amount'))})")

    # Parent award (for IDVs)
    parent = result.get('parent_award', {})
    if parent and parent.get('generated_unique_award_id'):
        print(f"\n  Parent Award: {parent.get('generated_unique_award_id')}")
        print(f"    Agency: {parent.get('agency_name', '?')}")


def cmd_subawards(args):
    """Search subaward/subcontractor data."""
    filters = {}

    if args.uei:
        filters["recipient_search_text"] = [args.uei]
    elif args.query:
        filters["recipient_search_text"] = [args.query]

    if args.award_id:
        filters["award_id"] = args.award_id

    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]

    award_types = GRANT_AWARD_TYPES if args.grants else CONTRACT_AWARD_TYPES
    filters["award_type_codes"] = award_types

    data = {
        "filters": filters,
        "limit": args.limit,
        "page": args.page,
    }

    result = _fetch_post("/subawards/", data)
    if not result:
        return

    results = result.get("results", [])
    total = result.get("page_metadata", {}).get("total", len(results))

    if write_output(results, args, summary=f"USAspending subawards ({total} total)"):
        return

    print(f"Found {total} subawards (showing {len(results)}):")
    for r in results:
        sub_name = r.get("recipient_name", "?")
        amount = r.get("amount", 0)
        date = r.get("action_date", "?")
        desc = r.get("description", "")
        prime_award = r.get("prime_award_generated_internal_id", "")

        print(f"\n  {sub_name} | {_fmt_money(amount)} | {date}")
        if prime_award:
            print(f"    Prime Award: {prime_award}")
        if desc:
            print(f"    Desc: {desc[:120]}")

    print()


def cmd_transactions(args):
    """Search individual transaction records."""
    filters = {}

    if args.uei:
        filters["recipient_search_text"] = [args.uei]
    elif args.query:
        filters["recipient_search_text"] = [args.query]

    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]

    if args.date_range:
        parts = args.date_range.split(",")
        if len(parts) == 2:
            filters["time_period"] = [{"start_date": parts[0], "end_date": parts[1]}]

    award_types = GRANT_AWARD_TYPES if args.grants else CONTRACT_AWARD_TYPES
    filters["award_type_codes"] = award_types

    data = {
        "filters": filters,
        "fields": [
            "Award ID", "Recipient Name", "Action Date", "Federal Action Obligation",
            "Awarding Agency", "Awarding Sub Agency", "Award Type", "Description"
        ],
        "limit": args.limit,
        "page": args.page,
        "sort": "Federal Action Obligation",
        "order": "desc"
    }

    result = _fetch_post("/search/spending_by_transaction/", data)
    if not result:
        return

    results = result.get("results", [])
    total = result.get("page_metadata", {}).get("total", len(results))

    if write_output(results, args, summary=f"USAspending transactions ({total} total)"):
        return

    print(f"Found {total} transactions (showing {len(results)}):")
    for r in results:
        award_id = r.get("Award ID", "?")
        name = r.get("Recipient Name", "?")
        amount = r.get("Federal Action Obligation", 0)
        date = r.get("Action Date", "?")
        agency = r.get("Awarding Agency", "?")
        desc = r.get("Description", "")

        print(f"\n  {award_id} | {_fmt_money(amount)} | {date}")
        print(f"    Recipient: {name}")
        print(f"    Agency: {agency}")
        if desc:
            print(f"    Desc: {desc[:120]}")

    print()


def cmd_spending_by_geography(args):
    """Analyze spending patterns by geographic area."""
    filters = {}

    if args.query:
        filters["recipient_search_text"] = [args.query]
    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]
    if args.date_range:
        parts = args.date_range.split(",")
        if len(parts) == 2:
            filters["time_period"] = [{"start_date": parts[0], "end_date": parts[1]}]

    award_types = GRANT_AWARD_TYPES if args.grants else CONTRACT_AWARD_TYPES
    filters["award_type_codes"] = award_types

    data = {
        "scope": args.scope,  # place_of_performance or recipient_location
        "geo_layer": args.geo_layer,  # state, county, or district
        "filters": filters,
    }

    result = _fetch_post("/search/spending_by_geography/", data)
    if not result:
        return

    results = result.get("results", [])

    if write_output(results, args, summary=f"USAspending geographic spending ({len(results)} regions)"):
        return

    # Sort by amount descending
    results.sort(key=lambda x: x.get("aggregated_amount", 0), reverse=True)

    print(f"Spending by {args.geo_layer} ({args.scope}):")
    for r in results[:args.limit]:
        name = r.get("display_name", r.get("shape_code", "?"))
        amount = r.get("aggregated_amount", 0)
        count = r.get("per_capita", "")
        if amount > 0:
            print(f"  {name}: {_fmt_money(amount)}")

    print()


def cmd_spending_over_time(args):
    """Analyze spending trends over time."""
    filters = {}

    if args.query:
        filters["recipient_search_text"] = [args.query]
    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]

    award_types = GRANT_AWARD_TYPES if args.grants else CONTRACT_AWARD_TYPES
    filters["award_type_codes"] = award_types

    data = {
        "group": args.group,  # fiscal_year, quarter, month
        "filters": filters,
    }

    result = _fetch_post("/search/spending_over_time/", data)
    if not result:
        return

    results = result.get("results", [])

    if write_output(results, args, summary=f"USAspending spending over time ({len(results)} periods)"):
        return

    print(f"Spending by {args.group}:")
    for r in results:
        fy = r.get("time_period", {})
        period = fy.get("fiscal_year", "")
        if "quarter" in fy:
            period += f" Q{fy['quarter']}"
        if "month" in fy:
            period += f" M{fy['month']}"
        amount = r.get("aggregated_amount", 0)
        if amount != 0:
            print(f"  {period}: {_fmt_money(amount)}")

    print()


def cmd_top_recipients(args):
    """Find top recipients by spending amount."""
    filters = {}

    if args.agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": args.agency}]
    if args.naics:
        filters["naics_codes"] = [{"naics_code": args.naics, "is_primary": True}]
    if args.date_range:
        parts = args.date_range.split(",")
        if len(parts) == 2:
            filters["time_period"] = [{"start_date": parts[0], "end_date": parts[1]}]

    award_types = GRANT_AWARD_TYPES if args.grants else CONTRACT_AWARD_TYPES
    filters["award_type_codes"] = award_types

    data = {
        "category": "recipient",
        "filters": filters,
        "limit": args.limit,
        "page": 1,
    }

    result = _fetch_post("/search/spending_by_category/recipient/", data)
    if not result:
        return

    results = result.get("results", [])

    if write_output(results, args, summary=f"Top {len(results)} recipients"):
        return

    print(f"Top recipients:")
    for i, r in enumerate(results, 1):
        name = r.get("name", "?")
        amount = r.get("amount", 0)
        print(f"  {i}. {name}: {_fmt_money(amount)}")

    print()


def cmd_agencies(args):
    """List top-tier federal agencies."""
    result = _fetch_get("/references/toptier_agencies/")
    if not result:
        return

    results = result.get("results", result) if isinstance(result, dict) else result

    if write_output(results, args, summary=f"USAspending toptier agencies"):
        return

    if isinstance(results, list):
        # Sort by budget
        results.sort(key=lambda x: x.get("budget_authority_amount", 0) or 0, reverse=True)
        print(f"Top agencies by budget authority:")
        for a in results[:args.limit]:
            name = a.get("agency_name", "?")
            abbr = a.get("abbreviation", "")
            budget = a.get("budget_authority_amount", 0)
            print(f"  {name} ({abbr}): {_fmt_money(budget)}")


def main():
    parser = argparse.ArgumentParser(
        description="USAspending.gov — federal spending, contracts, grants, and recipient analysis",
        epilog="No auth required. Rate limit: ~10 req/sec."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Search
    p = sub.add_parser("search", help="Search for recipients (autocomplete)")
    p.add_argument("query", help="Recipient name fragment")
    add_output_args(p)

    # Awards
    p = sub.add_parser("awards", help="Search spending by award")
    p.add_argument("query", nargs="?", help="Recipient name")
    p.add_argument("--uei", help="Recipient UEI")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--page", type=int, default=1, help="Page number")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--grants", action="store_true", help="Search grants instead of contracts")
    add_output_args(p)

    # Award detail
    p = sub.add_parser("award", help="Get full detail for a specific award")
    p.add_argument("award_id", help="Award ID (generated_unique_award_id or internal ID)")
    add_output_args(p)

    # Recipient Profile
    p = sub.add_parser("recipient", help="Get recipient summary")
    p.add_argument("query", help="Recipient name")
    add_output_args(p)

    # Subawards
    p = sub.add_parser("subawards", help="Search subaward/subcontractor data")
    p.add_argument("query", nargs="?", help="Recipient name")
    p.add_argument("--uei", help="Recipient UEI")
    p.add_argument("--award-id", help="Filter by prime award ID")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--grants", action="store_true", help="Search grant subawards")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--page", type=int, default=1, help="Page number")
    add_output_args(p)

    # Transactions
    p = sub.add_parser("transactions", help="Search individual transaction records")
    p.add_argument("query", nargs="?", help="Recipient name")
    p.add_argument("--uei", help="Recipient UEI")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--date-range", help="Date range: YYYY-MM-DD,YYYY-MM-DD")
    p.add_argument("--grants", action="store_true", help="Search grant transactions")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--page", type=int, default=1, help="Page number")
    add_output_args(p)

    # Spending by geography
    p = sub.add_parser("geography", help="Spending patterns by geographic area")
    p.add_argument("query", nargs="?", help="Recipient name filter")
    p.add_argument("--scope", choices=["place_of_performance", "recipient_location"],
                   default="recipient_location", help="Geography dimension")
    p.add_argument("--geo-layer", choices=["state", "county", "district"],
                   default="state", help="Geographic granularity")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--date-range", help="Date range: YYYY-MM-DD,YYYY-MM-DD")
    p.add_argument("--grants", action="store_true", help="Grants instead of contracts")
    p.add_argument("--limit", type=int, default=20, help="Max results to show")
    add_output_args(p)

    # Spending over time
    p = sub.add_parser("timeline", help="Spending trends over time")
    p.add_argument("query", nargs="?", help="Recipient name filter")
    p.add_argument("--group", choices=["fiscal_year", "quarter", "month"],
                   default="fiscal_year", help="Time granularity")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--grants", action="store_true", help="Grants instead of contracts")
    add_output_args(p)

    # Top recipients
    p = sub.add_parser("top-recipients", help="Top recipients by spending")
    p.add_argument("--agency", help="Filter by awarding agency name")
    p.add_argument("--naics", help="Filter by NAICS code")
    p.add_argument("--date-range", help="Date range: YYYY-MM-DD,YYYY-MM-DD")
    p.add_argument("--grants", action="store_true", help="Grants instead of contracts")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    # Agencies
    p = sub.add_parser("agencies", help="List top-tier federal agencies")
    p.add_argument("--limit", type=int, default=30, help="Max results")
    add_output_args(p)

    # COVID
    p = sub.add_parser("covid", help="Search for COVID-19 relief awards")
    p.add_argument("query", help="Recipient name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    # Loans
    p = sub.add_parser("loans", help="Search specifically for loan awards")
    p.add_argument("query", help="Recipient name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "search": cmd_search,
        "awards": cmd_awards,
        "award": cmd_award_detail,
        "recipient": cmd_recipient,
        "subawards": cmd_subawards,
        "transactions": cmd_transactions,
        "geography": cmd_spending_by_geography,
        "timeline": cmd_spending_over_time,
        "top-recipients": cmd_top_recipients,
        "agencies": cmd_agencies,
        "covid": cmd_covid,
        "loans": cmd_loans,
    }

    handlers[args.command](args)

if __name__ == "__main__":
    main()
