#!/usr/bin/env python3
"""
FINRA BrokerCheck API wrapper.

Queries broker/dealer registrations, employment history, disciplinary actions,
and firm details. No authentication required.

API: https://api.brokercheck.finra.org
Endpoints:
    /search/individual?query=NAME    — Search individuals by name or CRD
    /search/firm?query=NAME          — Search firms by name or firm ID
    /search/individual/{CRD}         — Full individual detail (employment, disclosures)
    /search/firm/{FIRM_ID}           — Full firm detail

Usage:
    python tools/query_finra.py search "Leon Black" --limit 10
    python tools/query_finra.py search "Bear Stearns" --type firm
    python tools/query_finra.py detail 1234567
    python tools/query_finra.py detail 1234567 --type firm
    python tools/query_finra.py disclosures 1234567
    python tools/query_finra.py employment 1234567
"""

import argparse
import json
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://api.brokercheck.finra.org"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OSINT-Research/1.0",
}


def _fetch_json(url):
    """Fetch JSON from a URL."""
    req = Request(url, headers=HEADERS)
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


def _search(entity_type, query, nrows=10, start=0):
    """Search FINRA BrokerCheck for individuals or firms."""
    params = {
        "query": query,
        "hl": "false",
        "nrows": nrows,
        "start": start,
        "r": 25,
        "sort": "score+desc",
        "wt": "json",
    }
    url = f"{BASE_URL}/search/{entity_type}?{urlencode(params)}"
    return _fetch_json(url)


def _get_detail(entity_type, source_id):
    """Get full detail for an individual or firm by source ID."""
    url = f"{BASE_URL}/search/{entity_type}/{source_id}"
    data = _fetch_json(url)
    if not data:
        return None
    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        return None
    source = hits[0].get("_source", {})
    content = source.get("content")
    if content and isinstance(content, str):
        return json.loads(content)
    return source


def _format_individual(src):
    """Format an individual search result for display."""
    crd = src.get("ind_source_id", "?")
    first = src.get("ind_firstname", "")
    middle = src.get("ind_middlename", "")
    last = src.get("ind_lastname", "")
    name = f"{first} {middle} {last}".replace("  ", " ").strip()
    scope = src.get("ind_bc_scope", "?")
    ia_scope = src.get("ind_ia_scope", "")
    disclosures = "DISCLOSURES" if src.get("ind_bc_disclosure_fl") == "Y" else ""
    emp_count = src.get("ind_employments_count", 0)
    industry_date = src.get("ind_industry_cal_date", "")

    lines = [f"  CRD# {crd}: {name} [{scope}]"]
    if ia_scope and ia_scope != "Not In Scope":
        lines[0] += f" (IA: {ia_scope})"
    if disclosures:
        lines[0] += f" ** {disclosures} **"
    if emp_count or industry_date:
        lines.append(f"    Employments: {emp_count} | In industry since: {industry_date}")

    current = src.get("ind_current_employments", [])
    seen_firms = set()
    for emp in current:
        firm_name = emp.get("firm_name", "?")
        if firm_name in seen_firms:
            continue
        seen_firms.add(firm_name)
        city = emp.get("branch_city", "")
        state = emp.get("branch_state", "")
        loc = f"{city}, {state}" if city else ""
        lines.append(f"    Current: {firm_name}" + (f" ({loc})" if loc else ""))

    other_names = src.get("ind_other_names", [])
    if other_names:
        lines.append(f"    AKA: {', '.join(other_names[:3])}")

    return "\n".join(lines)


def _format_firm(src):
    """Format a firm search result for display."""
    firm_id = src.get("firm_source_id", "?")
    name = src.get("firm_name", "?")
    scope = src.get("firm_scope", "?")
    branches = src.get("firm_branches_count", 0)
    disclosures = "DISCLOSURES" if src.get("firm_ia_disclosure_fl") == "Y" else ""

    line = f"  Firm# {firm_id}: {name} [{scope}] — {branches} branches"
    if disclosures:
        line += f" ** {disclosures} **"

    other_names = src.get("firm_other_names", [])
    if other_names and other_names != [name]:
        extras = [n for n in other_names if n != name][:3]
        if extras:
            line += f"\n    AKA: {', '.join(extras)}"

    return line


