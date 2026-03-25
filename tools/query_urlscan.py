#!/usr/bin/env python3
"""
URLScan.io API — passive web scan search and analysis.

Search past scans to discover technology stacks, linked domains, hosting details,
IP addresses, and page content without touching the target. Reveals what a website
actually does (HTTP transactions, external resources, scripts) vs. what it shows.

API: https://urlscan.io/api/v1/
Auth: Optional. Free tier allows search of public scans. API key for submissions.
      Set URLSCAN_API_KEY in .env for higher rate limits and scan submission.
Rate limits: 60 req/min (search), 2 req/min (submit) without API key.

Usage:
    python tools/query_urlscan.py search domain:example.com
    python tools/query_urlscan.py search ip:198.202.211.1
    python tools/query_urlscan.py search "page.title:Leading The Future"
    python tools/query_urlscan.py search "server:cloudflare AND domain:example.com"
    python tools/query_urlscan.py result <scan-uuid>
    python tools/query_urlscan.py technologies <scan-uuid>
    python tools/query_urlscan.py links <scan-uuid>
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

BASE_URL = "https://urlscan.io/api/v1"


def _get_headers():
    """Build request headers, including API key if available."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    }
    api_key = os.environ.get("URLSCAN_API_KEY")
    if api_key:
        headers["API-Key"] = api_key
    return headers


