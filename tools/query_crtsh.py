#!/usr/bin/env python3
"""
Certificate Transparency log search via crt.sh.

Discovers certificates issued for a domain, enumerates subdomains via SANs,
tracks certificate issuance timeline, and identifies issuer patterns.
Essential for passive infrastructure reconnaissance.

API: https://crt.sh (Sectigo's CT log aggregator)
Auth: None required.
Rate limits: None enforced, but be polite (1 req/sec).

Usage:
    python tools/query_crtsh.py search example.com
    python tools/query_crtsh.py search example.com --subdomains
    python tools/query_crtsh.py search "Organization Name" --org
    python tools/query_crtsh.py subdomains example.com
    python tools/query_crtsh.py timeline example.com
    python tools/query_crtsh.py cert 12345678
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://crt.sh"


def _fetch(params, timeout=30):
    """Fetch from crt.sh JSON API."""
    params["output"] = "json"
    url = f"{BASE_URL}/?{urlencode(params)}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            if not text.strip():
                return []
            return json.loads(text)
    except HTTPError as e:
        print(f"ERROR: crt.sh returned {e.code}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("ERROR: crt.sh returned non-JSON response (may be overloaded, retry later)", file=sys.stderr)
        sys.exit(1)


def _dedupe_certs(records):
    """Deduplicate by serial_number, keeping earliest entry_timestamp."""
    seen = {}
    for r in records:
        serial = r.get("serial_number", "")
        if serial not in seen:
            seen[serial] = r
    return list(seen.values())


def _extract_subdomains(records, base_domain):
    """Extract unique subdomains from certificate name_value fields."""
    subs = set()
    base = base_domain.lower()
    for r in records:
        for field in ("common_name", "name_value"):
            val = r.get(field, "")
            if not val:
                continue
            for name in val.split("\n"):
                name = name.strip().lower()
                if name.startswith("*."):
                    name = name[2:]
                if name == base or name.endswith("." + base):
                    subs.add(name)
    return sorted(subs)


# -- Commands ----------------------------------------------------------------


def cmd_search(args):
    """Search for certificates matching a domain or organization."""
    if args.org:
        params = {"O": args.query}
    elif args.subdomains:
        params = {"q": f"%.{args.query}"}
    else:
        params = {"q": args.query}

    if args.exclude_expired:
        params["exclude"] = "expired"

    records = _fetch(params, timeout=60)
    deduped = _dedupe_certs(records)

    if write_output(deduped, args, summary=f"crt.sh '{args.query}' ({len(deduped)} unique certs from {len(records)} log entries)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(deduped, indent=2))
        return

    print(f"Certificates for '{args.query}': {len(deduped)} unique ({len(records)} log entries)")

    for r in deduped[:args.limit]:
        cn = r.get("common_name", "N/A")
        issuer = r.get("issuer_name", "N/A")
        # Extract short issuer org
        issuer_short = issuer
        for part in issuer.split(", "):
            if part.startswith("O="):
                issuer_short = part[2:]
                break
        not_before = r.get("not_before", "?")[:10]
        not_after = r.get("not_after", "?")[:10]
        names = r.get("name_value", "")
        san_count = len(names.split("\n")) if names else 0

        print(f"\n  CN: {cn}")
        print(f"  Issuer: {issuer_short}")
        print(f"  Valid: {not_before} to {not_after}")
        if san_count > 1:
            sans = [n.strip() for n in names.split("\n") if n.strip()]
            print(f"  SANs ({san_count}): {', '.join(sans[:5])}")
            if san_count > 5:
                print(f"        ... and {san_count - 5} more")
        print(f"  ID: {r.get('id', '?')} | Serial: {r.get('serial_number', '?')[:20]}")


def cmd_subdomains(args):
    """Enumerate subdomains from CT logs."""
    # Fetch wildcard query for subdomains
    records = _fetch({"q": f"%.{args.domain}"}, timeout=60)

    subs = _extract_subdomains(records, args.domain)

    data = {
        "domain": args.domain,
        "subdomains": subs,
        "count": len(subs),
        "cert_count": len(records),
    }

    if write_output(data, args, summary=f"subdomains for {args.domain}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Subdomains for {args.domain} ({len(subs)} from {len(records)} cert entries):\n")
    for s in subs:
        print(f"  {s}")


def cmd_timeline(args):
    """Show certificate issuance timeline for a domain."""
    records = _fetch({"q": args.domain}, timeout=60)
    deduped = _dedupe_certs(records)

    # Sort by not_before date
    def sort_key(r):
        try:
            return r.get("not_before", "9999")
        except Exception:
            return "9999"

    deduped.sort(key=sort_key)

    # Group by month
    monthly = defaultdict(list)
    issuers = Counter()
    for r in deduped:
        nb = r.get("not_before", "")[:7]  # YYYY-MM
        if nb:
            monthly[nb].append(r)
        issuer = r.get("issuer_name", "")
        for part in issuer.split(", "):
            if part.startswith("O="):
                issuers[part[2:]] += 1
                break

    data = {
        "domain": args.domain,
        "total_certs": len(deduped),
        "first_seen": deduped[0].get("not_before", "?")[:10] if deduped else "N/A",
        "last_seen": deduped[-1].get("not_before", "?")[:10] if deduped else "N/A",
        "issuers": dict(issuers.most_common()),
        "monthly": {k: len(v) for k, v in sorted(monthly.items())},
    }

    if write_output(data, args, summary=f"timeline for {args.domain}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Certificate Timeline: {args.domain}")
    print(f"Total unique certificates: {len(deduped)}")
    if deduped:
        print(f"First issued: {data['first_seen']}")
        print(f"Last issued: {data['last_seen']}")

    print(f"\nIssuers:")
    for issuer, count in issuers.most_common():
        print(f"  {issuer}: {count}")

    print(f"\nMonthly issuance:")
    for month, certs in sorted(monthly.items()):
        bar = "#" * len(certs)
        print(f"  {month}: {bar} ({len(certs)})")


def cmd_cert(args):
    """Get details for a specific certificate by crt.sh ID."""
    params = {"id": args.cert_id, "output": "json"}
    url = f"{BASE_URL}/?{urlencode(params)}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            data = json.loads(text) if text.strip() else {}
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"ERROR: Failed to fetch cert {args.cert_id}: {e}", file=sys.stderr)
        sys.exit(1)

    if write_output(data, args, summary=f"cert {args.cert_id}"):
        return

    print(json.dumps(data, indent=2))


# -- CLI ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="crt.sh Certificate Transparency search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search certificates by domain or org name")
    p.add_argument("query", help="Domain name or organization name")
    p.add_argument("--subdomains", action="store_true", help="Search for subdomain certificates (wildcard match)")
    p.add_argument("--org", action="store_true", help="Search by organization name in certificate subject")
    p.add_argument("--exclude-expired", action="store_true", help="Exclude expired certificates")
    p.add_argument("--limit", type=int, default=30, help="Max results to display (default 30)")
    add_output_args(p)

    # subdomains
    p = sub.add_parser("subdomains", help="Enumerate subdomains from CT logs")
    p.add_argument("domain", help="Base domain to enumerate")
    add_output_args(p)

    # timeline
    p = sub.add_parser("timeline", help="Certificate issuance timeline")
    p.add_argument("domain", help="Domain to show timeline for")
    add_output_args(p)

    # cert
    p = sub.add_parser("cert", help="Get certificate details by crt.sh ID")
    p.add_argument("cert_id", help="crt.sh certificate ID")
    add_output_args(p)

    args = parser.parse_args()
    commands = {
        "search": cmd_search,
        "subdomains": cmd_subdomains,
        "timeline": cmd_timeline,
        "cert": cmd_cert,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
