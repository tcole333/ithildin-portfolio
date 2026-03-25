#!/usr/bin/env python3
"""
Medicare (CMS) Data API wrapper for provider spending research.

Searches for Medicare Physician and Other Practitioners spending data
to identify anomalies and wealth flows in the healthcare sector.

API: https://data.cms.gov/data-api/v1/dataset/
Auth: None required for public API.
Rate Limits: Approx 10 requests per second.

Usage:
    uv run python tools/query_medicare.py search "Enkeshafi"
    uv run python tools/query_medicare.py provider 1003000126
    uv run python tools/query_medicare.py stats
"""

import argparse
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://data.cms.gov/data-api/v1/dataset"

# Default dataset: Medicare Physician & Other Practitioners - by Provider (2023)
DEFAULT_DATASET_ID = "8889d81e-2ee7-448f-8713-f071038289b5"

# Mapping of dataset nicknames to UUIDs
DATASETS = {
    "physician_2023": "8889d81e-2ee7-448f-8713-f071038289b5",
    "physician_2022": "ddd61835-af7a-41d2-a69e-73b3e547085c", # Dictionary or data? (Verification needed)
    "enrollment": "2457ea29-fc82-48b0-86ec-3b0755de7515",
}

def _fetch(dataset_id, params=None):
    """Fetch from CMS Data API."""
    query = f"?{urlencode(params)}" if params else ""
    url = f"{BASE_URL}/{dataset_id}/data{query}"
    
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })
    
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None

def cmd_search(args):
    """Search for providers by name or organization."""
    dataset_id = DATASETS.get(args.dataset, args.dataset)
    
    # CMS API uses filter[field]=value
    params = {
        "size": args.limit,
    }
    
    # Try filtering by last name or org name
    # Note: CMS API filtering can be tricky, some fields need exact match
    if args.query.isdigit():
        params["filter[Rndrng_NPI]"] = args.query
    else:
        # Search is usually exact or prefix-based in this API
        params["filter[Rndrng_Prvdr_Last_Org_Name]"] = args.query.upper()

    results = _fetch(dataset_id, params)
    if not results:
        # Try first name if last name had no results and it's not numeric
        if not args.query.isdigit():
            params = {"size": args.limit, "filter[Rndrng_Prvdr_First_Name]": args.query.upper()}
            results = _fetch(dataset_id, params)

    if write_output(results, args, summary=f"Medicare search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    if not results:
        print(f"No results found for '{args.query}' in dataset {args.dataset}")
        return

    print(f"Found {len(results)} provider records:")
    for r in results:
        npi = r.get("Rndrng_NPI")
        last = r.get("Rndrng_Prvdr_Last_Org_Name")
        first = r.get("Rndrng_Prvdr_First_Name", "")
        city = r.get("Rndrng_Prvdr_City", "")
        state = r.get("Rndrng_Prvdr_State_Abrvtn", "")
        type_ = r.get("Rndrng_Prvdr_Type", "")
        
        # Payment fields
        payment = float(r.get("Tot_Mdcr_Pymt_Amt", 0))
        
        print(f"  {npi} | {last}, {first} ({type_})")
        print(f"    Location: {city}, {state}")
        print(f"    Total Medicare Pymt: ${payment:,.2f}")
        print()

def cmd_provider(args):
    """Get detailed spending for a specific NPI."""
    dataset_id = DATASETS.get(args.dataset, args.dataset)
    params = {
        "filter[Rndrng_NPI]": args.npi,
    }
    
    results = _fetch(dataset_id, params)
    
    if write_output(results, args, summary=f"Medicare provider {args.npi}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(results, indent=2))
        return

    if not results:
        print(f"Provider {args.npi} not found in dataset {args.dataset}")
        return

    r = results[0]
    print(f"Provider: {r.get('Rndrng_Prvdr_First_Name')} {r.get('Rndrng_Prvdr_Last_Org_Name')}")
    print(f"NPI: {r.get('Rndrng_NPI')} | Type: {r.get('Rndrng_Prvdr_Type')}")
    print(f"Address: {r.get('Rndrng_Prvdr_St1')}, {r.get('Rndrng_Prvdr_City')}, {r.get('Rndrng_Prvdr_State_Abrvtn')} {r.get('Rndrng_Prvdr_Zip5')}")
    print("-" * 40)
    
    # Financial metrics
    submitted = float(r.get("Tot_Sbmtd_Chrg", 0))
    allowed = float(r.get("Tot_Mdcr_Alowd_Amt", 0))
    payment = float(r.get("Tot_Mdcr_Pymt_Amt", 0))
    
    print(f"Submitted Charges:  ${submitted:14,.2f}")
    print(f"Medicare Allowed:   ${allowed:14,.2f}")
    print(f"Medicare Payment:   ${payment:14,.2f}")
    print("-" * 40)
    
    # Service metrics
    print(f"Total Beneficiaries: {r.get('Tot_Benes')}")
    print(f"Total Services:      {r.get('Tot_Srvcs')}")
    print(f"Unique HCPCS Codes:  {r.get('Tot_HCPCS_Cds')}")
    
    # Patient demographics/risk
    print(f"Avg Beneficiary Age: {r.get('Bene_Avg_Age')}")
    print(f"Avg Risk Score:      {r.get('Bene_Avg_Risk_Scre')}")

def main():
    parser = argparse.ArgumentParser(description="Medicare (CMS) provider spending search")
    sub = parser.add_subparsers(dest="command", required=True)

    # Search
    p = sub.add_parser("search", help="Search for providers by name")
    p.add_argument("query", help="Last name or organization name")
    p.add_argument("--dataset", default="physician_2023", help="Dataset nickname or UUID")
    p.add_argument("--limit", type=int, default=10, help="Max results")
    add_output_args(p)

    # Provider
    p = sub.add_parser("provider", help="Get details for a specific NPI")
    p.add_argument("npi", help="National Provider Identifier (NPI)")
    p.add_argument("--dataset", default="physician_2023", help="Dataset nickname or UUID")
    add_output_args(p)

    args = parser.parse_args()
    
    handlers = {
        "search": cmd_search,
        "provider": cmd_provider,
    }
    
    handlers[args.command](args)

if __name__ == "__main__":
    main()
