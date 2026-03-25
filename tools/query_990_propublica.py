#!/usr/bin/env python3
"""
ProPublica Nonprofit Explorer API — internal module.

Used by query_990.py for org metadata enrichment and filing lookups.
Can also be run directly for standalone ProPublica queries.

Importable functions:
    search_orgs(query, state, page, limit) -> list[dict]
    get_org(ein) -> dict | None  (returns full org + filings)
    get_filings(ein) -> list[dict]
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


BASE_URL = "https://projects.propublica.org/nonprofits/api/v2"


def _fetch(url):
    """Fetch JSON from URL with basic error handling."""
    req = urllib.request.Request(url, headers={"User-Agent": "OSINT-Research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason}", file=sys.stderr)
        return None


def search_orgs(query, state=None, page=0, limit=25):
    """Search nonprofit organizations by name."""
    params = {"q": query, "page": page}
    if state:
        params["state[id]"] = state
    url = f"{BASE_URL}/search.json?{urllib.parse.urlencode(params)}"
    data = _fetch(url)
    if not data:
        return []

    orgs = data.get("organizations", [])
    total = data.get("total_results", 0)
    _log(query, "990", total)
    print(f"Found {total} organizations matching '{query}' (showing page {page})")
    print()

    for org in orgs[:limit]:
        ein = org.get("ein", "?")
        name = org.get("name", "?")
        city = org.get("city", "")
        state = org.get("state", "")
        subsection = org.get("subsection_code", "")
        ntee = org.get("ntee_code", "")
        revenue = org.get("total_revenue", 0)
        print(f"  EIN: {ein}")
        print(f"  Name: {name}")
        if city or state:
            print(f"  Location: {city}, {state}")
        if subsection:
            print(f"  Type: 501(c)({subsection})")
        if revenue:
            print(f"  Revenue: ${revenue:,.0f}")
        print()

    return orgs


def get_org(ein):
    """Get detailed organization info by EIN."""
    ein_str = str(ein).replace("-", "")
    url = f"{BASE_URL}/organizations/{ein_str}.json"
    data = _fetch(url)
    if not data:
        return None

    org = data.get("organization", {})
    filings = data.get("filings_with_data", [])
    filings_no_data = data.get("filings_without_data", [])

    print(f"Organization: {org.get('name', '?')}")
    print(f"EIN: {org.get('ein', '?')}")
    print(f"Address: {org.get('address', '')}, {org.get('city', '')}, {org.get('state', '')} {org.get('zipcode', '')}")
    print(f"Type: 501(c)({org.get('subsection_code', '?')})")
    print(f"NTEE: {org.get('ntee_code', '?')}")
    print(f"Ruling Date: {org.get('ruling_date', '?')}")
    print(f"Latest Revenue: ${org.get('total_revenue', 0):,.0f}")
    print(f"Latest Assets: ${org.get('total_assets', 0):,.0f}")
    print()

    if filings:
        print(f"=== Filings with Data ({len(filings)}) ===")
        for f in filings[:10]:
            tax_period = f.get("tax_prd_yr", "?")
            form = f.get("formtype", "?")
            revenue = f.get("totrevenue", 0)
            expenses = f.get("totfuncexpns", 0)
            assets = f.get("totassetsend", 0)
            pdf = f.get("pdf_url", "")
            print(f"  {tax_period} ({form}): Revenue ${revenue:,.0f} | Expenses ${expenses:,.0f} | Assets ${assets:,.0f}")
            if pdf:
                print(f"    PDF: {pdf}")

            # Show key officers if available
            officers = f.get("officers", [])
            if officers:
                print(f"    Officers:")
                for o in officers[:10]:
                    name = o.get("name", "?")
                    title = o.get("title", "?")
                    comp = o.get("compensation", 0)
                    print(f"      {name} ({title}) — ${comp:,.0f}")
            print()

    if filings_no_data:
        print(f"=== Additional Filings ({len(filings_no_data)}) ===")
        for f in filings_no_data[:5]:
            print(f"  {f.get('tax_prd_yr', '?')} ({f.get('formtype', '?')})")
            pdf = f.get("pdf_url", "")
            if pdf:
                print(f"    PDF: {pdf}")

    return data


def get_filings(ein):
    """Get filing list for an EIN. Returns list of filing dicts with PDF links."""
    data = get_org(ein)
    if not data:
        return []
    filings = data.get("filings_with_data", []) + data.get("filings_without_data", [])
    return filings


def fulltext_search(query, limit=25):
    """Full-text search inside 990 filing content."""
    params = {"q": query}
    url = f"https://projects.propublica.org/nonprofits/full_text_search?{urllib.parse.urlencode(params)}&utf8=%E2%9C%93"

    # This endpoint returns HTML, not JSON. Use the API approach instead.
    # ProPublica doesn't have a documented full-text JSON API, so we search
    # the standard API with the query and show results.
    print(f"Full-text search for '{query}'")
    print(f"Note: Use ProPublica web UI for full-text 990 search:")
    print(f"  https://projects.propublica.org/nonprofits/full_text_search?q={urllib.parse.quote(query)}")
    print()

    # Fall back to standard org search
    return search_orgs(query, limit=limit)


def batch_search(queries, delay=1.0):
    """Search multiple terms with rate limiting."""
    all_results = {}
    for q in queries:
        print(f"\n{'='*60}")
        print(f"Searching: {q}")
        print(f"{'='*60}")
        results = search_orgs(q)
        all_results[q] = results
        if delay > 0:
            time.sleep(delay)
    return all_results


def main():
    parser = argparse.ArgumentParser(description="ProPublica Nonprofit Explorer API")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p_search = sub.add_parser("search", help="Search organizations by name")
    p_search.add_argument("query", help="Search term")
    p_search.add_argument("--state", help="Filter by state (e.g., NY, VI)")
    p_search.add_argument("--page", type=int, default=0, help="Page number (0-indexed)")
    p_search.add_argument("--limit", type=int, default=25, help="Max results to show")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")
    add_output_args(p_search)

    # ein
    p_ein = sub.add_parser("ein", help="Get organization by EIN")
    p_ein.add_argument("ein", help="EIN (with or without dash)")
    p_ein.add_argument("--json", action="store_true", help="Output as JSON")
    add_output_args(p_ein)

    # filings (alias for ein)
    p_filings = sub.add_parser("filings", help="Get filing details by EIN")
    p_filings.add_argument("ein", help="EIN")
    p_filings.add_argument("--json", action="store_true", help="Output as JSON")
    add_output_args(p_filings)

    # fulltext
    p_ft = sub.add_parser("fulltext", help="Full-text search inside 990 filings")
    p_ft.add_argument("query", help="Search term")
    p_ft.add_argument("--limit", type=int, default=25)

    # batch
    p_batch = sub.add_parser("batch", help="Batch search multiple terms")
    p_batch.add_argument("queries", nargs="+", help="Search terms")
    p_batch.add_argument("--delay", type=float, default=1.0, help="Delay between queries (seconds)")

    args = parser.parse_args()

    if args.command == "search":
        results = search_orgs(args.query, state=args.state, page=args.page, limit=args.limit)
        if write_output(results, args, summary=f"990 search '{args.query}'"):
            pass
        elif getattr(args, "json", False):
            print(json.dumps(results, indent=2))
    elif args.command in ("ein", "filings"):
        data = get_org(args.ein)
        if data and write_output(data, args, summary=f"990 EIN {args.ein}"):
            pass
        elif getattr(args, "json", False) and data:
            print(json.dumps(data, indent=2))
    elif args.command == "fulltext":
        fulltext_search(args.query, limit=args.limit)
    elif args.command == "batch":
        batch_search(args.queries, delay=args.delay)


if __name__ == "__main__":
    main()
