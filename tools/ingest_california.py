#!/usr/bin/env python3
"""
California Secretary of State corporate registry tool.

Uses the CA SoS BE Public Search API (Azure APIM) at calico.sos.ca.gov.
Requires a free subscription key from https://calicodev.sos.ca.gov/signup

Endpoints:
  - BusinessEntityDetails: Lookup by entity number
  - BusinessEntityKeywordSearch: Search by keyword (top 150 results)

Usage:
    python tools/ingest_california.py search "PARAFI CAPITAL"
    python tools/ingest_california.py search "Epstein" --begins-with
    python tools/ingest_california.py search "Apollo" --date-start 1990-01-01 --date-end 2020-12-31
    python tools/ingest_california.py search-number 202150010654
    python tools/ingest_california.py detail 202150010654
    python tools/ingest_california.py ingest-entity 202150010654
    python tools/ingest_california.py ingest-batch "Epstein"
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))
from query_registry import get_db, _rebuild_fts

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
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

BASE_URL = "https://calico.sos.ca.gov/cbc/v1/api"

# Entity type mapping: CA API "EntityType" → registry schema entity_type
TYPE_MAP = {
    "stock corporation - ca": "corp",
    "stock corporation - out of state": "foreign_corp",
    "domestic stock corporation": "corp",
    "foreign stock corporation": "foreign_corp",
    "limited liability company - ca": "llc",
    "limited liability company - out of state": "foreign_llc",
    "domestic limited liability company": "llc",
    "foreign limited liability company": "foreign_llc",
    "limited partnership - ca": "lp",
    "limited partnership - out of state": "foreign_lp",
    "domestic limited partnership": "lp",
    "foreign limited partnership": "foreign_lp",
    "limited liability partnership": "llp",
    "general partnership": "gp",
    "nonprofit corporation - ca": "nonprofit",
    "nonprofit corporation - out of state": "foreign_nonprofit",
    "domestic nonprofit corporation": "nonprofit",
    "foreign nonprofit corporation": "foreign_nonprofit",
    "corporation - ca": "corp",
    "corporation - out of state": "foreign_corp",
}

# Status description → registry schema status
STATUS_MAP = {
    "active": "active",
    "suspended": "suspended",
    "suspended - sos": "suspended",
    "suspended - ftb": "suspended",
    "suspended - sos & ftb": "suspended",
    "dissolved": "dissolved",
    "forfeited": "forfeited",
    "surrendered": "surrendered",
    "cancelled": "cancelled",
    "merged out": "inactive",
    "converted out": "inactive",
    "withdrawn": "inactive",
}


def _get_api_key(args=None):
    """Get API subscription key from args or environment."""
    key = getattr(args, "api_key", None) if args else None
    if not key:
        key = os.environ.get("CA_SOS_API_KEY")
    if not key:
        print("ERROR: CA SoS API key required. Set CA_SOS_API_KEY in .env or use --api-key", file=sys.stderr)
        print("Register at: https://calicodev.sos.ca.gov/signup", file=sys.stderr)
        sys.exit(1)
    return key


def _api_request(endpoint, params=None, api_key=None, retries=3):
    """Make a CA SoS API request with retry logic."""
    url = f"{BASE_URL}/{endpoint}"
    if params:
        url += "?" + urlencode(params)

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Accept": "application/json",
    }
    req = Request(url, headers=headers)

    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                if not body.strip():
                    return None
                return json.loads(body)
        except HTTPError as e:
            if e.code == 429:
                wait = (2 ** attempt) * 2
                print(f"  Rate limited (429), waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            elif e.code == 400:
                body = e.read().decode()[:500]
                print(f"ERROR: Bad request (400): {body}", file=sys.stderr)
                return None
            elif e.code == 503:
                body = e.read().decode()[:500]
                print(f"ERROR: Key missing/invalid (503): {body}", file=sys.stderr)
                return None
            else:
                body = e.read().decode()[:500]
                print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            print(f"ERROR: {e}", file=sys.stderr)
            return None

    return None


def _parse_entity(r):
    """Parse a CA API entity record into a display-friendly dict."""
    filing_date = r.get("FilingDate", "")
    if filing_date and "T" in str(filing_date):
        filing_date = str(filing_date).split("T")[0]

    return {
        "entity_id": r.get("EntityID", ""),
        "entity_name": r.get("EntityName", ""),
        "foreign_name": r.get("ForeignEntityName"),
        "entity_type": r.get("EntityType", ""),
        "status": r.get("StatusDescription", ""),
        "status_code": r.get("StatusCode"),
        "filing_date": filing_date,
        "jurisdiction": r.get("Jurisdiction", ""),
        "management": r.get("ManagementDescription"),
        "entity_address": {
            "street1": r.get("EntityStreetAddress1"),
            "street2": r.get("EntityStreetAddress2"),
            "city": r.get("EntityCity"),
            "state": r.get("EntityState"),
            "zip": r.get("EntityZipCode"),
        },
        "agent": {
            "name": r.get("AgentName"),
            "address1": r.get("AgentAddress1"),
            "address2": r.get("AgentAddress2"),
            "city": r.get("AgentCity"),
            "state": r.get("AgentState"),
            "zip": r.get("AgentZipCode"),
        },
        "mailing_address": {
            "street1": r.get("MailingStreetAddress1"),
            "street2": r.get("MailingStreetAddress2"),
            "city": r.get("MailingCity"),
            "state": r.get("MailingState"),
            "zip": r.get("MailingZipCode"),
        },
        "standing": {
            "sos": r.get("StandingSOS"),
            "sos_date": r.get("StandingSOSDate"),
            "ftb": r.get("StandingFTB"),
            "ftb_date": r.get("StandingFTBDate"),
            "agent": r.get("StandingAgent"),
            "agent_date": r.get("StandingAgentDate"),
            "vcfcf": r.get("StandingVCFCF"),
            "vcfcf_date": r.get("StandingVCFCFDate"),
        },
        "si_frequency": r.get("SiFrequency"),
        "raw": r,
    }


def _print_entity(e):
    """Print a parsed entity in human-readable format."""
    print(f"  [CA] {e['entity_name']} ({e['entity_type']}, {e['status']})")
    print(f"    Entity #: {e['entity_id']}")
    if e["filing_date"]:
        print(f"    Filed: {e['filing_date']}")
    if e["jurisdiction"] and e["jurisdiction"] != "CALIFORNIA":
        print(f"    Jurisdiction: {e['jurisdiction']}")
    if e["management"]:
        print(f"    Management: {e['management']}")
    if e["foreign_name"]:
        print(f"    Foreign name: {e['foreign_name']}")

    # Entity address
    addr = e["entity_address"]
    if addr.get("street1"):
        parts = [addr["street1"]]
        if addr.get("street2"):
            parts.append(addr["street2"])
        city_state = ", ".join(filter(None, [addr.get("city"), addr.get("state")]))
        if city_state:
            parts.append(city_state)
        if addr.get("zip"):
            parts[-1] = parts[-1] + " " + addr["zip"]
        print(f"    Address: {', '.join(parts)}")

    # Agent
    agent = e["agent"]
    if agent.get("name"):
        agent_line = agent["name"]
        if agent.get("address1"):
            addr_parts = [agent["address1"]]
            if agent.get("city"):
                addr_parts.append(agent["city"])
            if agent.get("state"):
                addr_parts[-1] = addr_parts[-1] + ", " + agent["state"]
            if agent.get("zip"):
                addr_parts[-1] = addr_parts[-1] + " " + agent["zip"]
            agent_line += " — " + ", ".join(addr_parts)
        print(f"    Agent: {agent_line}")

    # Mailing address
    mail = e["mailing_address"]
    if mail.get("street1"):
        parts = [mail["street1"]]
        if mail.get("street2"):
            parts.append(mail["street2"])
        city_state = ", ".join(filter(None, [mail.get("city"), mail.get("state")]))
        if city_state:
            parts.append(city_state)
        if mail.get("zip"):
            parts[-1] = parts[-1] + " " + mail["zip"]
        print(f"    Mailing: {', '.join(parts)}")

    print()


def cmd_search(args):
    """Search CA business entities by keyword."""
    api_key = _get_api_key(args)

    params = {"search-term": args.query}
    if args.begins_with:
        params["beginsWith"] = "true"
    if args.date_start:
        params["created-date-start"] = args.date_start
    if args.date_end:
        params["created-date-end"] = args.date_end

    data = _api_request("BusinessEntityKeywordSearch", params, api_key)
    if not data:
        print(f"No results for '{args.query}'")
        return

    # Handle response format: { "RecordCount": N, "EntityData": [...] }
    entities_raw = []
    if isinstance(data, dict):
        count = data.get("RecordCount", 0)
        entities_raw = data.get("EntityData", [])
        if not entities_raw and count == 0:
            # Single entity returned directly?
            if "EntityID" in data:
                entities_raw = [data]
    elif isinstance(data, list):
        entities_raw = data
        count = len(data)
    else:
        print(f"Unexpected response format: {type(data)}", file=sys.stderr)
        return

    if not entities_raw:
        print(f"No entities found matching '{args.query}'")
        return

    entities = [_parse_entity(r) for r in entities_raw]

    # Output handling
    if write_output(entities, args, summary=f"CA search '{args.query}'"):
        return

    print(f"Found {len(entities)} CA entities matching '{args.query}' (API reports {count} total)")
    print()
    for e in entities:
        _print_entity(e)


def cmd_search_number(args):
    """Lookup a CA entity by entity number."""
    api_key = _get_api_key(args)

    params = {"entity-number": args.entity_number}
    data = _api_request("BusinessEntityDetails", params, api_key)
    if not data:
        print(f"Entity {args.entity_number} not found")
        return

    # Entity detail returns a single entity object
    if isinstance(data, dict):
        if "EntityData" in data and isinstance(data["EntityData"], list):
            entities_raw = data["EntityData"]
        elif "EntityID" in data:
            entities_raw = [data]
        else:
            print(f"Unexpected response: {json.dumps(data)[:200]}", file=sys.stderr)
            return
    elif isinstance(data, list):
        entities_raw = data
    else:
        print(f"Unexpected response format", file=sys.stderr)
        return

    entities = [_parse_entity(r) for r in entities_raw]

    if write_output(entities, args, summary=f"CA entity #{args.entity_number}"):
        return

    for e in entities:
        _print_entity(e)


def cmd_detail(args):
    """Get full details for a CA entity (same as search-number but with raw JSON output)."""
    api_key = _get_api_key(args)

    params = {"entity-number": args.entity_number}
    data = _api_request("BusinessEntityDetails", params, api_key)
    if not data:
        print(f"Entity {args.entity_number} not found")
        return

    if write_output(data, args, summary=f"CA detail #{args.entity_number}"):
        return

    print(json.dumps(data, indent=2, default=str))


def cmd_ingest_entity(args):
    """Ingest a CA entity by entity number into registry.db."""
    api_key = _get_api_key(args)
    db = get_db()

    params = {"entity-number": args.entity_number}
    data = _api_request("BusinessEntityDetails", params, api_key)
    if not data:
        print(f"Entity {args.entity_number} not found")
        return

    # Normalize response
    if isinstance(data, dict) and "EntityData" in data:
        raw = data["EntityData"][0] if data["EntityData"] else data
    elif isinstance(data, dict) and "EntityID" in data:
        raw = data
    else:
        print(f"Unexpected response format", file=sys.stderr)
        return

    entity_id = _upsert_entity(db, raw)
    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass
    print(f"Ingested: {raw.get('EntityName')} (CA #{raw.get('EntityID')}) → registry ID {entity_id}")


def cmd_ingest_batch(args):
    """Search + ingest all matching CA entities."""
    api_key = _get_api_key(args)
    db = get_db()

    params = {"search-term": args.query}
    if args.begins_with:
        params["beginsWith"] = "true"

    data = _api_request("BusinessEntityKeywordSearch", params, api_key)
    if not data:
        print(f"No results for '{args.query}'")
        return

    entities_raw = []
    if isinstance(data, dict):
        entities_raw = data.get("EntityData", [])
    elif isinstance(data, list):
        entities_raw = data

    if not entities_raw:
        print(f"No entities found matching '{args.query}'")
        return

    print(f"Ingesting {len(entities_raw)} CA entities matching '{args.query}'")
    for i, r in enumerate(entities_raw):
        entity_id = _upsert_entity(db, r)
        print(f"  [{i+1}/{len(entities_raw)}] {r.get('EntityName')} (CA #{r.get('EntityID')})")

    db.commit()
    try:
        _rebuild_fts(db)
    except Exception:
        pass
    print(f"\nBatch ingest complete: {len(entities_raw)} CA entities")


def cmd_server_status(args):
    """Check CA SoS API server status."""
    api_key = _get_api_key(args)
    data = _api_request("ServerStatus", api_key=api_key)
    if data:
        print(json.dumps(data, indent=2, default=str))
    else:
        print("Server status check returned no data (may return HTML — API may be up)")


def _upsert_entity(db, r):
    """Insert or update a CA entity in registry.db."""
    entity_number = str(r.get("EntityID", ""))
    name = r.get("EntityName", "?")

    # Map entity type
    etype_raw = (r.get("EntityType") or "").lower()
    etype = TYPE_MAP.get(etype_raw, etype_raw if etype_raw else None)

    # Map status
    status_raw = (r.get("StatusDescription") or "").lower()
    status = STATUS_MAP.get(status_raw, status_raw if status_raw else None)

    # Filing date
    filing_date = r.get("FilingDate", "")
    if filing_date and "T" in str(filing_date):
        filing_date = str(filing_date).split("T")[0]

    # Entity principal address
    entity_addr = r.get("EntityStreetAddress1", "")
    if r.get("EntityStreetAddress2"):
        entity_addr = (entity_addr or "") + " " + r["EntityStreetAddress2"]

    # Mailing address
    mail_addr = r.get("MailingStreetAddress1", "")
    if r.get("MailingStreetAddress2"):
        mail_addr = (mail_addr or "") + " " + r["MailingStreetAddress2"]

    # Jurisdiction as state_of_formation
    jurisdiction = r.get("Jurisdiction", "")

    db.execute("""
        INSERT OR REPLACE INTO registry_entities (
            source_jurisdiction, source_id, entity_name, entity_type, status,
            formation_date,
            principal_address, principal_city, principal_state, principal_zip, principal_country,
            mailing_address, mailing_city, mailing_state, mailing_zip, mailing_country,
            state_of_formation, source_url, raw_data
        ) VALUES ('ca', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'US', ?, ?, ?, ?, 'US', ?, ?, ?)
    """, [
        entity_number, name, etype, status, filing_date or None,
        (entity_addr or "").strip() or None,
        r.get("EntityCity"), r.get("EntityState"), r.get("EntityZipCode"),
        (mail_addr or "").strip() or None,
        r.get("MailingCity"), r.get("MailingState"), r.get("MailingZipCode"),
        jurisdiction or "CALIFORNIA",
        f"https://bizfileonline.sos.ca.gov/search/business?entity-number={entity_number}",
        json.dumps(r, default=str),
    ])

    row = db.execute(
        "SELECT id FROM registry_entities WHERE source_jurisdiction='ca' AND source_id=?",
        [entity_number]
    ).fetchone()
    entity_id = row[0]

    # Registered agent
    agent_name = (r.get("AgentName") or "").strip()
    if agent_name:
        agent_addr = r.get("AgentAddress1", "")
        if r.get("AgentAddress2"):
            agent_addr = (agent_addr or "") + " " + r["AgentAddress2"]
        try:
            db.execute("""
                INSERT OR IGNORE INTO registry_agents
                (entity_id, agent_name, address, city, state, zip)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [entity_id, agent_name, (agent_addr or "").strip() or None,
                  r.get("AgentCity"), r.get("AgentState"), r.get("AgentZipCode")])
        except Exception:
            pass

    return entity_id


