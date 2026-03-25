#!/usr/bin/env python3
"""
Senate LDA (Lobbying Disclosure Act) API wrapper.

Searches federal lobbying registrations, activity reports (LD-2),
and contribution reports (LD-203).

API: https://lda.senate.gov/api/v1/
Auth: None required.
Note: Migrating to LDA.gov by 06/30/2026.

Usage:
    python tools/query_lobbying.py client "International Peace Institute"
    python tools/query_lobbying.py client "Humpty Dumpty Institute"
    python tools/query_lobbying.py registrant "Epstein"
    python tools/query_lobbying.py lobbyist "Weingarten"
    python tools/query_lobbying.py filings --client "International Peace Institute" --type ld2
    python tools/query_lobbying.py contributions "International Peace Institute"
"""

import argparse
import gzip
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


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


BASE_URL = "https://lda.senate.gov/api/v1"

# Filing type codes
FILING_TYPES = {
    "RR": "Registration",
    "Q1": "1st Quarter Report",
    "Q2": "2nd Quarter Report",
    "Q3": "3rd Quarter Report",
    "Q4": "4th Quarter Report",
    "MY": "Mid-Year Report",
    "MM": "Mid-Year Report",
    "YE": "Year-End Report",
    "Q1A": "1st Quarter Amendment",
    "Q2A": "2nd Quarter Amendment",
    "Q3A": "3rd Quarter Amendment",
    "Q4A": "4th Quarter Amendment",
    "MYA": "Mid-Year Amendment",
    "YEA": "Year-End Amendment",
    "RA": "Registration Amendment",
    "TR": "Termination Report",
    "TA": "Termination Amendment",
    "ML": "Misc Document",
}


def _fetch(endpoint, params=None):
    """Fetch from LDA API."""
    if params is None:
        params = {}

    url = f"{BASE_URL}{endpoint}"
    if params:
        url += "?" + urlencode(params, doseq=True)

    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except TimeoutError:
        print(f"ERROR: Request timed out (60s). LDA API may be slow — try again.", file=sys.stderr)
        return None
    except HTTPError as e:
        raw = e.read()
        try:
            body = gzip.decompress(raw).decode()[:500]
        except Exception:
            try:
                body = raw.decode()[:500]
            except Exception:
                body = str(raw[:200])
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None


def _paginate(endpoint, params, max_results=100):
    """Paginate through LDA results."""
    params["page_size"] = min(25, max_results)
    all_results = []
    page = 1

    while len(all_results) < max_results:
        params["page"] = page
        data = _fetch(endpoint, params)
        if not data:
            break

        results = data.get("results", [])
        all_results.extend(results)

        if not data.get("next"):
            break
        page += 1
        time.sleep(0.5)

    return all_results[:max_results], data.get("count", len(all_results)) if data else 0


def _format_money(val):
    """Format dollar value."""
    if val is None:
        return ""
    try:
        return f"${float(val):,.0f}"
    except (ValueError, TypeError):
        return str(val)


def _print_filing(f):
    """Print a single lobbying filing."""
    filing_type = f.get("filing_type", "?")
    filing_desc = FILING_TYPES.get(filing_type, f.get("filing_type_display", filing_type))
    year = f.get("filing_year", "?")
    period = f.get("filing_period_display", f.get("filing_period", ""))

    registrant = f.get("registrant", {})
    reg_name = registrant.get("name", "?") if isinstance(registrant, dict) else "?"

    client = f.get("client", {})
    client_name = client.get("name", "?") if isinstance(client, dict) else "?"

    income = _format_money(f.get("income"))
    expenses = _format_money(f.get("expenses"))

    print(f"  [{filing_type}] {filing_desc} — {year} {period}")
    print(f"    Registrant: {reg_name}")
    print(f"    Client: {client_name}")
    if income:
        print(f"    Income: {income}")
    if expenses:
        print(f"    Expenses: {expenses}")

    # Show lobbying activities if present
    activities = f.get("lobbying_activities", [])
    if activities:
        print(f"    Activities ({len(activities)}):")
        for act in activities[:5]:
            issue = act.get("general_issue_code_display", act.get("general_issue_code", "?"))
            desc = act.get("description", "")
            print(f"      Issue: {issue}")
            if desc:
                print(f"      Detail: {desc[:150]}")
            lobbyists = act.get("lobbyists", [])
            if lobbyists:
                names = []
                for l in lobbyists:
                    lob = l.get("lobbyist", l) if isinstance(l, dict) else {}
                    if isinstance(lob, dict):
                        first = lob.get("first_name", "")
                        last = lob.get("last_name", "")
                        name = lob.get("name", f"{first} {last}".strip())
                    else:
                        name = str(lob)
                    if name:
                        names.append(name)
                if names:
                    print(f"      Lobbyists: {', '.join(names)}")

    doc_url = f.get("filing_document_url", "")
    if doc_url:
        print(f"    Document: {doc_url}")

    print()


