#!/usr/bin/env python3
"""
FEC (Federal Election Commission) campaign finance API wrapper.

Covers Schedule A (receipts/contributions), Schedule B (disbursements/spending),
Schedule E (independent expenditures), committee totals, and candidate/committee lookups.

API: https://api.open.fec.gov/v1/
Auth: Free API key from api.data.gov (required). Set FEC_API_KEY in .env.
Pagination: Cursor-based (last_indexes), not page numbers. 100 results/page max.

IMPORTANT: Multiple people named "Jeffrey Epstein" exist in FEC records.
Always cross-reference employer, address, and occupation to disambiguate.

Usage:
    # Schedule A — contributions/receipts
    python tools/query_fec.py donor "Jeffrey Epstein" --limit 50
    python tools/query_fec.py donor "Leon Black" --min-amount 1000
    python tools/query_fec.py donor "Black" --employer "Apollo" --limit 10
    python tools/query_fec.py employer "Gratitude America"
    python tools/query_fec.py address "10021" --name "Epstein"
    python tools/query_fec.py recipient C00580100 --cycle 2018

    # Schedule B — disbursements (where did the money go?)
    python tools/query_fec.py disbursements C00916114 --limit 200
    python tools/query_fec.py disbursements C00916114 --recipient "Summit Ridge"
    python tools/query_fec.py disbursements C00916114 --min-amount 50000

    # Schedule E — independent expenditures (support/oppose ads)
    python tools/query_fec.py ie C00916114 --limit 200
    python tools/query_fec.py ie C00916114 --support-oppose S
    python tools/query_fec.py ie C00916114 --candidate P80001571

    # Committee financial summary
    python tools/query_fec.py totals C00916114
    python tools/query_fec.py totals C00916114 --cycle 2024

    # Lookups
    python tools/query_fec.py committee C00916114
    python tools/query_fec.py candidate "Gonzalez"
    python tools/query_fec.py batch-persons
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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


# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

BASE_URL = "https://api.open.fec.gov/v1"


def _get_api_key():
    """Get FEC API key from environment."""
    key = os.environ.get("FEC_API_KEY")
    if not key:
        print("WARNING: FEC_API_KEY not set in .env. Using DEMO_KEY (very limited).", file=sys.stderr)
        return "DEMO_KEY"
    return key


def _fetch(endpoint, params, max_pages=1):
    """Fetch from FEC API with cursor-based pagination."""
    api_key = _get_api_key()
    params["api_key"] = api_key
    params["per_page"] = params.get("per_page", 100)

    all_results = []
    data = {}
    page = 0

    while page < max_pages:
        url = f"{BASE_URL}{endpoint}?{urlencode(params, doseq=True)}"
        req = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "OSINT-Research/1.0",
        })

        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
        except HTTPError as e:
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
            break
        except URLError as e:
            print(f"ERROR: {e.reason}", file=sys.stderr)
            break

        results = data.get("results", [])
        all_results.extend(results)

        # Check for more pages via cursor
        pagination = data.get("pagination", {})
        last_indexes = pagination.get("last_indexes", {})
        if not last_indexes or not results:
            break

        # Update params with cursor for next page
        for k, v in last_indexes.items():
            if v is not None:
                params[k] = v

        page += 1
        if page < max_pages:
            time.sleep(0.5)

    return all_results, data.get("pagination", {})


def _format_amount(amount):
    """Format donation amount."""
    if amount is None:
        return "?"
    try:
        return f"${float(amount):,.2f}"
    except (ValueError, TypeError):
        return str(amount)


def _print_contribution(c):
    """Print a single contribution record."""
    name = c.get("contributor_name", "?")
    amount = _format_amount(c.get("contribution_receipt_amount"))
    date = c.get("contribution_receipt_date", "?")
    employer = c.get("contributor_employer", "")
    occupation = c.get("contributor_occupation", "")
    city = c.get("contributor_city", "")
    state = c.get("contributor_state", "")
    zipcode = c.get("contributor_zip", "")
    committee = c.get("committee", {})
    committee_name = committee.get("name", c.get("committee_name", "?"))
    committee_id = c.get("committee_id", "?")

    print(f"  {name} — {amount} on {date}")
    if employer or occupation:
        print(f"    Employer: {employer} | Occupation: {occupation}")
    if city or state:
        print(f"    Location: {city}, {state} {zipcode}")
    print(f"    To: {committee_name} ({committee_id})")
    print()


def cmd_donor(args):
    """Search individual contributions by donor name."""
    params = {"contributor_name": args.query, "sort": "-contribution_receipt_date"}
    if args.employer:
        params["contributor_employer"] = args.employer
    if args.min_amount:
        params["min_amount"] = args.min_amount
    if args.max_amount:
        params["max_amount"] = args.max_amount
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle
    if args.state:
        params["contributor_state"] = args.state

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_a/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC donor '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    _log(args.query, "fec", total)
    employer_note = f" at employer '{args.employer}'" if args.employer else ""
    print(f"Found {total} contributions from donors matching '{args.query}'{employer_note} (showing {min(len(results), args.limit)})")
    print()

    # Flag disambiguation issues
    unique_employers = set()
    unique_cities = set()
    for r in results[:args.limit]:
        emp = r.get("contributor_employer", "")
        if emp:
            unique_employers.add(emp.upper())
        city = r.get("contributor_city", "")
        if city:
            unique_cities.add(city.upper())

    if len(unique_employers) > 3 or len(unique_cities) > 5:
        print(f"  WARNING: Multiple distinct donors may share this name.")
        print(f"  Unique employers: {len(unique_employers)} | Unique cities: {len(unique_cities)}")
        print(f"  Cross-reference employer/address before recording findings.")
        print()

    for r in results[:args.limit]:
        _print_contribution(r)


def cmd_employer(args):
    """Search contributions by employer name."""
    params = {"contributor_employer": args.query, "sort": "-contribution_receipt_date"}
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_a/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC employer '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    _log(args.query, "fec", total)
    print(f"Found {total} contributions from employees of '{args.query}' (showing {min(len(results), args.limit)})")
    print()

    for r in results[:args.limit]:
        _print_contribution(r)


def cmd_address(args):
    """Search contributions by donor ZIP code, optionally filtered by name."""
    params = {"contributor_zip": args.zip_code, "sort": "-contribution_receipt_date"}
    if args.name:
        params["contributor_name"] = args.name
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_a/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC address ZIP {args.zip_code}"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    name_filter = f" with name '{args.name}'" if args.name else ""
    print(f"Found {total} contributions from ZIP {args.zip_code}{name_filter} (showing {min(len(results), args.limit)})")
    print()

    for r in results[:args.limit]:
        _print_contribution(r)


def cmd_recipient(args):
    """Get contributions received by a specific committee."""
    params = {"committee_id": args.committee_id, "sort": "-contribution_receipt_date"}
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle
    if args.min_amount:
        params["min_amount"] = args.min_amount

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_a/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC recipient {args.committee_id}"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    print(f"Found {total} contributions to committee {args.committee_id} (showing {min(len(results), args.limit)})")
    print()

    for r in results[:args.limit]:
        _print_contribution(r)


def _print_disbursement(d):
    """Print a single disbursement record."""
    recipient = d.get("recipient_name", "?")
    amount = _format_amount(d.get("disbursement_amount"))
    date = d.get("disbursement_date", "?")
    desc = d.get("disbursement_description", "")
    memo = d.get("memo_text", "")
    city = d.get("recipient_city", "")
    state = d.get("recipient_state", "")

    print(f"  {recipient} — {amount} on {date}")
    if desc:
        print(f"    Description: {desc}")
    if memo:
        print(f"    Memo: {memo}")
    if city or state:
        print(f"    Location: {city}, {state}")
    print()


def _print_ie(ie):
    """Print a single independent expenditure record."""
    candidate = ie.get("candidate_name", "?")
    so = ie.get("support_oppose_indicator", "?")
    so_label = "SUPPORT" if so == "S" else "OPPOSE" if so == "O" else so
    payee = ie.get("payee_name", "?")
    amount = _format_amount(ie.get("expenditure_amount"))
    date = ie.get("expenditure_date", "?")
    desc = ie.get("expenditure_description", "")
    office = ie.get("office_full", "")
    state = ie.get("state", "")
    district = ie.get("district", "")

    print(f"  [{so_label}] {candidate} — {amount} on {date}")
    print(f"    Payee: {payee}")
    if desc:
        print(f"    Description: {desc}")
    if office:
        office_str = office
        if state:
            office_str += f" — {state}"
        if district:
            office_str += f" District {district}"
        print(f"    Office: {office_str}")
    print()


def cmd_disbursements(args):
    """Search committee disbursements (Schedule B — where the money went)."""
    params = {"committee_id": args.committee_id, "sort": "-disbursement_date"}
    if args.recipient:
        params["recipient_name"] = args.recipient
    if args.description:
        params["disbursement_description"] = args.description
    if args.min_date:
        params["min_date"] = args.min_date
    if args.max_date:
        params["max_date"] = args.max_date
    if args.min_amount:
        params["min_amount"] = args.min_amount
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_b/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC disbursements {args.committee_id}"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    print(f"Found {total} disbursements from {args.committee_id} (showing {min(len(results), args.limit)})")
    print()

    # Aggregate by recipient
    recipient_totals = {}
    for r in results[:args.limit]:
        name = r.get("recipient_name", "UNKNOWN")
        amt = float(r.get("disbursement_amount", 0) or 0)
        recipient_totals[name] = recipient_totals.get(name, 0) + amt

    if recipient_totals:
        print("  === TOP RECIPIENTS ===")
        for name, total_amt in sorted(recipient_totals.items(), key=lambda x: -x[1])[:15]:
            print(f"    {name}: {_format_amount(total_amt)}")
        print()

    for r in results[:args.limit]:
        _print_disbursement(r)


def cmd_ie(args):
    """Search independent expenditures (Schedule E — support/oppose ads)."""
    params = {"committee_id": args.committee_id, "sort": "-expenditure_date"}
    if args.candidate:
        params["candidate_id"] = args.candidate
    if args.support_oppose:
        params["support_oppose_indicator"] = args.support_oppose
    if args.min_date:
        params["min_date"] = args.min_date
    if args.max_date:
        params["max_date"] = args.max_date
    if args.min_amount:
        params["min_amount"] = args.min_amount
    if args.cycle:
        params["two_year_transaction_period"] = args.cycle

    max_pages = max(1, args.limit // 100 + 1)
    results, pagination = _fetch("/schedules/schedule_e/", params, max_pages=max_pages)

    if write_output(results[:args.limit], args, summary=f"FEC IEs {args.committee_id}"):
        return
    if args.json_out:
        print(json.dumps(results[:args.limit], indent=2, default=str))
        return

    total = pagination.get("count", len(results))
    print(f"Found {total} independent expenditures from {args.committee_id} (showing {min(len(results), args.limit)})")
    print()

    # Aggregate by candidate + support/oppose
    candidate_totals = {}
    for r in results[:args.limit]:
        cand = r.get("candidate_name", "UNKNOWN")
        so = r.get("support_oppose_indicator", "?")
        key = f"{'SUPPORT' if so == 'S' else 'OPPOSE'} {cand}"
        amt = float(r.get("expenditure_amount", 0) or 0)
        candidate_totals[key] = candidate_totals.get(key, 0) + amt

    if candidate_totals:
        print("  === IE SUMMARY ===")
        for key, total_amt in sorted(candidate_totals.items(), key=lambda x: -x[1]):
            print(f"    {key}: {_format_amount(total_amt)}")
        print()

    for r in results[:args.limit]:
        _print_ie(r)


def cmd_totals(args):
    """Get committee financial summary (total receipts, disbursements, cash on hand)."""
    params = {}
    if args.cycle:
        params["cycle"] = args.cycle

    url = f"{BASE_URL}/committee/{args.committee_id}/totals/?{urlencode(params)}&api_key={_get_api_key()}"
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "OSINT-Research/1.0"})
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return

    results = data.get("results", [])

    if write_output(results, args, summary=f"FEC totals {args.committee_id}"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    if not results:
        print(f"No financial totals found for {args.committee_id}")
        return

    for t in results:
        cycle = t.get("cycle", "?")
        print(f"Committee {args.committee_id} — Cycle {cycle}")
        print(f"  Receipts:              {_format_amount(t.get('receipts'))}")
        print(f"  Disbursements:         {_format_amount(t.get('disbursements'))}")
        print(f"  Cash on Hand:          {_format_amount(t.get('last_cash_on_hand_end_period'))}")
        print(f"  Debts Owed:            {_format_amount(t.get('last_debts_owed_to_committee'))}")
        print(f"  Individual Contrib:    {_format_amount(t.get('individual_contributions'))}")
        print(f"  PAC Contributions:     {_format_amount(t.get('other_political_committee_contributions'))}")
        print(f"  Independent Expend:    {_format_amount(t.get('independent_expenditures'))}")
        cov_start = t.get("coverage_start_date", "?")
        cov_end = t.get("coverage_end_date", "?")
        print(f"  Coverage: {cov_start} to {cov_end}")
        print()


def cmd_committee(args):
    """Look up committee details."""
    results, _ = _fetch(f"/committees/{args.committee_id}/", {})

    if not results:
        # Single entity endpoint returns differently
        url = f"{BASE_URL}/committees/{args.committee_id}/?api_key={_get_api_key()}"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "OSINT-Research/1.0"})
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
                results = data.get("results", [])
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return

    if write_output(results, args, summary=f"FEC committee {args.committee_id}"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    for c in results:
        print(f"Committee: {c.get('name', '?')}")
        print(f"  ID: {c.get('committee_id', '?')}")
        print(f"  Type: {c.get('committee_type_full', c.get('committee_type', '?'))}")
        print(f"  Designation: {c.get('designation_full', c.get('designation', '?'))}")
        print(f"  Party: {c.get('party_full', c.get('party', '?'))}")
        print(f"  Treasurer: {c.get('treasurer_name', '?')}")
        cands = c.get("candidate_ids", [])
        if cands:
            print(f"  Candidate IDs: {', '.join(cands)}")
        print()


def cmd_candidate(args):
    """Search for candidates by name."""
    params = {"q": args.query, "per_page": args.limit}

    url = f"{BASE_URL}/candidates/search/?{urlencode(params)}&api_key={_get_api_key()}"
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "OSINT-Research/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return

    results = data.get("results", [])

    if write_output(results, args, summary=f"FEC candidate '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    total = data.get("pagination", {}).get("count", len(results))
    print(f"Found {total} candidates matching '{args.query}'")
    print()

    for c in results:
        print(f"  {c.get('name', '?')} ({c.get('party_full', '?')})")
        print(f"    ID: {c.get('candidate_id', '?')}")
        print(f"    Office: {c.get('office_full', '?')} — {c.get('state', '')} {c.get('district', '')}")
        print(f"    Cycles: {c.get('cycles', [])}")
        print()


def cmd_batch_persons(args):
    """Search all known network persons against FEC."""
    # Top correspondents and known associates
    persons = [
        "Jeffrey Epstein",
        "Leon Black",
        "Lawrence Summers",
        "Reid Weingarten",
        "Michael Wolff",
        "Kathryn Ruemmler",
        "Landon Thomas",
        "Steve Bannon",
        "Ehud Barak",
        "Noam Chomsky",
        "Terje Rod-Larsen",
        "Darren Indyke",
        "Richard Kahn",
        "Leslie Wexner",
        "Ghislaine Maxwell",
        "Alan Dershowitz",
        "Ken Starr",
    ]

    # Also pull entity officers from investigation.db
    db_path = Path(__file__).parent.parent / "investigation.db"
    if db_path.exists():
        db = sqlite3.connect(str(db_path))
        rows = db.execute("""
            SELECT DISTINCT person_name FROM entity_roles
            WHERE person_name IS NOT NULL AND person_name != ''
            LIMIT 30
        """).fetchall()
        db.close()
        for row in rows:
            name = row[0]
            if name not in persons and len(name.split()) >= 2:
                persons.append(name)

    print(f"Searching FEC for {len(persons)} known network persons")
    print("=" * 70)

    for person in persons:
        params = {
            "contributor_name": person,
            "sort": "-contribution_receipt_amount",
            "per_page": 5,
        }
        results, pagination = _fetch("/schedules/schedule_a/", params, max_pages=1)
        total = pagination.get("count", 0)

        if total > 0:
            total_amount = sum(
                float(r.get("contribution_receipt_amount", 0) or 0)
                for r in results
            )
            print(f"\n  {person}: {total} contributions (top 5 sample sum: ${total_amount:,.2f})")
            for r in results[:3]:
                amt = _format_amount(r.get("contribution_receipt_amount"))
                date = r.get("contribution_receipt_date", "?")
                emp = r.get("contributor_employer", "")
                to_name = r.get("committee", {}).get("name", "?")
                print(f"    {date} | {amt} | Emp: {emp} | To: {to_name}")
        else:
            print(f"\n  {person}: 0 contributions")

        time.sleep(0.5)

    print(f"\n{'='*70}")
    print("Done. Review results for disambiguation — same name ≠ same person.")


def main():
    parser = argparse.ArgumentParser(description="FEC campaign finance API")
    sub = parser.add_subparsers(dest="command", required=True)

    # donor
    p = sub.add_parser("donor", help="Search contributions by donor name")
    p.add_argument("query", help="Donor name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--min-amount", type=float, help="Minimum contribution amount")
    p.add_argument("--max-amount", type=float, help="Maximum contribution amount")
    p.add_argument("--cycle", type=int, help="Election cycle year (e.g., 2016)")
    p.add_argument("--state", help="Donor state (e.g., NY)")
    p.add_argument("--employer", help="Filter by employer name (e.g., 'Apollo')")
    add_output_args(p)

    # employer
    p = sub.add_parser("employer", help="Search contributions by employer name")
    p.add_argument("query", help="Employer name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    add_output_args(p)

    # address (by zip)
    p = sub.add_parser("address", help="Search contributions by donor ZIP code")
    p.add_argument("zip_code", help="ZIP code")
    p.add_argument("--name", help="Filter by donor name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    add_output_args(p)

    # recipient
    p = sub.add_parser("recipient", help="Contributions received by a committee")
    p.add_argument("committee_id", help="FEC committee ID (e.g., C00580100)")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    p.add_argument("--min-amount", type=float, help="Minimum amount")
    add_output_args(p)

    # disbursements (Schedule B)
    p = sub.add_parser("disbursements", help="Committee disbursements (Schedule B)")
    p.add_argument("committee_id", help="FEC committee ID (e.g., C00916114)")
    p.add_argument("--recipient", help="Filter by recipient name")
    p.add_argument("--description", help="Filter by disbursement description")
    p.add_argument("--min-date", help="Earliest date (YYYY-MM-DD)")
    p.add_argument("--max-date", help="Latest date (YYYY-MM-DD)")
    p.add_argument("--min-amount", type=float, help="Minimum amount")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    add_output_args(p)

    # ie (Schedule E — independent expenditures)
    p = sub.add_parser("ie", help="Independent expenditures (Schedule E)")
    p.add_argument("committee_id", help="FEC committee ID (e.g., C00916114)")
    p.add_argument("--candidate", help="Filter by candidate ID (e.g., P80001571)")
    p.add_argument("--support-oppose", choices=["S", "O"], help="S=support, O=oppose")
    p.add_argument("--min-date", help="Earliest date (YYYY-MM-DD)")
    p.add_argument("--max-date", help="Latest date (YYYY-MM-DD)")
    p.add_argument("--min-amount", type=float, help="Minimum amount")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    add_output_args(p)

    # totals (committee financial summary)
    p = sub.add_parser("totals", help="Committee financial summary")
    p.add_argument("committee_id", help="FEC committee ID (e.g., C00916114)")
    p.add_argument("--cycle", type=int, help="Election cycle year")
    add_output_args(p)

    # committee
    p = sub.add_parser("committee", help="Look up committee details")
    p.add_argument("committee_id", help="FEC committee ID")
    add_output_args(p)

    # candidate
    p = sub.add_parser("candidate", help="Search for candidates by name")
    p.add_argument("query", help="Candidate name")
    p.add_argument("--limit", type=int, default=10, help="Max results")
    add_output_args(p)

    # batch-persons
    p = sub.add_parser("batch-persons", help="Search all known network persons")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "donor": cmd_donor,
        "employer": cmd_employer,
        "address": cmd_address,
        "recipient": cmd_recipient,
        "disbursements": cmd_disbursements,
        "ie": cmd_ie,
        "totals": cmd_totals,
        "committee": cmd_committee,
        "candidate": cmd_candidate,
        "batch-persons": cmd_batch_persons,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
