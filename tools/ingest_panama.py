#!/usr/bin/env python3
"""
Panama corporate registry ingester (hybrid approach).

Direct scraping of Panama's Registro Publico is impractical (Blazor Server /
WebSocket only). Instead, this tool combines three free sources:

1. ICIJ Offshore Leaks API — Panama Papers entities (Mossack Fonseca, ~200K)
2. OCCRP Aleph API — 2008 full registry scrape (~600K companies with directors)
3. PANADATA REST API — live registry data (paid, $0.50/lookup, optional)

Usage:
    python tools/ingest_panama.py search "Epstein"
    python tools/ingest_panama.py search "Mossack" --source icij
    python tools/ingest_panama.py search "Financial Trust" --source aleph
    python tools/ingest_panama.py detail-icij <node_id>
    python tools/ingest_panama.py ingest-batch "Epstein"
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

sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

ICIJ_BASE = "https://offshoreleaks.icij.org/api/v1/reconcile"
ALEPH_BASE = "https://aleph.occrp.org/api/2"
PANADATA_BASE = "https://api.panadata.net/v1"  # Placeholder — check panadata.readme.io

ALEPH_PANAMA_COLLECTION = "96"  # Panama Companies Registry (2008)


def _http_get(url, headers=None, timeout=30):
    """Generic HTTP GET with error handling."""
    if not headers:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0)",
        }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:300]
        print(f"  HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"  Network error: {e.reason}", file=sys.stderr)
        return None


# ── ICIJ Offshore Leaks (Reconciliation API) ──

def _search_icij(query, entity_type="Entity", limit=20):
    """Search ICIJ Offshore Leaks via Reconciliation API.

    entity_type: Entity, Officer, Intermediary, Address, Other
    Note: ICIJ Reconciliation API does not support country filtering directly.
    Panama Papers results are identified by description containing 'Panama Papers'.
    """
    payload = json.dumps({
        "queries": {
            "q0": {
                "query": query,
                "type": entity_type,
                "limit": limit,
            }
        }
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0)",
    }
    req = Request(ICIJ_BASE, data=payload, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("q0", {}).get("result", [])
    except (HTTPError, URLError) as e:
        print(f"  ICIJ API error: {e}", file=sys.stderr)
        return []


def _get_icij_node(node_id):
    """Get ICIJ node details via suggest endpoint."""
    # REST nodes endpoint currently returns 500; use the web URL for reference
    return {"node_id": node_id, "url": f"https://offshoreleaks.icij.org/nodes/{node_id}"}


# ── OCCRP Aleph ──

def _search_aleph(query, schema="Company", limit=20, collection=None):
    """Search Aleph for Panama entities."""
    params = {
        "q": query,
        "filter:schemata": schema,
        "filter:countries": "pa",
        "limit": limit,
    }
    if collection:
        params["filter:collection_id"] = collection
    url = f"{ALEPH_BASE}/entities?{urlencode(params, doseq=True)}"
    return _http_get(url)


def _get_aleph_entity(entity_id):
    """Get Aleph entity details."""
    url = f"{ALEPH_BASE}/entities/{entity_id}"
    return _http_get(url)


def _expand_aleph(entity_id, limit=20):
    """Expand Aleph entity relationships."""
    params = {"limit": limit}
    url = f"{ALEPH_BASE}/entities/{entity_id}/expand?{urlencode(params)}"
    return _http_get(url)


# ── Display Formatting ──

def _format_icij_result(r):
    """Format an ICIJ reconciliation result."""
    name = r.get("name", "?")
    node_id = r.get("id", "?")
    description = r.get("description", "")
    score = r.get("score", 0)
    types = r.get("types", [])
    type_names = ", ".join(t.get("name", "?") for t in types) if types else "?"

    lines = [f"  [ICIJ] {name} (score: {score:.0f})"]
    lines.append(f"    Node: {node_id} | Type: {type_names}")
    if description:
        lines.append(f"    Source: {description}")
    lines.append(f"    URL: https://offshoreleaks.icij.org/nodes/{node_id}")
    return "\n".join(lines)


def _format_aleph_result(r):
    """Format an Aleph search result."""
    props = r.get("properties", {})
    coll = r.get("collection", {})
    schema = r.get("schema", "?")
    name = ", ".join(props.get("name", ["?"]))
    entity_id = r.get("id", "?")

    lines = [f"  [Aleph] {name} ({schema})"]
    lines.append(f"    Collection: {coll.get('label', '?')}")
    countries = props.get("country", coll.get("countries", []))
    if countries:
        lines.append(f"    Countries: {', '.join(countries)}")
    reg_num = props.get("registrationNumber", [])
    if reg_num:
        lines.append(f"    Reg #: {', '.join(reg_num)}")
    inc_date = props.get("incorporationDate", [])
    if inc_date:
        lines.append(f"    Incorporated: {', '.join(inc_date)}")
    status = props.get("status", [])
    if status:
        lines.append(f"    Status: {', '.join(status)}")
    lines.append(f"    ID: {entity_id}")
    lines.append(f"    URL: https://aleph.occrp.org/entities/{entity_id}")
    return "\n".join(lines)


# ── Commands ──

def cmd_search(args):
    """Search Panama entities across ICIJ and Aleph."""
    icij_results = []
    aleph_results = []

    if args.source in (None, "icij"):
        print(f"Searching ICIJ Offshore Leaks for '{args.query}'...")
        # Search both Entity and Officer types
        icij_results = _search_icij(args.query, entity_type="Entity", limit=args.limit)
        officer_results = _search_icij(args.query, entity_type="Officer", limit=args.limit)
        icij_results.extend(officer_results)
        time.sleep(1)

    if args.source in (None, "aleph"):
        print(f"Searching Aleph for '{args.query}' (Panama)...")
        # Search both the 2008 registry and all Panama data
        data = _search_aleph(args.query, schema="Company", limit=args.limit)
        if data:
            aleph_results = data.get("results", [])

        # Also search for persons
        data_persons = _search_aleph(args.query, schema="Person", limit=args.limit)
        if data_persons:
            aleph_results.extend(data_persons.get("results", []))

    # Display results
    total = len(icij_results) + len(aleph_results)
    print(f"\nFound {total} Panama results ({len(icij_results)} ICIJ, {len(aleph_results)} Aleph)")
    print()

    if icij_results:
        print("=== ICIJ Offshore Leaks ===")
        for r in icij_results[:args.limit]:
            print(_format_icij_result(r))
            print()

    if aleph_results:
        print("=== OCCRP Aleph ===")
        for r in aleph_results[:args.limit]:
            print(_format_aleph_result(r))
            print()

    if args.json_out:
        print(json.dumps({"icij": icij_results, "aleph": aleph_results}, indent=2, default=str))


def cmd_detail_icij(args):
    """Get ICIJ node details."""
    data = _get_icij_node(args.node_id)
    if not data:
        print(f"Node {args.node_id} not found")
        return

    print(f"=== ICIJ Node {args.node_id} ===")
    for key, val in sorted(data.items()):
        if val and key not in ("_id", "id"):
            print(f"  {key}: {val}")
    print(f"  URL: https://offshoreleaks.icij.org/nodes/{args.node_id}")

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_ingest_batch(args):
    """Ingest Panama entities from Aleph into registry.db."""
    db = get_db()

    # Search Aleph for companies
    print(f"Searching Aleph for '{args.query}' (Panama, Companies)...")
    data = _search_aleph(args.query, schema="Company", limit=args.limit,
                         collection=ALEPH_PANAMA_COLLECTION if args.registry_only else None)
    if not data:
        print("No results from Aleph")
        return

    results = data.get("results", [])
    total_api = data.get("total", len(results))
    print(f"Found {total_api} total ({len(results)} to ingest)")

    ingested = 0
    for r in results:
        props = r.get("properties", {})
        coll = r.get("collection", {})
        entity_id = r.get("id", "")

        name = props.get("name", ["?"])[0]
        reg_num = props.get("registrationNumber", [""])[0]
        inc_date = props.get("incorporationDate", [""])[0]
        diss_date = props.get("dissolutionDate", [""])[0]
        status_list = props.get("status", [])
        status = status_list[0].lower() if status_list else None

        source_id = reg_num or entity_id

        # Map status
        if status:
            status_map = {"vigente": "active", "disuelta": "dissolved", "active": "active"}
            status = status_map.get(status, status)

        # Parse date (may be YYYY-MM-DD or just YYYY)
        formation_date = inc_date if inc_date else None
        dissolution_date = diss_date if diss_date else None

        # Insert entity
        try:
            db.execute("""
                INSERT OR REPLACE INTO registry_entities (
                    source_jurisdiction, source_id, entity_name, entity_type, status,
                    formation_date, dissolution_date, source_url, raw_data
                ) VALUES ('pa', ?, ?, 'company', ?, ?, ?, ?, ?)
            """, [
                source_id, name, status, formation_date, dissolution_date,
                f"https://aleph.occrp.org/entities/{entity_id}",
                json.dumps({"aleph_id": entity_id, "properties": props}, default=str),
            ])

            row = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='pa' AND source_id=?",
                [source_id]
            ).fetchone()
            reg_entity_id = row[0]

            # Try to get directors from Aleph expand
            if args.expand:
                time.sleep(1)
                expand_data = _expand_aleph(entity_id, limit=20)
                if expand_data:
                    for group in expand_data.get("results", []):
                        prop_name = group.get("property", "")
                        if "director" in str(prop_name).lower() or "officer" in str(prop_name).lower():
                            for e in group.get("entities", []):
                                officer_name = e.get("properties", {}).get("name", [""])[0]
                                if officer_name:
                                    try:
                                        db.execute("""
                                            INSERT OR IGNORE INTO registry_officers
                                            (entity_id, officer_name, title, officer_type)
                                            VALUES (?, ?, 'director', 'person')
                                        """, [reg_entity_id, officer_name])
                                    except Exception:
                                        pass

            # Insert registered agent if available
            agent_list = props.get("agent", props.get("registeredAgent", []))
            for agent_name in agent_list:
                try:
                    db.execute("""
                        INSERT OR IGNORE INTO registry_agents
                        (entity_id, agent_name)
                        VALUES (?, ?)
                    """, [reg_entity_id, agent_name])
                except Exception:
                    pass

            ingested += 1
            print(f"  [{ingested}/{len(results)}] {name}")

        except Exception as e:
            print(f"  Error ingesting {name}: {e}", file=sys.stderr)

    # Log
    db.execute("""
        INSERT INTO registry_ingest_log (jurisdiction, source_type, record_count, notes)
        VALUES ('pa', 'aleph_api', ?, ?)
    """, [ingested, f"Query: {args.query}, Collection: {ALEPH_PANAMA_COLLECTION if args.registry_only else 'all'}"])
    db.commit()

    try:
        _rebuild_fts(db)
    except Exception:
        pass

    print(f"\nIngest complete: {ingested} Panama entities")


def main():
    parser = argparse.ArgumentParser(description="Panama corporate registry (hybrid ICIJ + Aleph)")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="Search Panama entities")
    p.add_argument("query")
    p.add_argument("--source", choices=["icij", "aleph"], help="Search only one source")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("detail-icij", help="Get ICIJ node details")
    p.add_argument("node_id")

    p = sub.add_parser("ingest-batch", help="Ingest entities from Aleph into registry.db")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--registry-only", action="store_true", help="Only search 2008 registry collection")
    p.add_argument("--expand", action="store_true", help="Expand entities to get directors (slower)")

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "detail-icij": cmd_detail_icij,
        "ingest-batch": cmd_ingest_batch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
