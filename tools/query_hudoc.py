#!/usr/bin/env python3
"""
HUDOC (European Court of Human Rights) case database query tool.

Searches ECHR judgments, decisions, and communications via the undocumented
hudoc.echr.coe.int REST API. No authentication required.

Covers ~20,000 judgments and ~100,000 decisions from 1959 to present.
Can search by counsel name, respondent state, application number, or full text.

Usage:
    python tools/query_hudoc.py search "Ron Soffer"
    python tools/query_hudoc.py search "Soffer, avocat" --limit 20
    python tools/query_hudoc.py case 001-99808
    python tools/query_hudoc.py appno "34868/03"
    python tools/query_hudoc.py respondent ROU --limit 50
    python tools/query_hudoc.py text 001-99808
"""

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

SEARCH_URL = "https://hudoc.echr.coe.int/app/query/results"
TEXT_URL = "https://hudoc.echr.coe.int/app/conversion/docx/html/body"

DEFAULT_FIELDS = "itemid,documentcollectionid2,languageisocode,extractedappno,respondent,kpdate,conclusion,decisiondate,judgmentdate,docname"
RATE_LIMIT = 0.5


def _get(url, retries=2):
    """GET request with retries."""
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={
                "User-Agent": "OSINT-Research/1.0",
                "Accept": "application/json"
            })
            with urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
        except URLError:
            if attempt < retries:
                time.sleep(1)
                continue
            raise


def _build_query(terms, respondent=None, lang=None, collection=None):
    """Build HUDOC query string."""
    parts = [f'contentsitename:ECHR AND ({terms})']
    if respondent:
        parts.append(f'respondent:"{respondent}"')
    if lang:
        parts.append(f'languageisocode:"{lang}"')
    if collection:
        parts.append(f'documentcollectionid2:"{collection}"')
    return " AND ".join(parts)


def _flatten_result(r):
    """Flatten a HUDOC result into a clean dict."""
    cols = r.get("columns", {})
    return {
        "itemid": cols.get("itemid"),
        "docname": cols.get("docname"),
        "respondent": cols.get("respondent"),
        "appno": cols.get("extractedappno"),
        "conclusion": cols.get("conclusion"),
        "decision_date": cols.get("decisiondate", "").split(" ")[0] if cols.get("decisiondate") else None,
        "judgment_date": cols.get("judgmentdate", "").split(" ")[0] if cols.get("judgmentdate") else None,
        "date": (cols.get("kpdate") or "")[:10],
        "language": cols.get("languageisocode"),
        "collection": cols.get("documentcollectionid2"),
        "rank": cols.get("rank"),
    }


def search_cases(query, start=0, length=20, respondent=None, lang=None):
    """Full-text search of ECHR cases."""
    q = _build_query(f'"{query}"', respondent=respondent, lang=lang)
    params = {
        "query": q,
        "select": DEFAULT_FIELDS,
        "sort": "",
        "start": start,
        "length": length,
    }
    url = f"{SEARCH_URL}?{urlencode(params)}"
    raw = _get(url)
    data = json.loads(raw)

    results = [_flatten_result(r) for r in data.get("results", [])]
    total = data.get("resultcount", len(results))

    return {"total": total, "start": start, "length": length, "records": results}


def search_appno(appno):
    """Search by ECHR application number (e.g., 34868/03)."""
    q = f'contentsitename:ECHR AND appno:"{appno}"'
    params = {
        "query": q,
        "select": DEFAULT_FIELDS,
        "sort": "",
        "start": 0,
        "length": 20,
    }
    url = f"{SEARCH_URL}?{urlencode(params)}"
    raw = _get(url)
    data = json.loads(raw)

    results = [_flatten_result(r) for r in data.get("results", [])]
    total = data.get("resultcount", len(results))

    return {"total": total, "records": results}


def get_case(itemid):
    """Get case metadata by item ID."""
    q = f'contentsitename:ECHR AND itemid:"{itemid}"'
    params = {
        "query": q,
        "select": DEFAULT_FIELDS,
        "sort": "",
        "start": 0,
        "length": 1,
    }
    url = f"{SEARCH_URL}?{urlencode(params)}"
    raw = _get(url)
    data = json.loads(raw)

    results = data.get("results", [])
    if results:
        return _flatten_result(results[0])
    return None


def get_text(itemid):
    """Get full text of a judgment/decision as plain text."""
    url = f"{TEXT_URL}?library=ECHR&id={quote(itemid)}"
    raw_html = _get(url)

    # Strip HTML tags for plain text
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw_html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '\n', text)
    text = html.unescape(text)
    # Clean up whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()

    return text


