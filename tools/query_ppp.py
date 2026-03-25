#!/usr/bin/env python3
"""
SBA PPP/EIDL Loan query tool (DuckDB over Parquet).

~11M PPP loan records from SBA FOIA bulk download. Includes borrower name,
address, NAICS code, lender, jobs reported, loan amount, and forgiveness.

Data: https://data.sba.gov/dataset/ppp-foia
Convert: python scripts/convert_ppp_csv.py data/public_*.csv

Usage:
    python tools/query_ppp.py stats
    python tools/query_ppp.py search "Acme Corp"
    python tools/query_ppp.py borrower "EXACT BORROWER NAME"
    python tools/query_ppp.py address "123 Main St"
    python tools/query_ppp.py lender "JPMorgan Chase"
    python tools/query_ppp.py naics 541511
    python tools/query_ppp.py sql "SELECT * FROM ppp WHERE currentapprovalamount > 1000000 LIMIT 10"
"""

import argparse
import sys
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("ERROR: duckdb required. Install: uv add duckdb", file=sys.stderr)
    sys.exit(1)

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PARQUET_PATH = DATA_DIR / "ppp_loans.parquet"


def _connect():
    """Return DuckDB connection with parquet registered as view."""
    if not PARQUET_PATH.exists():
        print(f"ERROR: {PARQUET_PATH} not found.", file=sys.stderr)
        print("Download PPP CSV from https://data.sba.gov/dataset/ppp-foia", file=sys.stderr)
        print("Then convert: python scripts/convert_ppp_csv.py data/public_*.csv", file=sys.stderr)
        sys.exit(1)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW ppp AS SELECT * FROM read_parquet('{PARQUET_PATH}')")
    return con


def _fmt_money(val):
    if val is None:
        return "?"
    return f"${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _fmt_int(val):
    return f"{val:,}" if val is not None else "?"


# --- Commands ---

def cmd_stats(con):
    """Dataset summary."""
    r = con.execute("""
        SELECT
            count(*) as total_loans,
            sum(currentapprovalamount) as total_approved,
            sum(forgivenessAmount) as total_forgiven,
            avg(currentapprovalamount) as avg_loan,
            count(DISTINCT borrowername) as unique_borrowers,
            count(DISTINCT servicinglendername) as unique_lenders,
            count(DISTINCT naicscode) as unique_naics,
            min(dateapproved) as first_approval,
            max(dateapproved) as last_approval
        FROM ppp
    """).fetchone()

    return {
        "total_loans": r[0], "total_approved": r[1], "total_forgiven": r[2],
        "avg_loan": r[3], "unique_borrowers": r[4], "unique_lenders": r[5],
        "unique_naics": r[6], "first_approval": str(r[7]), "last_approval": str(r[8]),
    }


def _print_stats(s):
    print(f"\n  SBA PPP Loan Dataset")
    print(f"  {'='*50}")
    print(f"  Total Loans:       {_fmt_int(s['total_loans'])}")
    print(f"  Total Approved:    {_fmt_money(s['total_approved'])}")
    print(f"  Total Forgiven:    {_fmt_money(s['total_forgiven'])}")
    print(f"  Avg Loan:          {_fmt_money(s['avg_loan'])}")
    print(f"  Unique Borrowers:  {_fmt_int(s['unique_borrowers'])}")
    print(f"  Unique Lenders:    {_fmt_int(s['unique_lenders'])}")
    print(f"  NAICS Codes:       {_fmt_int(s['unique_naics'])}")
    print(f"  Period:            {s['first_approval']} to {s['last_approval']}")


def cmd_search(con, query, limit=50):
    """Search borrower names (case-insensitive contains)."""
    rows = con.execute("""
        SELECT borrowername, borroweraddress, borrowercity, borrowerstate, borrowerzip,
               currentapprovalamount, forgivenessAmount, dateapproved,
               servicinglendername, naicscode, jobsreported
        FROM ppp
        WHERE borrowername ILIKE ?
        ORDER BY currentapprovalamount DESC
        LIMIT ?
    """, (f"%{query}%", limit)).fetchall()

    cols = ["borrower", "address", "city", "state", "zip", "approved", "forgiven",
            "date_approved", "lender", "naics", "jobs"]
    records = [dict(zip(cols, r)) for r in rows]
    return {"query": query, "total": len(records), "records": records}


