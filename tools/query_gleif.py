#!/usr/bin/env python3
"""
GLEIF (Global Legal Entity Identifier Foundation) API wrapper for OSINT investigations.

Maps corporate hierarchies (parent-subsidiary relationships) for financial institutions
in the Epstein network. LEI is the global standard for identifying legal entities in
regulated financial transactions.

No authentication required. Rate limit: 60 requests/minute.
API docs: https://api.gleif.org

Test targets:
    - Apollo Global Management: search "Apollo Global" (LEI: 54930054P2G7ZJB0KM79, now Apollo Asset Management)
    - JPMorgan Chase: search "JPMorgan" (LEI: 8I5DZWZKVSZI1NUHU748)
    - Deutsche Bank: search "Deutsche Bank" (LEI: 7LTWFZYICNSX8D621K86)
    - Limited Brands / L Brands / Bath & Body Works: search "L Brands" or "Bath Body Works"
    - Wexner-related entities: search "Wexner"

Cross-reference CIKs from investigation:
    - Apollo: CIK 1411494 / 1858681
    - JPM: CIK 19617
    - Leon Black: CIK 1032666
    - Deutsche Bank: CIK 1159508
    - Wexner: CIK 921462

Usage:
    python tools/query_gleif.py search "Apollo Global"
    python tools/query_gleif.py search "JPMorgan" --country US --limit 10
    python tools/query_gleif.py entity 54930054P2G7ZJB0KM79
    python tools/query_gleif.py parents 54930054P2G7ZJB0KM79
    python tools/query_gleif.py children 54930054P2G7ZJB0KM79
    python tools/query_gleif.py hierarchy 54930054P2G7ZJB0KM79
    python tools/query_gleif.py cross-ref
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://api.gleif.org/api/v1"
INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

# Track request timing for rate limiting (60/min)
_last_request_time = 0.0


def _request(path, params=None, allow_404=False):
    """Make an API request to GLEIF. Returns parsed JSON or None on 404 if allow_404."""
    global _last_request_time

    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params, doseq=True)

    headers = {
        "Accept": "application/vnd.api+json",
        "User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0)",
    }

    # Rate limiting: 60 req/min = 1 req/sec minimum spacing
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    req = Request(url, headers=headers)
    retries = 0
    while retries < 3:
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            if e.code == 429:
                retries += 1
                wait = 2 ** retries
                print(f"  Rate limited (429), waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code} from GLEIF: {body}", file=sys.stderr)
            sys.exit(1)
        except URLError as e:
            print(f"ERROR: Cannot reach GLEIF: {e.reason}", file=sys.stderr)
            sys.exit(1)

    print("ERROR: Exhausted retries on rate limit", file=sys.stderr)
    sys.exit(1)


def _format_address(addr):
    """Format a GLEIF address object into a single line."""
    if not addr:
        return "N/A"
    parts = []
    for line in addr.get("addressLines", []):
        if line:
            parts.append(line.strip())
    city = addr.get("city", "")
    region = addr.get("region", "")
    country = addr.get("country", "")
    postal = addr.get("postalCode", "")
    loc_parts = []
    if city:
        loc_parts.append(city)
    if region:
        loc_parts.append(region)
    if postal:
        loc_parts.append(postal)
    if country:
        loc_parts.append(country)
    if loc_parts:
        parts.append(", ".join(loc_parts))
    return ", ".join(parts) if parts else "N/A"


def _format_record_short(record):
    """Format a lei-record for list display."""
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    lei = attrs.get("lei", "?")
    name = entity.get("legalName", {}).get("name", "?")
    jurisdiction = entity.get("jurisdiction", "?")
    status = entity.get("status", "?")
    category = entity.get("category", "")
    reg_status = attrs.get("registration", {}).get("status", "")

    lines = [f"  LEI: {lei}"]
    lines.append(f"  Name: {name}")

    # Other names (aliases, previous names)
    other_names = entity.get("otherNames", [])
    if other_names:
        for on in other_names[:3]:
            on_name = on.get("name", "")
            on_type = on.get("type", "")
            if on_name:
                label = f" ({on_type})" if on_type else ""
                lines.append(f"  AKA: {on_name}{label}")
        if len(other_names) > 3:
            lines.append(f"  ... and {len(other_names) - 3} more names")

    lines.append(f"  Jurisdiction: {jurisdiction}")
    lines.append(f"  Status: {status}" + (f" (LEI: {reg_status})" if reg_status else ""))
    if category and category != "GENERAL":
        lines.append(f"  Category: {category}")

    hq = entity.get("headquartersAddress")
    if hq and hq.get("city"):
        lines.append(f"  HQ: {_format_address(hq)}")

    reg_as = entity.get("registeredAs")
    if reg_as:
        lines.append(f"  Reg #: {reg_as}")

    return "\n".join(lines)


def _format_record_full(record):
    """Format a lei-record with full details."""
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    reg = attrs.get("registration", {})
    lei = attrs.get("lei", "?")
    name = entity.get("legalName", {}).get("name", "?")

    lines = [f"=== {name} ==="]
    lines.append(f"  LEI: {lei}")
    lines.append(f"  Legal Name: {name}")

    # Other names
    other_names = entity.get("otherNames", [])
    for on in other_names:
        on_name = on.get("name", "")
        on_type = on.get("type", "")
        if on_name:
            label = f" ({on_type})" if on_type else ""
            lines.append(f"  Other Name: {on_name}{label}")

    # Core fields
    lines.append(f"  Jurisdiction: {entity.get('jurisdiction', '?')}")
    lines.append(f"  Entity Status: {entity.get('status', '?')}")
    category = entity.get("category", "GENERAL")
    if category != "GENERAL":
        lines.append(f"  Category: {category}")
    sub_cat = entity.get("subCategory")
    if sub_cat:
        lines.append(f"  Sub-Category: {sub_cat}")

    # Legal form
    legal_form = entity.get("legalForm", {})
    if legal_form.get("id"):
        lines.append(f"  Legal Form: {legal_form['id']}" +
                      (f" ({legal_form['other']})" if legal_form.get("other") else ""))

    # Registration number
    reg_as = entity.get("registeredAs")
    if reg_as:
        lines.append(f"  Registered As: {reg_as}")

    reg_at = entity.get("registeredAt", {})
    if reg_at.get("id"):
        lines.append(f"  Registration Authority: {reg_at['id']}")

    # Addresses
    legal_addr = entity.get("legalAddress")
    if legal_addr:
        lines.append(f"  Legal Address: {_format_address(legal_addr)}")
    hq_addr = entity.get("headquartersAddress")
    if hq_addr:
        lines.append(f"  HQ Address: {_format_address(hq_addr)}")

    # Other addresses
    other_addrs = entity.get("otherAddresses", [])
    for oa in other_addrs:
        oa_type = oa.get("type", "OTHER")
        lines.append(f"  Other Address ({oa_type}): {_format_address(oa)}")

    # Creation date
    creation = entity.get("creationDate")
    if creation:
        lines.append(f"  Entity Created: {creation[:10]}")

    # Expiration
    expiration = entity.get("expiration", {})
    if expiration.get("date"):
        lines.append(f"  Expiration: {expiration['date'][:10]} ({expiration.get('reason', '?')})")

    # Successor
    successor = entity.get("successorEntity", {})
    if successor.get("lei"):
        lines.append(f"  Successor: {successor.get('name', '?')} (LEI: {successor['lei']})")
    for se in entity.get("successorEntities", []):
        if se.get("lei"):
            lines.append(f"  Successor: {se.get('name', '?')} (LEI: {se['lei']})")

    # Associated entity
    assoc = entity.get("associatedEntity", {})
    if assoc.get("lei"):
        lines.append(f"  Associated Entity: {assoc.get('name', '?')} (LEI: {assoc['lei']})")

    # LEI registration details
    lines.append("")
    lines.append("  --- LEI Registration ---")
    lines.append(f"  LEI Status: {reg.get('status', '?')}")
    lines.append(f"  Initial Registration: {reg.get('initialRegistrationDate', '?')[:10] if reg.get('initialRegistrationDate') else '?'}")
    lines.append(f"  Last Update: {reg.get('lastUpdateDate', '?')[:10] if reg.get('lastUpdateDate') else '?'}")
    next_renewal = reg.get("nextRenewalDate")
    if next_renewal:
        lines.append(f"  Next Renewal: {next_renewal[:10]}")
    lines.append(f"  Corroboration: {reg.get('corroborationLevel', '?')}")
    managing = reg.get("managingLou")
    if managing:
        lines.append(f"  Managing LOU: {managing}")

    # BIC / MIC codes
    bic = attrs.get("bic")
    if bic:
        lines.append(f"  BIC: {', '.join(bic) if isinstance(bic, list) else bic}")
    mic = attrs.get("mic")
    if mic:
        lines.append(f"  MIC: {', '.join(mic) if isinstance(mic, list) else mic}")

    # OCID
    ocid = attrs.get("ocid")
    if ocid:
        lines.append(f"  OCID: {ocid}")

    return "\n".join(lines)


def cmd_search(args):
    """Search GLEIF by full-text or legal name."""
    params = {
        "page[size]": min(args.limit, 50),
        "page[number]": 1,
    }
    if args.country:
        params["filter[entity.legalAddress.country]"] = args.country.upper()

    # Use fulltext search
    params["filter[fulltext]"] = args.query

    all_results = []
    total = 0

    while len(all_results) < args.limit:
        data = _request("/lei-records", params)
        meta = data.get("meta", {}).get("pagination", {})
        total = meta.get("total", 0)
        batch = data.get("data", [])
        if not batch:
            break
        all_results.extend(batch)
        if meta.get("currentPage", 1) >= meta.get("lastPage", 1):
            break
        params["page[number]"] = meta.get("currentPage", 1) + 1

    all_results = all_results[:args.limit]

    if write_output(all_results, args, summary=f"GLEIF search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps([r for r in all_results], indent=2, default=str))
        return

    country_label = f" (country={args.country.upper()})" if args.country else ""
    print(f"GLEIF: {total} total results for '{args.query}'{country_label} (showing {len(all_results)})")
    print()

    for r in all_results:
        print(_format_record_short(r))
        print()


def cmd_entity(args):
    """Get full entity details by LEI."""
    data = _request(f"/lei-records/{args.lei}", allow_404=True)
    if not data:
        print(f"No entity found for LEI: {args.lei}")
        return

    record = data.get("data", data)

    if write_output(data, args, summary=f"GLEIF entity {args.lei}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    print(_format_record_full(record))
    print()


def cmd_parents(args):
    """Show direct parent and ultimate parent for a LEI."""
    # First get the entity itself
    entity_data = _request(f"/lei-records/{args.lei}", allow_404=True)
    if not entity_data:
        print(f"No entity found for LEI: {args.lei}")
        return

    entity_record = entity_data.get("data", entity_data)
    entity_name = entity_record.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "?")

    # Direct parent
    direct = _request(f"/lei-records/{args.lei}/direct-parent", allow_404=True)

    # Ultimate parent
    ultimate = _request(f"/lei-records/{args.lei}/ultimate-parent", allow_404=True)

    out = {"direct_parent": direct, "ultimate_parent": ultimate}
    if write_output(out, args, summary=f"GLEIF parents for {args.lei}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(out, indent=2, default=str))
        return

    print(f"=== Parent Relationships for {entity_name} ({args.lei}) ===")
    print()

    if direct and direct.get("data"):
        parent_record = direct["data"]
        parent_attrs = parent_record.get("attributes", {})
        parent_entity = parent_attrs.get("entity", {})
        parent_lei = parent_attrs.get("lei", "?")
        parent_name = parent_entity.get("legalName", {}).get("name", "?")
        parent_jurisdiction = parent_entity.get("jurisdiction", "?")
        parent_status = parent_entity.get("status", "?")
        print(f"  Direct Parent:")
        print(f"    LEI: {parent_lei}")
        print(f"    Name: {parent_name}")
        print(f"    Jurisdiction: {parent_jurisdiction}")
        print(f"    Status: {parent_status}")
        hq = parent_entity.get("headquartersAddress")
        if hq and hq.get("city"):
            print(f"    HQ: {_format_address(hq)}")
    else:
        print("  Direct Parent: None reported")

    print()

    if ultimate and ultimate.get("data"):
        parent_record = ultimate["data"]
        parent_attrs = parent_record.get("attributes", {})
        parent_entity = parent_attrs.get("entity", {})
        parent_lei = parent_attrs.get("lei", "?")
        parent_name = parent_entity.get("legalName", {}).get("name", "?")
        parent_jurisdiction = parent_entity.get("jurisdiction", "?")
        parent_status = parent_entity.get("status", "?")
        print(f"  Ultimate Parent:")
        print(f"    LEI: {parent_lei}")
        print(f"    Name: {parent_name}")
        print(f"    Jurisdiction: {parent_jurisdiction}")
        print(f"    Status: {parent_status}")
        hq = parent_entity.get("headquartersAddress")
        if hq and hq.get("city"):
            print(f"    HQ: {_format_address(hq)}")

        # Note if direct == ultimate
        direct_lei = None
        if direct and direct.get("data"):
            direct_lei = direct["data"].get("attributes", {}).get("lei")
        if direct_lei and direct_lei == parent_lei:
            print("    (Same as direct parent)")
    else:
        print("  Ultimate Parent: None reported")

    print()


def cmd_children(args):
    """List all direct subsidiaries of a LEI."""
    entity_data = _request(f"/lei-records/{args.lei}", allow_404=True)
    if not entity_data:
        print(f"No entity found for LEI: {args.lei}")
        return

    entity_record = entity_data.get("data", entity_data)
    entity_name = entity_record.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "?")

    all_children = []
    total = 0
    page = 1
    page_size = min(args.limit, 50)

    while len(all_children) < args.limit:
        params = {"page[size]": page_size, "page[number]": page}
        data = _request(f"/lei-records/{args.lei}/direct-children", params)
        meta = data.get("meta", {}).get("pagination", {})
        total = meta.get("total", 0)
        batch = data.get("data", [])
        if not batch:
            break
        all_children.extend(batch)
        if meta.get("currentPage", 1) >= meta.get("lastPage", 1):
            break
        page += 1

    all_children = all_children[:args.limit]

    if write_output(all_children, args, summary=f"GLEIF children of {args.lei}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(all_children, indent=2, default=str))
        return

    print(f"=== Direct Subsidiaries of {entity_name} ({args.lei}) ===")
    print(f"Total: {total} subsidiaries (showing {len(all_children)})")
    print()

    # Group by country for readability
    by_country = {}
    for child in all_children:
        attrs = child.get("attributes", {})
        entity = attrs.get("entity", {})
        country = entity.get("jurisdiction", entity.get("legalAddress", {}).get("country", "??"))
        by_country.setdefault(country, []).append(child)

    for country in sorted(by_country.keys()):
        children = by_country[country]
        print(f"  --- {country} ({len(children)}) ---")
        for child in children:
            attrs = child.get("attributes", {})
            entity = attrs.get("entity", {})
            lei = attrs.get("lei", "?")
            name = entity.get("legalName", {}).get("name", "?")
            status = entity.get("status", "?")
            print(f"    {lei}  {name}  [{status}]")
        print()


def cmd_hierarchy(args):
    """Full hierarchy: ultimate parent -> direct parent -> entity -> children (tree view)."""
    # Get the entity itself
    entity_data = _request(f"/lei-records/{args.lei}", allow_404=True)
    if not entity_data:
        print(f"No entity found for LEI: {args.lei}")
        return

    entity_record = entity_data.get("data", entity_data)
    entity_attrs = entity_record.get("attributes", {})
    entity_entity = entity_attrs.get("entity", {})
    entity_name = entity_entity.get("legalName", {}).get("name", "?")
    entity_lei = entity_attrs.get("lei", args.lei)
    entity_jurisdiction = entity_entity.get("jurisdiction", "?")
    entity_status = entity_entity.get("status", "?")

    # Ultimate parent
    ultimate = _request(f"/lei-records/{args.lei}/ultimate-parent", allow_404=True)
    ultimate_lei = None
    if ultimate and ultimate.get("data"):
        u_attrs = ultimate["data"].get("attributes", {})
        u_entity = u_attrs.get("entity", {})
        ultimate_lei = u_attrs.get("lei", "?")
        ultimate_name = u_entity.get("legalName", {}).get("name", "?")
        ultimate_jurisdiction = u_entity.get("jurisdiction", "?")
        ultimate_status = u_entity.get("status", "?")

    # Direct parent
    direct = _request(f"/lei-records/{args.lei}/direct-parent", allow_404=True)
    direct_lei = None
    if direct and direct.get("data"):
        d_attrs = direct["data"].get("attributes", {})
        d_entity = d_attrs.get("entity", {})
        direct_lei = d_attrs.get("lei", "?")
        direct_name = d_entity.get("legalName", {}).get("name", "?")
        direct_jurisdiction = d_entity.get("jurisdiction", "?")
        direct_status = d_entity.get("status", "?")

    # Children
    children_data = _request(f"/lei-records/{args.lei}/direct-children",
                              {"page[size]": 50, "page[number]": 1})
    children = children_data.get("data", [])
    children_total = children_data.get("meta", {}).get("pagination", {}).get("total", len(children))

    out = {
        "entity": entity_data,
        "direct_parent": direct,
        "ultimate_parent": ultimate,
        "children": children_data,
    }
    if write_output(out, args, summary=f"GLEIF hierarchy for {args.lei}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(out, indent=2, default=str))
        return

    # Build tree display
    indent = ""

    print(f"=== Corporate Hierarchy for {entity_name} ===")
    print()

    # Show ultimate parent if different from direct parent
    if ultimate_lei and ultimate_lei != entity_lei:
        if not direct_lei or ultimate_lei != direct_lei:
            print(f"{indent}[ULTIMATE PARENT]")
            print(f"{indent}{ultimate_name}")
            print(f"{indent}LEI: {ultimate_lei}  |  {ultimate_jurisdiction}  |  {ultimate_status}")
            print(f"{indent}  |")
            print(f"{indent}  |  (may have intermediate entities)")
            print(f"{indent}  |")
            indent = ""

    # Show direct parent if it exists and is different from entity
    if direct_lei and direct_lei != entity_lei:
        print(f"{indent}[DIRECT PARENT]")
        print(f"{indent}{direct_name}")
        print(f"{indent}LEI: {direct_lei}  |  {direct_jurisdiction}  |  {direct_status}")
        print(f"{indent}  |")

    # Show entity itself
    marker = ">>>"
    print(f"{indent}{marker} [THIS ENTITY]")
    print(f"{indent}{marker} {entity_name}")
    print(f"{indent}{marker} LEI: {entity_lei}  |  {entity_jurisdiction}  |  {entity_status}")

    if children:
        print(f"{indent}  |")
        print(f"{indent}  +-- {children_total} direct subsidiaries:")

        for i, child in enumerate(children):
            c_attrs = child.get("attributes", {})
            c_entity = c_attrs.get("entity", {})
            c_lei = c_attrs.get("lei", "?")
            c_name = c_entity.get("legalName", {}).get("name", "?")
            c_jurisdiction = c_entity.get("jurisdiction", "?")
            c_status = c_entity.get("status", "?")
            connector = "|--" if i < len(children) - 1 else "`--"
            print(f"{indent}      {connector} {c_name}")
            print(f"{indent}      {'|' if i < len(children) - 1 else ' '}   LEI: {c_lei}  |  {c_jurisdiction}  |  {c_status}")

        if children_total > len(children):
            print(f"{indent}      ... and {children_total - len(children)} more")
    else:
        print(f"{indent}  (no direct subsidiaries reported)")

    print()


def cmd_cross_ref(args):
    """Cross-reference all entities in investigation.db against GLEIF."""
    if not INVESTIGATION_DB.exists():
        print(f"ERROR: investigation.db not found at {INVESTIGATION_DB}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(INVESTIGATION_DB))
    conn.row_factory = sqlite3.Row

    # Get entities from investigation.db
    try:
        rows = conn.execute("""
            SELECT id, name, entity_type, jurisdiction, status
            FROM entities
            ORDER BY name
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"ERROR: Cannot query entities table: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    if not rows:
        print("No entities found in investigation.db")
        return

    print(f"Cross-referencing {len(rows)} entities from investigation.db against GLEIF")
    print(f"(This will make up to {len(rows)} API requests)")
    print()

    matches = []
    no_match = []
    errors = 0

    for row in rows:
        name = row["name"]
        entity_id = row["id"]
        entity_type = row["entity_type"] or "?"
        jurisdiction = row["jurisdiction"] or "?"

        # Skip person entities — GLEIF only has legal entities
        if entity_type and entity_type.lower() in ("person", "individual"):
            continue

        # Skip very short names that will produce garbage results
        if len(name) < 3:
            continue

        sys.stdout.write(f"  Searching: {name[:60]:<60} ... ")
        sys.stdout.flush()

        try:
            params = {
                "filter[fulltext]": name,
                "page[size]": 5,
                "page[number]": 1,
            }
            data = _request("/lei-records", params)
            results = data.get("data", [])
            total = data.get("meta", {}).get("pagination", {}).get("total", 0)

            if results:
                # Check for close name match
                best = None
                for r in results:
                    r_name = r.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "")
                    # Check other names too
                    other_names = [on.get("name", "").upper() for on in
                                   r.get("attributes", {}).get("entity", {}).get("otherNames", [])]
                    all_names = [r_name.upper()] + other_names
                    if name.upper() in all_names or any(name.upper() in n for n in all_names):
                        best = r
                        break
                    # Also check if search name is a substring
                    if any(name.upper() in n for n in all_names):
                        best = r
                        break

                if not best:
                    best = results[0]

                best_attrs = best.get("attributes", {})
                best_entity = best_attrs.get("entity", {})
                best_lei = best_attrs.get("lei", "?")
                best_name = best_entity.get("legalName", {}).get("name", "?")
                best_jurisdiction = best_entity.get("jurisdiction", "?")
                best_status = best_entity.get("status", "?")

                print(f"MATCH ({total} results)")
                print(f"    -> {best_name}")
                print(f"       LEI: {best_lei}  |  {best_jurisdiction}  |  {best_status}")

                matches.append({
                    "investigation_id": entity_id,
                    "investigation_name": name,
                    "investigation_type": entity_type,
                    "gleif_lei": best_lei,
                    "gleif_name": best_name,
                    "gleif_jurisdiction": best_jurisdiction,
                    "gleif_status": best_status,
                    "gleif_total_results": total,
                })
            else:
                print("no results")
                no_match.append(name)

        except Exception as e:
            print(f"ERROR: {e}")
            errors += 1

    out = {"matches": matches, "no_match": no_match, "errors": errors}
    if write_output(out, args, summary=f"GLEIF cross-ref {len(matches)} matches"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(out, indent=2, default=str))
        return

    # Summary
    print()
    print("=" * 70)
    print(f"GLEIF Cross-Reference Summary")
    print(f"  Entities searched: {len(rows)}")
    print(f"  Matches found: {len(matches)}")
    print(f"  No results: {len(no_match)}")
    print(f"  Errors: {errors}")
    print()

    if matches:
        print("=== Matched Entities ===")
        for m in matches:
            print(f"  {m['investigation_name']}")
            print(f"    Investigation ID: {m['investigation_id']}, Type: {m['investigation_type']}")
            print(f"    GLEIF: {m['gleif_name']}")
            print(f"    LEI: {m['gleif_lei']}  |  {m['gleif_jurisdiction']}  |  {m['gleif_status']}")
            print()


