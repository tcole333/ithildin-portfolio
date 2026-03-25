#!/usr/bin/env python3
"""
Medicaid provider corporate trace pipeline.

Takes billing NPIs (from anomaly detection or manual input) and traces them through:
  NPI → NPPES enrichment → state corporate registry → officer/agent network

This is the unique value layer: connecting Medicaid billing entities to their
corporate structures, revealing shared officers, agents, and addresses.

Usage:
    # Trace a single NPI
    python tools/trace_provider.py trace 1376609297 --output /tmp/trace.json

    # Trace top anomalous providers and cross-ref against corporate registries
    python tools/trace_provider.py batch --top-anomalies 20 --output /tmp/batch.json

    # Trace NPIs from a file (one per line)
    python tools/trace_provider.py batch --file /tmp/npis.txt --output /tmp/batch.json

    # Find officer networks — people who appear across multiple billing entities
    python tools/trace_provider.py officer-network --min-entities 2 --output /tmp/officers.json

    # Find shared registered agents across billing entities
    python tools/trace_provider.py agent-network --min-entities 3 --output /tmp/agents.json

    # Cross-reference billing NPIs against OIG exclusion list
    python tools/trace_provider.py excluded --output /tmp/excluded.json

    # Full pipeline: anomalies → trace → officer network → report
    python tools/trace_provider.py pipeline --top-anomalies 50 --output /tmp/pipeline.json
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

import duckdb

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# NY DOS live lookup support
try:
    from tools.query_nydos import (
        _api_post as nydos_api_post,
        _ingest_entity_to_registry as nydos_ingest,
        ALL_ENTITY_TYPES as NYDOS_ENTITY_TYPES,
    )
    HAS_NYDOS = True
except ImportError:
    try:
        from query_nydos import (
            _api_post as nydos_api_post,
            _ingest_entity_to_registry as nydos_ingest,
            ALL_ENTITY_TYPES as NYDOS_ENTITY_TYPES,
        )
        HAS_NYDOS = True
    except ImportError:
        HAS_NYDOS = False

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REGISTRY_DB = Path(__file__).resolve().parent.parent / "registry.db"
PARQUET_PATH = DATA_DIR / "medicaid_spending.parquet"
BILLING_PROVIDERS_PATH = DATA_DIR / "billing_providers.parquet"
SERVICING_PROVIDERS_PATH = DATA_DIR / "servicing_providers.parquet"
LEIE_PATH = DATA_DIR / "leie_exclusions.csv"


def _duckdb():
    """Return DuckDB connection with parquet views registered."""
    con = duckdb.connect()
    con.execute(f"CREATE VIEW m AS SELECT * FROM read_parquet('{PARQUET_PATH}')")
    if BILLING_PROVIDERS_PATH.exists():
        con.execute(f"CREATE VIEW bp AS SELECT * FROM read_parquet('{BILLING_PROVIDERS_PATH}')")
    if SERVICING_PROVIDERS_PATH.exists():
        con.execute(f"CREATE VIEW sp AS SELECT * FROM read_parquet('{SERVICING_PROVIDERS_PATH}')")
    return con


def _registry():
    """Return registry.db connection with WAL mode and busy timeout."""
    if not REGISTRY_DB.exists():
        print(f"Warning: {REGISTRY_DB} not found", file=sys.stderr)
        return None
    db = sqlite3.connect(str(REGISTRY_DB), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    return db


def _fmt(n):
    """Format a dollar amount."""
    if n is None:
        return "?"
    if abs(n) >= 1_000_000_000:
        return f"${n/1e9:.1f}B"
    if abs(n) >= 1_000_000:
        return f"${n/1e6:.1f}M"
    if abs(n) >= 1_000:
        return f"${n/1e3:.0f}K"
    return f"${n:.0f}"


# --- NPPES lookup ---

def lookup_nppes(con, npi):
    """Look up a billing NPI in NPPES data. Returns dict or None."""
    row = con.execute(
        "SELECT * FROM bp WHERE npi = ?", [str(npi)]
    ).fetchone()
    if row:
        cols = [d[0] for d in con.description]
        return dict(zip(cols, row))
    # Try servicing providers
    row = con.execute(
        "SELECT * FROM sp WHERE npi = ?", [str(npi)]
    ).fetchone()
    if row:
        cols = [d[0] for d in con.description]
        return dict(zip(cols, row))
    return None


def lookup_spending(con, npi):
    """Get aggregate spending stats for a billing NPI."""
    row = con.execute("""
        SELECT billing_npi,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(paid)/NULLIF(sum(claims),0) as avg_per_claim,
            count(DISTINCT hcpcs_code) as code_count,
            count(DISTINCT servicing_npi) as svc_count,
            min(claim_month) as first_month,
            max(claim_month) as last_month,
            count(DISTINCT claim_month) as active_months,
            sum(beneficiaries) as total_beneficiaries
        FROM m WHERE billing_npi = ?
        GROUP BY billing_npi
    """, [str(npi)]).fetchone()
    if not row:
        return None
    cols = [d[0] for d in con.description]
    return dict(zip(cols, row))


# --- Registry matching ---

def search_registry(db, org_name, state=None):
    """Search registry.db for entities matching an organization name.

    Returns list of matching entities with officers and agents.
    """
    if not db:
        return []

    # Normalize name for search
    name = org_name.strip().upper()
    # Remove common suffixes for broader matching
    for suffix in [", LLC", " LLC", ", INC", " INC", ", INC.", " INC.",
                   ", CORP", " CORP", ", CORP.", " CORP.",
                   ", LP", " LP", ", LLP", " LLP"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
            break

    matches = []

    # Fast path: jurisdiction-scoped LIKE search when we know the state
    # Uses source_jurisdiction index — instant for small jurisdictions
    rows = []
    if state:
        juris = state.lower()
        rows = db.execute("""
            SELECT * FROM registry_entities
            WHERE source_jurisdiction = ?
            AND entity_name LIKE ?
            LIMIT 20
        """, [juris, f"%{name}%"]).fetchall()

    # Fall back to FTS for cross-jurisdiction or if scoped search empty
    if not rows:
        try:
            rows = db.execute("""
                SELECT re.* FROM registry_entities_fts fts
                JOIN registry_entities re ON fts.rowid = re.id
                WHERE registry_entities_fts MATCH ?
                ORDER BY rank LIMIT 20
            """, [f'"{name}"']).fetchall()
        except sqlite3.OperationalError:
            rows = []

    # Final fallback: unscoped LIKE (only when no state filter — avoids 6M row scan)
    if not rows and not state:
        rows = db.execute("""
            SELECT * FROM registry_entities
            WHERE entity_name LIKE ? LIMIT 20
        """, [f"%{name}%"]).fetchall()

    for row in rows:
        entity = dict(row)
        eid = entity["id"]

        # Get officers
        officers = [dict(o) for o in db.execute(
            "SELECT * FROM registry_officers WHERE entity_id = ?", [eid]
        ).fetchall()]

        # Get agents
        agents = [dict(a) for a in db.execute(
            "SELECT * FROM registry_agents WHERE entity_id = ?", [eid]
        ).fetchall()]

        # Get filings
        filings = [dict(f) for f in db.execute(
            "SELECT * FROM registry_filings WHERE entity_id = ? ORDER BY filing_date DESC LIMIT 10", [eid]
        ).fetchall()]

        entity["officers"] = officers
        entity["agents"] = agents
        entity["recent_filings"] = filings
        matches.append(entity)

    return matches


def search_registry_by_address(db, address, city=None, state=None):
    """Search registry for entities at a given address."""
    if not db:
        return []

    addr_norm = address.strip().upper()

    # Scope to jurisdiction for performance when state is known
    if state:
        query = "SELECT * FROM registry_entities WHERE source_jurisdiction = ? AND UPPER(principal_address) LIKE ? "
        params = [state.lower(), f"%{addr_norm}%"]
    else:
        query = "SELECT * FROM registry_entities WHERE UPPER(principal_address) LIKE ? "
        params = [f"%{addr_norm}%"]

    if city:
        query += "AND UPPER(principal_city) LIKE ? "
        params.append(f"%{city.upper()}%")

    query += "LIMIT 50"
    return [dict(r) for r in db.execute(query, params).fetchall()]


def search_nydos_live(db, org_name):
    """Search NY DOS API live and ingest results into registry.db.

    Returns list of ingested entity IDs, or empty list if no matches.
    """
    if not HAS_NYDOS or not db:
        return []

    # Normalize name for search
    name = org_name.strip()
    # Remove suffixes that might interfere with DOS search
    for suffix in [", LLC", " LLC", ", INC", " INC", ", INC.", " INC.",
                   ", CORP", " CORP", ", CORP.", " CORP."]:
        if name.upper().endswith(suffix):
            name = name[:-len(suffix)].strip()
            break

    # Search NY DOS
    search_data = nydos_api_post("GetComplexSearchMatchingEntities", {
        "searchValue": name,
        "searchByTypeIndicator": "EntityName",
        "searchExpressionIndicator": "Contains",
        "entityStatusIndicator": "AllStatuses",
        "entityTypeIndicator": NYDOS_ENTITY_TYPES,
        "listPaginationInfo": {"listStartRecord": 1, "listEndRecord": 20},
    })

    if not search_data or not search_data.get("entitySearchResultList"):
        return []

    results = search_data["entitySearchResultList"]
    ingested = []

    for r in results[:10]:  # Limit to top 10 matches
        dos_id = r.get("dosID")
        if not dos_id:
            continue
        # Check if already ingested
        existing = db.execute(
            "SELECT id FROM registry_entities WHERE source_jurisdiction='ny' AND source_id=?",
            [str(dos_id)]
        ).fetchone()
        if existing:
            ingested.append(existing[0])
            continue

        entity_id = nydos_ingest(db, dos_id, r.get("entityName"))
        if entity_id:
            ingested.append(entity_id)

    if ingested:
        db.commit()
        # Incrementally add new entities to FTS (much faster than full rebuild)
        for eid in ingested:
            try:
                db.execute("""
                    INSERT INTO registry_entities_fts(rowid, entity_name, principal_address, purpose)
                    SELECT id, entity_name, principal_address, purpose
                    FROM registry_entities WHERE id = ?
                """, [eid])
            except sqlite3.OperationalError:
                pass
            try:
                # Also index any new officers
                db.execute("""
                    INSERT INTO registry_officers_fts(rowid, officer_name, address)
                    SELECT id, officer_name, address
                    FROM registry_officers WHERE entity_id = ?
                """, [eid])
            except sqlite3.OperationalError:
                pass
        db.commit()

    return ingested


def find_officer_across_entities(db, officer_name, jurisdiction=None):
    """Find all entities where a person is listed as an officer.

    When jurisdiction is provided, only searches within that jurisdiction
    to avoid expensive full-table scans on large registries (6M+ FL entities).
    """
    if not db:
        return []

    name_norm = officer_name.strip().upper()

    if jurisdiction:
        rows = db.execute("""
            SELECT ro.*, re.entity_name, re.source_jurisdiction, re.source_id,
                   re.status, re.formation_date, re.entity_type
            FROM registry_officers ro
            JOIN registry_entities re ON ro.entity_id = re.id
            WHERE re.source_jurisdiction = ?
            AND UPPER(ro.officer_name) LIKE ?
            ORDER BY re.formation_date
        """, [jurisdiction.lower(), f"%{name_norm}%"]).fetchall()
    else:
        rows = db.execute("""
            SELECT ro.*, re.entity_name, re.source_jurisdiction, re.source_id,
                   re.status, re.formation_date, re.entity_type
            FROM registry_officers ro
            JOIN registry_entities re ON ro.entity_id = re.id
            WHERE UPPER(ro.officer_name) LIKE ?
            ORDER BY re.formation_date
        """, [f"%{name_norm}%"]).fetchall()
    return [dict(r) for r in rows]


# --- OIG LEIE Exclusion check ---

def load_leie():
    """Load OIG LEIE exclusion list."""
    if not LEIE_PATH.exists():
        return {}
    exclusions = {}
    with open(LEIE_PATH, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            npi = row.get("NPI", "").strip()
            if npi and npi != "0000000000":
                exclusions[npi] = {
                    "name": row.get("BUSNAME") or f"{row.get('LASTNAME', '')}, {row.get('FIRSTNAME', '')}".strip(", "),
                    "excl_type": row.get("EXCLTYPE", ""),
                    "excl_date": row.get("EXCLDATE", ""),
                    "rein_date": row.get("REINDATE", ""),
                    "state": row.get("STATE", ""),
                    "city": row.get("CITY", ""),
                }
    return exclusions


# --- Trace command ---

def trace_npi(npi, con, db, leie=None):
    """Full trace of a single NPI: NPPES → spending → registry → officers."""
    result = {"npi": str(npi), "steps": []}

    # Step 1: NPPES lookup
    nppes = lookup_nppes(con, npi)
    if nppes:
        result["nppes"] = nppes
        result["org_name"] = nppes.get("org_name") or f"{nppes.get('first_name', '')} {nppes.get('last_name', '')}".strip()
        result["state"] = nppes.get("state")
        result["city"] = nppes.get("city")
        result["address"] = nppes.get("address_line1")
        result["taxonomy"] = nppes.get("taxonomy_code")
        result["steps"].append("nppes_found")
    else:
        result["org_name"] = None
        result["steps"].append("nppes_not_found")

    # Step 2: Spending summary
    spending = lookup_spending(con, npi)
    if spending:
        result["spending"] = spending
        result["steps"].append("spending_found")
    else:
        result["steps"].append("no_billing_data")

    # Step 3: LEIE exclusion check
    if leie and str(npi) in leie:
        result["excluded"] = leie[str(npi)]
        result["steps"].append("oig_excluded")

    # Step 4: Registry search (by name)
    if result.get("org_name") and db:
        registry_matches = search_registry(db, result["org_name"], result.get("state"))

        # Filter to same-state matches if possible
        provider_state = (result.get("state") or "").upper()
        state_matches = []
        if provider_state and registry_matches:
            state_matches = [m for m in registry_matches
                             if (m.get("principal_state") or "").upper() == provider_state
                             or (m.get("source_jurisdiction") or "").upper() == provider_state]

        # Step 4b: Live state registry lookup if no same-state match
        if not state_matches and provider_state == "NY":
            ingested_ids = search_nydos_live(db, result["org_name"])
            if ingested_ids:
                result["steps"].append(f"nydos_live_ingested_{len(ingested_ids)}")
                registry_matches = search_registry(db, result["org_name"], result.get("state"))
                state_matches = [m for m in registry_matches
                                 if (m.get("principal_state") or "").upper() == "NY"
                                 or (m.get("source_jurisdiction") or "").upper() == "NY"]

        # Prefer same-state matches; fall back to all matches
        if state_matches:
            registry_matches = state_matches

            result["registry_matches"] = registry_matches
            result["steps"].append(f"registry_{len(registry_matches)}_matches")

            # Collect all unique officers and agents across matches
            all_officers = {}
            all_agents = {}
            for m in registry_matches:
                for o in m.get("officers", []):
                    oname = o.get("officer_name", "").upper()
                    if oname:
                        all_officers[oname] = o
                for a in m.get("agents", []):
                    aname = a.get("agent_name", "").upper()
                    if aname:
                        all_agents[aname] = a

            result["unique_officers"] = list(all_officers.keys())
            result["unique_agents"] = list(all_agents.keys())

            # Step 5: Cross-reference officers to find other entities they control
            # Scope to same jurisdiction for performance (avoids 6M row FL scan)
            jurisdictions = list({m.get("source_jurisdiction") for m in registry_matches if m.get("source_jurisdiction")})
            officer_network = {}
            matched_ids = {m["id"] for m in registry_matches}
            for oname in all_officers:
                other_entities = []
                for j in jurisdictions:
                    other_entities.extend(find_officer_across_entities(db, oname, jurisdiction=j))
                other_only = [e for e in other_entities if e.get("entity_id") not in matched_ids]
                if other_only:
                    officer_network[oname] = other_only
                    result["steps"].append(f"officer_{oname}_controls_{len(other_only)}_other_entities")

            if officer_network:
                result["officer_network"] = officer_network

            # Step 6: Search by address to find co-located entities
            if result.get("address"):
                addr_matches = search_registry_by_address(
                    db, result["address"], result.get("city"), result.get("state")
                )
                addr_new = [a for a in addr_matches if a["id"] not in matched_ids]
                if addr_new:
                    result["colocated_entities"] = addr_new
                    result["steps"].append(f"address_{len(addr_new)}_colocated")
        else:
            result["steps"].append("registry_no_match")

    return result


# --- Commands ---

def cmd_trace(args):
    """Trace a single billing NPI through the full pipeline."""
    con = _duckdb()
    db = _registry()
    leie = load_leie() if LEIE_PATH.exists() else {}

    result = trace_npi(args.npi, con, db, leie)

    if write_output(result, args, summary=f"trace NPI {args.npi}"):
        return

    _print_trace(result)


def cmd_batch(args):
    """Trace multiple NPIs — from anomaly detection or a file."""
    con = _duckdb()
    db = _registry()
    leie = load_leie() if LEIE_PATH.exists() else {}

    npis = []

    if args.top_anomalies:
        # Pull top anomalous NPIs from the anomaly query
        rows = con.execute(f"""
            WITH provider_stats AS (
                SELECT billing_npi,
                    sum(paid) as total_paid,
                    count(DISTINCT hcpcs_code) as code_count,
                    count(DISTINCT claim_month) as active_months
                FROM m
                GROUP BY billing_npi
                HAVING sum(paid) > 10000000
            )
            SELECT ps.billing_npi, ps.total_paid, bp.org_name, bp.state
            FROM provider_stats ps
            LEFT JOIN bp ON ps.billing_npi = bp.npi
            WHERE bp.entity_type = 2
            ORDER BY ps.total_paid DESC
            LIMIT {args.top_anomalies}
        """).fetchall()
        npis = [r[0] for r in rows]
        print(f"Tracing top {len(npis)} billing orgs by total paid")

    elif args.file:
        with open(args.file) as f:
            for line in f:
                npi = line.strip()
                if npi and npi.isdigit():
                    npis.append(npi)
        print(f"Loaded {len(npis)} NPIs from {args.file}")

    elif args.npis:
        npis = args.npis

    if not npis:
        print("No NPIs to trace. Use --top-anomalies N, --file FILE, or provide NPIs as arguments.")
        return

    results = []
    for i, npi in enumerate(npis, 1):
        print(f"  [{i}/{len(npis)}] Tracing {npi}...", end="", flush=True)
        try:
            result = trace_npi(npi, con, db, leie)
        except Exception as e:
            print(f" ERROR: {e}")
            result = {"npi": str(npi), "steps": [f"error: {e}"], "error": str(e)}
        results.append(result)
        name = result.get("org_name") or "?"
        rmatch = len(result.get("registry_matches", []))
        steps = result.get("steps", [])
        flags = []
        if "oig_excluded" in steps:
            flags.append("EXCLUDED")
        if rmatch > 0:
            flags.append(f"{rmatch} reg")
        if result.get("officer_network"):
            total_other = sum(len(v) for v in result["officer_network"].values())
            flags.append(f"{total_other} linked")
        if result.get("colocated_entities"):
            flags.append(f"{len(result['colocated_entities'])} colocated")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f" {name[:40]}{flag_str}")

    # Summary stats
    summary = {
        "total_traced": len(results),
        "registry_matched": sum(1 for r in results if r.get("registry_matches")),
        "with_officer_network": sum(1 for r in results if r.get("officer_network")),
        "with_colocated": sum(1 for r in results if r.get("colocated_entities")),
        "oig_excluded": sum(1 for r in results if r.get("excluded")),
    }

    output = {"summary": summary, "traces": results}

    if write_output(output, args, summary=f"batch trace {len(results)} NPIs"):
        return

    print(f"\n  Summary:")
    print(f"    Traced: {summary['total_traced']}")
    print(f"    Registry matches: {summary['registry_matched']}")
    print(f"    Officer networks found: {summary['with_officer_network']}")
    print(f"    Co-located entities: {summary['with_colocated']}")
    print(f"    OIG excluded: {summary['oig_excluded']}")


def cmd_officer_network(args):
    """Find officers who appear across multiple billing-entity registry records."""
    db = _registry()
    if not db:
        print("registry.db not found")
        return

    con = _duckdb()

    # Get all officers grouped by name, with entity count
    rows = db.execute("""
        SELECT ro.officer_name, COUNT(DISTINCT ro.entity_id) as entity_count,
               GROUP_CONCAT(DISTINCT re.entity_name, ' | ') as entities,
               GROUP_CONCAT(DISTINCT re.source_jurisdiction || ':' || re.source_id, ', ') as ids
        FROM registry_officers ro
        JOIN registry_entities re ON ro.entity_id = re.id
        GROUP BY UPPER(ro.officer_name)
        HAVING COUNT(DISTINCT ro.entity_id) >= ?
        ORDER BY COUNT(DISTINCT ro.entity_id) DESC
        LIMIT ?
    """, [args.min_entities, args.limit]).fetchall()

    results = []
    for r in rows:
        officer = {
            "officer_name": r["officer_name"],
            "entity_count": r["entity_count"],
            "entities": r["entities"],
            "registry_ids": r["ids"],
        }

        # Check if any of these entities are Medicaid billers
        entity_names = [e.strip() for e in r["entities"].split(" | ")]
        billing_matches = []
        for ename in entity_names:
            match = con.execute("""
                SELECT bp.npi, bp.org_name, sum(m.paid) as total_paid
                FROM bp
                JOIN m ON bp.npi = m.billing_npi
                WHERE UPPER(bp.org_name) LIKE ?
                GROUP BY bp.npi, bp.org_name
                HAVING sum(m.paid) > 0
                LIMIT 3
            """, [f"%{ename[:30].upper()}%"]).fetchall()
            for bm in match:
                billing_matches.append({
                    "npi": bm[0], "org_name": bm[1], "total_paid": bm[2]
                })

        if billing_matches:
            officer["medicaid_billing"] = billing_matches
            officer["total_medicaid_paid"] = sum(b["total_paid"] for b in billing_matches)

        results.append(officer)

    # Sort: officers with Medicaid billing first, then by entity count
    results.sort(key=lambda x: (
        -x.get("total_medicaid_paid", 0),
        -x["entity_count"]
    ))

    output = {"total": len(results), "min_entities": args.min_entities, "officers": results}

    if write_output(output, args, summary=f"officer network ({len(results)} officers, >={args.min_entities} entities)"):
        return

    print(f"\n  Officers controlling {args.min_entities}+ entities ({len(results)} found)")
    print(f"  {'='*100}")
    for i, o in enumerate(results[:50], 1):
        paid_str = _fmt(o.get("total_medicaid_paid", 0)) if o.get("medicaid_billing") else ""
        entities_short = o["entities"][:60]
        print(f"  {i:>3}. {o['officer_name'][:30]:<30} {o['entity_count']:>3} entities  {paid_str:>10}  {entities_short}")


def cmd_agent_network(args):
    """Find registered agents shared across multiple entities, filtered to Medicaid billers."""
    db = _registry()
    if not db:
        print("registry.db not found")
        return

    rows = db.execute("""
        SELECT ra.agent_name, COUNT(DISTINCT ra.entity_id) as entity_count,
               GROUP_CONCAT(DISTINCT re.entity_name, ' | ') as entities,
               ra.address, ra.city, ra.state
        FROM registry_agents ra
        JOIN registry_entities re ON ra.entity_id = re.id
        WHERE ra.agent_name IS NOT NULL AND ra.agent_name != ''
        GROUP BY UPPER(ra.agent_name)
        HAVING COUNT(DISTINCT ra.entity_id) >= ?
        ORDER BY COUNT(DISTINCT ra.entity_id) DESC
        LIMIT ?
    """, [args.min_entities, args.limit]).fetchall()

    results = []
    for r in rows:
        results.append({
            "agent_name": r["agent_name"],
            "entity_count": r["entity_count"],
            "entities": r["entities"],
            "address": r["address"],
            "city": r["city"],
            "state": r["state"],
        })

    output = {"total": len(results), "min_entities": args.min_entities, "agents": results}

    if write_output(output, args, summary=f"agent network ({len(results)} agents, >={args.min_entities} entities)"):
        return

    print(f"\n  Registered agents serving {args.min_entities}+ entities ({len(results)} found)")
    print(f"  {'='*100}")
    for i, a in enumerate(results[:50], 1):
        loc = f"{a['city'] or ''}, {a['state'] or ''}".strip(", ")
        print(f"  {i:>3}. {a['agent_name'][:35]:<35} {a['entity_count']:>4} entities  {loc}")


def cmd_excluded(args):
    """Cross-reference billing NPIs against OIG LEIE exclusion list."""
    con = _duckdb()
    leie = load_leie()
    if not leie:
        print("LEIE exclusion list not found at", LEIE_PATH)
        return

    # Get all billing NPIs with spending
    print(f"  Checking {len(leie)} excluded NPIs against billing data...")
    leie_npis = list(leie.keys())

    # DuckDB can handle IN clause with many values via a temp table
    con.execute("CREATE TEMP TABLE excl_npis (npi VARCHAR)")
    for npi in leie_npis:
        con.execute("INSERT INTO excl_npis VALUES (?)", [npi])

    rows = con.execute("""
        SELECT m.billing_npi, sum(m.paid) as total_paid, sum(m.claims) as total_claims,
               min(m.claim_month) as first_month, max(m.claim_month) as last_month,
               bp.org_name, bp.city, bp.state
        FROM m
        JOIN excl_npis e ON m.billing_npi = e.npi
        LEFT JOIN bp ON m.billing_npi = bp.npi
        GROUP BY m.billing_npi, bp.org_name, bp.city, bp.state
        HAVING sum(m.paid) > 0
        ORDER BY sum(m.paid) DESC
    """).fetchall()

    results = []
    for r in rows:
        npi = r[0]
        excl = leie.get(npi, {})
        results.append({
            "npi": npi,
            "total_paid": r[1],
            "total_claims": r[2],
            "first_month": r[3],
            "last_month": r[4],
            "org_name": r[5],
            "city": r[6],
            "state": r[7],
            "excl_name": excl.get("name"),
            "excl_type": excl.get("excl_type"),
            "excl_date": excl.get("excl_date"),
            "rein_date": excl.get("rein_date"),
        })

    total_paid = sum(r["total_paid"] for r in results)
    output = {
        "total_excluded_billing": len(results),
        "total_paid": total_paid,
        "providers": results,
    }

    if write_output(output, args, summary=f"excluded providers ({len(results)}, {_fmt(total_paid)})"):
        return

    print(f"\n  {len(results)} excluded providers still billing ({_fmt(total_paid)} total)")
    print(f"  {'='*110}")
    print(f"  {'NPI':>12} {'Total Paid':>12} {'Claims':>8} {'First':>7} {'Last':>7} {'ExclDate':>8} {'Type':>7} Name")
    print(f"  {'-'*110}")
    for r in results:
        name = (r["org_name"] or r["excl_name"] or "?")[:40]
        print(f"  {r['npi']:>12} {_fmt(r['total_paid']):>12} {r['total_claims']:>8,} {r['first_month']:>7} {r['last_month']:>7} {r['excl_date']:>8} {r['excl_type']:>7} {name}")


def cmd_pipeline(args):
    """Full pipeline: anomalies → trace → officer network → summary report."""
    con = _duckdb()
    db = _registry()
    leie = load_leie() if LEIE_PATH.exists() else {}

    n = args.top_anomalies
    print(f"=== Phase 1: Identify top {n} anomalous billing organizations ===")

    # Get anomalous providers
    rows = con.execute(f"""
        WITH provider_stats AS (
            SELECT billing_npi,
                sum(paid) as total_paid,
                sum(claims) as total_claims,
                sum(paid)/NULLIF(sum(claims),0) as avg_per_claim,
                count(DISTINCT hcpcs_code) as code_count,
                count(DISTINCT servicing_npi) as svc_count,
                count(DISTINCT claim_month) as active_months
            FROM m
            GROUP BY billing_npi
            HAVING sum(paid) > 10000000
        ),
        top_code AS (
            SELECT billing_npi, hcpcs_code,
                sum(paid) as code_paid,
                ROW_NUMBER() OVER (PARTITION BY billing_npi ORDER BY sum(paid) DESC) as rn
            FROM m GROUP BY billing_npi, hcpcs_code
        ),
        code_medians AS (
            SELECT hcpcs_code,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY paid/NULLIF(claims,0)) as median_per_claim
            FROM m WHERE claims > 100
            GROUP BY hcpcs_code
        )
        SELECT ps.billing_npi, ps.total_paid, ps.avg_per_claim, ps.code_count,
            ps.active_months, ps.svc_count,
            tc.hcpcs_code as top_code,
            tc.code_paid / ps.total_paid as top_code_pct,
            ps.avg_per_claim / NULLIF(cm.median_per_claim, 0) as cost_ratio,
            bp.org_name, bp.city, bp.state, bp.taxonomy_code, bp.address_line1
        FROM provider_stats ps
        LEFT JOIN top_code tc ON ps.billing_npi = tc.billing_npi AND tc.rn = 1
        LEFT JOIN code_medians cm ON tc.hcpcs_code = cm.hcpcs_code
        LEFT JOIN bp ON ps.billing_npi = bp.npi
        WHERE bp.entity_type = 2
        ORDER BY
            (CASE WHEN tc.code_paid / ps.total_paid > 0.9 THEN 2 ELSE 0 END)
            + (CASE WHEN ps.avg_per_claim / NULLIF(cm.median_per_claim, 0) > 3 THEN 2 ELSE 0 END)
            + (CASE WHEN ps.code_count <= 3 THEN 1 ELSE 0 END)
            + (CASE WHEN ps.active_months < 36 THEN 1 ELSE 0 END)
            DESC, ps.total_paid DESC
        LIMIT {n}
    """).fetchall()

    anomalies = []
    for r in rows:
        anomalies.append({
            "billing_npi": r[0], "total_paid": r[1], "avg_per_claim": r[2],
            "code_count": r[3], "active_months": r[4], "svc_count": r[5],
            "top_code": r[6], "top_code_pct": r[7], "cost_ratio": r[8],
            "org_name": r[9], "city": r[10], "state": r[11],
            "taxonomy": r[12], "address": r[13],
        })
    print(f"  Found {len(anomalies)} anomalous organizations")

    print(f"\n=== Phase 2: Trace {len(anomalies)} NPIs through corporate registries ===")
    traces = []
    for i, a in enumerate(anomalies, 1):
        npi = a["billing_npi"]
        print(f"  [{i}/{len(anomalies)}] {a.get('org_name', '?')[:45]}...", end="", flush=True)
        result = trace_npi(npi, con, db, leie)
        traces.append(result)
        rmatch = len(result.get("registry_matches", []))
        flags = []
        if "oig_excluded" in result.get("steps", []):
            flags.append("EXCLUDED")
        if rmatch > 0:
            flags.append(f"{rmatch} reg")
        if result.get("officer_network"):
            flags.append(f"officer_net")
        print(f" [{', '.join(flags)}]" if flags else " [no match]")

    print(f"\n=== Phase 3: Build officer/agent network from traces ===")

    # Collect all officers and agents across all traced entities
    officer_map = {}  # officer_name -> list of entity names
    agent_map = {}    # agent_name -> list of entity names
    for trace in traces:
        org = trace.get("org_name") or trace["npi"]
        for m in trace.get("registry_matches", []):
            for o in m.get("officers", []):
                oname = o.get("officer_name", "").upper()
                if oname:
                    officer_map.setdefault(oname, set()).add(org)
            for a in m.get("agents", []):
                aname = a.get("agent_name", "").upper()
                if aname:
                    agent_map.setdefault(aname, set()).add(org)

    # Officers controlling multiple traced entities
    shared_officers = {k: list(v) for k, v in officer_map.items() if len(v) >= 2}
    shared_agents = {k: list(v) for k, v in agent_map.items() if len(v) >= 2}

    print(f"  Officers controlling 2+ traced entities: {len(shared_officers)}")
    print(f"  Agents serving 2+ traced entities: {len(shared_agents)}")

    for oname, ents in sorted(shared_officers.items(), key=lambda x: -len(x[1])):
        print(f"    {oname}: {', '.join(e[:30] for e in ents)}")

    # Build output
    output = {
        "pipeline_summary": {
            "anomalies_scanned": len(anomalies),
            "registry_matched": sum(1 for t in traces if t.get("registry_matches")),
            "officer_networks_found": sum(1 for t in traces if t.get("officer_network")),
            "colocated_found": sum(1 for t in traces if t.get("colocated_entities")),
            "oig_excluded": sum(1 for t in traces if t.get("excluded")),
            "shared_officers": len(shared_officers),
            "shared_agents": len(shared_agents),
        },
        "anomalies": anomalies,
        "traces": traces,
        "shared_officers": shared_officers,
        "shared_agents": shared_agents,
    }

    if write_output(output, args, summary=f"pipeline ({len(anomalies)} providers, {len(shared_officers)} shared officers)"):
        return

    print(f"\n  === Pipeline Summary ===")
    for k, v in output["pipeline_summary"].items():
        print(f"    {k}: {v}")


# --- Display ---

def _print_trace(result):
    """Pretty-print a single trace result."""
    npi = result["npi"]
    org = result.get("org_name") or "Unknown"
    print(f"\n  === Trace: {org} (NPI {npi}) ===")
    print(f"  Steps: {' → '.join(result['steps'])}")

    if result.get("nppes"):
        n = result["nppes"]
        print(f"\n  NPPES:")
        print(f"    Name: {n.get('org_name') or n.get('last_name', '')}")
        print(f"    Address: {n.get('address_line1', '')} {n.get('city', '')}, {n.get('state', '')} {n.get('zip', '')}")
        print(f"    Taxonomy: {n.get('taxonomy_code', '?')}")
        print(f"    Enumerated: {n.get('enumeration_date', '?')}")

    if result.get("spending"):
        s = result["spending"]
        print(f"\n  Spending (2018-2024):")
        print(f"    Total Paid: {_fmt(s['total_paid'])}")
        print(f"    Claims: {s['total_claims']:,}")
        print(f"    Avg/Claim: {_fmt(s['avg_per_claim'])}")
        print(f"    HCPCS Codes Used: {s['code_count']}")
        print(f"    Servicing NPIs: {s['svc_count']}")
        print(f"    Active: {s['first_month']} to {s['last_month']} ({s['active_months']} months)")

    if result.get("excluded"):
        e = result["excluded"]
        print(f"\n  *** OIG EXCLUDED ***")
        print(f"    Name: {e['name']}")
        print(f"    Type: {e['excl_type']}")
        print(f"    Date: {e['excl_date']}")

    if result.get("registry_matches"):
        print(f"\n  Registry Matches ({len(result['registry_matches'])}):")
        for m in result["registry_matches"]:
            status = m.get("status", "?")
            formed = m.get("formation_date", "?")
            juris = m.get("source_jurisdiction", "?")
            print(f"    - {m['entity_name']} [{juris.upper()}:{m.get('source_id', '')}]")
            print(f"      Type: {m.get('entity_type')}  Status: {status}  Formed: {formed}")
            if m.get("officers"):
                for o in m["officers"]:
                    print(f"      Officer: {o['officer_name']} ({o.get('title', '?')})")
            if m.get("agents"):
                for a in m["agents"]:
                    print(f"      Agent: {a['agent_name']}")

    if result.get("officer_network"):
        print(f"\n  Officer Network (other entities controlled by same officers):")
        for oname, entities in result["officer_network"].items():
            print(f"    {oname} also controls:")
            for e in entities[:5]:
                print(f"      - {e['entity_name']} [{e.get('source_jurisdiction', '?').upper()}] ({e.get('status', '?')})")
            if len(entities) > 5:
                print(f"      ... and {len(entities) - 5} more")

    if result.get("colocated_entities"):
        print(f"\n  Co-located Entities ({len(result['colocated_entities'])} at same address):")
        for e in result["colocated_entities"][:10]:
            print(f"    - {e['entity_name']} ({e.get('entity_type', '?')}, {e.get('status', '?')})")


def main():
    parser = argparse.ArgumentParser(
        description="Medicaid provider corporate trace pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # trace
    p = sub.add_parser("trace", help="Trace a single billing NPI")
    p.add_argument("npi", help="Billing NPI to trace")
    add_output_args(p)

    # batch
    p = sub.add_parser("batch", help="Trace multiple NPIs")
    p.add_argument("--top-anomalies", type=int, help="Trace top N anomalous billing orgs")
    p.add_argument("--file", help="File with NPIs, one per line")
    p.add_argument("npis", nargs="*", help="NPIs to trace")
    add_output_args(p)

    # officer-network
    p = sub.add_parser("officer-network", help="Find officers across multiple entities")
    p.add_argument("--min-entities", type=int, default=2, help="Min entities per officer")
    p.add_argument("--limit", type=int, default=100)
    add_output_args(p)

    # agent-network
    p = sub.add_parser("agent-network", help="Find shared registered agents")
    p.add_argument("--min-entities", type=int, default=3, help="Min entities per agent")
    p.add_argument("--limit", type=int, default=100)
    add_output_args(p)

    # excluded
    p = sub.add_parser("excluded", help="Cross-reference billing NPIs against OIG exclusion list")
    add_output_args(p)

    # pipeline
    p = sub.add_parser("pipeline", help="Full pipeline: anomalies → trace → officer network")
    p.add_argument("--top-anomalies", type=int, default=50, help="Number of top anomalous providers")
    add_output_args(p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "trace": cmd_trace,
        "batch": cmd_batch,
        "officer-network": cmd_officer_network,
        "agent-network": cmd_agent_network,
        "excluded": cmd_excluded,
        "pipeline": cmd_pipeline,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