def _print_case(c):
    """Print a single case record."""
    date = c.get("judgment_date") or c.get("decision_date") or c.get("date", "?")
    name = c.get("docname") or ""
    resp = c.get("respondent", "?")
    conclusion = c.get("conclusion", "")
    appno = c.get("appno", "")
    lang = c.get("language", "")

    print(f"\n  [{c['itemid']}] {name}")
    print(f"  Respondent: {resp} | Date: {date} | Lang: {lang}")
    if appno:
        print(f"  App nos: {appno}")
    if conclusion:
        print(f"  Conclusion: {conclusion}")
    coll = c.get("collection", "")
    if "JUDGMENTS" in coll:
        print(f"  Type: Judgment")
    elif "DECISIONS" in coll:
        print(f"  Type: Decision")
    elif "COMMUNICATED" in coll:
        print(f"  Type: Communicated case")


def main():
    parser = argparse.ArgumentParser(description="HUDOC (ECHR) case database search")
    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Full-text search of ECHR cases")
    p_search.add_argument("query", help="Search query (e.g., counsel name, topic)")
    p_search.add_argument("--limit", type=int, default=20, help="Max results")
    p_search.add_argument("--start", type=int, default=0, help="Start offset")
    p_search.add_argument("--respondent", help="Filter by respondent state code (e.g., ROU, FRA, GBR)")
    p_search.add_argument("--lang", help="Filter by language (ENG, FRE)")
    add_output_args(p_search)

    # case
    p_case = sub.add_parser("case", help="Get case by HUDOC item ID")
    p_case.add_argument("itemid", help="HUDOC item ID (e.g., 001-99808)")
    add_output_args(p_case)

    # appno
    p_appno = sub.add_parser("appno", help="Search by application number")
    p_appno.add_argument("number", help="ECHR application number (e.g., 34868/03)")
    add_output_args(p_appno)

    # text
    p_text = sub.add_parser("text", help="Get full text of a judgment/decision")
    p_text.add_argument("itemid", help="HUDOC item ID")
    p_text.add_argument("--raw", action="store_true", help="Output raw HTML instead of plain text")
    add_output_args(p_text)

    # respondent
    p_resp = sub.add_parser("respondent", help="Search by respondent state")
    p_resp.add_argument("state", help="State code (e.g., ROU, FRA, GBR, ISR)")
    p_resp.add_argument("--query", help="Additional search terms")
    p_resp.add_argument("--limit", type=int, default=20, help="Max results")
    add_output_args(p_resp)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        result = search_cases(args.query, start=args.start, length=args.limit,
                            respondent=args.respondent, lang=args.lang)
        log_search("hudoc_echr", args.query, result["total"])

        if not write_output(result, args, summary=f"HUDOC search '{args.query}'"):
            print(f"HUDOC: {result['total']} results for '{args.query}'")
            for c in result["records"]:
                _print_case(c)

    elif args.command == "case":
        result = get_case(args.itemid)
        log_search("hudoc_echr", f"case:{args.itemid}", 1 if result else 0)

        if result is None:
            print(f"No case found for item ID {args.itemid}")
            sys.exit(1)

        if not write_output(result, args, summary=f"case {args.itemid}"):
            _print_case(result)

    elif args.command == "appno":
        result = search_appno(args.number)
        log_search("hudoc_echr", f"appno:{args.number}", result["total"])

        if not write_output(result, args, summary=f"appno {args.number}"):
            print(f"HUDOC: {result['total']} results for application {args.number}")
            for c in result["records"]:
                _print_case(c)

    elif args.command == "text":
        if args.raw:
            url = f"{TEXT_URL}?library=ECHR&id={quote(args.itemid)}"
            text = _get(url)
        else:
            text = get_text(args.itemid)

        log_search("hudoc_echr", f"text:{args.itemid}", 1)

        if not write_output({"itemid": args.itemid, "text": text}, args, summary=f"text {args.itemid}"):
            print(text[:5000])
            if len(text) > 5000:
                print(f"\n... [{len(text)} chars total, truncated. Use --output to save full text]")

    elif args.command == "respondent":
        result = search_cases(args.query or "*", length=args.limit,
                            respondent=args.state)
        log_search("hudoc_echr", f"respondent:{args.state}", result["total"])

        if not write_output(result, args, summary=f"respondent {args.state}"):
            print(f"HUDOC: {result['total']} cases against {args.state}")
            for c in result["records"]:
                _print_case(c)


if __name__ == "__main__":
    main()
