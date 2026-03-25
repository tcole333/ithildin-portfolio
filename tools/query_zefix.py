#!/usr/bin/env python3
"""
Swiss Federal Commercial Registry (Zefix) query tool via SPARQL.

Searches Swiss corporate registry using public SPARQL endpoint at lindas.admin.ch.
Covers all Swiss companies, foundations, associations. No authentication required.

Usage:
    python tools/query_zefix.py search "ILEX"
    python tools/query_zefix.py search "UBS" --limit 20
    python tools/query_zefix.py company "https://register.ld.admin.ch/zefix/company/20243"
    python tools/query_zefix.py uid CHE107848049
    python tools/query_zefix.py stats
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

# Public SPARQL endpoint (no auth)
SPARQL_ENDPOINT = "https://lindas.admin.ch/query/"

# Common prefixes
PREFIXES = """
PREFIX schema: <http://schema.org/>
PREFIX admin: <https://schema.ld.admin.ch/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
"""


def sparql_query(query, max_retries=3):
    """Execute SPARQL query against Lindas endpoint."""
    for attempt in range(max_retries):
        try:
            data = urlencode({"query": query}).encode("utf-8")
            req = Request(SPARQL_ENDPOINT, data=data, method="POST")
            req.add_header("Accept", "application/sparql-results+json")
            req.add_header("User-Agent", "OSINT-Research/1.0")

            with urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["results"]["bindings"]
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def search_companies(name, limit=50):
    """Search for companies by name (case-insensitive substring match)."""
    query = f"""{PREFIXES}
SELECT ?company ?name ?uid ?chid ?status ?legalForm WHERE {{
  ?company a admin:ZefixOrganisation ;
           schema:name ?name .
  OPTIONAL {{ ?company admin:uid ?uid . }}
  OPTIONAL {{ ?company admin:chid ?chid . }}
  OPTIONAL {{ ?company schema:validThrough ?validThrough . }}
  OPTIONAL {{ ?company schema:additionalType ?legalFormUri .
              ?legalFormUri schema:name ?legalForm . }}
  FILTER(CONTAINS(LCASE(?name), LCASE("{name}")))
  BIND(IF(BOUND(?validThrough), "inactive", "active") AS ?status)
}} LIMIT {limit}
"""

    results = sparql_query(query)
    companies = []

    for binding in results:
        company = {
            "uri": binding["company"]["value"],
            "name": binding["name"]["value"],
        }
        if "uid" in binding:
            company["uid"] = binding["uid"]["value"]
        if "chid" in binding:
            company["chid"] = binding["chid"]["value"]
        if "status" in binding:
            company["status"] = binding["status"]["value"]
        if "legalForm" in binding:
            company["legal_form"] = binding["legalForm"]["value"]

        companies.append(company)

    return companies


def get_company_details(company_uri):
    """Get full details for a specific company by URI."""
    query = f"""{PREFIXES}
SELECT ?p ?o WHERE {{
  <{company_uri}> ?p ?o .
}}
"""

    results = sparql_query(query)
    details = {"uri": company_uri}
    identifiers = []

    for binding in results:
        predicate = binding["p"]["value"]
        obj = binding["o"]

        # Extract field name from URI
        field = predicate.split("/")[-1].split("#")[-1]

        if predicate == "http://schema.org/name":
            details["name"] = obj["value"]
        elif predicate == "http://schema.org/description":
            details["description"] = obj["value"]
        elif predicate == "http://schema.org/identifier":
            identifiers.append(obj["value"])
        elif predicate == "http://schema.org/address":
            # Fetch address details
            addr = get_address(obj["value"])
            if addr:
                details["address"] = addr
        elif predicate == "http://schema.org/validFrom":
            details["valid_from"] = obj["value"]
        elif predicate == "http://schema.org/validThrough":
            details["valid_through"] = obj["value"]
        elif predicate == "http://schema.org/additionalType":
            # Get legal form
            legal_form = get_legal_form(obj["value"])
            if legal_form:
                details["legal_form"] = legal_form

    # Parse identifiers
    for ident in identifiers:
        if "/UID/" in ident:
            details["uid"] = ident.split("/UID/")[-1]
        elif "/CHID/" in ident:
            details["chid"] = ident.split("/CHID/")[-1]
        elif "/EHRAID" in ident:
            details["ehraid"] = ident.split("/EHRAID/")[-1] if "/EHRAID/" in ident else "present"

    return details


def get_address(address_uri):
    """Fetch address details."""
    query = f"""{PREFIXES}
