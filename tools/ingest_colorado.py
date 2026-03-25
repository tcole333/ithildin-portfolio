#!/usr/bin/env python3
"""
Colorado Secretary of State corporate registry ingester.

Uses the Socrata SODA API to query data.colorado.gov datasets:
  - Business Entities (4ykn-tg5h): 1.3M+ entities since 1864

Coverage: Colorado corporations, LLCs, partnerships, nonprofits, foreign entities.

Usage:
    python tools/ingest_colorado.py search "Epstein"
    python tools/ingest_colorado.py search "Zorro Ranch" --limit 50
    python tools/ingest_colorado.py search-agent "Corporation Service"
    python tools/ingest_colorado.py search-address "Denver"
    python tools/ingest_colorado.py ingest-entity <ENTITY_ID>    # Ingest specific entity
    python tools/ingest_colorado.py ingest-batch "Epstein"       # Ingest all matching entities
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

# Load .env for optional app token
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

# Dataset ID
BUSINESS_ENTITIES_ID = "4ykn-tg5h"
BASE_URL = "https://data.colorado.gov/resource"


def _soda_request(dataset_id, params, limit=1000):
    """Make a SODA API request."""
    url = f"{BASE_URL}/{dataset_id}.json"
    params["$limit"] = limit

    token = os.environ.get("CO_SODA_APP_TOKEN")
    if token:
        params["$$app_token"] = token

    full_url = url + "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    req = Request(full_url, headers=headers)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return []


def _soda_paginate(dataset_id, where_clause, limit=5000, max_results=50000):
    """Paginate through SODA results."""
    all_results = []
    offset = 0
    page_size = min(limit, 5000)

    while len(all_results) < max_results:
        params = {
            "$where": where_clause,
            "$limit": page_size,
            "$offset": offset,
        }
        batch = _soda_request(dataset_id, params, limit=page_size)
        if not batch:
            break
        all_results.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
        time.sleep(0.5)

    return all_results[:max_results]


def cmd_search(args):
    """Search Colorado business entities by name."""
    where = f"upper(entityname) LIKE upper('%{args.query}%')"
    results = _soda_request(BUSINESS_ENTITIES_ID, {"$where": where}, limit=args.limit)

    log_search(args.query, "CO_SOS", len(results))

    output_data = []
    for r in results:
        name = r.get("entityname", "?")
        entity_id = r.get("entityid", "?")
        etype = r.get("entitytype", "?")
        status = r.get("entitystatus", "?")
        formed = r.get("entityformdate", "")
        jurisdiction = r.get("jurisdictonofformation", "")

        output_data.append({
            "name": name,
            "entity_id": entity_id,
            "type": etype,
            "status": status,
            "formed_date": formed[:10] if formed else "",
            "jurisdiction": jurisdiction,
            "principal_address": {
                "line1": r.get("principaladdress1", ""),
                "city": r.get("principalcity", ""),
                "state": r.get("principalstate", ""),
                "zipcode": r.get("principalzipcode", ""),
                "country": r.get("principalcountry", ""),
            },
            "agent": {
                "name": r.get("agentorganizationname") or " ".join(filter(None, [
                    r.get("agentfirstname", ""),
                    r.get("agentmiddlename", ""),
                    r.get("agentlastname", "")
                ])) or None,
                "address": {
                    "line1": r.get("agentprincipaladdress1", ""),
                    "city": r.get("agentprincipalcity", ""),
                    "state": r.get("agentprincipalstate", ""),
                    "zipcode": r.get("agentprincipalzipcode", ""),
                }
            },
            "source": "CO_SOS",
        })

    if write_output(output_data, args):
        print(f"Found {len(results)} Colorado entities matching '{args.query}' → {args.output}")
    else:
        print(f"Found {len(results)} Colorado entities matching '{args.query}'")
        print()
        for item in output_data:
            print(f"  [CO] {item['name']} ({item['type']}, {item['status']})")
            print(f"    Entity ID: {item['entity_id']}")
            if item['formed_date']:
                print(f"    Formed: {item['formed_date']}")
            if item['jurisdiction'] and item['jurisdiction'] != 'COLORADO':
                print(f"    Jurisdiction: {item['jurisdiction']}")

            addr = item['principal_address']
            if addr['line1']:
                print(f"    Principal: {addr['line1']}, {addr['city']}, {addr['state']} {addr['zipcode']}")

            agent = item['agent']
            if agent['name']:
                print(f"    Agent: {agent['name']}")
                if agent['address']['line1']:
                    ag_addr = agent['address']
                    print(f"    Agent addr: {ag_addr['line1']}, {ag_addr['city']}, {ag_addr['state']} {ag_addr['zipcode']}")

            print()


def cmd_search_agent(args):
    """Search by registered agent name."""
    clauses = [
        f"upper(agentorganizationname) LIKE upper('%{args.name}%')",
        f"upper(agentfirstname) LIKE upper('%{args.name}%')",
        f"upper(agentlastname) LIKE upper('%{args.name}%')",
    ]
    where = " OR ".join(clauses)
    results = _soda_request(BUSINESS_ENTITIES_ID, {"$where": where}, limit=args.limit)

    log_search(args.name, "CO_SOS_AGENT", len(results))

    output_data = []
    for r in results:
        agent_name = r.get("agentorganizationname") or " ".join(filter(None, [
            r.get("agentfirstname", ""),
            r.get("agentmiddlename", ""),
            r.get("agentlastname", "")
        ]))

        output_data.append({
            "agent_name": agent_name,
            "entity_name": r.get("entityname", "?"),
            "entity_id": r.get("entityid", "?"),
            "entity_type": r.get("entitytype", "?"),
            "status": r.get("entitystatus", "?"),
            "agent_address": {
                "line1": r.get("agentprincipaladdress1", ""),
                "city": r.get("agentprincipalcity", ""),
                "state": r.get("agentprincipalstate", ""),
                "zipcode": r.get("agentprincipalzipcode", ""),
            },
            "source": "CO_SOS",
        })

    if write_output(output_data, args):
        print(f"Found {len(results)} entities with agent matching '{args.name}' → {args.output}")
    else:
        print(f"Found {len(results)} entities with agent matching '{args.name}'")
        print()
        for item in output_data:
            print(f"  {item['agent_name']} — [{item['entity_type']}] {item['entity_name']} (ID: {item['entity_id']}, {item['status']})")
            addr = item['agent_address']
            if addr['line1']:
                print(f"    Address: {addr['line1']}, {addr['city']}, {addr['state']} {addr['zipcode']}")
            print()


def cmd_search_address(args):
    """Search by address across all address fields."""
    clauses = [
        f"upper(principaladdress1) LIKE upper('%{args.query}%')",
        f"upper(principalcity) LIKE upper('%{args.query}%')",
        f"upper(mailingaddress1) LIKE upper('%{args.query}%')",
        f"upper(mailingcity) LIKE upper('%{args.query}%')",
        f"upper(agentprincipaladdress1) LIKE upper('%{args.query}%')",
        f"upper(agentmailingaddress1) LIKE upper('%{args.query}%')",
    ]
    where = " OR ".join(clauses)
    results = _soda_request(BUSINESS_ENTITIES_ID, {"$where": where}, limit=args.limit)

    log_search(args.query, "CO_SOS_ADDRESS", len(results))

    output_data = []
    for r in results:
        output_data.append({
            "entity_name": r.get("entityname", "?"),
            "entity_id": r.get("entityid", "?"),
            "status": r.get("entitystatus", "?"),
            "principal_address": f"{r.get('principaladdress1', '')}, {r.get('principalcity', '')}, {r.get('principalstate', '')} {r.get('principalzipcode', '')}".strip(", "),
            "mailing_address": f"{r.get('mailingaddress1', '')}, {r.get('mailingcity', '')}, {r.get('mailingstate', '')} {r.get('mailingzipcode', '')}".strip(", "),
            "source": "CO_SOS",
        })

    if write_output(output_data, args):
        print(f"Found {len(results)} entities with address matching '{args.query}' → {args.output}")
    else:
        print(f"Found {len(results)} entities with address matching '{args.query}'")
        print()
        for item in output_data:
            print(f"  [CO] {item['entity_name']} (ID: {item['entity_id']}, {item['status']})")
            if item['principal_address'].strip():
                print(f"    Principal: {item['principal_address']}")
            if item['mailing_address'].strip():
                print(f"    Mailing: {item['mailing_address']}")
            print()


def cmd_ingest_entity(args):
    """Ingest a single Colorado entity into registry.db."""
    where = f"entityid = '{args.entity_id}'"
    results = _soda_request(BUSINESS_ENTITIES_ID, {"$where": where}, limit=1)

    if not results:
        print(f"No entity found with ID {args.entity_id}")
        return

    r = results[0]
    db = get_db()

    # Insert entity
    entity_name = r.get("entityname", "")
    source_id = r.get("entityid", "")

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, state_of_formation,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip, mailing_country
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "CO",
        source_id,
        entity_name,
        r.get("entitytype", ""),
        r.get("entitystatus", ""),
        r.get("entityformdate", "")[:10] if r.get("entityformdate") else None,
        r.get("jurisdictonofformation", ""),  # Note: typo in source data
        r.get("principaladdress1", ""),
        r.get("principalcity", ""),
        r.get("principalstate", ""),
        r.get("principalzipcode", ""),
        r.get("principalcountry", "USA"),
        r.get("mailingaddress1", ""),
        r.get("mailingcity", ""),
        r.get("mailingstate", ""),
        r.get("mailingzipcode", ""),
        r.get("mailingcountry", "USA"),
    ))

    # Get the auto-incremented entity_id
    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='CO' AND source_id=?",
        [source_id]
    ).fetchone()
    if not row:
        print(f"ERROR: Failed to get entity_id for {source_id}")
        return
    entity_id = row[0]

    # Insert agent
    agent_name = r.get("agentorganizationname") or " ".join(filter(None, [
        r.get("agentfirstname", ""),
        r.get("agentmiddlename", ""),
        r.get("agentlastname", "")
    ])) or None

    if agent_name:
        db.execute("""
            INSERT OR REPLACE INTO registry_agents
            (entity_id, agent_name, address, city, state, zip, country)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            entity_id,
            agent_name,
            r.get("agentprincipaladdress1", ""),
            r.get("agentprincipalcity", ""),
            r.get("agentprincipalstate", ""),
            r.get("agentprincipalzipcode", ""),
            "USA",
        ))

    db.commit()
    _rebuild_fts(db)
    print(f"✓ Ingested CO entity: {entity_name} ({source_id})")