def _print_search(data):
    print(f"\n  PPP Loans matching '{data['query']}': {data['total']} results")
    print(f"  {'='*100}")
    for r in data["records"]:
        addr = ", ".join(filter(None, [r["address"], r["city"], r["state"], str(r["zip"] or "")]))
        print(f"  {r['borrower']}")
        print(f"    {addr}")
        print(f"    Approved: {_fmt_money(r['approved'])}  Forgiven: {_fmt_money(r['forgiven'])}  "
              f"Jobs: {r['jobs']}  NAICS: {r['naics']}  Lender: {r['lender']}")
        print(f"    Date: {r['date_approved']}")
        print()


def cmd_borrower(con, name, limit=20):
    """Exact borrower lookup with full detail."""
    rows = con.execute("""
        SELECT *
        FROM ppp
        WHERE borrowername ILIKE ?
        ORDER BY dateapproved DESC
        LIMIT ?
    """, (name, limit)).fetchall()

    cols = [desc[0] for desc in con.description]
    records = [dict(zip(cols, r)) for r in rows]
    return {"borrower": name, "total": len(records), "records": records}


def _print_borrower(data):
    print(f"\n  PPP Loans for '{data['borrower']}': {data['total']} records")
    print(f"  {'='*80}")
    for r in data["records"]:
        print(f"  Loan #{r.get('loannumber', '?')}")
        for k, v in r.items():
            if v is not None and str(v).strip():
                print(f"    {k}: {v}")
        print()


def cmd_address(con, addr, limit=50):
    """All PPP loans at an address (partial match)."""
    rows = con.execute("""
        SELECT borrowername, borroweraddress, borrowercity, borrowerstate, borrowerzip,
               currentapprovalamount, forgivenessAmount, dateapproved,
               servicinglendername, naicscode, jobsreported
        FROM ppp
        WHERE borroweraddress ILIKE ?
        ORDER BY currentapprovalamount DESC
        LIMIT ?
    """, (f"%{addr}%", limit)).fetchall()

    cols = ["borrower", "address", "city", "state", "zip", "approved", "forgiven",
            "date_approved", "lender", "naics", "jobs"]
    records = [dict(zip(cols, r)) for r in rows]
    return {"address": addr, "total": len(records), "records": records}


def _print_address(data):
    print(f"\n  PPP Loans at '{data['address']}': {data['total']} results")
    print(f"  {'='*100}")
    for r in data["records"]:
        print(f"  {r['borrower']}  ({r['city']}, {r['state']} {r['zip']})")
        print(f"    Approved: {_fmt_money(r['approved'])}  Forgiven: {_fmt_money(r['forgiven'])}  "
              f"Lender: {r['lender']}  NAICS: {r['naics']}")
        print()


def cmd_lender(con, name, limit=50):
    """All loans from a lender."""
    rows = con.execute("""
        SELECT borrowername, borroweraddress, borrowercity, borrowerstate,
               currentapprovalamount, forgivenessAmount, dateapproved, naicscode, jobsreported
        FROM ppp
        WHERE servicinglendername ILIKE ?
        ORDER BY currentapprovalamount DESC
        LIMIT ?
    """, (f"%{name}%", limit)).fetchall()

    cols = ["borrower", "address", "city", "state", "approved", "forgiven",
            "date_approved", "naics", "jobs"]
    records = [dict(zip(cols, r)) for r in rows]

    # Also get aggregate stats for this lender
    agg = con.execute("""
        SELECT count(*) as loan_count, sum(currentapprovalamount) as total_approved,
               sum(forgivenessAmount) as total_forgiven
        FROM ppp WHERE servicinglendername ILIKE ?
    """, (f"%{name}%",)).fetchone()

    return {
        "lender": name, "total": len(records),
        "loan_count": agg[0], "total_approved": agg[1], "total_forgiven": agg[2],
        "records": records,
    }


