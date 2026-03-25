#!/usr/bin/env python3
"""Search DugganUSA Epstein Files API (329K+ documents, all 12 DOJ datasets)."""

import os
import requests
import json
import sys
import time
from pathlib import Path
try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

API_BASE = "https://analytics.dugganusa.com/api/v1/search"
INDEX = "epstein_files"
DEFAULT_LIMIT = 20


def _get_api_key():
    """Load DugganUSA API key from env var or .env file."""
    key = os.environ.get("DUGGANUSA_API_KEY")
    if key:
        return key
    # Try loading from .env file in project root
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DUGGANUSA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def search(query, limit=DEFAULT_LIMIT, offset=0, show_content=False):
    """Search the Epstein Files index."""
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "DUGGANUSA_API_KEY not set. Register at https://epstein.dugganusa.com/register.html "
            "and add DUGGANUSA_API_KEY=dugusa_... to your .env file."
        )
    params = {
        "q": query,
        "indexes": INDEX,
        "limit": limit,
        "offset": offset,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(API_BASE, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    raw = resp.json()

    # API wraps response in {"success": true, "data": {...}}
    if isinstance(raw, dict) and "data" in raw:
        data = raw["data"]
    else:
        data = raw

    if isinstance(data, dict):
        hits = data.get("hits", data.get("results", []))
        total = data.get("totalHits", data.get("estimatedTotalHits", len(hits)))
    elif isinstance(data, list):
        hits = data
        total = len(hits)
    else:
        hits = []
        total = 0

    return hits, total


def search_all(query, max_results=200):
    """Paginate through all results up to max_results."""
    all_hits = []
    offset = 0
    total = None

    while True:
        hits, total_hits = search(query, limit=20, offset=offset)
        if total is None:
            total = total_hits
        if not hits:
            break
        all_hits.extend(hits)
        offset += len(hits)
        if offset >= min(total, max_results):
            break
        time.sleep(0.3)

    return all_hits, total


def print_results(hits, total, query, show_content=False):
    """Pretty-print search results."""
    print(f"\n{'='*80}")
    print(f"Query: {query}")
    print(f"Total hits: {total} | Showing: {len(hits)}")
    print(f"{'='*80}\n")

    for i, hit in enumerate(hits, 1):
        efta = hit.get("efta_id", hit.get("id", "unknown"))
        dataset = hit.get("dataset", "?")
        chars = hit.get("char_count", 0)
        doj_url = hit.get("doj_url", "")

        print(f"--- [{i}] {efta} (dataset: {dataset}, {chars} chars) ---")
        if doj_url:
            print(f"    PDF: {doj_url}")

        if show_content:
            content = hit.get("content", "")
            # Show first 500 chars
            preview = content[:500].replace("\n", "\n    ")
            print(f"    {preview}")
            if len(content) > 500:
                print(f"    ... [{len(content) - 500} more chars]")
        else:
            preview = hit.get("content_preview", "")
            if preview:
                print(f"    {preview[:200]}")

        print()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Search DugganUSA Epstein Files API")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-n", "--limit", type=int, default=20, help="Max results (default 20)")
    parser.add_argument("-a", "--all", action="store_true", help="Fetch all results (up to 200)")
    parser.add_argument("-c", "--content", action="store_true", help="Show full content")
    add_output_args(parser)
    args = parser.parse_args()

    if args.all:
        hits, total = search_all(args.query, max_results=args.limit or 200)
    else:
        hits, total = search(args.query, limit=args.limit)

    data = {"query": args.query, "total": total, "hits": hits}
    if not write_output(data, args, summary=f"DugganUSA search '{args.query}': {len(hits)}/{total} hits"):
        if getattr(args, "json_out", False):
            print(json.dumps(data, indent=2))
        else:
            print_results(hits, total, args.query, show_content=args.content)


if __name__ == "__main__":
    main()
