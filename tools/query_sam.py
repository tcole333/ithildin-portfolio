#!/usr/bin/env python3
"""
SAM.gov API wrapper for government entity registration, exclusions, and contract awards.

Covers the four main SAM.gov APIs:
- Entity Management: Who is registered to do business with the federal government
- Exclusions: Debarments, suspensions, and exclusions from federal contracting
- Contract Awards: Federal procurement records (replaces FPDS, decommissioned Feb 2026)
- Opportunities: Active and historical contract solicitations

Auth: Requires SAM_API_KEY (free registration at sam.gov → Account Details → API Key).
      Basic non-federal tier: 10 requests/day. Request SAM role for 1,000/day.

Usage:
    uv run python tools/query_sam.py entity "Palantir"
    uv run python tools/query_sam.py entity "Palantir" --status A --sections all
    uv run python tools/query_sam.py exclusions "QUERY" --type Firm
    uv run python tools/query_sam.py contracts "RECIPIENT" --limit 25
    uv run python tools/query_sam.py opportunities "surveillance" --posted-from 01/01/2025
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

SAM_API_KEY = os.environ.get("SAM_API_KEY", "")

ENTITY_BASE = "https://api.sam.gov/entity-information/v4"
EXCLUSIONS_BASE = "https://api.sam.gov/entity-information/v4"
CONTRACTS_BASE = "https://api.sam.gov/contract-awards/v1"
OPPORTUNITIES_BASE = "https://api.sam.gov/opportunities/v2"

RATE_LIMIT_DELAY = 1.5  # Conservative: 10 req/day on basic tier


def _check_api_key():
    if not SAM_API_KEY:
        print("ERROR: SAM_API_KEY not set. Get a free key at sam.gov → Account Details → API Key.", file=sys.stderr)
        print("Set it: export SAM_API_KEY=your_key_here", file=sys.stderr)
        sys.exit(1)


def _fetch(url, params=None):
    """Fetch from SAM.gov API with rate limiting."""
    if params:
        params["api_key"] = SAM_API_KEY
    else:
        params = {"api_key": SAM_API_KEY}

    query = urlencode(params, doseq=True)
    full_url = f"{url}?{query}"

    req = Request(full_url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    try:
        time.sleep(RATE_LIMIT_DELAY)
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 429:
            print("ERROR: Rate limit exceeded. Basic tier allows 10 requests/day.", file=sys.stderr)
            print("Register for a SAM role to get 1,000/day.", file=sys.stderr)
        else:
            print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None


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


# ── Entity Management ───────────────────────────────────────

def cmd_entity(args):
    """Search SAM.gov entity registrations."""
    _check_api_key()

    params = {}

    if args.uei:
        params["ueiSAM"] = args.uei
    elif args.cage:
        params["cageCode"] = args.cage
    else:
        params["legalBusinessName"] = args.query

    if args.status:
        params["registrationStatus"] = args.status  # A=Active, E=Expired
    if args.state:
        params["physicalAddressStateCode"] = args.state
    if args.naics:
        params["primaryNaics"] = args.naics

    # Sections to include
    if args.sections == "all":
        params["includeSections"] = "entityRegistration,coreData,assertions,pointsOfContact,integrityInformation"
    elif args.sections:
        params["includeSections"] = args.sections
    else:
        params["includeSections"] = "entityRegistration,coreData"

    result = _fetch(f"{ENTITY_BASE}/entities", params)
    if not result:
        return

    entities = result.get("entityData", [])
    total = result.get("totalRecords", len(entities))

    if write_output(entities, args, summary=f"SAM.gov entities matching '{args.query or args.uei or args.cage}' ({total} total)"):
        return

    print(f"Found {total} registered entities:")
    for e in entities:
        reg = e.get("entityRegistration", {})
        core = e.get("coreData", {})
        entity_info = core.get("entityInformation", {})
        addr = core.get("physicalAddress", {})

        uei = reg.get("ueiSAM", "N/A")
        name = reg.get("legalBusinessName", "Unknown")
        status = reg.get("registrationStatus", "?")
        cage = reg.get("cageCode", "")
        exp_date = reg.get("registrationExpirationDate", "")

        city = addr.get("city", "")
        state = addr.get("stateOrProvinceCode", "")
        country = addr.get("countryCode", "")
        zip_code = addr.get("zipCode", "")

        print(f"\n  {name}")
        print(f"    UEI: {uei} | CAGE: {cage} | Status: {status}")
        print(f"    Location: {city}, {state} {zip_code} {country}")
        if exp_date:
            print(f"    Registration expires: {exp_date}")

        # Entity type
        entity_type = entity_info.get("entityStructureDesc", "")
        bus_types = entity_info.get("businessTypes", {}).get("businessTypeList", [])
        if entity_type:
            print(f"    Structure: {entity_type}")
        if bus_types:
            type_names = [bt.get("businessTypeDesc", "") for bt in bus_types[:5]]
            print(f"    Business types: {', '.join(t for t in type_names if t)}")

        # Points of contact
        pocs = e.get("pointsOfContact", {})
        if pocs:
            gov_poc = pocs.get("governmentBusinessPOC", {})
            if gov_poc and gov_poc.get("firstName"):
                poc_name = f"{gov_poc.get('firstName', '')} {gov_poc.get('lastName', '')}"
                poc_title = gov_poc.get("title", "")
                print(f"    POC: {poc_name.strip()}" + (f" ({poc_title})" if poc_title else ""))

        # Integrity / proceedings
        integrity = e.get("integrityInformation", {})
        if integrity:
            proceedings = integrity.get("proceedingsList", [])
            if proceedings:
                print(f"    Proceedings: {len(proceedings)} on record")

    print()


# ── Exclusions ──────────────────────────────────────────────

def cmd_exclusions(args):
    """Search SAM.gov exclusions (debarments, suspensions)."""
    _check_api_key()

    params = {}

    if args.query:
        params["q"] = args.query

    if args.classification:
        params["classification"] = args.classification  # Individual, Firm, Vessel, Special Entity Designation
    if args.type:
        params["exclusionType"] = args.type  # Ineligible, Prohibition/Restriction, Voluntary
    if args.agency:
        params["excludingAgencyName"] = args.agency
    if args.state:
        params["stateProvince"] = args.state
    if args.uei:
        params["ueiSAM"] = args.uei
    if args.npi:
        params["npi"] = args.npi

    result = _fetch(f"{EXCLUSIONS_BASE}/exclusions", params)
    if not result:
        return

    exclusions = result.get("results", [])
    total = result.get("totalRecords", len(exclusions))

    if write_output(exclusions, args, summary=f"SAM.gov exclusions matching '{args.query}' ({total} total)"):
        return

    print(f"Found {total} exclusion records:")
    for ex in exclusions:
        name = ex.get("name", "Unknown")
        classification = ex.get("classification", {}).get("classificationDesc", "?")
        exclusion_type = ex.get("exclusionType", {}).get("exclusionTypeDesc", "?")
        agency = ex.get("excludingAgency", {}).get("excludingAgencyName", "?")

        activation = ex.get("activationDate", "?")
        termination = ex.get("terminationDate", "Active")

        addr = ex.get("address", {})
        city = addr.get("city", "")
        state = addr.get("stateOrProvince", "")
        country = addr.get("country", "")

        print(f"\n  {name} ({classification})")
        print(f"    Type: {exclusion_type}")
        print(f"    Agency: {agency}")
        print(f"    Dates: {activation} to {termination}")
        if city or state:
            print(f"    Location: {city}, {state} {country}")

        uei = ex.get("ueiSAM", "")
        if uei:
            print(f"    UEI: {uei}")

        desc = ex.get("description", "")
        if desc:
            print(f"    Description: {desc[:150]}")

    print()


# ── Contract Awards (replaces FPDS) ────────────────────────

def cmd_contracts(args):
    """Search SAM.gov contract awards (federal procurement records)."""
    _check_api_key()

    params = {}

    if args.uei:
        params["awardeeUniqueEntityId"] = args.uei
    elif args.query:
        params["awardeeLegalBusinessName"] = args.query

    if args.piid:
        params["piid"] = args.piid
    if args.naics:
        params["naicsCode"] = args.naics
    if args.psc:
        params["productOrServiceCode"] = args.psc
    if args.agency:
        params["contractingDepartmentName"] = args.agency
    if args.date_signed_from:
        params["dateSigned"] = f"[{args.date_signed_from},{args.date_signed_to or ''}]"
    if args.min_amount is not None:
        params["dollarsObligated"] = f"[{args.min_amount},]"

    if args.sections == "all":
        params["includeSections"] = "contractId,coreData,awardDetails,awardeeData"
    elif args.sections:
        params["includeSections"] = args.sections

    params["limit"] = args.limit

    result = _fetch(f"{CONTRACTS_BASE}/search", params)
    if not result:
        return

    awards = result.get("data", [])
    total = result.get("totalRecords", len(awards))

    if write_output(awards, args, summary=f"SAM.gov contracts for '{args.query or args.uei}' ({total} total)"):
        return

    print(f"Found {total} contract awards (showing {len(awards)}):")
    for a in awards:
        contract_id = a.get("contractId", {})
        core = a.get("coreData", {})
        details = a.get("awardDetails", {})
        awardee = a.get("awardeeData", {})

        piid = contract_id.get("piid", "?")
        agency = contract_id.get("contractingDepartmentName", "?")
        sub_agency = contract_id.get("contractingOfficeName", "")

        awardee_name = awardee.get("awardeeLegalBusinessName", "?")
        awardee_uei = awardee.get("awardeeUniqueEntityId", "")

        dollars = core.get("dollarsObligated", 0)
        date_signed = core.get("dateSigned", "?")
        naics = core.get("naicsCode", "")
        psc = core.get("productOrServiceCode", "")
        desc = core.get("descriptionOfContractRequirement", "")

        print(f"\n  PIID: {piid} | {_fmt_money(dollars)} | {date_signed}")
        print(f"    Awardee: {awardee_name}" + (f" (UEI: {awardee_uei})" if awardee_uei else ""))
        print(f"    Agency: {agency}" + (f" / {sub_agency}" if sub_agency else ""))
        if naics:
            print(f"    NAICS: {naics} | PSC: {psc}")
        if desc:
            print(f"    Desc: {desc[:120]}")

    print()


# ── Opportunities ───────────────────────────────────────────

def cmd_opportunities(args):
    """Search SAM.gov contract opportunities (solicitations)."""
    _check_api_key()

    params = {
        "postedFrom": args.posted_from,
        "postedTo": args.posted_to,
        "limit": args.limit,
    }

    if args.query:
        params["title"] = args.query
    if args.naics:
        params["ncode"] = args.naics
    if args.state:
        params["state"] = args.state
    if args.sol_num:
        params["solnum"] = args.sol_num
    if args.set_aside:
        params["typeOfSetAside"] = args.set_aside

    result = _fetch(f"{OPPORTUNITIES_BASE}/search", params)
    if not result:
        return

    opps = result.get("opportunitiesData", [])
    total = result.get("totalRecords", len(opps))

    if write_output(opps, args, summary=f"SAM.gov opportunities matching '{args.query}' ({total} total)"):
        return

    print(f"Found {total} opportunities (showing {len(opps)}):")
    for o in opps:
        title = o.get("title", "Untitled")
        sol_num = o.get("solicitationNumber", "")
        notice_type = o.get("type", "?")
        posted = o.get("postedDate", "?")
        deadline = o.get("responseDeadLine", "")
        org = o.get("fullParentPathName", "") or o.get("organizationType", "")
        set_aside = o.get("typeOfSetAside", "")
        naics = o.get("naicsCode", "")
        ui_link = o.get("uiLink", "")

        print(f"\n  {title}")
        print(f"    Sol#: {sol_num} | Type: {notice_type} | Posted: {posted}")
        if deadline:
            print(f"    Deadline: {deadline}")
        if org:
            print(f"    Agency: {org}")
        if naics:
            print(f"    NAICS: {naics}" + (f" | Set-aside: {set_aside}" if set_aside else ""))
        if ui_link:
            print(f"    Link: {ui_link}")

    print()


# ── CLI ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SAM.gov API — entity registrations, exclusions, contracts, opportunities",
        epilog="Requires SAM_API_KEY env var (free at sam.gov). Basic tier: 10 req/day."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Entity
    p = sub.add_parser("entity", help="Search entity registrations")
    p.add_argument("query", nargs="?", help="Legal business name search")
    p.add_argument("--uei", help="Search by UEI (Unique Entity ID)")
    p.add_argument("--cage", help="Search by CAGE code")
    p.add_argument("--status", choices=["A", "E"], help="A=Active, E=Expired")
    p.add_argument("--state", help="Physical address state code (e.g., NY, CA)")
    p.add_argument("--naics", help="Primary NAICS code")
    p.add_argument("--sections", help="Sections to include (or 'all')")
    add_output_args(p)

    # Exclusions
    p = sub.add_parser("exclusions", help="Search debarments, suspensions, exclusions")
    p.add_argument("query", nargs="?", help="Free text search (AND/OR/NOT/wildcard)")
    p.add_argument("--classification", choices=["Individual", "Firm", "Vessel", "Special Entity Designation"])
    p.add_argument("--type", choices=["Ineligible", "Prohibition/Restriction", "Voluntary"],
                   help="Exclusion type")
    p.add_argument("--agency", help="Excluding agency name")
    p.add_argument("--state", help="State/province code")
    p.add_argument("--uei", help="Search by UEI")
    p.add_argument("--npi", help="Search by NPI")
    add_output_args(p)

    # Contracts
    p = sub.add_parser("contracts", help="Search federal contract awards (replaces FPDS)")
    p.add_argument("query", nargs="?", help="Awardee legal business name")
    p.add_argument("--uei", help="Awardee UEI")
    p.add_argument("--piid", help="Procurement Instrument ID")
    p.add_argument("--naics", help="NAICS code")
    p.add_argument("--psc", help="Product/Service code")
    p.add_argument("--agency", help="Contracting department name")
    p.add_argument("--date-signed-from", help="Date signed from (YYYY-MM-DD)")
    p.add_argument("--date-signed-to", help="Date signed to (YYYY-MM-DD)")
    p.add_argument("--min-amount", type=float, help="Minimum dollars obligated")
    p.add_argument("--limit", type=int, default=25, help="Max results (default 25)")
    p.add_argument("--sections", help="Sections to include (or 'all')")
    add_output_args(p)

    # Opportunities
    p = sub.add_parser("opportunities", help="Search contract solicitations")
    p.add_argument("query", nargs="?", help="Title search")
    p.add_argument("--posted-from", required=True, help="Posted from date (MM/DD/YYYY, required)")
    p.add_argument("--posted-to", help="Posted to date (MM/DD/YYYY, defaults to today)")
    p.add_argument("--naics", help="NAICS code filter")
    p.add_argument("--state", help="State code filter")
    p.add_argument("--sol-num", help="Solicitation number")
    p.add_argument("--set-aside", help="Set-aside type filter")
    p.add_argument("--limit", type=int, default=25, help="Max results")
    add_output_args(p)

    args = parser.parse_args()

    # Default posted-to to today if not provided
    if args.command == "opportunities" and not args.posted_to:
        from datetime import datetime
        args.posted_to = datetime.now().strftime("%m/%d/%Y")

    handlers = {
        "entity": cmd_entity,
        "exclusions": cmd_exclusions,
        "contracts": cmd_contracts,
        "opportunities": cmd_opportunities,
    }

    handlers[args.command](args)


if __name__ == "__main__":
    main()
