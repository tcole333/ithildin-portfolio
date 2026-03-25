#!/usr/bin/env python3
"""
Shodan API wrapper for infrastructure reconnaissance.

Searches internet-connected devices, resolves IPs, enumerates DNS records,
and discovers SSL certificates. Useful for mapping organizational infrastructure,
identifying hosting providers, and finding exposed services.

API: https://api.shodan.io
Auth: API key required. Set SHODAN_API_KEY in .env.
Rate limits: Depend on plan tier (1 req/sec for free, higher for paid).

Usage:
    python tools/query_shodan.py host 198.202.211.1
    python tools/query_shodan.py search "ssl:leadingthefuture.com"
    python tools/query_shodan.py search "org:\"Webflow\" port:443" --limit 50
    python tools/query_shodan.py domain leadingthefuture.com
    python tools/query_shodan.py dns-resolve google.com,example.com
    python tools/query_shodan.py reverse-dns 8.8.8.8,8.8.4.4
    python tools/query_shodan.py ssl-cert leadingthefuture.com
    python tools/query_shodan.py info
"""

import argparse
import json
import os
import sys
import time
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

BASE_URL = "https://api.shodan.io"


def _get_api_key():
    """Get Shodan API key from environment."""
    key = os.environ.get("SHODAN_API_KEY")
    if not key:
        print("ERROR: SHODAN_API_KEY not set in .env.", file=sys.stderr)
        sys.exit(1)
    return key


