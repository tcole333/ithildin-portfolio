#!/usr/bin/env python3
"""
LittleSis API wrapper for OSINT investigations.

LittleSis is a free database of who-knows-who at the heights of business and
government. Epstein is entity 36043 with 500+ pre-mapped relationships.

API: https://littlesis.org/api — No authentication required. JSON:API 2.0 format.

Category IDs:
  1=Position, 2=Education, 3=Membership, 4=Family, 5=Donation,
  6=Transaction, 7=Lobby, 8=Social, 9=Professional, 10=Ownership,
  11=Hierarchy, 12=Generic

Usage:
    python tools/query_littlesis.py search "Jeffrey Epstein"
    python tools/query_littlesis.py entity 36043
    python tools/query_littlesis.py relationships 36043 --category 5
    python tools/query_littlesis.py relationships 36043 --sort amount
    python tools/query_littlesis.py connections 36043 --category 1
    python tools/query_littlesis.py batch 36043,12345,67890
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


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


BASE_URL = "https://littlesis.org/api"

CATEGORIES = {
    1: "Position",
    2: "Education",
    3: "Membership",
    4: "Family",
    5: "Donation",
    6: "Transaction",
    7: "Lobby",
    8: "Social",
    9: "Professional",
    10: "Ownership",
    11: "Hierarchy",
    12: "Generic",
}


def _request(path, params=None, retries=4):
    """Make an API request to LittleSis with retry on 503."""
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params, doseq=True)

    headers = {
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    }

    for attempt in range(retries):
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 503 and attempt < retries - 1:
                wait = 3 * (attempt + 1)  # 3, 6, 9s — LittleSis is aggressive
                print(f"  503 from LittleSis, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 404:
                print(f"Not found: {path}", file=sys.stderr)
                return None
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code} from LittleSis: {body}", file=sys.stderr)
            sys.exit(1)
        except URLError as e:
            print(f"ERROR: Cannot reach LittleSis: {e.reason}", file=sys.stderr)
            sys.exit(1)
    return None


def format_entity(entity):
    """Format a LittleSis entity for display."""
    attrs = entity.get("attributes", {})
    eid = entity.get("id", "?")
    name = attrs.get("name", "?")
    blurb = attrs.get("blurb", "")
    primary_ext = attrs.get("primary_ext", "")
    types = attrs.get("types", [])
    url = attrs.get("url", f"https://littlesis.org/entities/{eid}")

    lines = [f"  [{primary_ext}] {name} (id={eid})"]
    if blurb:
        lines.append(f"    {blurb}")
    if types:
        lines.append(f"    Types: {', '.join(types)}")
    lines.append(f"    URL: {url}")
    return "\n".join(lines)


def format_relationship(rel):
    """Format a LittleSis relationship for display."""
    attrs = rel.get("attributes", {})
    rid = rel.get("id", "?")
    desc1 = attrs.get("description1", "")
    desc2 = attrs.get("description2", "")
    cat_id = attrs.get("category_id")
    cat_name = CATEGORIES.get(int(cat_id), f"cat:{cat_id}") if cat_id else "?"
    amount = attrs.get("amount")
    currency = attrs.get("currency", "")
    start = attrs.get("start_date", "")
    end = attrs.get("end_date", "")
    is_current = attrs.get("is_current")
    url = attrs.get("url", f"https://littlesis.org/relationships/{rid}")

    entity1_id = attrs.get("entity1_id", "?")
    entity2_id = attrs.get("entity2_id", "?")

    desc = desc1 or desc2 or ""
    lines = [f"  [{cat_name}] {desc} (rel={rid})"]
    lines.append(f"    Entity1={entity1_id} → Entity2={entity2_id}")

    if amount:
        amt_str = f"${int(amount):,}" if amount else ""
        if currency and currency != "USD":
            amt_str += f" {currency}"
        lines.append(f"    Amount: {amt_str}")

    date_parts = []
    if start:
        date_parts.append(f"from {start}")
    if end:
        date_parts.append(f"to {end}")
    if is_current:
        date_parts.append("(current)")
    if date_parts:
        lines.append(f"    Dates: {' '.join(date_parts)}")

    lines.append(f"    URL: {url}")
    return "\n".join(lines)


def cmd_search(args):
    """Search LittleSis entities by name."""
    params = {"q": args.query}
    data = _request("/entities/search", params)
    if not data:
        print("No results.")
        return

    entities = data.get("data", [])
    meta = data.get("meta", {})
    total = meta.get("pageCount", len(entities))
    _log(args.query, "littlesis", len(entities))

    if write_output(data, args, summary=f"LittleSis search '{args.query}'"):
        return
    if args.json_out:
        print(json.dumps(data, indent=2, default=str))
        return

    print(f"Found {len(entities)} entities matching '{args.query}'")
    print()

    for e in entities:
        print(format_entity(e))
        print()


def cmd_entity(args):
    """Get full entity details by ID."""
    data = _request(f"/entities/{args.entity_id}")
    if not data:
        return

    entity = data.get("data", {})
    attrs = entity.get("attributes", {})

    if write_output(data, args, summary=f"LittleSis entity {args.entity_id}"):
        return
    if args.json_out:
        print(json.dumps(data, indent=2, default=str))
        return

    print(f"=== Entity {args.entity_id} ===")
    print(format_entity(entity))
    print()

    # Show extended attributes
    ext = attrs.get("extensions", {})
    if ext:
        for ext_name, ext_data in ext.items():
            if ext_data:
                print(f"  --- {ext_name} ---")
                for k, v in ext_data.items():
                    if v:
                        print(f"    {k}: {v}")
        print()


def cmd_relationships(args):
    """Get relationships for an entity."""
    params = {"page": 1}
    if args.category:
        params["category_id"] = args.category
    if args.sort:
        params["sort"] = args.sort

    # Paginate to collect results up to limit
    all_rels = []
    page = 1
    while len(all_rels) < args.limit:
        params["page"] = page
        data = _request(f"/entities/{args.entity_id}/relationships", params)
        if not data:
            break

        rels = data.get("data", [])
        if not rels:
            break

        all_rels.extend(rels)
        meta = data.get("meta", {})
        page_count = meta.get("pageCount", 1)
        if page >= page_count:
            break
        page += 1
        time.sleep(0.5)

    all_rels = all_rels[:args.limit]

    if write_output(all_rels, args, summary=f"LittleSis relationships for {args.entity_id}"):
        return
    if args.json_out:
        print(json.dumps(all_rels, indent=2, default=str))
        return

    print(f"Relationships for entity {args.entity_id} ({len(all_rels)} shown)")
    if args.category:
        cat_name = CATEGORIES.get(args.category, f"cat:{args.category}")
        print(f"Filtered: {cat_name}")
    print()

    for rel in all_rels:
        # Use the description field which already includes both entity names
        attrs = rel.get("attributes", {})
        desc = attrs.get("description", "")
        cat_id = attrs.get("category_id")
        cat_name = CATEGORIES.get(int(cat_id), f"cat:{cat_id}") if cat_id else "?"
        amount = attrs.get("amount")
        rid = rel.get("id", "?")

        print(f"  [{cat_name}] {desc} (rel={rid})")
        if amount:
            currency = attrs.get("currency", "USD") or "USD"
            print(f"    Amount: ${int(float(amount)):,} {currency}")
        start = attrs.get("start_date", "")
        end = attrs.get("end_date", "")
        if start or end:
            print(f"    Dates: {start or '?'} — {end or 'present'}")
        entity_url = rel.get("entity", "")
        related_url = rel.get("related", "")
        if entity_url:
            print(f"    Entity: {entity_url}")
        if related_url:
            print(f"    Related: {related_url}")
        self_url = rel.get("self", f"https://littlesis.org/relationships/{rid}")
        print(f"    URL: {self_url}")
        print()


def cmd_connections(args):
    """Get connected entities (entity-centric view)."""
    params = {}
    if args.category:
        params["category_id"] = args.category

    # Paginate to collect results up to limit
    all_entities = []
    page = 1
    while len(all_entities) < args.limit:
        params["page"] = page
        data = _request(f"/entities/{args.entity_id}/connections", params)
        if not data:
            break

        entities = data.get("data", [])
        if not entities:
            break

        all_entities.extend(entities)
        page += 1
        time.sleep(1)  # Connections endpoint rate-limits aggressively

    all_entities = all_entities[:args.limit]

    if write_output(all_entities, args, summary=f"LittleSis connections for {args.entity_id}"):
        return
    if args.json_out:
        print(json.dumps(all_entities, indent=2, default=str))
        return

    cat_filter = ""
    if args.category:
        cat_filter = f" (category: {CATEGORIES.get(args.category, args.category)})"

    print(f"Connected entities for {args.entity_id}{cat_filter} ({len(all_entities)} shown)")
    print()

    for e in all_entities:
        print(format_entity(e))
        print()


def cmd_batch(args):
    """Get details for multiple entities."""
    ids = [x.strip() for x in args.entity_ids.split(",")]
    print(f"Fetching {len(ids)} entities...")
    print()

    for eid in ids:
        data = _request(f"/entities/{eid}")
        if data:
            entity = data.get("data", {})
            print(format_entity(entity))
            print()
        time.sleep(0.5)  # Rate limiting courtesy


def main():
    parser = argparse.ArgumentParser(description="LittleSis relationship API for OSINT investigation")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name")
    p.add_argument("query")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get entity by ID")
    p.add_argument("entity_id")
    add_output_args(p)

    # relationships
    p = sub.add_parser("relationships", help="Get entity relationships")
    p.add_argument("entity_id")
    p.add_argument("--category", type=int, help="Filter by category ID (1-12)")
    p.add_argument("--sort", choices=["amount"], help="Sort relationships")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    # connections
    p = sub.add_parser("connections", help="Get connected entities")
    p.add_argument("entity_id")
    p.add_argument("--category", type=int, help="Filter by category ID")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    # batch
    p = sub.add_parser("batch", help="Get details for multiple entities")
    p.add_argument("entity_ids", help="Comma-separated entity IDs")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "relationships": cmd_relationships,
        "connections": cmd_connections,
        "batch": cmd_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