def _print_lender(data):
    print(f"\n  PPP Loans via '{data['lender']}' (showing {data['total']} of {_fmt_int(data['loan_count'])})")
    print(f"  Total approved: {_fmt_money(data['total_approved'])}  Forgiven: {_fmt_money(data['total_forgiven'])}")
    print(f"  {'='*100}")
    for r in data["records"]:
        print(f"  {r['borrower']}  ({r['city']}, {r['state']})")
        print(f"    Approved: {_fmt_money(r['approved'])}  Forgiven: {_fmt_money(r['forgiven'])}  "
              f"NAICS: {r['naics']}  Jobs: {r['jobs']}")
        print()


def cmd_naics(con, code, limit=50):
    """Loans by NAICS code."""
    rows = con.execute("""
        SELECT borrowername, borrowercity, borrowerstate,
               currentapprovalamount, forgivenessAmount, servicinglendername, jobsreported
        FROM ppp
        WHERE CAST(naicscode AS VARCHAR) LIKE ?
        ORDER BY currentapprovalamount DESC
        LIMIT ?
    """, (f"{code}%", limit)).fetchall()

    cols = ["borrower", "city", "state", "approved", "forgiven", "lender", "jobs"]
    records = [dict(zip(cols, r)) for r in rows]

    agg = con.execute("""
        SELECT count(*) as cnt, sum(currentapprovalamount) as total
        FROM ppp WHERE CAST(naicscode AS VARCHAR) LIKE ?
    """, (f"{code}%",)).fetchone()

    return {"naics": code, "total": len(records), "full_count": agg[0],
            "total_approved": agg[1], "records": records}


def _print_naics(data):
    print(f"\n  NAICS {data['naics']}: {_fmt_int(data['full_count'])} loans, {_fmt_money(data['total_approved'])} total")
    print(f"  Showing top {data['total']} by amount:")
    print(f"  {'='*90}")
    for r in data["records"]:
        print(f"  {r['borrower']}  ({r['city']}, {r['state']})  {_fmt_money(r['approved'])}  Lender: {r['lender']}")


def cmd_sql(con, query):
    """Ad-hoc DuckDB SQL (table: ppp)."""
    rows = con.execute(query).fetchall()
    cols = [desc[0] for desc in con.description]
    records = [dict(zip(cols, r)) for r in rows]
    return {"columns": cols, "total": len(records), "records": records}


# --- Enrichment ---

