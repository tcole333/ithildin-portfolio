#!/usr/bin/env python3
"""HigherGov API — Federal contract, grant, awardee, and IDV search.

Usage:
    uv run python tools/query_highergov.py contract --award-id "N0002325D0075-70CDCR26FR0000040"
    uv run python tools/query_highergov.py contract --parent-award "N0002325D0075" --page-size 50
    uv run python tools/query_highergov.py contract --vehicle-key 8751 --page-size 100
    uv run python tools/query_highergov.py contract --awardee-uei ZE2JVFS8ML75
    uv run python tools/query_highergov.py idv --vehicle-key 8751 --page-size 100
    uv run python tools/query_highergov.py idv --award-id N0002325D0075
    uv run python tools/query_highergov.py awardee --uei ZE2JVFS8ML75
    uv run python tools/query_highergov.py awardee --cage 9MFB2
    uv run python tools/query_highergov.py subcontract --awardee-uei ZE2JVFS8ML75
    uv run python tools/query_highergov.py partnership --awardee-key 509623647
    uv run python tools/query_highergov.py vehicle --vehicle-key 8751
    uv run python tools/query_highergov.py agency --agency-key 904
    uv run python tools/query_highergov.py grant --awardee-uei ZE2JVFS8ML75
    uv run python tools/query_highergov.py people --email "john.doe@ice.dhs.gov"
    uv run python tools/query_highergov.py opportunity --source-id "26-SOL-DCR01"

Auth: HIGHERGOV_API_KEY in .env or --key flag.
Rate limit: 10 req/sec, 100K req/day. 10K records/month on base plan.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

# Load .env
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

BASE_URL = "https://www.highergov.com/api-external"


def get_api_key(args_key: str | None = None) -> str:
    key = args_key or os.environ.get("HIGHERGOV_API_KEY")
    if not key:
        print("Error: Set HIGHERGOV_API_KEY in .env or pass --key", file=sys.stderr)
        sys.exit(1)
    return key


def api_get(endpoint: str, params: dict, api_key: str) -> dict:
    params["api_key"] = api_key
    # Remove None values
    params = {k: v for k, v in params.items() if v is not None}
    url = f"{BASE_URL}/{endpoint}/"
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 403:
        print("Error: Invalid API key", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 400:
        print(f"Error: Bad request — {resp.text}", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def paginate_all(endpoint: str, params: dict, api_key: str, max_pages: int = 50) -> list:
    """Fetch all pages up to max_pages."""
    all_results = []
    params["page_size"] = params.get("page_size", 100)
    page = 1
    while page <= max_pages:
        params["page_number"] = page
        data = api_get(endpoint, params.copy(), api_key)
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            break
        all_results.extend(results)
        # Check if there are more pages
        count = data.get("count") if isinstance(data, dict) else None
        if count and len(all_results) >= count:
            break
        if len(results) < params["page_size"]:
            break
        page += 1
        time.sleep(0.15)  # Rate limit courtesy
    return all_results


def cmd_contract(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.award_id:
        params["award_id"] = args.award_id
    if args.parent_award:
        params["parent_award_id"] = args.parent_award
    if args.awardee_uei:
        params["awardee_uei"] = args.awardee_uei
    if args.awardee_key:
        params["awardee_key"] = args.awardee_key
    if args.vehicle_key:
        params["vehicle_key"] = args.vehicle_key
    if args.naics:
        params["naics_code"] = args.naics
    if args.psc:
        params["psc_code"] = args.psc
    if args.agency_key:
        params["awarding_agency_key"] = args.agency_key
    if args.since:
        params["last_modified_date"] = args.since
    if args.ordering:
        params["ordering"] = args.ordering
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    if args.all_pages:
        results = paginate_all("contract", params, api_key)
    else:
        data = api_get("contract", params, api_key)
        results = data if isinstance(data, list) else data.get("results", [])
        count = data.get("count") if isinstance(data, dict) else len(results)
        print(f"Showing {len(results)} of {count} contracts", file=sys.stderr)

    output(results, args.output, "contracts")


def cmd_idv(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.award_id:
        params["award_id"] = args.award_id
    if args.awardee_uei:
        params["awardee_uei"] = args.awardee_uei
    if args.awardee_key:
        params["awardee_key"] = args.awardee_key
    if args.vehicle_key:
        params["vehicle_key"] = args.vehicle_key
    if args.parent_award:
        params["parent_award_id"] = args.parent_award
    if args.naics:
        params["naics_code"] = args.naics
    if args.since:
        params["last_modified_date"] = args.since
    if args.ordering:
        params["ordering"] = args.ordering
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    if args.all_pages:
        results = paginate_all("idv", params, api_key)
    else:
        data = api_get("idv", params, api_key)
        results = data if isinstance(data, list) else data.get("results", [])
        count = data.get("count") if isinstance(data, dict) else len(results)
        print(f"Showing {len(results)} of {count} IDVs", file=sys.stderr)

    output(results, args.output, "IDVs")


def cmd_awardee(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.uei:
        params["uei"] = args.uei
    if args.cage:
        params["cage_code"] = args.cage
    if args.naics:
        params["primary_naics"] = args.naics
    if args.parent_key:
        params["awardee_key_parent"] = args.parent_key
    if args.since:
        params["registration_last_update_date"] = args.since
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("awardee", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "awardees")


def cmd_subcontract(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.awardee_uei:
        params["awardee_uei"] = args.awardee_uei
    if args.awardee_key:
        params["awardee_key"] = args.awardee_key
    if args.agency_key:
        params["awarding_agency_key"] = args.agency_key
    if args.since:
        params["last_modified_date"] = args.since
    if args.ordering:
        params["ordering"] = args.ordering
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    if args.all_pages:
        results = paginate_all("subcontract", params, api_key)
    else:
        data = api_get("subcontract", params, api_key)
        results = data if isinstance(data, list) else data.get("results", [])
        count = data.get("count") if isinstance(data, dict) else len(results)
        print(f"Showing {len(results)} of {count} subcontracts", file=sys.stderr)

    output(results, args.output, "subcontracts")


def cmd_partnership(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.awardee_key:
        params["awardee_key_prime"] = args.awardee_key
    if args.sub_key:
        params["awardee_key_sub"] = args.sub_key
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("awardee-partnership", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "partnerships")


def cmd_vehicle(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.vehicle_key:
        params["vehicle_key"] = args.vehicle_key
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("vehicle", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "vehicles")


def cmd_agency(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.agency_key:
        params["agency_key"] = args.agency_key
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("agency", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "agencies")


def cmd_grant(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.awardee_uei:
        params["awardee_uei"] = args.awardee_uei
    if args.awardee_key:
        params["awardee_key"] = args.awardee_key
    if args.agency_key:
        params["awarding_agency_key"] = args.agency_key
    if args.cfda:
        params["cfda_program_number"] = args.cfda
    if args.since:
        params["last_modified_date"] = args.since
    if args.ordering:
        params["ordering"] = args.ordering
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    if args.all_pages:
        results = paginate_all("grant", params, api_key)
    else:
        data = api_get("grant", params, api_key)
        results = data if isinstance(data, list) else data.get("results", [])
        count = data.get("count") if isinstance(data, dict) else len(results)
        print(f"Showing {len(results)} of {count} grants", file=sys.stderr)

    output(results, args.output, "grants")


def cmd_people(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.email:
        params["contact_email"] = args.email
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("people", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "people")


def cmd_opportunity(args):
    api_key = get_api_key(args.key)
    params = {}
    if args.source_id:
        params["source_id"] = args.source_id
    if args.opp_key:
        params["opp_key"] = args.opp_key
    if args.agency_key:
        params["agency_key"] = args.agency_key
    if args.source_type:
        params["source_type"] = args.source_type
    if args.since:
        params["captured_date"] = args.since
    params["page_size"] = args.page_size

    if not any(k != "page_size" for k in params):
        print("Error: At least one filter parameter required", file=sys.stderr)
        sys.exit(1)

    data = api_get("opportunity", params, api_key)
    results = data if isinstance(data, list) else data.get("results", [])
    output(results, args.output, "opportunities")


def output(results: list, output_path: str | None, label: str):
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"{len(results)} {label} saved to {output_path}")
    else:
        print(json.dumps(results, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="HigherGov API — federal contracts, grants, awardees")
    parser.add_argument("--key", help="API key (or set HIGHERGOV_API_KEY)")
    sub = parser.add_subparsers(dest="command", required=True)

    # contract
    p = sub.add_parser("contract", help="Federal prime contracts")
    p.add_argument("--award-id", help="Specific award ID")
    p.add_argument("--parent-award", help="Parent award/IDV ID")
    p.add_argument("--awardee-uei", help="Awardee UEI")
    p.add_argument("--awardee-key", type=int, help="HigherGov awardee key")
    p.add_argument("--vehicle-key", type=int, help="Contract vehicle key (e.g. 8751 for WEXMAC)")
    p.add_argument("--naics", help="NAICS code filter")
    p.add_argument("--psc", help="PSC code filter")
    p.add_argument("--agency-key", type=int, help="Awarding agency key")
    p.add_argument("--since", help="Modified since date (YYYY-MM-DD)")
    p.add_argument("--ordering", help="Sort order")
    p.add_argument("--page-size", type=int, default=100, help="Results per page (max 100)")
    p.add_argument("--all-pages", action="store_true", help="Fetch all pages")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_contract)

    # idv
    p = sub.add_parser("idv", help="Federal IDVs (IDIQ, BPA, BOA)")
    p.add_argument("--award-id", help="Specific IDV award ID")
    p.add_argument("--parent-award", help="Parent award ID")
    p.add_argument("--awardee-uei", help="Awardee UEI")
    p.add_argument("--awardee-key", type=int, help="HigherGov awardee key")
    p.add_argument("--vehicle-key", type=int, help="Contract vehicle key")
    p.add_argument("--naics", help="NAICS code filter")
    p.add_argument("--since", help="Modified since date (YYYY-MM-DD)")
    p.add_argument("--ordering", help="Sort order")
    p.add_argument("--page-size", type=int, default=100, help="Results per page (max 100)")
    p.add_argument("--all-pages", action="store_true", help="Fetch all pages")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_idv)

    # awardee
    p = sub.add_parser("awardee", help="Federal awardee/contractor lookup")
    p.add_argument("--uei", help="Unique Entity ID")
    p.add_argument("--cage", help="CAGE code")
    p.add_argument("--naics", help="Primary NAICS code")
    p.add_argument("--parent-key", type=int, help="Parent awardee key")
    p.add_argument("--since", help="Registration updated since (YYYY-MM-DD)")
    p.add_argument("--page-size", type=int, default=10, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_awardee)

    # subcontract
    p = sub.add_parser("subcontract", help="Federal subcontract awards")
    p.add_argument("--awardee-uei", help="Subawardee UEI")
    p.add_argument("--awardee-key", type=int, help="Subawardee key")
    p.add_argument("--agency-key", type=int, help="Awarding agency key")
    p.add_argument("--since", help="Modified since date (YYYY-MM-DD)")
    p.add_argument("--ordering", help="Sort order")
    p.add_argument("--page-size", type=int, default=100, help="Results per page")
    p.add_argument("--all-pages", action="store_true", help="Fetch all pages")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_subcontract)

    # partnership
    p = sub.add_parser("partnership", help="Awardee teaming partnerships")
    p.add_argument("--awardee-key", type=int, help="Prime awardee key")
    p.add_argument("--sub-key", type=int, help="Sub awardee key")
    p.add_argument("--page-size", type=int, default=100, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_partnership)

    # vehicle
    p = sub.add_parser("vehicle", help="Multi-award contract vehicles")
    p.add_argument("--vehicle-key", type=int, help="Vehicle key (e.g. 8751 for WEXMAC 2.0)")
    p.add_argument("--page-size", type=int, default=100, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_vehicle)

    # agency
    p = sub.add_parser("agency", help="Federal/state agency lookup")
    p.add_argument("--agency-key", type=int, help="Agency key")
    p.add_argument("--page-size", type=int, default=10, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_agency)

    # grant
    p = sub.add_parser("grant", help="Federal prime grants")
    p.add_argument("--awardee-uei", help="Awardee UEI")
    p.add_argument("--awardee-key", type=int, help="Awardee key")
    p.add_argument("--agency-key", type=int, help="Awarding agency key")
    p.add_argument("--cfda", help="CFDA program number")
    p.add_argument("--since", help="Modified since date (YYYY-MM-DD)")
    p.add_argument("--ordering", help="Sort order")
    p.add_argument("--page-size", type=int, default=100, help="Results per page")
    p.add_argument("--all-pages", action="store_true", help="Fetch all pages")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_grant)

    # people
    p = sub.add_parser("people", help="Federal/state people lookup")
    p.add_argument("--email", help="Contact email address")
    p.add_argument("--page-size", type=int, default=10, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_people)

    # opportunity
    p = sub.add_parser("opportunity", help="Contract/grant opportunities")
    p.add_argument("--source-id", help="Agency opportunity/solicitation ID")
    p.add_argument("--opp-key", help="HigherGov opportunity key")
    p.add_argument("--agency-key", type=int, help="Agency key")
    p.add_argument("--source-type", help="Type: sam, dibbs, sbir, grant, sled")
    p.add_argument("--since", help="Captured since date (YYYY-MM-DD)")
    p.add_argument("--page-size", type=int, default=10, help="Results per page")
    p.add_argument("--output", "-o", help="Output file path")
    p.set_defaults(func=cmd_opportunity)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