def _fetch(url, timeout=30):
    """Fetch from URLScan.io API."""
    req = Request(url, headers=_get_headers())
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if e.code == 429:
            print("ERROR: URLScan.io rate limit exceeded. Wait and retry.", file=sys.stderr)
        elif e.code == 404:
            print("ERROR: Scan not found.", file=sys.stderr)
        else:
            print(f"ERROR: URLScan.io returned {e.code}: {body[:500]}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)


# -- Commands ----------------------------------------------------------------


def cmd_search(args):
    """Search public URLScan.io scans."""
    params = {
        "q": args.query,
        "size": args.limit,
    }
    if args.after:
        params["search_after"] = args.after

    url = f"{BASE_URL}/search/?{urlencode(params)}"
    data = _fetch(url)

    results = data.get("results", [])

    if write_output(data, args, summary=f"urlscan '{args.query}'"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"URLScan.io results for '{args.query}': {data.get('total', 0)} total, showing {len(results)}")

    for r in results:
        task = r.get("task", {})
        page = r.get("page", {})
        stats = r.get("stats", {})

        scan_time = task.get("time", "?")[:10]
        scan_url = task.get("url", "?")
        uuid = task.get("uuid", "?")
        ip = page.get("ip", "?")
        server = page.get("server", "?")
        title = page.get("title", "")
        asn = page.get("asn", "?")
        asnname = page.get("asnname", "?")
        country = page.get("country", "?")
        tls_issuer = page.get("tlsIssuer", "")
        tls_days = page.get("tlsValidDays", "")
        requests = stats.get("requests", 0)
        ips = stats.get("uniqIPs", 0)

        print(f"\n  [{scan_time}] {scan_url}")
        print(f"    IP: {ip} | ASN: {asn} ({asnname}) | Country: {country}")
        print(f"    Server: {server}")
        if title:
            print(f"    Title: {title}")
        if tls_issuer:
            print(f"    TLS: {tls_issuer} ({tls_days}d valid)")
        print(f"    Requests: {requests} | Unique IPs: {ips}")
        print(f"    UUID: {uuid}")


def cmd_result(args):
    """Get full scan result details."""
    url = f"{BASE_URL}/result/{args.uuid}/"
    data = _fetch(url, timeout=60)

    if write_output(data, args, summary=f"result {args.uuid}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    # Extract key info
    page = data.get("page", {})
    lists = data.get("lists", {})
    meta = data.get("meta", {})
    stats = data.get("stats", {})
    verdicts = data.get("verdicts", {})

    print(f"Scan Result: {args.uuid}")
    print(f"  URL: {page.get('url', '?')}")
    print(f"  IP: {page.get('ip', '?')}")
    print(f"  Country: {page.get('country', '?')}")
    print(f"  Server: {page.get('server', '?')}")
    print(f"  Status: {page.get('status', '?')}")

    # IPs contacted
    ips = lists.get("ips", [])
    if ips:
        print(f"\n  IPs contacted ({len(ips)}):")
        for ip_info in ips[:10]:
            if isinstance(ip_info, str):
                print(f"    {ip_info}")
            else:
                print(f"    {ip_info}")

    # Domains contacted
    domains = lists.get("domains", [])
    if domains:
        print(f"\n  Domains contacted ({len(domains)}):")
        for d in domains[:20]:
            print(f"    {d}")

    # URLs
    urls = lists.get("urls", [])
    if urls:
        print(f"\n  URLs loaded ({len(urls)}):")
        for u in urls[:15]:
            print(f"    {u}")

    # Certificates
    certs = lists.get("certificates", [])
    if certs:
        print(f"\n  Certificates ({len(certs)}):")
        for c in certs[:10]:
            if isinstance(c, dict):
                print(f"    {c.get('subject', {}).get('CN', '?')} — {c.get('issuer', {}).get('O', '?')}")
            else:
                print(f"    {c}")

    # Technologies
    techs = meta.get("processors", {}).get("wappa", {}).get("data", [])
    if techs:
        print(f"\n  Technologies detected:")
        for t in techs:
            if isinstance(t, dict):
                cats = ", ".join(c.get("name", "?") for c in t.get("categories", []))
                print(f"    {t.get('app', '?')} ({cats})")

    # Verdicts
    overall = verdicts.get("overall", {})
    if overall:
        print(f"\n  Verdict: {'MALICIOUS' if overall.get('malicious') else 'Clean'} (score: {overall.get('score', '?')})")


def cmd_technologies(args):
    """Extract detected technologies from a scan result."""
    url = f"{BASE_URL}/result/{args.uuid}/"
    data = _fetch(url, timeout=60)

    meta = data.get("meta", {})
    techs = meta.get("processors", {}).get("wappa", {}).get("data", [])

    tech_list = []
    for t in techs:
        if isinstance(t, dict):
            cats = [c.get("name", "?") for c in t.get("categories", [])]
            tech_list.append({
                "app": t.get("app", "?"),
                "categories": cats,
                "website": t.get("website", ""),
                "confidence": t.get("confidenceTotal", 0),
            })

    result = {"uuid": args.uuid, "technologies": tech_list}

    if write_output(result, args, summary=f"technologies for {args.uuid}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2))
        return

    print(f"Technologies detected in scan {args.uuid}:")
    for t in tech_list:
        cats = ", ".join(t["categories"])
        print(f"  {t['app']:30s}  [{cats}]")


def cmd_links(args):
    """Extract all domains and IPs contacted during a scan."""
    url = f"{BASE_URL}/result/{args.uuid}/"
    data = _fetch(url, timeout=60)

    lists = data.get("lists", {})
    result = {
        "uuid": args.uuid,
        "domains": lists.get("domains", []),
        "ips": lists.get("ips", []),
        "urls": lists.get("urls", []),
        "certificates": lists.get("certificates", []),
        "hashes": lists.get("hashes", []),
    }

    if write_output(result, args, summary=f"links for {args.uuid}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2))
        return

    domains = result["domains"]
    ips = result["ips"]
    urls = result["urls"]

    print(f"Links from scan {args.uuid}:")
    if domains:
        print(f"\n  Domains ({len(domains)}):")
        for d in domains:
            print(f"    {d}")
    if ips:
        print(f"\n  IPs ({len(ips)}):")
        for ip in ips[:20]:
            print(f"    {ip}")
    if urls:
        print(f"\n  URLs ({len(urls)}):")
        for u in urls[:30]:
            print(f"    {u}")


# -- CLI ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="URLScan.io — passive web scan search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search public scans")
    p.add_argument("query", help="Search query (e.g. 'domain:example.com', 'ip:1.2.3.4', 'page.title:X')")
    p.add_argument("--limit", type=int, default=20, help="Max results (default 20, max 100)")
    p.add_argument("--after", help="Pagination cursor (search_after value)")
    add_output_args(p)

    # result
    p = sub.add_parser("result", help="Get full scan result")
    p.add_argument("uuid", help="Scan UUID")
    add_output_args(p)

    # technologies
    p = sub.add_parser("technologies", help="Extract detected technologies from a scan")
    p.add_argument("uuid", help="Scan UUID")
    add_output_args(p)

    # links
    p = sub.add_parser("links", help="Extract all contacted domains/IPs/URLs from a scan")
    p.add_argument("uuid", help="Scan UUID")
    add_output_args(p)

    args = parser.parse_args()
    commands = {
        "search": cmd_search,
        "result": cmd_result,
        "technologies": cmd_technologies,
        "links": cmd_links,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