SELECT ?street ?locality ?postalCode ?canton WHERE {{
  <{address_uri}> schema:streetAddress ?street .
  OPTIONAL {{ <{address_uri}> schema:addressLocality ?locality . }}
  OPTIONAL {{ <{address_uri}> schema:postalCode ?postalCode . }}
  OPTIONAL {{ <{address_uri}> admin:canton ?canton . }}
}}
"""

    results = sparql_query(query)
    if not results:
        return None

    binding = results[0]
    addr = {
        "street": binding["street"]["value"]
    }
    if "locality" in binding:
        addr["locality"] = binding["locality"]["value"]
    if "postalCode" in binding:
        addr["postal_code"] = binding["postalCode"]["value"]
    if "canton" in binding:
        addr["canton"] = binding["canton"]["value"]

    return addr


def get_legal_form(legal_form_uri):
    """Get legal form name."""
    query = f"""{PREFIXES}
SELECT ?name WHERE {{
  <{legal_form_uri}> schema:name ?name .
}}
"""

    results = sparql_query(query)
    if results and "name" in results[0]:
        return results[0]["name"]["value"]
    return None


def search_by_uid(uid):
    """Search for a company by UID."""
    query = f"""{PREFIXES}
SELECT ?company ?name WHERE {{
  ?company a admin:ZefixOrganisation ;
           schema:name ?name ;
           schema:identifier ?identifier .
  ?identifier schema:value "{uid}" .
}}
"""

    results = sparql_query(query)
    if not results:
        return None

    return get_company_details(results[0]["company"]["value"])


def get_stats():
    """Get basic statistics about the registry."""
    query = f"""{PREFIXES}
SELECT (COUNT(?company) AS ?total) WHERE {{
  ?company a admin:ZefixOrganisation .
}}
"""

    results = sparql_query(query)
    total = int(results[0]["total"]["value"])

    # Get active vs inactive
    active_query = f"""{PREFIXES}
SELECT (COUNT(?company) AS ?active) WHERE {{
  ?company a admin:ZefixOrganisation .
  FILTER NOT EXISTS {{ ?company schema:validThrough ?validThrough . }}
}}
"""
    active_results = sparql_query(active_query)
    active = int(active_results[0]["active"]["value"])

    return {
        "total_companies": total,
        "active": active,
        "inactive": total - active
    }


def main():
    parser = argparse.ArgumentParser(
        description="Query Swiss Federal Commercial Registry (Zefix) via SPARQL"
    )
    sub = parser.add_subparsers(dest="command", help="Command to execute")

    # search command
    search_cmd = sub.add_parser("search", help="Search companies by name")
    search_cmd.add_argument("name", help="Company name to search for")
    search_cmd.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    add_output_args(search_cmd)

    # company command
    company_cmd = sub.add_parser("company", help="Get company details by URI")
    company_cmd.add_argument("uri", help="Company URI (e.g., https://register.ld.admin.ch/zefix/company/20243)")
    add_output_args(company_cmd)

    # uid command
    uid_cmd = sub.add_parser("uid", help="Search by UID")
    uid_cmd.add_argument("uid", help="Swiss UID (e.g., CHE107848049)")
    add_output_args(uid_cmd)

    # stats command
    stats_cmd = sub.add_parser("stats", help="Get registry statistics")
    add_output_args(stats_cmd)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "search":
            results = search_companies(args.name, limit=args.limit)
            log_search(
                query_text=args.name,
                source="zefix",
                result_count=len(results)
            )
            write_output(results, args)
            if not getattr(args, "output", None):
                print(f"\nFound {len(results)} companies")
                for c in results[:10]:
                    print(f"  {c['name']}")
                    if "uid" in c:
                        print(f"    UID: {c['uid']}")
                    if "status" in c:
                        print(f"    Status: {c['status']}")
                    print(f"    URI: {c['uri']}")
                if len(results) > 10:
                    print(f"  ... and {len(results) - 10} more")

        elif args.command == "company":
            details = get_company_details(args.uri)
            log_search(
                query_text=args.uri,
                source="zefix",
                result_count=1 if details else 0
            )
            write_output(details, args)
            if not getattr(args, "output", None):
                print(json.dumps(details, indent=2, ensure_ascii=False))

        elif args.command == "uid":
            details = search_by_uid(args.uid)
            log_search(
                query_text=args.uid,
                source="zefix",
                result_count=1 if details else 0
            )
            if details:
                write_output(details, args)
                if not getattr(args, "output", None):
                    print(json.dumps(details, indent=2, ensure_ascii=False))
            else:
                print(f"No company found with UID: {args.uid}")
                sys.exit(1)

        elif args.command == "stats":
            stats = get_stats()
            write_output(stats, args)
            if not getattr(args, "output", None):
                print(f"Total companies: {stats['total_companies']:,}")
                print(f"Active: {stats['active']:,}")
                print(f"Inactive: {stats['inactive']:,}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
