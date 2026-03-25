#!/usr/bin/env python3
"""
Unified IRS 990 query tool — local bulk DB + ProPublica API enrichment.

Primary data: local SQLite (datasets/irs990_grants.db) with 22M+ grants,
5M+ officers, 600K+ financials, Schedule J/L, checklist flags.
Enrichment: ProPublica Nonprofit Explorer API for org metadata, NTEE codes,
filing PDFs, and EIN discovery.

Usage:
    python tools/query_990.py search "Epstein"
    python tools/query_990.py lookup 660789697           # comprehensive EIN view
    python tools/query_990.py filer 660789697
    python tools/query_990.py recipient "Gratitude"
    python tools/query_990.py recipient-ein 030213226
    python tools/query_990.py network 660789697 --depth 2
    python tools/query_990.py co-grantors "MELANOMA RESEARCH ALLIANCE"
    python tools/query_990.py cross-ref
    python tools/query_990.py top --by amount --limit 20
    python tools/query_990.py officers 660789697
    python tools/query_990.py officer-search "John Smith"
    python tools/query_990.py financials 660789697
    python tools/query_990.py filings 660789697          # filing list + PDF links
    python tools/query_990.py red-flags 660789697
    python tools/query_990.py top-compensated --min-comp 500000
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# ProPublica enrichment module (optional — degrades gracefully if unavailable)
try:
    from tools.query_990_propublica import search_orgs as _pp_search, get_org as _pp_get_org, get_filings as _pp_get_filings
except ImportError:
    try:
        from query_990_propublica import search_orgs as _pp_search, get_org as _pp_get_org, get_filings as _pp_get_filings
    except ImportError:
        _pp_search = None
        _pp_get_org = None
        _pp_get_filings = None

DB_PATH = Path(__file__).parent.parent / "datasets" / "irs990_grants.db"
INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"


def get_db():
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} not found. Run: ingest_990_bulk.py download-index → process", file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _has_fts():
    """Check if FTS5 tables exist."""
    db = get_db()
    try:
        db.execute("SELECT COUNT(*) FROM grants_fts LIMIT 1")
        db.close()
        return True
    except sqlite3.OperationalError:
        db.close()
        return False


def _fmt_amount(amt):
    if amt is None:
        return "$0"
    return f"${amt:,.0f}"


# ── search ──────────────────────────────────────────────────────

def cmd_search(args):
    """Full-text search across grants and related orgs."""
    db = get_db()
    query = args.query
    limit = args.limit

    # Try FTS5 first, fall back to LIKE
    fts = _has_fts()
    grants = []
    related = []

    if fts:
        # FTS5 search
        rows = db.execute("""
            SELECT g.* FROM grants g
            JOIN grants_fts ON grants_fts.rowid = g.id
            WHERE grants_fts MATCH ?
            ORDER BY g.cash_amount DESC
            LIMIT ?
        """, (query, limit)).fetchall()
        grants = [dict(r) for r in rows]

        rows = db.execute("""
            SELECT r.* FROM related_orgs r
            JOIN related_orgs_fts ON related_orgs_fts.rowid = r.id
            WHERE related_orgs_fts MATCH ?
            LIMIT ?
        """, (query, limit)).fetchall()
        related = [dict(r) for r in rows]
    else:
        # LIKE fallback
        pattern = f"%{query}%"
        rows = db.execute("""
            SELECT * FROM grants
            WHERE filer_name LIKE ? OR recipient_name LIKE ?
                OR purpose LIKE ? OR filer_ein LIKE ? OR recipient_ein LIKE ?
            ORDER BY cash_amount DESC
            LIMIT ?
        """, (pattern, pattern, pattern, pattern, pattern, limit)).fetchall()
        grants = [dict(r) for r in rows]

        rows = db.execute("""
            SELECT * FROM related_orgs
            WHERE filer_name LIKE ? OR related_name LIKE ?
                OR primary_activities LIKE ? OR filer_ein LIKE ? OR related_ein LIKE ?
            LIMIT ?
        """, (pattern, pattern, pattern, pattern, pattern, limit)).fetchall()
        related = [dict(r) for r in rows]

    print(f"\nSearch '{query}': {len(grants)} grants, {len(related)} related orgs" +
          ("" if fts else " (no FTS — using LIKE, run build-fts for faster search)"))

    if grants:
        print(f"\n  GRANTS ({len(grants)}):")
        for g in grants[:30]:
            print(f"    {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  "
                  f"{g.get('filer_name', '?')[:30]:30s} → {g.get('recipient_name', '?')[:40]}")

    if related:
        print(f"\n  RELATED ORGS ({len(related)}):")
        for r in related[:20]:
            print(f"    {r.get('tax_year', '?'):>5}  [{r.get('relationship_type', ''):20s}] "
                  f"{r.get('filer_name', '?')[:30]:30s} ↔ {r.get('related_name', '?')[:40]}")

    results = {"grants": grants, "related_orgs": related}
    write_output(results, args, summary=f"990 bulk search '{query}'")
    db.close()


# ── filer ───────────────────────────────────────────────────────

def cmd_filer(args):
    """List all grants made by a filer EIN."""
    db = get_db()
    ein = args.ein.replace("-", "")

    rows = db.execute("""
        SELECT * FROM grants
        WHERE filer_ein = ? OR filer_ein = ?
        ORDER BY tax_year DESC, cash_amount DESC
    """, (ein, args.ein)).fetchall()
    results = [dict(r) for r in rows]

    total = sum(r.get("cash_amount", 0) or 0 for r in results)
    filer_name = results[0]["filer_name"] if results else ein

    print(f"\nGrants by {filer_name} (EIN {ein}): {len(results)} grants, {_fmt_amount(total)} total")
    for g in results[:50]:
        print(f"  {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  "
              f"→ {g.get('recipient_name', '?')[:50]}"
              + (f"  ({g.get('recipient_ein')})" if g.get("recipient_ein") else ""))

    if len(results) > 50:
        print(f"  ... and {len(results) - 50} more (use --output to see all)")

    write_output(results, args, summary=f"990 grants by EIN {ein}")
    db.close()


# ── recipient ───────────────────────────────────────────────────

def cmd_recipient(args):
    """Find grants received by name (FTS5 or LIKE)."""
    db = get_db()
    name = args.name
    limit = args.limit

    fts = _has_fts()
    if fts:
        rows = db.execute("""
            SELECT g.* FROM grants g
            JOIN grants_fts ON grants_fts.rowid = g.id
            WHERE grants_fts.recipient_name MATCH ?
            ORDER BY g.cash_amount DESC
            LIMIT ?
        """, (name, limit)).fetchall()
    else:
        rows = db.execute("""
            SELECT * FROM grants
            WHERE recipient_name LIKE ?
            ORDER BY cash_amount DESC
            LIMIT ?
        """, (f"%{name}%", limit)).fetchall()

    results = [dict(r) for r in rows]
    total = sum(r.get("cash_amount", 0) or 0 for r in results)

    print(f"\nGrants to '{name}': {len(results)} grants, {_fmt_amount(total)} total")
    for g in results[:50]:
        print(f"  {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  "
              f"← {g.get('filer_name', '?')[:40]:40s} ({g.get('filer_ein', '?')})")

    write_output(results, args, summary=f"990 grants to '{name}'")
    db.close()


# ── recipient-ein ───────────────────────────────────────────────

def cmd_recipient_ein(args):
    """Find all grants received by a specific EIN."""
    db = get_db()
    ein = args.ein.replace("-", "")

    rows = db.execute("""
        SELECT * FROM grants
        WHERE recipient_ein = ? OR recipient_ein = ?
        ORDER BY tax_year DESC, cash_amount DESC
    """, (ein, args.ein)).fetchall()
    results = [dict(r) for r in rows]
    total = sum(r.get("cash_amount", 0) or 0 for r in results)

    recip_name = results[0]["recipient_name"] if results else ein
    unique_funders = len(set(r["filer_ein"] for r in results if r.get("filer_ein")))

    print(f"\nGrants to {recip_name} (EIN {ein}): {len(results)} grants from {unique_funders} funders, {_fmt_amount(total)} total")
    for g in results[:50]:
        print(f"  {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  "
              f"← {g.get('filer_name', '?')[:40]:40s} ({g.get('filer_ein', '?')})")

    write_output(results, args, summary=f"990 grants to EIN {ein}")
    db.close()


# ── network ─────────────────────────────────────────────────────

def cmd_network(args):
    """BFS grant network from a seed EIN.

    Depth 1: seed's recipients.
    Depth 2: who else funds seed's recipients (co-grantors).
    Depth 3+: repeat.
    """
    db = get_db()
    seed_ein = args.ein.replace("-", "")
    max_depth = args.depth
    limit = args.limit

    visited_eins = set()
    edges = []  # (filer_ein, filer_name, recipient_ein, recipient_name, total_amount, grant_count)
    frontier = {seed_ein}

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        new_frontier = set()

        for ein in frontier:
            if ein in visited_eins:
                continue
            visited_eins.add(ein)

            if depth % 2 == 1:
                # Odd depth: find recipients of this filer
                rows = db.execute("""
                    SELECT recipient_ein, recipient_name,
                           SUM(cash_amount) as total, COUNT(*) as cnt
                    FROM grants
                    WHERE (filer_ein = ?) AND recipient_ein != ''
                    GROUP BY recipient_ein
                    ORDER BY total DESC
                    LIMIT ?
                """, (ein, limit)).fetchall()
                for r in rows:
                    edges.append({
                        "filer_ein": ein,
                        "recipient_ein": r["recipient_ein"],
                        "recipient_name": r["recipient_name"],
                        "total_amount": r["total"],
                        "grant_count": r["cnt"],
                        "depth": depth,
                        "direction": "outgoing",
                    })
                    if r["recipient_ein"] not in visited_eins:
                        new_frontier.add(r["recipient_ein"])
            else:
                # Even depth: find funders of this recipient
                rows = db.execute("""
                    SELECT filer_ein, filer_name,
                           SUM(cash_amount) as total, COUNT(*) as cnt
                    FROM grants
                    WHERE (recipient_ein = ?) AND filer_ein != ''
                    GROUP BY filer_ein
                    ORDER BY total DESC
                    LIMIT ?
                """, (ein, limit)).fetchall()
                for r in rows:
                    edges.append({
                        "filer_ein": r["filer_ein"],
                        "filer_name": r["filer_name"],
                        "recipient_ein": ein,
                        "total_amount": r["total"],
                        "grant_count": r["cnt"],
                        "depth": depth,
                        "direction": "incoming",
                    })
                    if r["filer_ein"] not in visited_eins:
                        new_frontier.add(r["filer_ein"])

        frontier = new_frontier
        print(f"  Depth {depth}: {len(edges)} edges, {len(frontier)} new EINs to explore")

    print(f"\nNetwork from EIN {seed_ein} (depth {max_depth}): {len(edges)} edges, {len(visited_eins)} nodes")
    for e in edges[:40]:
        direction = "→" if e["direction"] == "outgoing" else "←"
        name = e.get("recipient_name") or e.get("filer_name") or "?"
        other_ein = e["recipient_ein"] if e["direction"] == "outgoing" else e["filer_ein"]
        print(f"  d{e['depth']} {direction} {_fmt_amount(e['total_amount']):>14} ({e['grant_count']}x)  "
              f"{other_ein} {name[:40]}")

    result = {"seed_ein": seed_ein, "depth": max_depth, "edges": edges, "nodes_visited": len(visited_eins)}
    write_output(result, args, summary=f"990 network from {seed_ein}")
    db.close()


# ── co-grantors ─────────────────────────────────────────────────

def cmd_co_grantors(args):
    """Find foundations that fund the same recipient."""
    db = get_db()
    name = args.name
    limit = args.limit

    # First find the recipient's EIN (if available)
    fts = _has_fts()
    if fts:
        recipients = db.execute("""
            SELECT DISTINCT g.recipient_ein, g.recipient_name FROM grants g
            JOIN grants_fts ON grants_fts.rowid = g.id
            WHERE grants_fts.recipient_name MATCH ?
        """, (name,)).fetchall()
    else:
        recipients = db.execute("""
            SELECT DISTINCT recipient_ein, recipient_name FROM grants
            WHERE recipient_name LIKE ?
        """, (f"%{name}%",)).fetchall()

    if not recipients:
        print(f"No recipients found matching '{name}'")
        db.close()
        return

    # Collect EINs and names
    target_eins = list(set(r["recipient_ein"] for r in recipients if r["recipient_ein"]))
    target_names = list(set(r["recipient_name"] for r in recipients if r["recipient_name"]))
    display_name = recipients[0]["recipient_name"]

    print(f"\nCo-grantors of '{display_name}'"
          + (f" (EIN(s): {', '.join(target_eins[:3])})" if target_eins else "")
          + f" ({len(target_names)} name variant(s)):")

    # Find all funders — by EIN if available, otherwise by name
    if target_eins:
        placeholders = ",".join("?" for _ in target_eins)
        rows = db.execute(f"""
            SELECT filer_ein, filer_name,
                   SUM(cash_amount) as total, COUNT(*) as cnt,
                   MIN(tax_year) as first_year, MAX(tax_year) as last_year
            FROM grants
            WHERE recipient_ein IN ({placeholders}) AND filer_ein != ''
            GROUP BY filer_ein
            ORDER BY total DESC
            LIMIT ?
        """, target_eins + [limit]).fetchall()
    else:
        # Fall back to name matching (common for 990-PF which lacks recipient EINs)
        name_conditions = " OR ".join("recipient_name = ?" for _ in target_names)
        rows = db.execute(f"""
            SELECT filer_ein, filer_name,
                   SUM(cash_amount) as total, COUNT(*) as cnt,
                   MIN(tax_year) as first_year, MAX(tax_year) as last_year
            FROM grants
            WHERE ({name_conditions}) AND filer_ein != ''
            GROUP BY filer_ein
            ORDER BY total DESC
            LIMIT ?
        """, target_names + [limit]).fetchall()
    results = [dict(r) for r in rows]

    for r in results:
        print(f"  {_fmt_amount(r['total']):>14} ({r['cnt']}x, {r.get('first_year','?')}-{r.get('last_year','?')})  "
              f"{r.get('filer_name', '?')[:45]:45s} ({r['filer_ein']})")

    write_output(results, args, summary=f"990 co-grantors of '{name}'")
    db.close()


# ── cross-ref ───────────────────────────────────────────────────

def cmd_cross_ref(args):
    """Match investigation.db entities against the bulk grant database."""
    if not INVESTIGATION_DB.exists():
        print("Error: investigation.db not found", file=sys.stderr)
        sys.exit(1)

    inv_db = sqlite3.connect(str(INVESTIGATION_DB))
    inv_db.row_factory = sqlite3.Row

    # Get all entity names and EINs from investigation
    entities = inv_db.execute("""
        SELECT id, name, ein FROM entities WHERE name IS NOT NULL
    """).fetchall()
    inv_db.close()

    if not entities:
        print("No entities in investigation.db")
        return

    db = get_db()
    matches = []

    print(f"Cross-referencing {len(entities)} investigation entities against bulk grants...")

    for ent in entities:
        name = ent["name"]
        ein = ent["ein"]
        entity_id = ent["id"]

        # Search by EIN first (exact)
        if ein:
            clean_ein = ein.replace("-", "")
            as_filer = db.execute("""
                SELECT filer_ein, COUNT(*) as cnt, SUM(cash_amount) as total
                FROM grants WHERE filer_ein = ?
                GROUP BY filer_ein
            """, (clean_ein,)).fetchone()
            if as_filer and as_filer["cnt"]:
                matches.append({
                    "entity_id": entity_id,
                    "entity_name": name,
                    "ein": clean_ein,
                    "role": "filer",
                    "grant_count": as_filer["cnt"],
                    "total_amount": as_filer["total"],
                })

            as_recipient = db.execute("""
                SELECT recipient_ein, COUNT(*) as cnt, SUM(cash_amount) as total
                FROM grants WHERE recipient_ein = ?
                GROUP BY recipient_ein
            """, (clean_ein,)).fetchone()
            if as_recipient and as_recipient["cnt"]:
                matches.append({
                    "entity_id": entity_id,
                    "entity_name": name,
                    "ein": clean_ein,
                    "role": "recipient",
                    "grant_count": as_recipient["cnt"],
                    "total_amount": as_recipient["total"],
                })

        # Name search (LIKE — slower but catches non-EIN matches)
        if name and len(name) > 3:
            as_filer_name = db.execute("""
                SELECT filer_name, filer_ein, COUNT(*) as cnt, SUM(cash_amount) as total
                FROM grants WHERE filer_name LIKE ?
                GROUP BY filer_ein
                LIMIT 5
            """, (f"%{name}%",)).fetchall()
            for r in as_filer_name:
                if r["cnt"]:
                    matches.append({
                        "entity_id": entity_id,
                        "entity_name": name,
                        "matched_name": r["filer_name"],
                        "ein": r["filer_ein"],
                        "role": "filer (name match)",
                        "grant_count": r["cnt"],
                        "total_amount": r["total"],
                    })

    # Deduplicate
    seen = set()
    unique = []
    for m in matches:
        key = (m.get("ein", ""), m["role"], m["entity_id"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    unique.sort(key=lambda x: x.get("total_amount", 0) or 0, reverse=True)

    print(f"\n{len(unique)} matches found:")
    for m in unique[:40]:
        print(f"  {m['entity_name'][:30]:30s} {m['role']:20s} "
              f"{_fmt_amount(m.get('total_amount')):>14} ({m.get('grant_count', 0)} grants)  "
              f"EIN={m.get('ein', '?')}")

    write_output(unique, args, summary="990 bulk cross-ref")
    db.close()


# ── top ─────────────────────────────────────────────────────────

def cmd_top(args):
    """Top grantmakers or recipients by amount or count."""
    db = get_db()
    by = args.by
    limit = args.limit

    if by == "amount":
        # Top grantmakers by total amount
        rows = db.execute("""
            SELECT filer_ein, filer_name,
                   SUM(cash_amount) as total, COUNT(*) as cnt,
                   COUNT(DISTINCT recipient_ein) as unique_recipients,
                   MIN(tax_year) as first_year, MAX(tax_year) as last_year
            FROM grants
            WHERE filer_ein != ''
            GROUP BY filer_ein
            ORDER BY total DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nTop {limit} grantmakers by total amount:")
        for r in results:
            print(f"  {_fmt_amount(r['total']):>16}  {r['cnt']:>6} grants  "
                  f"{r.get('unique_recipients', '?'):>5} recipients  "
                  f"{r.get('first_year', '?')}-{r.get('last_year', '?')}  "
                  f"{r.get('filer_name', '?')[:40]}  ({r['filer_ein']})")

    elif by == "count":
        # Top by grant count
        rows = db.execute("""
            SELECT filer_ein, filer_name,
                   SUM(cash_amount) as total, COUNT(*) as cnt
            FROM grants
            WHERE filer_ein != ''
            GROUP BY filer_ein
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nTop {limit} grantmakers by grant count:")
        for r in results:
            print(f"  {r['cnt']:>6} grants  {_fmt_amount(r['total']):>16}  "
                  f"{r.get('filer_name', '?')[:40]}  ({r['filer_ein']})")

    elif by == "recipients":
        # Top recipients by total received
        rows = db.execute("""
            SELECT recipient_ein, recipient_name,
                   SUM(cash_amount) as total, COUNT(*) as cnt,
                   COUNT(DISTINCT filer_ein) as unique_funders
            FROM grants
            WHERE recipient_name != ''
            GROUP BY COALESCE(NULLIF(recipient_ein, ''), recipient_name)
            ORDER BY total DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nTop {limit} recipients by total received:")
        for r in results:
            print(f"  {_fmt_amount(r['total']):>16}  {r['cnt']:>6} grants from {r.get('unique_funders', '?'):>4} funders  "
                  f"{r.get('recipient_name', '?')[:40]}  ({r.get('recipient_ein', '')})")

    elif by == "single":
        # Largest single grants
        rows = db.execute("""
            SELECT * FROM grants
            ORDER BY cash_amount DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nTop {limit} largest single grants:")
        for r in results:
            print(f"  {r.get('tax_year', '?'):>5}  {_fmt_amount(r.get('cash_amount')):>16}  "
                  f"{r.get('filer_name', '?')[:25]:25s} → {r.get('recipient_name', '?')[:35]}")

    else:
        print(f"Unknown --by option: {by}. Use: amount, count, recipients, single")
        db.close()
        return

    write_output(results, args, summary=f"990 bulk top {by}")
    db.close()


# ── main ────────────────────────────────────────────────────────

# ── lookup (comprehensive EIN view) ────────────────────────────

def cmd_lookup(args):
    """Comprehensive view of a nonprofit: metadata + financials + officers + grants."""
    db = get_db()
    ein = args.ein.replace("-", "")

    result = {"ein": ein}

    # ProPublica org metadata (NTEE, subsection, ruling date, address)
    pp_data = None
    if _pp_get_org:
        print("Fetching org metadata from ProPublica...")
        pp_data = _pp_get_org(ein)
        if pp_data:
            org = pp_data.get("organization", {})
            result["metadata"] = {
                "name": org.get("name"),
                "address": org.get("address"),
                "city": org.get("city"),
                "state": org.get("state"),
                "zipcode": org.get("zipcode"),
                "ntee_code": org.get("ntee_code"),
                "subsection_code": org.get("subsection_code"),
                "ruling_date": org.get("ruling_date"),
                "foundation_code": org.get("foundation_code"),
            }

    # Latest financials from bulk DB
    fin = db.execute("""
        SELECT * FROM financials WHERE ein = ? ORDER BY tax_year DESC LIMIT 1
    """, (ein,)).fetchone()
    if fin:
        f = dict(fin)
        org_name = f.get("filer_name", result.get("metadata", {}).get("name", ein))
        result["org_name"] = org_name
        print(f"\n{'='*60}")
        print(f"{org_name} (EIN {ein})")
        print(f"{'='*60}")

        if result.get("metadata"):
            m = result["metadata"]
            if m.get("ntee_code"):
                print(f"  NTEE: {m['ntee_code']}  |  Type: 501(c)({m.get('subsection_code', '?')})  |  Ruling: {m.get('ruling_date', '?')}")

        print(f"\n  FINANCIALS ({f.get('tax_year', '?')}):")
        prog_pct = f"{f['program_expense_ratio']:.0%}" if f.get("program_expense_ratio") is not None else "N/A"
        print(f"    Revenue:  {_fmt_amount(f.get('total_revenue')):>14}  |  Expenses:  {_fmt_amount(f.get('total_expenses')):>14}")
        print(f"    Program:  {_fmt_amount(f.get('program_expenses')):>14}  ({prog_pct})")
        print(f"    Assets:   {_fmt_amount(f.get('total_assets_eoy')):>14}")
        result["financials"] = f
    else:
        org_name = result.get("metadata", {}).get("name", ein)
        result["org_name"] = org_name
        print(f"\n{org_name} (EIN {ein})")
        print("  No financials in bulk DB")

    # Officers
    officers = db.execute("""
        SELECT person_name, title, total_comp, is_director, is_officer
        FROM officers WHERE ein = ?
        ORDER BY tax_year DESC, total_comp DESC LIMIT 15
    """, (ein,)).fetchall()
    if officers:
        print(f"\n  OFFICERS ({len(officers)}):")
        for o in [dict(r) for r in officers[:10]]:
            print(f"    {o['person_name']:35s} {o['title'][:25]:25s} {_fmt_amount(o['total_comp']):>12s}")
        result["officers"] = [dict(r) for r in officers]

    # Top grants made
    grants_made = db.execute("""
        SELECT recipient_name, cash_amount, tax_year FROM grants
        WHERE filer_ein = ? ORDER BY cash_amount DESC LIMIT 10
    """, (ein,)).fetchall()
    if grants_made:
        total = db.execute("SELECT SUM(cash_amount) FROM grants WHERE filer_ein = ?", (ein,)).fetchone()[0] or 0
        count = db.execute("SELECT COUNT(*) FROM grants WHERE filer_ein = ?", (ein,)).fetchone()[0]
        print(f"\n  GRANTS MADE ({count} total, {_fmt_amount(total)}):")
        for g in [dict(r) for r in grants_made[:5]]:
            print(f"    {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  → {g['recipient_name'][:45]}")
        result["grants_made_total"] = total
        result["grants_made_count"] = count

    # Grants received
    grants_rcvd = db.execute("""
        SELECT filer_name, cash_amount, tax_year FROM grants
        WHERE recipient_ein = ? ORDER BY cash_amount DESC LIMIT 10
    """, (ein,)).fetchall()
    if grants_rcvd:
        total_r = db.execute("SELECT SUM(cash_amount) FROM grants WHERE recipient_ein = ?", (ein,)).fetchone()[0] or 0
        count_r = db.execute("SELECT COUNT(*) FROM grants WHERE recipient_ein = ?", (ein,)).fetchone()[0]
        print(f"\n  GRANTS RECEIVED ({count_r} total, {_fmt_amount(total_r)}):")
        for g in [dict(r) for r in grants_rcvd[:5]]:
            print(f"    {g.get('tax_year', '?'):>5}  {_fmt_amount(g.get('cash_amount')):>14}  ← {g['filer_name'][:45]}")
        result["grants_received_total"] = total_r
        result["grants_received_count"] = count_r

    # Checklist flags
    flags = db.execute("SELECT * FROM checklist_flags WHERE ein = ? ORDER BY tax_year DESC LIMIT 1", (ein,)).fetchone()
    if flags:
        f = dict(flags)
        alerts = []
        if f.get("excess_benefit_transaction"): alerts.append("Excess benefit transaction reported")
        if not f.get("conflict_of_interest_policy"): alerts.append("No conflict of interest policy")
        if not f.get("whistleblower_policy"): alerts.append("No whistleblower policy")
        if alerts:
            print(f"\n  ALERTS:")
            for a in alerts:
                print(f"    - {a}")

    write_output(result, args, summary=f"990 lookup EIN {ein}")
    db.close()


# ── filings (from ProPublica) ─────────────────────────────────

def cmd_filings(args):
    """List filings for an EIN, with PDF links (via ProPublica API)."""
    ein = args.ein.replace("-", "")

    if not _pp_get_org:
        print("ProPublica module not available — cannot fetch filing list", file=sys.stderr)
        return

    data = _pp_get_org(ein)
    if not data:
        print(f"No data found for EIN {ein}")
        return

    filings_data = data.get("filings_with_data", [])
    filings_no_data = data.get("filings_without_data", [])

    all_filings = []
    if filings_data:
        print(f"\n  Filings with data ({len(filings_data)}):")
        for f in filings_data:
            yr = f.get("tax_prd_yr", "?")
            form = f.get("formtype", "?")
            rev = f.get("totrevenue", 0)
            exp = f.get("totfuncexpns", 0)
            pdf = f.get("pdf_url", "")
            print(f"    {yr} ({form:6s})  Rev: {_fmt_amount(rev):>14}  Exp: {_fmt_amount(exp):>14}")
            if pdf:
                print(f"      PDF: {pdf}")
            all_filings.append(dict(f))

    if filings_no_data:
        print(f"\n  Additional filings ({len(filings_no_data)}):")
        for f in filings_no_data[:10]:
            yr = f.get("tax_prd_yr", "?")
            form = f.get("formtype", "?")
            pdf = f.get("pdf_url", "")
            line = f"    {yr} ({form})"
            if pdf:
                line += f"  PDF: {pdf}"
            print(line)
            all_filings.append(dict(f))

    write_output(all_filings, args, summary=f"990 filings for EIN {ein}")


# ── officers ────────────────────────────────────────────────────

def cmd_officers(args):
    """List officers/directors for a nonprofit by EIN."""
    db = get_db()
    ein = args.ein.replace("-", "")

    rows = db.execute("""
        SELECT o.*, f.filer_name FROM officers o
        JOIN filings f ON o.object_id = f.object_id
        WHERE o.ein = ?
        ORDER BY o.tax_year DESC, o.total_comp DESC
    """, (ein,)).fetchall()
    results = [dict(r) for r in rows]

    if not results:
        print(f"No officers found for EIN {ein}")
        db.close()
        return

    org_name = results[0].get("filer_name", ein)
    years = sorted(set(r["tax_year"] for r in results if r["tax_year"]))
    print(f"\nOfficers of {org_name} (EIN {ein}) — {len(results)} records across {len(years)} years")

    # Show most recent year
    latest_year = max(years) if years else None
    if latest_year:
        latest = [r for r in results if r["tax_year"] == latest_year]
        print(f"\n  {latest_year} ({len(latest)} officers):")
        for o in latest:
            roles = []
            if o["is_director"]: roles.append("DIR")
            if o["is_officer"]: roles.append("OFF")
            if o["is_key_employee"]: roles.append("KEY")
            if o["is_highest_comp"]: roles.append("HCE")
            if o["is_former"]: roles.append("FMR")
            role_str = ",".join(roles) if roles else ""
            print(f"    {o['person_name']:35s} {o['title']:25s} "
                  f"comp: {_fmt_amount(o['total_comp']):>12s}  [{role_str}]")

    write_output(results, args, summary=f"990 officers for EIN {ein}")
    db.close()


# ── officer-search ─────────────────────────────────────────────

def cmd_officer_search(args):
    """Find a person across all nonprofits."""
    db = get_db()
    name = args.name
    limit = args.limit

    pattern = f"%{name}%"
    rows = db.execute("""
        SELECT o.ein, o.person_name, o.title, o.total_comp, o.tax_year,
               o.is_director, o.is_officer, o.is_key_employee,
               f.filer_name
        FROM officers o
        JOIN filings f ON o.object_id = f.object_id
        WHERE o.person_name LIKE ?
        ORDER BY o.total_comp DESC, o.tax_year DESC
        LIMIT ?
    """, (pattern, limit)).fetchall()
    results = [dict(r) for r in rows]

    print(f"\nOfficer search '{name}': {len(results)} results")
    # Group by EIN for cleaner display
    by_ein = defaultdict(list)
    for r in results:
        by_ein[r["ein"]].append(r)

    for ein, officers in list(by_ein.items())[:30]:
        org = officers[0]["filer_name"]
        latest = max(officers, key=lambda o: o["tax_year"] or 0)
        years = sorted(set(o["tax_year"] for o in officers if o["tax_year"]))
        max_comp = max(o["total_comp"] for o in officers)
        print(f"  {org[:45]:45s} EIN {ein}  comp: {_fmt_amount(max_comp):>12s}  "
              f"title: {latest['title'][:25]}  years: {years[0] if years else '?'}-{years[-1] if years else '?'}")

    write_output(results, args, summary=f"990 officer search '{name}'")
    db.close()


# ── financials ─────────────────────────────────────────────────

def cmd_financials(args):
    """Financial summary over time for a nonprofit."""
    db = get_db()
    ein = args.ein.replace("-", "")

    rows = db.execute("""
        SELECT * FROM financials
        WHERE ein = ?
        ORDER BY tax_year DESC
    """, (ein,)).fetchall()
    results = [dict(r) for r in rows]

    if not results:
        print(f"No financials found for EIN {ein}")
        db.close()
        return

    org_name = results[0].get("filer_name", ein)
    print(f"\nFinancials for {org_name} (EIN {ein})")
    print(f"  {'Year':>5}  {'Revenue':>14}  {'Expenses':>14}  {'Program':>14}  "
          f"{'Prog%':>6}  {'Fund%':>6}  {'Assets':>14}")
    print(f"  {'─'*5}  {'─'*14}  {'─'*14}  {'─'*14}  {'─'*6}  {'─'*6}  {'─'*14}")

    for f in results:
        prog_pct = f"{f['program_expense_ratio']:.0%}" if f.get("program_expense_ratio") is not None else "N/A"
        fund_pct = f"{f['fundraising_ratio']:.0%}" if f.get("fundraising_ratio") is not None else "N/A"
        print(f"  {f.get('tax_year', '?'):>5}  {_fmt_amount(f.get('total_revenue')):>14}  "
              f"{_fmt_amount(f.get('total_expenses')):>14}  "
              f"{_fmt_amount(f.get('program_expenses')):>14}  "
              f"{prog_pct:>6}  {fund_pct:>6}  "
              f"{_fmt_amount(f.get('total_assets_eoy')):>14}")

    write_output(results, args, summary=f"990 financials for EIN {ein}")
    db.close()


# ── red-flags ──────────────────────────────────────────────────

def cmd_red_flags(args):
    """Red-flag analysis for a nonprofit — ratio screening + checklist."""
    db = get_db()
    ein = args.ein.replace("-", "")

    # Financials
    fin = db.execute("""
        SELECT * FROM financials WHERE ein = ? ORDER BY tax_year DESC LIMIT 1
    """, (ein,)).fetchone()

    # Officers + compensation
    officers = db.execute("""
        SELECT person_name, title, total_comp FROM officers
        WHERE ein = ? ORDER BY tax_year DESC, total_comp DESC LIMIT 20
    """, (ein,)).fetchall()

    # Checklist flags
    flags = db.execute("""
        SELECT * FROM checklist_flags WHERE ein = ? ORDER BY tax_year DESC LIMIT 1
    """, (ein,)).fetchone()

    # Insider transactions
    insiders = db.execute("""
        SELECT * FROM insider_transactions WHERE ein = ? ORDER BY amount DESC
    """, (ein,)).fetchall()

    # Schedule J (high comp detail)
    comp_detail = db.execute("""
        SELECT * FROM compensation_detail WHERE ein = ? ORDER BY total_comp_from_org DESC LIMIT 10
    """, (ein,)).fetchall()

    org_name = fin["filer_name"] if fin else ein
    print(f"\nRed-flag analysis: {org_name} (EIN {ein})")

    alerts = []

    if fin:
        print(f"\n  FINANCIAL RATIOS ({fin['tax_year']}):")
        f = dict(fin)

        # Program expense ratio
        if f.get("program_expense_ratio") is not None:
            pct = f["program_expense_ratio"]
            flag = " ⚠ LOW" if pct < 0.33 else ""
            print(f"    Program expense ratio:  {pct:.1%}{flag}")
            if pct < 0.33:
                alerts.append(f"Low program expense ratio: {pct:.1%} (threshold: 33%)")

        # Fundraising ratio
        if f.get("fundraising_ratio") is not None:
            pct = f["fundraising_ratio"]
            flag = " ⚠ HIGH" if pct > 0.65 else ""
            print(f"    Fundraising ratio:      {pct:.1%}{flag}")
            if pct > 0.65:
                alerts.append(f"High fundraising ratio: {pct:.1%} (threshold: 65%)")

        # Admin ratio
        if f.get("admin_expense_ratio") is not None:
            pct = f["admin_expense_ratio"]
            flag = " ⚠ HIGH" if pct > 0.50 else ""
            print(f"    Admin expense ratio:    {pct:.1%}{flag}")
            if pct > 0.50:
                alerts.append(f"High admin ratio: {pct:.1%}")

        # Revenue vs expenses
        if f.get("total_revenue") and f.get("total_expenses"):
            if f["total_expenses"] > 0 and f["total_revenue"] / f["total_expenses"] > 3:
                alerts.append(f"Revenue {_fmt_amount(f['total_revenue'])} is 3x+ expenses {_fmt_amount(f['total_expenses'])} — hoarding?")

        print(f"    Revenue:                {_fmt_amount(f.get('total_revenue'))}")
        print(f"    Expenses:               {_fmt_amount(f.get('total_expenses'))}")
        print(f"    Assets EOY:             {_fmt_amount(f.get('total_assets_eoy'))}")

    if officers:
        print(f"\n  TOP COMPENSATED OFFICERS:")
        total_exp = fin["total_expenses"] if fin and fin["total_expenses"] else 0
        for o in [dict(r) for r in officers[:10]]:
            comp = o["total_comp"]
            pct_of_exp = f" ({comp/total_exp:.1%} of expenses)" if total_exp and comp else ""
            print(f"    {o['person_name']:35s} {_fmt_amount(comp):>12s}{pct_of_exp}")
            if total_exp and comp > total_exp * 0.25:
                alerts.append(f"Officer {o['person_name']} comp ({_fmt_amount(comp)}) > 25% of total expenses")

    if flags:
        print(f"\n  CHECKLIST FLAGS ({dict(flags).get('tax_year', '?')}):")
        f = dict(flags)
        items = [
            ("excess_benefit_transaction", "Excess benefit transaction"),
            ("schedule_j_required", "Schedule J required (high comp)"),
            ("whistleblower_policy", "Whistleblower policy"),
            ("document_retention_policy", "Document retention policy"),
            ("compensation_process_ceo", "CEO compensation process"),
            ("conflict_of_interest_policy", "Conflict of interest policy"),
        ]
        for key, label in items:
            val = f.get(key, 0)
            indicator = "YES" if val else "NO"
            flag = ""
            if key == "excess_benefit_transaction" and val:
                flag = " ⚠"
                alerts.append("Reported excess benefit transaction")
            if key in ("whistleblower_policy", "conflict_of_interest_policy") and not val:
                flag = " ⚠ MISSING"
                alerts.append(f"Missing {label.lower()}")
            print(f"    {label:40s} {indicator}{flag}")

    if insiders:
        print(f"\n  INSIDER TRANSACTIONS ({len(insiders)}):")
        for t in [dict(r) for r in insiders[:10]]:
            print(f"    [{t['transaction_type']:20s}] {t.get('person_name', '?'):30s} "
                  f"{_fmt_amount(t.get('amount')):>12s}  {(t.get('description') or '')[:40]}")
            alerts.append(f"Insider transaction: {t['transaction_type']} — {t.get('person_name', '?')} ({_fmt_amount(t.get('amount'))})")

    if alerts:
        print(f"\n  ⚠ ALERTS ({len(alerts)}):")
        for a in alerts:
            print(f"    • {a}")
    else:
        print(f"\n  No red flags detected.")

    result = {"ein": ein, "org_name": org_name, "alerts": alerts}
    write_output(result, args, summary=f"990 red-flags for EIN {ein}")
    db.close()


# ── top-compensated ────────────────────────────────────────────

def cmd_top_compensated(args):
    """Highest-compensated nonprofit officers."""
    db = get_db()
    min_comp = args.min_comp
    limit = args.limit

    rows = db.execute("""
        SELECT o.ein, o.person_name, o.title, o.total_comp, o.tax_year,
               o.comp_from_org, o.comp_from_related, o.other_comp,
               f.filer_name
        FROM officers o
        JOIN filings f ON o.object_id = f.object_id
        WHERE o.total_comp >= ?
        ORDER BY o.total_comp DESC
        LIMIT ?
    """, (min_comp, limit)).fetchall()
    results = [dict(r) for r in rows]

    print(f"\nTop compensated officers (min ${min_comp:,}): {len(results)} results")
    print(f"  {'Name':35s} {'Title':25s} {'Total Comp':>12s}  {'Year':>5}  Organization")
    print(f"  {'─'*35} {'─'*25} {'─'*12}  {'─'*5}  {'─'*40}")
    for r in results:
        print(f"  {r['person_name']:35s} {r['title'][:25]:25s} "
              f"{_fmt_amount(r['total_comp']):>12s}  {r.get('tax_year', '?'):>5}  "
              f"{r['filer_name'][:40]}")

    write_output(results, args, summary=f"990 top compensated (min ${min_comp:,})")
    db.close()


# ── flow (directed grant network with circular detection) ──────

def cmd_flow(args):
    """Directed grant flow graph with node aggregation and circular flow detection."""
    db = get_db()
    seed_ein = args.ein.replace("-", "")
    max_depth = args.depth
    limit = args.limit
    min_amount = args.min_amount

    # Get seed name
    seed_row = db.execute("SELECT filer_name FROM grants WHERE filer_ein = ? LIMIT 1", (seed_ein,)).fetchone()
    seed_name = seed_row["filer_name"] if seed_row else seed_ein

    visited = set()
    raw_edges = []  # (from_ein, from_name, to_ein, to_name, total, count, years)
    frontier = {seed_ein}

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        new_frontier = set()

        for ein in frontier:
            if ein in visited:
                continue
            visited.add(ein)

            if depth % 2 == 1:
                # Odd depth: outgoing grants (filer → recipients)
                rows = db.execute("""
                    SELECT recipient_ein, recipient_name,
                           SUM(cash_amount) as total, COUNT(*) as cnt,
                           GROUP_CONCAT(DISTINCT tax_year) as years
                    FROM grants
                    WHERE filer_ein = ? AND recipient_ein != '' AND cash_amount >= ?
                    GROUP BY recipient_ein
                    ORDER BY total DESC LIMIT ?
                """, (ein, min_amount, limit)).fetchall()

                filer_name_row = db.execute("SELECT filer_name FROM grants WHERE filer_ein = ? LIMIT 1", (ein,)).fetchone()
                filer_name = filer_name_row["filer_name"] if filer_name_row else ein

                for r in rows:
                    raw_edges.append({
                        "from_ein": ein, "from_name": filer_name,
                        "to_ein": r["recipient_ein"], "to_name": r["recipient_name"],
                        "amount": r["total"], "grant_count": r["cnt"],
                        "years": sorted(set(int(y) for y in (r["years"] or "").split(",") if y)),
                    })
                    if r["recipient_ein"] not in visited:
                        new_frontier.add(r["recipient_ein"])
            else:
                # Even depth: incoming grants (funders → this recipient)
                rows = db.execute("""
                    SELECT filer_ein, filer_name,
                           SUM(cash_amount) as total, COUNT(*) as cnt,
                           GROUP_CONCAT(DISTINCT tax_year) as years
                    FROM grants
                    WHERE recipient_ein = ? AND filer_ein != '' AND cash_amount >= ?
                    GROUP BY filer_ein
                    ORDER BY total DESC LIMIT ?
                """, (ein, min_amount, limit)).fetchall()

                recip_name_row = db.execute("SELECT recipient_name FROM grants WHERE recipient_ein = ? LIMIT 1", (ein,)).fetchone()
                recip_name = recip_name_row["recipient_name"] if recip_name_row else ein

                for r in rows:
                    raw_edges.append({
                        "from_ein": r["filer_ein"], "from_name": r["filer_name"],
                        "to_ein": ein, "to_name": recip_name,
                        "amount": r["total"], "grant_count": r["cnt"],
                        "years": sorted(set(int(y) for y in (r["years"] or "").split(",") if y)),
                    })
                    if r["filer_ein"] not in visited:
                        new_frontier.add(r["filer_ein"])

        frontier = new_frontier

    # Check for reverse flows (circular detection): for each A→B edge, check if B→A exists
    existing_pairs = {(e["from_ein"], e["to_ein"]) for e in raw_edges}
    reverse_check_pairs = [(e["to_ein"], e["from_ein"]) for e in raw_edges if (e["to_ein"], e["from_ein"]) not in existing_pairs]

    for from_ein, to_ein in set(reverse_check_pairs):
        row = db.execute("""
            SELECT SUM(cash_amount) as total, COUNT(*) as cnt,
                   GROUP_CONCAT(DISTINCT tax_year) as years
            FROM grants
            WHERE filer_ein = ? AND recipient_ein = ? AND cash_amount >= ?
        """, (from_ein, to_ein, min_amount)).fetchone()
        if row and row["total"] and row["total"] > 0:
            from_name_row = db.execute("SELECT filer_name FROM grants WHERE filer_ein = ? LIMIT 1", (from_ein,)).fetchone()
            to_name_row = db.execute("SELECT filer_name FROM grants WHERE filer_ein = ? LIMIT 1", (to_ein,)).fetchone()
            raw_edges.append({
                "from_ein": from_ein,
                "from_name": from_name_row["filer_name"] if from_name_row else from_ein,
                "to_ein": to_ein,
                "to_name": to_name_row["filer_name"] if to_name_row else to_ein,
                "amount": row["total"], "grant_count": row["cnt"],
                "years": sorted(set(int(y) for y in (row["years"] or "").split(",") if y)),
            })

    db.close()

    # Build node aggregations
    node_map = {}
    for e in raw_edges:
        for role, ein, name in [("filer", e["from_ein"], e["from_name"]), ("recipient", e["to_ein"], e["to_name"])]:
            if ein not in node_map:
                node_map[ein] = {"ein": ein, "name": name, "total_granted": 0, "total_received": 0}
        node_map[e["from_ein"]]["total_granted"] += e["amount"]
        node_map[e["to_ein"]]["total_received"] += e["amount"]

    for n in node_map.values():
        if n["total_granted"] > 0 and n["total_received"] > 0:
            n["type"] = "both"
        elif n["total_granted"] > 0:
            n["type"] = "filer"
        else:
            n["type"] = "recipient"

    # Detect circular flows (A→B and B→A both exist)
    edge_pairs = {}
    for e in raw_edges:
        key = tuple(sorted([e["from_ein"], e["to_ein"]]))
        if key not in edge_pairs:
            edge_pairs[key] = {}
        direction = f"{e['from_ein']}->{e['to_ein']}"
        edge_pairs[key][direction] = e["amount"]

    circular_flows = []
    for (ein_a, ein_b), flows in edge_pairs.items():
        if len(flows) == 2:
            a_to_b = flows.get(f"{ein_a}->{ein_b}", 0)
            b_to_a = flows.get(f"{ein_b}->{ein_a}", 0)
            net = abs(a_to_b - b_to_a)
            name_a = node_map.get(ein_a, {}).get("name", ein_a)
            name_b = node_map.get(ein_b, {}).get("name", ein_b)
            if a_to_b > b_to_a:
                direction = f"{name_a} → {name_b}"
            else:
                direction = f"{name_b} → {name_a}"
            circular_flows.append({
                "entities": [ein_a, ein_b],
                "names": [name_a, name_b],
                "flow_a_to_b": a_to_b,
                "flow_b_to_a": b_to_a,
                "net_flow": net,
                "direction": direction,
            })

    # Deduplicate edges (same pair may appear at different depths)
    seen = set()
    edges = []
    for e in raw_edges:
        key = (e["from_ein"], e["to_ein"])
        if key not in seen:
            seen.add(key)
            edges.append(e)

    result = {
        "root_ein": seed_ein,
        "root_name": seed_name,
        "depth": max_depth,
        "min_amount": min_amount,
        "nodes": list(node_map.values()),
        "edges": edges,
        "circular_flows": circular_flows,
        "stats": {
            "node_count": len(node_map),
            "edge_count": len(edges),
            "circular_count": len(circular_flows),
            "total_flow": sum(e["amount"] for e in edges),
        },
    }

    if write_output(result, args, summary=f"grant flow from {seed_name} ({len(edges)} edges, {len(circular_flows)} circular)"):
        if circular_flows:
            print(f"\n{len(circular_flows)} circular flows detected:")
            for c in circular_flows:
                print(f"  {c['names'][0][:30]} <-> {c['names'][1][:30]}: net {_fmt_amount(c['net_flow'])} ({c['direction']})")
        return

    print(f"─── Grant Flow: {seed_name} (depth {max_depth}, min {_fmt_amount(min_amount)}) ───")
    print(f"    {len(node_map)} nodes, {len(edges)} edges, {_fmt_amount(sum(e['amount'] for e in edges))} total flow")
    print()

    for e in edges[:30]:
        print(f"  {e['from_name'][:25]:25s} → {_fmt_amount(e['amount']):>14s} ({e['grant_count']}x) → {e['to_name'][:30]}")
    if len(edges) > 30:
        print(f"  ... ({len(edges) - 30} more edges)")

    if circular_flows:
        print(f"\n  {len(circular_flows)} CIRCULAR FLOWS:")
        for c in circular_flows:
            print(f"    {c['names'][0][:30]} ↔ {c['names'][1][:30]}")
            print(f"      A→B: {_fmt_amount(c['flow_a_to_b'])}  B→A: {_fmt_amount(c['flow_b_to_a'])}  Net: {_fmt_amount(c['net_flow'])} ({c['direction']})")


# ── shared-officers ────────────────────────────────────────────

def cmd_shared_officers(args):
    """Find officers/directors serving across multiple specified EINs."""
    db = get_db()
    eins = [e.replace("-", "") for e in args.eins]

    try:
        from tools.entity_resolution import normalize_person_name
    except ImportError:
        try:
            from entity_resolution import normalize_person_name
        except ImportError:
            def normalize_person_name(n):
                return n.lower().strip()

    # Pull all officers for the specified EINs
    placeholders = ",".join("?" for _ in eins)
    rows = db.execute(f"""
        SELECT ein, filer_name, person_name, title, total_comp, tax_year,
               is_director, is_officer, is_key_employee
        FROM officers
        WHERE ein IN ({placeholders})
        ORDER BY person_name, tax_year
    """, eins).fetchall()

    db.close()

    # Group by normalized name
    by_person = defaultdict(list)
    for r in rows:
        key = normalize_person_name(r["person_name"])
        by_person[key].append(dict(r))

    # Filter to people appearing in 2+ of the specified EINs
    shared = []
    for norm_name, records in by_person.items():
        unique_eins = set(r["ein"] for r in records)
        if len(unique_eins) >= 2:
            orgs = []
            for ein in unique_eins:
                ein_records = [r for r in records if r["ein"] == ein]
                latest = max(ein_records, key=lambda r: r["tax_year"])
                orgs.append({
                    "ein": ein,
                    "org_name": latest["filer_name"],
                    "title": latest["title"],
                    "compensation": latest["total_comp"],
                    "years_active": sorted(set(r["tax_year"] for r in ein_records)),
                })
            shared.append({
                "name": records[0]["person_name"],
                "normalized": norm_name,
                "org_count": len(unique_eins),
                "organizations": orgs,
            })

    shared.sort(key=lambda s: s["org_count"], reverse=True)

    result = {"eins_checked": eins, "shared_officers": shared, "total_shared": len(shared)}

    if write_output(result, args, summary=f"{len(shared)} shared officers across {len(eins)} EINs"):
        return

    print(f"─── Shared Officers across {len(eins)} Organizations ───")
    if not shared:
        print("  No shared officers/directors found.")
    else:
        for s in shared:
            print(f"\n  {s['name']} ({s['org_count']} orgs):")
            for o in s["organizations"]:
                comp = _fmt_amount(o["compensation"]) if o["compensation"] else "N/A"
                print(f"    {o['org_name'][:40]:40s}  {o['title'] or '':20s}  {comp:>12s}  {o['years_active']}")


# ── main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Query IRS 990 Bulk Database")
    sub = parser.add_subparsers(dest="command")

    p_sr = sub.add_parser("search", help="Full-text search grants + related orgs")
    p_sr.add_argument("query", help="Search term")
    p_sr.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_sr)

    p_fl = sub.add_parser("filer", help="Grants made by a filer EIN")
    p_fl.add_argument("ein", help="Filer EIN")
    add_output_args(p_fl)

    p_rc = sub.add_parser("recipient", help="Grants received by name (FTS5)")
    p_rc.add_argument("name", help="Recipient name")
    p_rc.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_rc)

    p_re = sub.add_parser("recipient-ein", help="Grants received by EIN")
    p_re.add_argument("ein", help="Recipient EIN")
    add_output_args(p_re)

    p_net = sub.add_parser("network", help="BFS grant network from seed EIN")
    p_net.add_argument("ein", help="Seed EIN")
    p_net.add_argument("--depth", type=int, default=2, help="BFS depth (default: 2)")
    p_net.add_argument("-n", "--limit", type=int, default=50, help="Max edges per hop")
    add_output_args(p_net)

    p_cg = sub.add_parser("co-grantors", help="Foundations funding the same recipient")
    p_cg.add_argument("name", help="Recipient name")
    p_cg.add_argument("-n", "--limit", type=int, default=50, help="Max results")
    add_output_args(p_cg)

    p_xr = sub.add_parser("cross-ref", help="Match investigation.db entities against bulk grants")
    add_output_args(p_xr)

    p_lu = sub.add_parser("lookup", help="Comprehensive EIN view (metadata + financials + officers + grants)")
    p_lu.add_argument("ein", help="Nonprofit EIN")
    add_output_args(p_lu)

    p_fil = sub.add_parser("filings", help="Filing list with PDF links (via ProPublica)")
    p_fil.add_argument("ein", help="Nonprofit EIN")
    add_output_args(p_fil)

    p_top = sub.add_parser("top", help="Top grantmakers/recipients")
    p_top.add_argument("--by", default="amount", help="Rank by: amount, count, recipients, single")
    p_top.add_argument("-n", "--limit", type=int, default=20, help="Number of results")
    add_output_args(p_top)

    p_off = sub.add_parser("officers", help="Officers/directors for a nonprofit by EIN")
    p_off.add_argument("ein", help="Nonprofit EIN")
    add_output_args(p_off)

    p_os = sub.add_parser("officer-search", help="Find a person across all nonprofits")
    p_os.add_argument("name", help="Person name (partial match)")
    p_os.add_argument("-n", "--limit", type=int, default=100, help="Max results")
    add_output_args(p_os)

    p_fin = sub.add_parser("financials", help="Financial summary over time")
    p_fin.add_argument("ein", help="Nonprofit EIN")
    add_output_args(p_fin)

    p_rf = sub.add_parser("red-flags", help="Red-flag analysis (ratios + checklist + insiders)")
    p_rf.add_argument("ein", help="Nonprofit EIN")
    add_output_args(p_rf)

    p_tc = sub.add_parser("top-compensated", help="Highest-compensated nonprofit officers")
    p_tc.add_argument("--min-comp", type=int, default=500000, help="Min compensation (default: 500000)")
    p_tc.add_argument("-n", "--limit", type=int, default=50, help="Max results")
    add_output_args(p_tc)

    p_fw = sub.add_parser("flow", help="Directed grant flow graph with circular detection")
    p_fw.add_argument("ein", help="Seed EIN")
    p_fw.add_argument("--depth", type=int, default=2, help="BFS depth (default: 2)")
    p_fw.add_argument("--min-amount", type=int, default=0, help="Min grant amount to include")
    p_fw.add_argument("-n", "--limit", type=int, default=50, help="Max edges per hop")
    add_output_args(p_fw)

    p_so = sub.add_parser("shared-officers", help="Officers serving across multiple EINs")
    p_so.add_argument("eins", nargs="+", help="Two or more EINs to check")
    add_output_args(p_so)

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "filer":
        cmd_filer(args)
    elif args.command == "recipient":
        cmd_recipient(args)
    elif args.command == "recipient-ein":
        cmd_recipient_ein(args)
    elif args.command == "network":
        cmd_network(args)
    elif args.command == "co-grantors":
        cmd_co_grantors(args)
    elif args.command == "cross-ref":
        cmd_cross_ref(args)
    elif args.command == "lookup":
        cmd_lookup(args)
    elif args.command == "filings":
        cmd_filings(args)
    elif args.command == "top":
        cmd_top(args)
    elif args.command == "officers":
        cmd_officers(args)
    elif args.command == "officer-search":
        cmd_officer_search(args)
    elif args.command == "financials":
        cmd_financials(args)
    elif args.command == "red-flags":
        cmd_red_flags(args)
    elif args.command == "top-compensated":
        cmd_top_compensated(args)
    elif args.command == "flow":
        cmd_flow(args)
    elif args.command == "shared-officers":
        cmd_shared_officers(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
