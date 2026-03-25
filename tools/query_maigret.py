#!/usr/bin/env python3
"""
Maigret username enumeration wrapper.

Searches for a username across 2500+ sites. Results are INFERENCE only —
a username match does NOT confirm identity. Require corroboration before
upgrading confidence.

Dependency: uv add maigret

Usage:
    python tools/query_maigret.py search "targetuser"
    python tools/query_maigret.py search "targetuser" --top 30
    python tools/query_maigret.py search "targetuser" --output results.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output


DISCLAIMER = (
    "WARNING: Username enumeration results are INFERENCE ONLY (confidence=low). "
    "A matching username does NOT confirm identity — many people share common usernames. "
    "Corroborate with other evidence before drawing conclusions."
)


def _check_maigret():
    """Verify maigret is installed."""
    try:
        result = subprocess.run(
            ["maigret", "--version"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def cmd_search(username, top=50):
    """Run Maigret username search and parse JSON output."""
    if not _check_maigret():
        return {"error": "maigret not installed. Run: uv add maigret"}

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "results.json"

        cmd = [
            "maigret", username,
            "--json", "simple",
            "-o", str(json_path),
            "--timeout", "10",
            "--no-color",
        ]
        if top:
            cmd.extend(["--top-sites", str(top)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=120,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return {"error": "Maigret timed out after 120 seconds", "username": username}

        # Parse JSON output
        results = []
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text())
                # Maigret JSON format varies by version; handle both styles
                if isinstance(data, dict):
                    for site_name, info in data.items():
                        if isinstance(info, dict) and info.get("status"):
                            results.append({
                                "site": site_name,
                                "url": info.get("url_user", info.get("url", "")),
                                "status": info.get("status", "Unknown"),
                            })
                elif isinstance(data, list):
                    for item in data:
                        results.append({
                            "site": item.get("sitename", item.get("site", "?")),
                            "url": item.get("url_user", item.get("url", "")),
                            "status": item.get("status", "Unknown"),
                        })
            except (json.JSONDecodeError, KeyError):
                pass

        # Filter to claimed/found results only
        found = [r for r in results if r.get("status") in ("Claimed", "Found", "Available")]

    return {
        "username": username,
        "disclaimer": DISCLAIMER,
        "claim_type": "inference",
        "confidence": "low",
        "total_found": len(found),
        "total_checked": len(results),
        "results": found,
    }


def _print_search(data):
    if data.get("error"):
        print(f"ERROR: {data['error']}")
        return

    print(f"\n  {DISCLAIMER}")
    print(f"\n  Username: {data['username']}")
    print(f"  Found on {data['total_found']} of {data['total_checked']} sites checked")
    print(f"  claim_type=inference  confidence=low")
    print(f"  {'='*70}")

    for r in data["results"]:
        print(f"  {r['site']:<30} {r['url']}")


def main():
    parser = argparse.ArgumentParser(description="Maigret username enumeration")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="Search for a username across sites")
    p.add_argument("username")
    p.add_argument("--top", type=int, default=50,
                   help="Number of top sites to check (default 50)")
    add_output_args(p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        result = cmd_search(args.username, top=args.top)
        if not write_output(result, args, summary=f"maigret '{args.username}': {result.get('total_found', 0)} found"):
            _print_search(result)


if __name__ == "__main__":
    main()