def cmd_client(args):
    """Search lobbying filings by client organization name."""
    params = {"client_name": args.query}
    if args.year:
        params["filing_year"] = args.year

    results, total = _paginate("/filings/", params, max_results=args.limit)
    _log(args.query, "lobbying", total)

    if write_output(results, args, summary=f"LDA client '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {total} lobbying filings for client '{args.query}' (showing {len(results)})")
    print()

    for f in results:
        _print_filing(f)


def cmd_registrant(args):
    """Search lobbying registrants (lobbying firms/organizations)."""
    params = {"registrant_name": args.query}
    if args.year:
        params["filing_year"] = args.year

    results, total = _paginate("/filings/", params, max_results=args.limit)
    _log(args.query, "lobbying", total)

    if write_output(results, args, summary=f"LDA registrant '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"Found {total} filings for registrant '{args.query}' (showing {len(results)})")
    print()

    for f in results:
        _print_filing(f)


def cmd_lobbyist(args):
    """Search for individual lobbyists by name."""
    # lobbyist_name is the correct filter (not 'search' which returns all)
    params = {"lobbyist_name": args.query}
    data = _fetch("/lobbyists/", params)

    if data and data.get("results"):
        results = data["results"]
        total = data.get("count", len(results))
        _log(args.query, "lobbying", total)

        if write_output(results[:args.limit], args, summary=f"LDA lobbyist '{args.query}'"):
            return
        if args.json_out:
            print(json.dumps(results[:args.limit], indent=2, default=str))
            return
        print(f"Found {total} lobbyists matching '{args.query}' (showing {min(len(results), args.limit)})")
        print()
        for l in results[:args.limit]:
            lobbyist = l.get("lobbyist", l) if isinstance(l, dict) else {}
            if isinstance(lobbyist, dict):
                first = lobbyist.get("first_name", "")
                last = lobbyist.get("last_name", "")
                prefix = lobbyist.get("prefix", "")
                suffix = lobbyist.get("suffix", "")
                full_name = " ".join(p for p in [prefix, first, last, suffix] if p).strip()
                lob_id = lobbyist.get("id", "?")
            else:
                full_name = str(lobbyist)
                lob_id = "?"

            print(f"  {full_name or '?'}")
            print(f"    ID: {lob_id}")
            registrant = l.get("registrant") if isinstance(l, dict) else None
            if registrant and isinstance(registrant, dict):
                print(f"    Registrant: {registrant.get('name', '?')}")
            print()

        # Also show filings for this lobbyist to give client/issue context
        if total > 0 and total <= 20:
            print(f"--- Filings involving '{args.query}' ---")
            print()
            filing_params = {"lobbyist_name": args.query}
            filing_results, filing_total = _paginate("/filings/", filing_params, max_results=args.limit)
            print(f"Found {filing_total} filings")
            print()
            for f in filing_results:
                _print_filing(f)
    else:
        # Fall back to filings search by lobbyist_name
        params = {"lobbyist_name": args.query}
        results, total = _paginate("/filings/", params, max_results=args.limit)
        _log(args.query, "lobbying", total)
        print(f"Found {total} filings with lobbyist '{args.query}' (showing {len(results)})")
        print()
        for f in results:
            _print_filing(f)


def cmd_filings(args):
    """Search lobbying filings with filters."""
    params = {}
    if args.client:
        params["client_name"] = args.client
    if args.registrant:
        params["registrant_name"] = args.registrant
    if args.year:
        params["filing_year"] = args.year
    if args.type:
        params["filing_type"] = args.type.upper()

    results, total = _paginate("/filings/", params, max_results=args.limit)

    if write_output(results, args, summary="LDA filings search"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    filters = []
    if args.client:
        filters.append(f"client='{args.client}'")
    if args.registrant:
        filters.append(f"registrant='{args.registrant}'")
    if args.type:
        filters.append(f"type={args.type}")
    if args.year:
        filters.append(f"year={args.year}")

    print(f"Found {total} filings ({', '.join(filters)}) — showing {len(results)}")
    print()

    for f in results:
        _print_filing(f)


def cmd_contributions(args):
    """Search LD-203 contribution reports for an organization."""
    # The /contributions/ endpoint requires registrant_name (not search)
    params = {"registrant_name": args.query}
    results, total = _paginate("/contributions/", params, max_results=args.limit)

    if write_output(results, args, summary=f"LDA contributions '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(results, indent=2, default=str))
        return

    if results:
        print(f"Found {total} contribution reports mentioning '{args.query}' (showing {len(results)})")
        print()
        for c in results:
            # Contribution records have different structure
            registrant = c.get("registrant", {})
            reg_name = registrant.get("name", "?") if isinstance(registrant, dict) else "?"
            filing_year = c.get("filing_year", "?")
            filing_type = c.get("filing_type_display", c.get("filing_type", "?"))

            print(f"  [{filing_type}] {filing_year}")
            print(f"    Registrant: {reg_name}")

            # Show individual contributions within the report
            contribs = c.get("contribution_items", c.get("contributions", []))
            if contribs:
                for item in contribs[:10]:
                    payee = item.get("payee_name", item.get("name", "?"))
                    amount = _format_money(item.get("amount"))
                    date = item.get("date", item.get("contribution_date", ""))
                    print(f"    → {payee}: {amount} ({date})")

            doc_url = c.get("filing_document_url", "")
            if doc_url:
                print(f"    Document: {doc_url}")
            print()
    else:
        print(f"No LD-203 contribution reports found for registrant '{args.query}'")
        print("Note: LD-203s are filed by lobbying FIRMS (registrants), not their clients.")
        print("If searching for a client, find their registrant first via: lobbying.py client \"<name>\"")
        print()
        # Try a shorter name in case the full name doesn't match
        short = args.query.split()[0] if " " in args.query else None
        if short and len(short) >= 3:
            params = {"registrant_name": short}
            results, total = _paginate("/contributions/", params, max_results=args.limit)
            if results:
                print(f"Partial match: {total} reports for registrant starting with '{short}'")
                print()
                for c in results[:5]:
                    registrant = c.get("registrant", {})
                    reg_name = registrant.get("name", "?") if isinstance(registrant, dict) else "?"
                    filing_year = c.get("filing_year", "?")
                    print(f"  [{filing_year}] {reg_name}")
                    doc_url = c.get("filing_document_url", "")
                    if doc_url:
                        print(f"    Document: {doc_url}")
                    print()


def main():
    parser = argparse.ArgumentParser(description="Senate LDA lobbying disclosure API")
    sub = parser.add_subparsers(dest="command", required=True)

    # client
    p = sub.add_parser("client", help="Search by client organization name")
    p.add_argument("query", help="Client name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--year", type=int, help="Filing year")
    add_output_args(p)

    # registrant
    p = sub.add_parser("registrant", help="Search by registrant (lobbying firm)")
    p.add_argument("query", help="Registrant name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--year", type=int, help="Filing year")
    add_output_args(p)

    # lobbyist
    p = sub.add_parser("lobbyist", help="Search by individual lobbyist name")
    p.add_argument("query", help="Lobbyist name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    # filings
    p = sub.add_parser("filings", help="Search filings with filters")
    p.add_argument("--client", help="Client name filter")
    p.add_argument("--registrant", help="Registrant name filter")
    p.add_argument("--type", help="Filing type (RR, Q1, Q2, Q3, Q4, MY, YE, TR, etc.)")
    p.add_argument("--year", type=int, help="Filing year")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    # contributions
    p = sub.add_parser("contributions", help="Search LD-203 contribution reports")
    p.add_argument("query", help="Organization name")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "client": cmd_client,
        "registrant": cmd_registrant,
        "lobbyist": cmd_lobbyist,
        "filings": cmd_filings,
        "contributions": cmd_contributions,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