def cmd_enrich(con, dry_run=False, threshold=90):
    """Match investigation entities against PPP data using fuzzy matching.

    Uses entity_resolution.normalize_entity_name for name normalization and
    rapidfuzz for scoring. Only accepts matches above the threshold (default 90).
    """
    import sqlite3 as _sqlite3
    try:
        from tools.entity_resolution import normalize_entity_name
    except ImportError:
        from entity_resolution import normalize_entity_name
    from rapidfuzz import fuzz

    db_path = Path(__file__).resolve().parent.parent / "investigation.db"
    db = _sqlite3.connect(str(db_path))
    db.row_factory = _sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")

    entities = db.execute(
        "SELECT id, name, entity_type, jurisdiction, address, notes FROM entities"
    ).fetchall()

    results = {
        "total_entities": len(entities),
        "threshold": threshold,
        "matched": 0,
        "addresses_added": 0,
        "notes_updated": 0,
        "colocated_entities": [],
        "matches": [],
    }

    seen_addresses = set()

    for ent in entities:
        ent_id = ent["id"]
        ent_name = ent["name"]
        norm = normalize_entity_name(ent_name)

        if len(norm) < 4:
            continue

        # Detect person names (2-3 words, no entity keywords remaining after normalization)
        # and short names — both need higher threshold to avoid false positives
        words = norm.split()
        is_person_like = (
            len(words) <= 3
            and norm == normalize_entity_name(ent_name)  # no suffix was stripped
            and ent_name.replace(".", "").replace(",", "").strip() == ent_name.strip()  # no punctuation stripped
        )
        effective_threshold = max(threshold, 97) if (len(norm) < 10 or is_person_like) else threshold

        # Search PPP with normalized name — use ILIKE for case-insensitive partial
        rows = con.execute("""
            SELECT borrowername, borroweraddress, borrowercity, borrowerstate, borrowerzip,
                   currentapprovalamount, forgivenessAmount, dateapproved,
                   servicinglendername, naicscode, jobsreported, businesstype
            FROM ppp
            WHERE borrowername ILIKE ?
            ORDER BY currentapprovalamount DESC
            LIMIT 10
        """, (f"%{norm}%",)).fetchall()

        if not rows:
            continue

        cols = ["borrower", "address", "city", "state", "zip", "approved", "forgiven",
                "date_approved", "lender", "naics", "jobs", "business_type"]

        best_match = None
        best_score = 0

        # Map jurisdiction to state abbreviations for cross-check
        jurisdiction = (ent["jurisdiction"] or "").upper()
        state_map = {
            "NY": "NY", "NEW YORK": "NY", "DE": "DE", "DELAWARE": "DE",
            "FL": "FL", "FLORIDA": "FL", "CA": "CA", "CALIFORNIA": "CA",
            "TX": "TX", "TEXAS": "TX", "DC": "DC", "NJ": "NJ",
            "CT": "CT", "MA": "MA", "IL": "IL", "VA": "VA",
            "US-NY": "NY", "US-DE": "DE", "US-FL": "FL", "US-CA": "CA",
            "US-TX": "TX", "US-NJ": "NJ", "US-CT": "CT", "US-VA": "VA",
        }
        expected_state = state_map.get(jurisdiction)

        for row in rows:
            rec = dict(zip(cols, row))
            ppp_norm = normalize_entity_name(rec["borrower"])

            # Score using token_sort_ratio (handles word order differences)
            score = fuzz.token_sort_ratio(norm, ppp_norm)

            # Bonus for matching jurisdiction/state (helps disambiguate common names)
            if expected_state and rec["state"] == expected_state:
                score = min(100, score + 3)

            if score > best_score:
                best_score = score
                best_match = rec

        if best_score < effective_threshold or not best_match:
            continue

        rec = best_match
        full_addr = ", ".join(filter(None, [
            rec["address"], rec["city"], rec["state"], str(rec["zip"] or "")
        ]))

        # Determine confidence: high if jurisdiction matches or address is at known location
        match_state = rec["state"] or ""
        confidence = "review"  # default: needs human review
        if expected_state and match_state == expected_state:
            confidence = "high"
        elif best_score == 100 and len(norm) >= 15:
            # Long unique names with perfect score are likely correct
            confidence = "high"

        match_info = {
            "entity_id": ent_id,
            "entity_name": ent_name,
            "match_score": best_score,
            "confidence": confidence,
            "ppp_borrower": rec["borrower"],
            "ppp_address": full_addr,
            "ppp_approved": rec["approved"],
            "ppp_forgiven": rec["forgiven"],
            "ppp_lender": rec["lender"],
            "ppp_naics": rec["naics"],
            "ppp_jobs": rec["jobs"],
            "ppp_business_type": rec["business_type"],
            "ppp_date": rec["date_approved"],
        }
        results["matches"].append(match_info)
        results["matched"] += 1

        if dry_run:
            continue

        # Only auto-enrich high-confidence matches
        if confidence != "high":
            continue

        # Add address to entity_addresses if we have one and it's new
        if full_addr and len(full_addr) > 5:
            existing = db.execute(
                "SELECT id FROM entity_addresses WHERE entity_id=? AND address LIKE ?",
                (ent_id, f"%{(rec['address'] or '')[:20]}%")
            ).fetchone()
            if not existing and rec["address"]:
                db.execute(
                    """INSERT INTO entity_addresses (entity_id, address, address_type, date_observed, source)
                       VALUES (?, ?, 'business', ?, 'SBA PPP FOIA')""",
                    (ent_id, full_addr, rec["date_approved"])
                )
                results["addresses_added"] += 1

        # Update entity.address if it's empty
        if not ent["address"] and full_addr and len(full_addr) > 5:
            db.execute("UPDATE entities SET address=? WHERE id=? AND (address IS NULL OR address='')",
                       (full_addr, ent_id))

        # Build enrichment note
        note_parts = []
        if rec["naics"]:
            note_parts.append(f"NAICS:{rec['naics']}")
        if rec["lender"]:
            note_parts.append(f"Lender:{rec['lender']}")
        if rec["jobs"]:
            note_parts.append(f"Jobs:{rec['jobs']}")
        if rec["approved"]:
            note_parts.append(f"PPP:${rec['approved']:,.0f}")
        if rec["business_type"]:
            note_parts.append(f"Type:{rec['business_type']}")

        if note_parts:
            ppp_note = f"[PPP {rec['date_approved']}] " + " | ".join(note_parts)
            existing_notes = ent["notes"] or ""
            if "PPP" not in existing_notes:
                new_notes = (existing_notes + "\n" + ppp_note).strip()
                db.execute("UPDATE entities SET notes=? WHERE id=?", (new_notes, ent_id))
                results["notes_updated"] += 1

        # Reverse address search — find co-located entities we don't know about
        if rec["address"] and rec["address"] not in seen_addresses:
            seen_addresses.add(rec["address"])
            neighbors = con.execute("""
                SELECT DISTINCT borrowername, currentapprovalamount, naicscode
                FROM ppp
                WHERE borroweraddress = ? AND borrowername != ?
                ORDER BY currentapprovalamount DESC
                LIMIT 10
            """, (rec["address"], rec["borrower"])).fetchall()

            for nb in neighbors:
                nb_name = nb[0]
                nb_norm = normalize_entity_name(nb_name)
                # Check if this neighbor is already in our entities
                known = False
                for e2 in entities:
                    e2_norm = normalize_entity_name(e2["name"])
                    if fuzz.token_sort_ratio(nb_norm, e2_norm) >= 90:
                        known = True
                        break
                if not known:
                    results["colocated_entities"].append({
                        "address": full_addr,
                        "known_entity": ent_name,
                        "unknown_neighbor": nb_name,
                        "neighbor_ppp_amount": nb[1],
                        "neighbor_naics": nb[2],
                    })

    if not dry_run:
        db.commit()
    db.close()

    return results