def main():
    parser = argparse.ArgumentParser(
        description="GLEIF LEI API for corporate hierarchy mapping in OSINT investigation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name (full-text)")
    p.add_argument("query", help="Search query (company name, keyword)")
    p.add_argument("--country", help="Filter by country ISO2 code (e.g., US, GB, DE)")
    p.add_argument("--limit", type=int, default=20, help="Max results (default 20)")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity details by LEI")
    p.add_argument("lei", help="20-character LEI identifier")
    add_output_args(p)

    # parents
    p = sub.add_parser("parents", help="Show direct and ultimate parent entities")
    p.add_argument("lei", help="20-character LEI identifier")
    add_output_args(p)

    # children
    p = sub.add_parser("children", help="List direct subsidiaries")
    p.add_argument("lei", help="20-character LEI identifier")
    p.add_argument("--limit", type=int, default=100, help="Max results (default 100)")
    add_output_args(p)

    # hierarchy
    p = sub.add_parser("hierarchy", help="Full hierarchy tree: parent -> entity -> children")
    p.add_argument("lei", help="20-character LEI identifier")
    add_output_args(p)

    # cross-ref
    p = sub.add_parser("cross-ref", help="Cross-reference investigation.db entities against GLEIF")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "parents": cmd_parents,
        "children": cmd_children,
        "hierarchy": cmd_hierarchy,
        "cross-ref": cmd_cross_ref,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