def cmd_ingest_batch(args):
    """Ingest all entities matching a search query."""
    where = f"upper(entityname) LIKE upper('%{args.query}%')"
    results = _soda_paginate(BUSINESS_ENTITIES_ID, where, limit=args.limit)

    if not results:
        print(f"No entities found matching '{args.query}'")
        return

    print(f"Ingesting {len(results)} Colorado entities matching '{args.query}'...")
    db = get_db()

    # Batch insert entities
    entities = []
    agents = []

    for r in results:
        source_id = r.get("entityid", "")
        entity_name = r.get("entityname", "")

        entities.append((
            "CO",
            source_id,
            entity_name,
            r.get("entitytype", ""),
            r.get("entitystatus", ""),
            r.get("entityformdate", "")[:10] if r.get("entityformdate") else None,
            r.get("jurisdictonofformation", ""),
            r.get("principaladdress1", ""),
            r.get("principalcity", ""),
            r.get("principalstate", ""),
            r.get("principalzipcode", ""),
            r.get("principalcountry", "USA"),
            r.get("mailingaddress1", ""),
            r.get("mailingcity", ""),
            r.get("mailingstate", ""),
            r.get("mailingzipcode", ""),
            r.get("mailingcountry", "USA"),
        ))

        # Collect agent data
        agent_name = r.get("agentorganizationname") or " ".join(filter(None, [
            r.get("agentfirstname", ""),
            r.get("agentmiddlename", ""),
            r.get("agentlastname", "")
        ])) or None

        if agent_name:
            agents.append((
                source_id,  # Will map to entity_id later
                agent_name,
                r.get("agentprincipaladdress1", ""),
                r.get("agentprincipalcity", ""),
                r.get("agentprincipalstate", ""),
                r.get("agentprincipalzipcode", ""),
                "USA",
            ))

    # Insert entities
    db.executemany("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date, state_of_formation,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip, mailing_country
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, entities)

    # Build mapping from source_id to entity_id
    if agents:
        source_ids = set(a[0] for a in agents)
        id_map = {}
        for sid in source_ids:
            row = db.execute(
                "SELECT id FROM registry_entities WHERE source_jurisdiction='CO' AND source_id=?",
                [sid]
            ).fetchone()
            if row:
                id_map[sid] = row[0]

        # Insert agents
        agent_rows = []
        for source_id, name, addr, city, state, zip_code, country in agents:
            entity_id = id_map.get(source_id)
            if entity_id:
                agent_rows.append((entity_id, name, addr, city, state, zip_code, country))

        if agent_rows:
            db.executemany("""
                INSERT OR REPLACE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip, country)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, agent_rows)

    db.commit()
    _rebuild_fts(db)
    print(f"✓ Ingested {len(results)} Colorado entities")


def main():
    parser = argparse.ArgumentParser(description="Colorado SoS corporate registry tool")
    sub = parser.add_subparsers(dest="command")

    # Search
    p_search = sub.add_parser("search", help="Search entities by name")
    p_search.add_argument("query", help="Entity name to search for")
    p_search.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_search)

    # Search agent
    p_agent = sub.add_parser("search-agent", help="Search by registered agent name")
    p_agent.add_argument("name", help="Agent name to search for")
    p_agent.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_agent)

    # Search address
    p_addr = sub.add_parser("search-address", help="Search by address")
    p_addr.add_argument("query", help="Address term to search for")
    p_addr.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_addr)

    # Ingest entity
    p_ingest = sub.add_parser("ingest-entity", help="Ingest specific entity into registry.db")
    p_ingest.add_argument("entity_id", help="Colorado entity ID")

    # Ingest batch
    p_batch = sub.add_parser("ingest-batch", help="Ingest all matching entities")
    p_batch.add_argument("query", help="Entity name to search for")
    p_batch.add_argument("-n", "--limit", type=int, default=5000, help="Max results")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        cmd_search(args)
    elif args.command == "search-agent":
        cmd_search_agent(args)
    elif args.command == "search-address":
        cmd_search_address(args)
    elif args.command == "ingest-entity":
        cmd_ingest_entity(args)
    elif args.command == "ingest-batch":
        cmd_ingest_batch(args)


if __name__ == "__main__":
    main()