def _print_enrich(data):
    high = [m for m in data["matches"] if m.get("confidence") == "high"]
    review = [m for m in data["matches"] if m.get("confidence") != "high"]

    print(f"\n  PPP Entity Enrichment (threshold={data.get('threshold', 90)})")
    print(f"  {'='*70}")
    print(f"  Entities scanned: {data['total_entities']}")
    print(f"  PPP matches: {data['matched']} ({len(high)} high-confidence, {len(review)} needs review)")
    print(f"  Addresses added: {data['addresses_added']}")
    print(f"  Notes updated: {data['notes_updated']}")
    print(f"  Co-located unknown entities: {len(data['colocated_entities'])}")

    if high:
        print(f"\n  --- High-Confidence Matches (auto-enriched) ---")
        for m in high:
            forgiven_pct = ""
            if m["ppp_approved"] and m["ppp_forgiven"]:
                pct = (m["ppp_forgiven"] / m["ppp_approved"]) * 100
                forgiven_pct = f" ({pct:.0f}% forgiven)"
            print(f"  #{m['entity_id']} {m['entity_name']}  [{m['match_score']}]")
            print(f"    -> {m['ppp_borrower']}")
            print(f"    Address: {m['ppp_address']}")
            print(f"    PPP: ${m['ppp_approved']:,.0f}{forgiven_pct}  Lender: {m['ppp_lender']}")
            print(f"    NAICS: {m['ppp_naics']}  Jobs: {m['ppp_jobs']}  Type: {m['ppp_business_type']}")
            print()

    if review:
        print(f"\n  --- Needs Review (not auto-enriched) ---")
        for m in review:
            print(f"  #{m['entity_id']} {m['entity_name']:40s} -> {m['ppp_borrower']:40s}  [{m['match_score']}]  ${m['ppp_approved']:,.0f}")

    if data["colocated_entities"]:
        print(f"\n  --- Unknown Co-located Entities ---")
        for c in data["colocated_entities"]:
            print(f"  At: {c['address']} (same as {c['known_entity']})")
            print(f"    -> {c['unknown_neighbor']}  PPP: ${c['neighbor_ppp_amount']:,.0f}  NAICS: {c['neighbor_naics']}")
            print()


