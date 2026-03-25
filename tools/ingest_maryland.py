#!/usr/bin/env python3
"""
Maryland SDAT (State Department of Assessments and Taxation) business entity search.

The Maryland Business Express portal at egov.maryland.gov has reCAPTCHA v2 protection,
so this tool requires manual CAPTCHA solving on first use per session. After the
initial CAPTCHA is solved, multiple entities can be searched in the same session.

Data available publicly (no login required):
  - Entity name, department ID, EIN
  - Entity type, status
  - Formation date, dissolution date
  - Resident agent name + address
  - Principal office address
  - Officers and directors (when available)

Note: Maryland also offers bulk data via SpecPrint Inc ($2,100/week).
This tool is for targeted queries, not bulk downloads.

Usage:
    python tools/ingest_maryland.py search "Capital Athletic Foundation"
    python tools/ingest_maryland.py search "Abramoff" --contains
    python tools/ingest_maryland.py detail D02357507          # By department ID
    python tools/ingest_maryland.py ingest-entity D02357507
    python tools/ingest_maryland.py ingest-batch "Capital Athletic Foundation LLC" "Eshkol Academy"

This tool requires the Playwright MCP server to be running.
User must solve CAPTCHA manually on first search.
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from output_util import add_output_args, write_output
except ImportError:
    from tools.output_util import add_output_args, write_output

try:
    from query_registry import get_db, _rebuild_fts
except ImportError:
    from tools.query_registry import get_db, _rebuild_fts

BASE_URL = "https://egov.maryland.gov/BusinessExpress/EntitySearch"

# Rate limiting: be respectful
REQUEST_DELAY = 2  # seconds between searches


def search_entity(name, contains=False):
    """
    Search for business entities by name.

    This is a MANUAL tool — requires user to:
    1. Navigate to Maryland Business Express
    2. Solve CAPTCHA
    3. Enter search term
    4. Extract results

    This function provides instructions only.
    Actual automation requires Playwright MCP integration.

    Args:
        name: Entity name to search for
        contains: If True, partial match; if False, exact match

    Returns:
        List of entity dicts with keys: name, department_id, status, type
    """

    print(f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║ MANUAL SEARCH REQUIRED                                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

Maryland SDAT requires manual CAPTCHA solving. Please follow these steps:

1. Open browser to: {BASE_URL}
2. Solve the reCAPTCHA
3. Select "Business Name" search
4. Enter: "{name}"
5. Click "Search"
6. Review results and note department IDs

For automation, this tool needs Playwright MCP integration with manual CAPTCHA
intervention on first use, then session reuse for subsequent searches.

Alternative: Purchase bulk data from SpecPrint Inc at $2,100/week
Contact: [email protected] / 410-561-9600
""", file=sys.stderr)

    return []


def get_entity_detail(department_id):
    """
    Get detailed information for a specific entity by department ID.

    Args:
        department_id: Maryland department ID (e.g., D02357507)

    Returns:
        Dict with entity details or None if not found
    """

    url = f"{BASE_URL}/BusinessInformation/{department_id}"

    print(f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║ MANUAL LOOKUP REQUIRED                                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

To retrieve details for department ID {department_id}:

1. Open browser to: {url}
2. Solve CAPTCHA if prompted
3. Review entity details across tabs:
   - General: name, status, formation date, agent, address
   - History: status changes, filings
   - PPF: Personal Property Filing information

For automated access, integrate Playwright MCP with session management.
""", file=sys.stderr)

    return None


def ingest_entity(department_id):
    """
    Fetch entity details and store in registry.db.

    Currently returns instructions for manual process.
    Full implementation requires Playwright MCP integration.
    """
    entity = get_entity_detail(department_id)

    if not entity:
        print(f"Entity {department_id} not available (manual process required)", file=sys.stderr)
        return False

    # TODO: Insert into registry.db when Playwright integration is complete
    print(f"Entity {department_id}: manual ingestion required", file=sys.stderr)
    return False


def ingest_batch(*entity_names):
    """
    Search for and ingest multiple entities.

    Args:
        entity_names: Variable number of entity names to search and ingest
    """
    print(f"Batch ingestion for {len(entity_names)} entities requires Playwright MCP.", file=sys.stderr)
    print("Workflow:", file=sys.stderr)
    print("  1. User solves CAPTCHA once", file=sys.stderr)
    print("  2. Tool searches each entity in sequence", file=sys.stderr)
    print("  3. Tool extracts department IDs", file=sys.stderr)
    print("  4. Tool retrieves details for each", file=sys.stderr)
    print("  5. Tool ingests into registry.db", file=sys.stderr)
    print("\nNot yet implemented.", file=sys.stderr)
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Maryland SDAT business entity search (manual CAPTCHA required)"
    )
    sub = parser.add_subparsers(dest="command", help="Command to execute")

    # search command
    search_cmd = sub.add_parser("search", help="Search for entities by name")
    search_cmd.add_argument("name", help="Entity name to search for")
    search_cmd.add_argument(
        "--contains",
        action="store_true",
        help="Partial match (default: exact match)",
    )
    add_output_args(search_cmd)

    # detail command
    detail_cmd = sub.add_parser("detail", help="Get entity details by department ID")
    detail_cmd.add_argument("department_id", help="Maryland department ID (e.g., D02357507)")
    add_output_args(detail_cmd)

    # ingest-entity command
    ingest_cmd = sub.add_parser("ingest-entity", help="Fetch and store entity in registry.db")
    ingest_cmd.add_argument("department_id", help="Maryland department ID")

    # ingest-batch command
    batch_cmd = sub.add_parser(
        "ingest-batch",
        help="Search and ingest multiple entities"
    )
    batch_cmd.add_argument(
        "entity_names",
        nargs="+",
        help="Entity names to search and ingest"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        results = search_entity(args.name, contains=args.contains)
        output = {
            "query": args.name,
            "source": "Maryland SDAT",
            "url": BASE_URL,
            "manual_process": True,
            "results": results,
            "note": "This tool requires manual CAPTCHA solving. See stderr for instructions."
        }
        if not write_output(output, args, summary=f"Maryland SDAT search '{args.name}'"):
            print(json.dumps(output, indent=2))

    elif args.command == "detail":
        entity = get_entity_detail(args.department_id)
        output = {
            "department_id": args.department_id,
            "source": "Maryland SDAT",
            "manual_process": True,
            "entity": entity,
            "note": "This tool requires manual CAPTCHA solving. See stderr for instructions."
        }
        if not write_output(output, args, summary=f"Maryland SDAT detail {args.department_id}"):
            print(json.dumps(output, indent=2))

    elif args.command == "ingest-entity":
        success = ingest_entity(args.department_id)
        sys.exit(0 if success else 1)

    elif args.command == "ingest-batch":
        results = ingest_batch(*args.entity_names)
        print(json.dumps({"ingested": len(results), "entities": results}, indent=2))


if __name__ == "__main__":
    main()