def _fetch(endpoint, params=None):
    """Fetch from Shodan API."""
    if params is None:
        params = {}
    params["key"] = _get_api_key()

    url = f"{BASE_URL}{endpoint}?{urlencode(params, doseq=True)}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if e.code == 401:
            print("ERROR: Invalid Shodan API key.", file=sys.stderr)
        elif e.code == 402:
            print("ERROR: Shodan query requires a paid plan.", file=sys.stderr)
        elif e.code == 429:
            print("ERROR: Shodan rate limit exceeded. Wait and retry.", file=sys.stderr)
        else:
            print(f"ERROR: Shodan API returned {e.code}: {body[:500]}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────


def cmd_host(args):
    """Get all available information for an IP address."""
    data = _fetch(f"/shodan/host/{args.ip}", {"minify": "false"})

    if write_output(data, args, summary=f"host {args.ip}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"IP: {data.get('ip_str', 'N/A')}")
    print(f"Org: {data.get('org', 'N/A')}")
    print(f"ASN: {data.get('asn', 'N/A')}")
    print(f"ISP: {data.get('isp', 'N/A')}")
    print(f"OS: {data.get('os', 'N/A')}")
    print(f"Location: {data.get('city', '?')}, {data.get('country_name', '?')}")

    hostnames = data.get("hostnames", [])
    if hostnames:
        print(f"Hostnames: {', '.join(hostnames)}")

    domains = data.get("domains", [])
    if domains:
        print(f"Domains: {', '.join(domains)}")

    ports = data.get("ports", [])
    if ports:
        print(f"Ports: {', '.join(str(p) for p in sorted(ports))}")

    vulns = data.get("vulns", [])
    if vulns:
        print(f"Vulnerabilities: {', '.join(sorted(vulns))}")

    print(f"\n{'─' * 60}")
    for service in data.get("data", []):
        port = service.get("port", "?")
        transport = service.get("transport", "tcp")
        product = service.get("product", "")
        version = service.get("version", "")
        module = service.get("_shodan", {}).get("module", "")
        print(f"\nPort {port}/{transport} ({module})")
        if product:
            print(f"  Product: {product} {version}".strip())

        ssl = service.get("ssl", {})
        if ssl:
            cert = ssl.get("cert", {})
            subject = cert.get("subject", {})
            issuer = cert.get("issuer", {})
            if subject:
                cn = subject.get("CN", "N/A")
                print(f"  SSL Subject: {cn}")
            if issuer:
                print(f"  SSL Issuer: {issuer.get('O', 'N/A')}")
            sans = cert.get("extensions", {}).get("subjectAltName", []) if cert else []
            if sans:
                print(f"  SANs: {', '.join(sans[:10])}")
                if len(sans) > 10:
                    print(f"        ... and {len(sans) - 10} more")

        http = service.get("http", {})
        if http:
            title = http.get("title", "")
            server = http.get("server", "")
            if title:
                print(f"  HTTP Title: {title}")
            if server:
                print(f"  HTTP Server: {server}")


def cmd_search(args):
    """Search Shodan for devices matching a query."""
    params = {"query": args.query}
    if args.facets:
        params["facets"] = args.facets

    # Use /shodan/host/search for full results, /shodan/host/count for counts only
    if args.count_only:
        data = _fetch("/shodan/host/count", params)
    else:
        params["page"] = args.page
        data = _fetch("/shodan/host/search", params)

    if write_output(data, args, summary=f"search '{args.query}'"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    total = data.get("total", 0)
    print(f"Total results: {total}")

    if args.count_only:
        for facet_name, facet_values in data.get("facets", {}).items():
            print(f"\n{facet_name}:")
            for item in facet_values[:20]:
                print(f"  {item['value']}: {item['count']}")
        return

    matches = data.get("matches", [])
    for i, m in enumerate(matches[:args.limit]):
        ip = m.get("ip_str", "N/A")
        port = m.get("port", "?")
        org = m.get("org", "N/A")
        hostnames = ", ".join(m.get("hostnames", [])) or "N/A"
        product = m.get("product", "")
        location = f"{m.get('location', {}).get('city', '?')}, {m.get('location', {}).get('country_name', '?')}"

        print(f"\n[{i+1}] {ip}:{port}")
        print(f"    Org: {org} | Location: {location}")
        print(f"    Hostnames: {hostnames}")
        if product:
            print(f"    Product: {product} {m.get('version', '')}".strip())

        ssl = m.get("ssl", {})
        if ssl:
            cn = ssl.get("cert", {}).get("subject", {}).get("CN", "")
            if cn:
                print(f"    SSL CN: {cn}")


def cmd_domain(args):
    """Get DNS information for a domain."""
    params = {}
    if args.history:
        params["history"] = "true"
    if args.type:
        params["type"] = args.type

    data = _fetch(f"/dns/domain/{args.domain}", params)

    if write_output(data, args, summary=f"domain {args.domain}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Domain: {data.get('domain', args.domain)}")
    tags = data.get("tags", [])
    if tags:
        print(f"Tags: {', '.join(tags)}")

    subdomains = data.get("subdomains", [])
    if subdomains:
        print(f"Subdomains: {', '.join(sorted(subdomains))}")

    print(f"\nDNS Records:")
    for record in data.get("data", []):
        rtype = record.get("type", "?")
        subdomain = record.get("subdomain", "@") or "@"
        value = record.get("value", "N/A")
        last_seen = record.get("last_seen", "")
        print(f"  {subdomain:30s} {rtype:6s} {value}")
        if last_seen and args.history:
            print(f"  {'':30s}        (last seen: {last_seen})")


def cmd_dns_resolve(args):
    """Resolve hostnames to IPs."""
    hostnames = args.hostnames.replace(" ", "")
    data = _fetch("/dns/resolve", {"hostnames": hostnames})

    if write_output(data, args, summary=f"dns-resolve {hostnames}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    for hostname, ip in sorted(data.items()):
        print(f"{hostname:40s} -> {ip or 'NXDOMAIN'}")


def cmd_reverse_dns(args):
    """Reverse DNS lookup for IPs."""
    ips = args.ips.replace(" ", "")
    data = _fetch("/dns/reverse", {"ips": ips})

    if write_output(data, args, summary=f"reverse-dns {ips}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    for ip, hostnames in sorted(data.items()):
        names = ", ".join(hostnames) if hostnames else "(no PTR)"
        print(f"{ip:20s} -> {names}")


def cmd_ssl_cert(args):
    """Search for SSL certificates by domain name."""
    query = f"ssl.cert.subject.CN:{args.domain}"
    params = {"query": query, "page": 1}
    data = _fetch("/shodan/host/search", params)

    if write_output(data, args, summary=f"ssl-cert {args.domain}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    total = data.get("total", 0)
    print(f"Hosts with SSL certs for '{args.domain}': {total}")

    matches = data.get("matches", [])
    seen_ips = set()
    for m in matches:
        ip = m.get("ip_str", "N/A")
        if ip in seen_ips:
            continue
        seen_ips.add(ip)

        port = m.get("port", "?")
        org = m.get("org", "N/A")
        ssl = m.get("ssl", {})
        cert = ssl.get("cert", {})
        subject = cert.get("subject", {})
        issuer = cert.get("issuer", {})
        expires = cert.get("expires", "N/A")

        print(f"\n  {ip}:{port} ({org})")
        print(f"    Subject CN: {subject.get('CN', 'N/A')}")
        print(f"    Issuer: {issuer.get('O', 'N/A')}")
        print(f"    Expires: {expires}")

        sans = cert.get("extensions", {}).get("subjectAltName", []) if cert else []
        if sans:
            print(f"    SANs: {', '.join(sans[:5])}")
            if len(sans) > 5:
                print(f"          ... and {len(sans) - 5} more")


def cmd_info(args):
    """Show API plan info and remaining credits."""
    data = _fetch("/api-info")

    if write_output(data, args, summary="api-info"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Plan: {data.get('plan', 'N/A')}")
    print(f"Query Credits: {data.get('query_credits', 0)}")
    print(f"Scan Credits: {data.get('scan_credits', 0)}")
    print(f"Monitored IPs: {data.get('monitored_ips', 0)} (limit: {data.get('monitored_ips_limit', 0)})")
    print(f"Unlocked: {data.get('unlocked', False)}")
    print(f"Unlocked Left: {data.get('unlocked_left', 0)}")


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Shodan API — infrastructure reconnaissance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # host
    p = sub.add_parser("host", help="Get info for an IP address")
    p.add_argument("ip", help="IP address to look up")
    add_output_args(p)

    # search
    p = sub.add_parser("search", help="Search Shodan")
    p.add_argument("query", help="Shodan search query (e.g. 'ssl:example.com')")
    p.add_argument("--limit", type=int, default=20, help="Max results to display (default 20)")
    p.add_argument("--page", type=int, default=1, help="Results page number")
    p.add_argument("--facets", help="Facets to include (e.g. 'org,country')")
    p.add_argument("--count-only", action="store_true", help="Only return count + facets")
    add_output_args(p)

    # domain
    p = sub.add_parser("domain", help="DNS records for a domain")
    p.add_argument("domain", help="Domain to look up")
    p.add_argument("--history", action="store_true", help="Include historical DNS data")
    p.add_argument("--type", help="Filter by record type (A, AAAA, CNAME, MX, NS, TXT)")
    add_output_args(p)

    # dns-resolve
    p = sub.add_parser("dns-resolve", help="Resolve hostnames to IPs")
    p.add_argument("hostnames", help="Comma-separated hostnames")
    add_output_args(p)

    # reverse-dns
    p = sub.add_parser("reverse-dns", help="Reverse DNS lookup")
    p.add_argument("ips", help="Comma-separated IP addresses")
    add_output_args(p)

    # ssl-cert
    p = sub.add_parser("ssl-cert", help="Find hosts with SSL certs for a domain")
    p.add_argument("domain", help="Domain to search certificates for")
    add_output_args(p)

    # info
    p = sub.add_parser("info", help="API plan info and credits")
    add_output_args(p)

    args = parser.parse_args()

    commands = {
        "host": cmd_host,
        "search": cmd_search,
        "domain": cmd_domain,
        "dns-resolve": cmd_dns_resolve,
        "reverse-dns": cmd_reverse_dns,
        "ssl-cert": cmd_ssl_cert,
        "info": cmd_info,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