def _print_sql(data):
    if not data["records"]:
        print("  No results")
        return
    cols = data["columns"]
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in data["records"])) for c in cols}
    header = "  ".join(f"{c:>{widths[c]}}" for c in cols)
    print(f"\n  {header}")
    print(f"  {'-' * len(header)}")
    for r in data["records"]:
        line = "  ".join(f"{str(r.get(c, '')):>{widths[c]}}" for c in cols)
        print(f"  {line}")
    print(f"\n  {data['total']} rows")


def main():
    parser = argparse.ArgumentParser(description="Query SBA PPP loan data (DuckDB/Parquet)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("stats", help="Dataset summary")
    add_output_args(p)

    p = sub.add_parser("search", help="Search borrower names")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    p = sub.add_parser("borrower", help="Exact borrower lookup with full detail")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    p = sub.add_parser("address", help="All loans at an address")
    p.add_argument("addr")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    p = sub.add_parser("lender", help="All loans from a lender")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    p = sub.add_parser("naics", help="Loans by NAICS industry code")
    p.add_argument("code")
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    p = sub.add_parser("sql", help="Ad-hoc SQL (table: ppp)")
    p.add_argument("query")
    add_output_args(p)

    p = sub.add_parser("enrich", help="Match entities against PPP, enrich addresses/metadata")
    p.add_argument("--dry-run", action="store_true", help="Preview matches without writing")
    p.add_argument("--threshold", type=int, default=90, help="Fuzzy match threshold 0-100 (default 90)")
    add_output_args(p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    con = _connect()

    handlers = {
        "stats": (lambda: cmd_stats(con), _print_stats, "PPP stats"),
        "search": (lambda: cmd_search(con, args.query, args.limit), _print_search,
                   lambda r: f"PPP search '{args.query}': {r['total']} results"),
        "borrower": (lambda: cmd_borrower(con, args.name, args.limit), _print_borrower,
                     lambda r: f"PPP borrower '{args.name}': {r['total']} records"),
        "address": (lambda: cmd_address(con, args.addr, args.limit), _print_address,
                    lambda r: f"PPP address '{args.addr}': {r['total']} results"),
        "lender": (lambda: cmd_lender(con, args.name, args.limit), _print_lender,
                   lambda r: f"PPP lender '{args.name}': {r['total']} results"),
        "naics": (lambda: cmd_naics(con, args.code, args.limit), _print_naics,
                  lambda r: f"NAICS {args.code}: {r['total']} results"),
        "sql": (lambda: cmd_sql(con, args.query), _print_sql, "PPP SQL"),
        "enrich": (lambda: cmd_enrich(con, dry_run=args.dry_run, threshold=args.threshold), _print_enrich,
                   lambda r: f"PPP enrich: {r['matched']} matches, {r['addresses_added']} addresses added"),
    }

    run_fn, print_fn, summary_fn = handlers[args.command]
    result = run_fn()
    summary = summary_fn(result) if callable(summary_fn) else summary_fn
    if not write_output(result, args, summary=summary):
        print_fn(result)


if __name__ == "__main__":
    main()