def main():
    parser = argparse.ArgumentParser(description="California SoS corporate registry (BE Public Search API)")
    parser.add_argument("--api-key", help="CA SoS API subscription key (or set CA_SOS_API_KEY env var)")
    add_output_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search CA entities by keyword (top 150 results)")
    p.add_argument("query")
    p.add_argument("--begins-with", action="store_true", dest="begins_with",
                   help="Match names starting with query (default: contains)")
    p.add_argument("--date-start", dest="date_start", help="Filter: filing date from (yyyy-mm-dd)")
    p.add_argument("--date-end", dest="date_end", help="Filter: filing date to (yyyy-mm-dd)")
    add_output_args(p)

    # search-number
    p = sub.add_parser("search-number", help="Lookup entity by CA entity number")
    p.add_argument("entity_number")
    add_output_args(p)

    # detail
    p = sub.add_parser("detail", help="Get full entity details (raw JSON)")
    p.add_argument("entity_number")
    add_output_args(p)

    # ingest-entity
    p = sub.add_parser("ingest-entity", help="Ingest CA entity into registry.db")
    p.add_argument("entity_number")

    # ingest-batch
    p = sub.add_parser("ingest-batch", help="Search + ingest all matching CA entities")
    p.add_argument("query")
    p.add_argument("--begins-with", action="store_true", dest="begins_with",
                   help="Match names starting with query")

    # server-status
    p = sub.add_parser("server-status", help="Check API server status")

    args = parser.parse_args()

    handlers = {
        "search": cmd_search,
        "search-number": cmd_search_number,
        "detail": cmd_detail,
        "ingest-entity": cmd_ingest_entity,
        "ingest-batch": cmd_ingest_batch,
        "server-status": cmd_server_status,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