def cmd_search(args):
    """Search for individuals or firms."""
    entity_type = args.type
    all_results = []
    start = 0
    nrows = min(args.limit, 100)

    while len(all_results) < args.limit:
        data = _search(entity_type, args.query, nrows=nrows, start=start)
        if not data:
            break
        hits = data.get("hits", {})
        total = hits.get("total", 0)
        records = hits.get("hits", [])
        if not records:
            break
        all_results.extend(records)
        start += len(records)
        if start >= total:
            break
        time.sleep(0.3)

    results = all_results[:args.limit]
    # Extract source data for output
    output_data = [h.get("_source", {}) for h in results]

    if write_output(output_data, args, summary=f"FINRA {entity_type} search '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(output_data, indent=2, default=str))
        return

    total = data.get("hits", {}).get("total", 0) if data else 0
    print(f"FINRA BrokerCheck: {total} {entity_type}s matching '{args.query}' (showing {len(results)})")
    print()

    formatter = _format_individual if entity_type == "individual" else _format_firm
    for hit in results:
        src = hit.get("_source", {})
        print(formatter(src))
        print()


def cmd_detail(args):
    """Get full detail for an individual or firm."""
    entity_type = args.type
    detail = _get_detail(entity_type, args.source_id)
    if not detail:
        print(f"No {entity_type} found with ID {args.source_id}", file=sys.stderr)
        sys.exit(1)

    if write_output(detail, args, summary=f"FINRA {entity_type} detail {args.source_id}"):
        return
    if args.json_out:
        print(json.dumps(detail, indent=2, default=str))
        return

    if entity_type == "individual":
        _print_individual_detail(detail)
    else:
        _print_firm_detail(detail)


def _print_individual_detail(detail):
    """Print formatted individual detail."""
    basic = detail.get("basicInformation", {})
    name = f"{basic.get('firstName', '')} {basic.get('middleName', '')} {basic.get('lastName', '')}".replace("  ", " ").strip()
    crd = basic.get("individualId", "?")

    print(f"=== {name} (CRD# {crd}) ===")
    print(f"  BC Scope: {basic.get('bcScope', '?')} | IA Scope: {basic.get('iaScope', '?')}")
    print(f"  Industry since: {basic.get('daysInIndustryCalculatedDate', '?')}")

    other_names = basic.get("otherNames", [])
    if other_names:
        print(f"  Other names: {', '.join(other_names)}")

    # Current employments
    current = detail.get("currentEmployments", [])
    current_ia = detail.get("currentIAEmployments", [])
    if current or current_ia:
        print(f"\n  CURRENT EMPLOYMENTS ({len(current)} BD, {len(current_ia)} IA):")
        for emp in current:
            _print_employment(emp, current=True)
        for emp in current_ia:
            _print_employment(emp, current=True, ia_only=True)

    # Previous employments
    prev = detail.get("previousEmployments", [])
    prev_ia = detail.get("previousIAEmployments", [])
    if prev or prev_ia:
        print(f"\n  PREVIOUS EMPLOYMENTS ({len(prev)} BD, {len(prev_ia)} IA):")
        for emp in sorted(prev + prev_ia, key=lambda e: e.get("registrationEndDate", ""), reverse=True):
            _print_employment(emp, current=False, ia_only=emp.get("iaOnly") == "Y")

    # Disclosures
    disclosures = detail.get("disclosures", [])
    if disclosures:
        print(f"\n  DISCLOSURES ({len(disclosures)}):")
        for disc in disclosures:
            _print_disclosure(disc)
    elif detail.get("disclosureFlag") == "Y":
        print("\n  DISCLOSURES: Flag is Y but no details returned")

    # Exams and registrations
    exams = detail.get("examsCount", 0)
    regs = detail.get("registrationCount", 0)
    states = detail.get("registeredStates", [])
    if exams or regs:
        print(f"\n  Exams passed: {exams} | Active registrations: {regs}")
    if states:
        print(f"  Registered states: {', '.join(states)}")


def _print_employment(emp, current=False, ia_only=False):
    """Print a single employment record."""
    firm = emp.get("firmName", "?")
    firm_id = emp.get("firmId", "?")
    start_date = emp.get("registrationBeginDate", "?")
    end_date = emp.get("registrationEndDate", "")
    city = emp.get("city", "")
    state = emp.get("state", "")
    tag = " [IA only]" if ia_only else ""

    date_range = f"{start_date} — {'present' if current else end_date}"
    loc = f" ({city}, {state})" if city else ""
    print(f"    {date_range}: {firm} (#{firm_id}){loc}{tag}")


def _print_disclosure(disc):
    """Print a single disclosure record."""
    event_date = disc.get("eventDate", "?")
    disc_type = disc.get("disclosureType", "?")
    resolution = disc.get("disclosureResolution", "?")

    print(f"    [{event_date}] {disc_type} — {resolution}")

    detail = disc.get("disclosureDetail", {})
    if not detail:
        return

    allegations = detail.get("Allegations", "")
    if allegations:
        # Truncate long allegations for display
        if len(allegations) > 200:
            allegations = allegations[:200] + "..."
        print(f"      Allegations: {allegations}")

    initiated = detail.get("Initiated By", "")
    if initiated:
        print(f"      Initiated by: {initiated}")

    res_text = detail.get("Resolution", "")
    if res_text:
        print(f"      Resolution: {res_text}")

    sanctions = detail.get("SanctionDetails", [])
    for s in sanctions:
        s_type = s.get("Type", "")
        s_detail = s.get("Details", "")
        if s_type or s_detail:
            print(f"      Sanction: {s_type} — {s_detail}")


def _print_firm_detail(detail):
    """Print formatted firm detail."""
    basic = detail.get("basicInformation", {})
    name = basic.get("firmName", "?")
    firm_id = basic.get("firmId", "?")

    print(f"=== {name} (Firm# {firm_id}) ===")
    print(f"  Scope: {basic.get('bcScope', '?')}")
    print(f"  Type: {basic.get('firmType', '?')}")
    print(f"  Status: {basic.get('firmStatus', '?')} (as of {basic.get('firmStatusDate', '?')})")
    print(f"  Formed: {basic.get('formedDate', '?')} in {basic.get('formedState', '?')}")
    print(f"  Regulator: {basic.get('regulator', '?')}")

    other_names = basic.get("otherNames", [])
    if other_names:
        print(f"  Other names: {', '.join(other_names)}")


def cmd_disclosures(args):
    """Get disclosures for an individual."""
    detail = _get_detail("individual", args.crd)
    if not detail:
        print(f"No individual found with CRD# {args.crd}", file=sys.stderr)
        sys.exit(1)

    disclosures = detail.get("disclosures", [])

    if write_output(disclosures, args, summary=f"FINRA disclosures for CRD# {args.crd}"):
        return
    if args.json_out:
        print(json.dumps(disclosures, indent=2, default=str))
        return

    basic = detail.get("basicInformation", {})
    name = f"{basic.get('firstName', '')} {basic.get('lastName', '')}".strip()

    if not disclosures:
        print(f"No disclosures for {name} (CRD# {args.crd})")
        return

    print(f"Disclosures for {name} (CRD# {args.crd}): {len(disclosures)} records")
    print()
    for disc in disclosures:
        _print_disclosure(disc)
        print()


def cmd_employment(args):
    """Get employment history for an individual."""
    detail = _get_detail("individual", args.crd)
    if not detail:
        print(f"No individual found with CRD# {args.crd}", file=sys.stderr)
        sys.exit(1)

    current = detail.get("currentEmployments", [])
    current_ia = detail.get("currentIAEmployments", [])
    prev = detail.get("previousEmployments", [])
    prev_ia = detail.get("previousIAEmployments", [])

    all_emps = {
        "current": current,
        "current_ia": current_ia,
        "previous": prev,
        "previous_ia": prev_ia,
    }

    if write_output(all_emps, args, summary=f"FINRA employment for CRD# {args.crd}"):
        return
    if args.json_out:
        print(json.dumps(all_emps, indent=2, default=str))
        return

    basic = detail.get("basicInformation", {})
    name = f"{basic.get('firstName', '')} {basic.get('lastName', '')}".strip()
    total = len(current) + len(current_ia) + len(prev) + len(prev_ia)

    print(f"Employment history for {name} (CRD# {args.crd}): {total} records")
    print(f"  Industry since: {basic.get('daysInIndustryCalculatedDate', '?')}")

    if current or current_ia:
        print(f"\n  CURRENT ({len(current)} BD, {len(current_ia)} IA):")
        for emp in current:
            _print_employment(emp, current=True)
        for emp in current_ia:
            _print_employment(emp, current=True, ia_only=True)

    if prev or prev_ia:
        print(f"\n  PREVIOUS ({len(prev)} BD, {len(prev_ia)} IA):")
        for emp in sorted(prev + prev_ia, key=lambda e: e.get("registrationEndDate", ""), reverse=True):
            _print_employment(emp, current=False, ia_only=emp.get("iaOnly") == "Y")


def main():
    parser = argparse.ArgumentParser(description="FINRA BrokerCheck query tool")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search individuals or firms by name")
    p.add_argument("query", help="Name, CRD number, or firm name")
    p.add_argument("--type", choices=["individual", "firm"], default="individual",
                   help="Entity type (default: individual)")
    p.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    add_output_args(p)

    # detail
    p = sub.add_parser("detail", help="Get full detail by CRD or firm ID")
    p.add_argument("source_id", help="CRD number (individual) or firm ID")
    p.add_argument("--type", choices=["individual", "firm"], default="individual",
                   help="Entity type (default: individual)")
    add_output_args(p)

    # disclosures
    p = sub.add_parser("disclosures", help="Get disclosures for an individual")
    p.add_argument("crd", help="CRD number")
    add_output_args(p)

    # employment
    p = sub.add_parser("employment", help="Get employment history for an individual")
    p.add_argument("crd", help="CRD number")
    add_output_args(p)

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "detail": cmd_detail,
        "disclosures": cmd_disclosures,
        "employment": cmd_employment,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
