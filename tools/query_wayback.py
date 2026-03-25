#!/usr/bin/env python3
"""
Wayback Machine CDX API — historical web snapshot search and retrieval.

Query the Internet Archive's CDX index to find every captured snapshot of a URL,
track when sites appeared/disappeared, detect content changes, and retrieve
archived pages. Essential for timeline reconstruction and detecting removed content.

API: https://web.archive.org/cdx/search/cdx
Auth: None required.
Rate limits: Be polite (1 req/sec). Heavy queries may timeout.

Usage:
    python tools/query_wayback.py snapshots example.com
    python tools/query_wayback.py snapshots example.com --from 2019 --to 2020
    python tools/query_wayback.py snapshots "*.example.com" --subdomains
    python tools/query_wayback.py timeline example.com
    python tools/query_wayback.py first example.com
    python tools/query_wayback.py diff example.com --from 20190101 --to 20200101
    python tools/query_wayback.py fetch example.com --timestamp 20190715
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

CDX_URL = "https://web.archive.org/cdx/search/cdx"
WEB_URL = "https://web.archive.org/web"


def _cdx_fetch(params, timeout=60):
    """Fetch from Wayback CDX API. Returns list of dicts."""
    params["output"] = "json"
    url = f"{CDX_URL}?{urlencode(params)}"
    req = Request(url, headers={
        "User-Agent": "OSINT-Research/1.0",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            if not text.strip():
                return []
            rows = json.loads(text)
            if not rows:
                return []
            # First row is headers, rest are data
            headers = rows[0]
            return [dict(zip(headers, row)) for row in rows[1:]]
    except HTTPError as e:
        if e.code == 404:
            return []
        print(f"ERROR: Wayback CDX returned {e.code}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("ERROR: Wayback CDX returned non-JSON response", file=sys.stderr)
        sys.exit(1)


def _format_timestamp(ts):
    """Convert CDX timestamp (YYYYMMDDHHmmss) to readable format."""
    if not ts or len(ts) < 8:
        return ts or "?"
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def _wayback_url(original, timestamp):
    """Build a Wayback Machine URL for a specific snapshot."""
    return f"{WEB_URL}/{timestamp}/{original}"


# -- Commands ----------------------------------------------------------------


def cmd_snapshots(args):
    """List all captured snapshots of a URL."""
    params = {
        "url": args.url,
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
    }
    if args.subdomains:
        params["matchType"] = "domain"
    if args.from_date:
        params["from"] = args.from_date
    if args.to_date:
        params["to"] = args.to_date
    if args.limit:
        params["limit"] = args.limit
    if args.mimetype:
        params["filter"] = f"mimetype:{args.mimetype}"
    if args.collapse:
        params["collapse"] = args.collapse

    records = _cdx_fetch(params)

    if write_output(records, args, summary=f"snapshots for {args.url}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(records, indent=2))
        return

    print(f"Snapshots for {args.url}: {len(records)}")
    if args.subdomains:
        print("(including subdomains)")

    for r in records[:args.display_limit]:
        ts = r.get("timestamp", "?")
        original = r.get("original", "?")
        status = r.get("statuscode", "?")
        mime = r.get("mimetype", "?")
        length = r.get("length", "?")

        status_icon = "  " if status == "200" else f"[{status}]"
        print(f"  {_format_timestamp(ts)}  {status_icon}  {mime:20s}  {length:>8s}B  {original}")


def cmd_timeline(args):
    """Show capture frequency over time for a URL."""
    params = {
        "url": args.url,
        "fl": "timestamp,statuscode",
        "collapse": "timestamp:6",  # One per month
    }
    if args.subdomains:
        params["matchType"] = "domain"

    records = _cdx_fetch(params)

    # Group by year-month
    monthly = Counter()
    yearly = Counter()
    statuses = Counter()
    for r in records:
        ts = r.get("timestamp", "")
        if len(ts) >= 6:
            monthly[ts[:6]] += 1
            yearly[ts[:4]] += 1
        statuses[r.get("statuscode", "?")] += 1

    first_ts = records[0].get("timestamp", "?") if records else "N/A"
    last_ts = records[-1].get("timestamp", "?") if records else "N/A"

    data = {
        "url": args.url,
        "total_snapshots": len(records),
        "first_capture": _format_timestamp(first_ts),
        "last_capture": _format_timestamp(last_ts),
        "status_codes": dict(statuses.most_common()),
        "yearly": dict(sorted(yearly.items())),
        "monthly": dict(sorted(monthly.items())),
    }

    if write_output(data, args, summary=f"timeline for {args.url}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Wayback Timeline: {args.url}")
    print(f"Total captures (monthly collapsed): {len(records)}")
    if records:
        print(f"First capture: {_format_timestamp(first_ts)}")
        print(f"Last capture: {_format_timestamp(last_ts)}")

    print(f"\nStatus codes: {dict(statuses.most_common())}")

    print(f"\nYearly captures:")
    for year, count in sorted(yearly.items()):
        bar = "#" * min(count, 60)
        print(f"  {year}: {bar} ({count})")

    if args.monthly:
        print(f"\nMonthly captures:")
        for month, count in sorted(monthly.items()):
            bar = "#" * min(count, 40)
            ym = f"{month[:4]}-{month[4:6]}"
            print(f"  {ym}: {bar} ({count})")


def cmd_first(args):
    """Find the first known capture of a URL."""
    params = {
        "url": args.url,
        "fl": "timestamp,original,statuscode,mimetype",
        "limit": 1,
        "sort": "default",  # Oldest first
    }

    records = _cdx_fetch(params)

    if not records:
        print(f"No captures found for {args.url}")
        return

    r = records[0]
    data = {
        "url": args.url,
        "first_capture": _format_timestamp(r.get("timestamp", "")),
        "timestamp": r.get("timestamp", ""),
        "original": r.get("original", ""),
        "statuscode": r.get("statuscode", ""),
        "wayback_url": _wayback_url(r.get("original", args.url), r.get("timestamp", "")),
    }

    if write_output(data, args, summary=f"first capture of {args.url}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"First capture of {args.url}:")
    print(f"  Date: {data['first_capture']}")
    print(f"  Status: {r.get('statuscode', '?')}")
    print(f"  URL: {data['wayback_url']}")


def cmd_diff(args):
    """Compare snapshots between two dates by listing unique content digests."""
    params_from = {
        "url": args.url,
        "fl": "timestamp,digest,statuscode,mimetype",
        "limit": 1,
        "to": args.from_date,
        "sort": "default",
    }
    params_to = {
        "url": args.url,
        "fl": "timestamp,digest,statuscode,mimetype",
        "limit": 1,
        "to": args.to_date,
        "sort": "default",
    }

    # Get all snapshots in range, collapsed by digest (unique content versions)
    params_range = {
        "url": args.url,
        "fl": "timestamp,digest,statuscode,mimetype,length",
        "collapse": "digest",
    }
    if args.from_date:
        params_range["from"] = args.from_date
    if args.to_date:
        params_range["to"] = args.to_date

    records = _cdx_fetch(params_range)

    data = {
        "url": args.url,
        "from": args.from_date,
        "to": args.to_date,
        "unique_versions": len(records),
        "versions": records,
    }

    if write_output(data, args, summary=f"diff for {args.url}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Content versions for {args.url}")
    print(f"Period: {args.from_date or 'earliest'} to {args.to_date or 'latest'}")
    print(f"Unique versions (by digest): {len(records)}")

    for r in records:
        ts = r.get("timestamp", "?")
        digest = r.get("digest", "?")[:12]
        status = r.get("statuscode", "?")
        length = r.get("length", "?")
        wb_url = _wayback_url(args.url, ts)
        print(f"\n  {_format_timestamp(ts)}  [{status}]  {length:>8s}B  digest:{digest}")
        print(f"    {wb_url}")


def cmd_fetch(args):
    """Fetch an archived page from the Wayback Machine."""
    if args.timestamp:
        url = f"{WEB_URL}/{args.timestamp}id_/{args.url}"
    else:
        url = f"{WEB_URL}/{args.url}"

    req = Request(url, headers={"User-Agent": "OSINT-Research/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            final_url = resp.url
    except HTTPError as e:
        print(f"ERROR: Wayback returned {e.code} for {url}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    data = {
        "url": args.url,
        "timestamp": args.timestamp or "latest",
        "wayback_url": final_url,
        "content_length": len(content),
        "content": content[:args.max_length] if args.max_length else content,
    }

    if write_output(data, args, summary=f"fetch {args.url} @ {args.timestamp or 'latest'}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Fetched: {final_url}")
    print(f"Content length: {len(content)} chars")
    if args.max_length:
        print(content[:args.max_length])
    else:
        print(content[:5000])
        if len(content) > 5000:
            print(f"\n... truncated ({len(content)} total chars, use --output to save full content)")


# -- CLI ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Wayback Machine CDX — historical web snapshot search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # snapshots
    p = sub.add_parser("snapshots", help="List all captured snapshots of a URL")
    p.add_argument("url", help="URL or domain to search")
    p.add_argument("--subdomains", action="store_true", help="Include all subdomains (matchType=domain)")
    p.add_argument("--from", dest="from_date", help="Start date (YYYY, YYYYMM, or YYYYMMDD)")
    p.add_argument("--to", dest="to_date", help="End date (YYYY, YYYYMM, or YYYYMMDD)")
    p.add_argument("--limit", type=int, default=500, help="Max CDX results (default 500)")
    p.add_argument("--display-limit", type=int, default=50, help="Max results to display (default 50)")
    p.add_argument("--mimetype", help="Filter by MIME type (e.g. text/html)")
    p.add_argument("--collapse", help="Collapse field (e.g. 'digest' for unique content, 'timestamp:6' for monthly)")
    add_output_args(p)

    # timeline
    p = sub.add_parser("timeline", help="Capture frequency over time")
    p.add_argument("url", help="URL or domain")
    p.add_argument("--subdomains", action="store_true", help="Include all subdomains")
    p.add_argument("--monthly", action="store_true", help="Show monthly breakdown (not just yearly)")
    add_output_args(p)

    # first
    p = sub.add_parser("first", help="Find the first known capture")
    p.add_argument("url", help="URL or domain")
    add_output_args(p)

    # diff
    p = sub.add_parser("diff", help="Show unique content versions between dates")
    p.add_argument("url", help="URL to compare")
    p.add_argument("--from", dest="from_date", help="Start date")
    p.add_argument("--to", dest="to_date", help="End date")
    add_output_args(p)

    # fetch
    p = sub.add_parser("fetch", help="Fetch an archived page")
    p.add_argument("url", help="URL to fetch")
    p.add_argument("--timestamp", help="Specific timestamp (YYYYMMDDHHmmss) or omit for latest")
    p.add_argument("--max-length", type=int, default=0, help="Max content chars to return (0=unlimited)")
    add_output_args(p)

    args = parser.parse_args()
    commands = {
        "snapshots": cmd_snapshots,
        "timeline": cmd_timeline,
        "first": cmd_first,
        "diff": cmd_diff,
        "fetch": cmd_fetch,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
