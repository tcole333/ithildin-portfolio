#!/usr/bin/env python3
"""Export financial flow data for Sankey diagrams from DS10 + investigation.db findings."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

LMSBAND_DB = Path(__file__).parent.parent / "datasets" / "lmsband_epstein_files.db"
INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"
OUTPUT_DIR = Path(__file__).parent.parent / "content" / "financials"

# Add tools to path for name_resolver
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.name_resolver import resolve_canonical


def normalize_entity(name: str) -> str:
    if not name:
        return name
    return resolve_canonical(name.strip())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _fetch_quality_metadata(inv_db_path: Path = INVESTIGATION_DB) -> dict:
    if not inv_db_path.exists():
        return {"quality_run_id": None, "math_checks_passed": False}

    conn = sqlite3.connect(str(inv_db_path))
    conn.row_factory = sqlite3.Row
    quality_run_id = None
    math_checks_passed = False
    try:
        if _table_exists(conn, "quality_runs"):
            row = conn.execute(
                """
                SELECT run_id
                FROM quality_runs
                WHERE dataset = 'ds10'
                ORDER BY COALESCE(completed_at, started_at) DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                quality_run_id = row["run_id"]
        if _table_exists(conn, "quality_issues"):
            crit = conn.execute(
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE dataset='ds10'
                  AND status='open'
                  AND severity='critical'
                  AND issue_code LIKE 'MATH%'
                """
            ).fetchone()[0]
            math_checks_passed = int(crit) == 0
    finally:
        conn.close()

    return {"quality_run_id": quality_run_id, "math_checks_passed": math_checks_passed}


def export_ds10_flows(
    min_amount: float = 50000,
    ds10_db_path: Path = LMSBAND_DB,
    inv_db_path: Path = INVESTIGATION_DB,
) -> dict:
    """Export DS10 transaction flows as Sankey-compatible data."""
    if not ds10_db_path.exists():
        print(f"  Warning: {ds10_db_path} not found, skipping DS10 flows")
        return {"nodes": [], "links": [], "stats": {}}

    conn = sqlite3.connect(str(ds10_db_path))
    conn.row_factory = sqlite3.Row

    qa_filter = ""
    if _column_exists(conn, "ds10_transactions", "qa_status"):
        qa_filter = "AND qa_status = 'promoted'"

    # Get aggregated flows between entities
    rows = conn.execute(
        f"""
        SELECT sender, receiver, SUM(amount) as total_amount, COUNT(*) as tx_count,
               MIN(tx_date) as first_date, MAX(tx_date) as last_date
        FROM ds10_transactions
        WHERE amount >= ? AND sender IS NOT NULL AND receiver IS NOT NULL
              AND sender != '' AND receiver != ''
              {qa_filter}
        GROUP BY sender, receiver
        HAVING total_amount >= ?
        ORDER BY total_amount DESC
        """,
        (0, min_amount),
    ).fetchall()

    # Build nodes and links
    node_set = set()
    links = []

    for row in rows:
        sender = normalize_entity(row["sender"])
        receiver = normalize_entity(row["receiver"])

        # Skip self-transfers and internal transfers
        if sender == receiver:
            continue
        if sender in ("INTERNAL TRANSFER", "TRANSFER") or receiver in ("INTERNAL TRANSFER", "TRANSFER"):
            continue

        node_set.add(sender)
        node_set.add(receiver)

        links.append({
            "source": sender,
            "target": receiver,
            "value": round(row["total_amount"], 2),
            "tx_count": row["tx_count"],
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        })

    nodes = [{"id": name, "name": name} for name in sorted(node_set)]

    # Get balance trajectory for key entities
    balances = {}
    for entity in ["Southern Trust Company", "WE LLC", "Plan D, LLC", "JEGE Inc", "IGO Company LLC"]:
        bal_rows = conn.execute(
            """
            SELECT balance_date, SUM(balance) as total_balance, account_type
            FROM ds10_balances
            WHERE account_holder LIKE ?
            GROUP BY balance_date
            ORDER BY balance_date
            """,
            (f"%{entity.split()[0]}%",),
        ).fetchall()
        if bal_rows:
            balances[entity] = [{"date": r["balance_date"], "balance": r["total_balance"]} for r in bal_rows]

    # Top transactions for detail
    top_tx = conn.execute(
        f"""
        SELECT tx_date, amount, sender, receiver, reference, efta_id
        FROM ds10_transactions
        WHERE amount >= 1000000
          {qa_filter}
        ORDER BY amount DESC
        LIMIT 50
        """,
    ).fetchall()

    conn.close()
    quality = _fetch_quality_metadata(inv_db_path)

    return {
        "nodes": nodes,
        "links": links,
        "balances": balances,
        "top_transactions": [dict(r) for r in top_tx],
        "quality_run_id": quality["quality_run_id"],
        "math_checks_passed": quality["math_checks_passed"],
        "stats": {
            "total_nodes": len(nodes),
            "total_links": len(links),
            "total_value": sum(l["value"] for l in links),
        },
    }


def export_apollo_pipeline(inv_db_path: Path = INVESTIGATION_DB) -> dict:
    """Export Apollo → Epstein money flows from findings."""
    conn = sqlite3.connect(str(inv_db_path))
    conn.row_factory = sqlite3.Row

    # Get financial findings related to Apollo/Black/Rowan/Harris
    targets = ["Leon Black", "Marc Rowan", "Joshua Harris", "Southern Trust Company",
               "Apollo Global Management", "The 2017 Caterpillar Trust"]
    placeholders = ",".join("?" * len(targets))

    findings = conn.execute(
        f"""
        SELECT f.id, f.target_name, f.finding_type, f.summary, f.detail,
               f.date_of_event, f.confidence
        FROM findings f
        WHERE f.target_name IN ({placeholders})
          AND f.finding_type = 'financial'
          AND f.verification_status != 'retracted'
        ORDER BY f.date_of_event
        """,
        targets,
    ).fetchall()

    # Manual Sankey nodes for the Apollo pipeline
    nodes = [
        {"id": "Leon Black", "name": "Leon Black", "category": "person"},
        {"id": "Marc Rowan", "name": "Marc Rowan", "category": "person"},
        {"id": "Joshua Harris", "name": "Joshua Harris", "category": "person"},
        {"id": "Caterpillar Trust", "name": "2017 Caterpillar Trust", "category": "trust"},
        {"id": "Family Office", "name": "Black Family Office", "category": "entity"},
        {"id": "EdR Trust", "name": "Edmond de Rothschild Trust", "category": "entity"},
        {"id": "STC", "name": "Southern Trust Company", "category": "entity"},
        {"id": "Jeffrey Epstein", "name": "Jeffrey Epstein", "category": "person"},
    ]

    # Known flows from findings (amounts from Wave 8-11)
    links = [
        {"source": "Leon Black", "target": "Family Office", "value": 158000000, "label": "$158M+ total"},
        {"source": "Family Office", "target": "STC", "value": 40000000, "label": "$40M in 2013 alone"},
        {"source": "Family Office", "target": "EdR Trust", "value": 25000000, "label": "$25M (unresolved)"},
        {"source": "Marc Rowan", "target": "Caterpillar Trust", "value": 1000000, "label": "Trust contributions"},
        {"source": "Joshua Harris", "target": "STC", "value": 1000000, "label": "Advisory fees"},
        {"source": "STC", "target": "Jeffrey Epstein", "value": 110000000, "label": "Peak balance $110M"},
        {"source": "EdR Trust", "target": "STC", "value": 25000000, "label": "$25M transfer"},
    ]

    conn.close()

    return {
        "title": "Apollo Money Pipeline",
        "subtitle": "How $158M+ flowed from three billionaires to a convicted sex offender",
        "nodes": nodes,
        "links": links,
        "findings": [dict(f) for f in findings],
    }


def export_wexner_architecture() -> dict:
    """Export Wexner trust/property architecture."""
    nodes = [
        {"id": "Les Wexner", "name": "Les Wexner", "category": "person"},
        {"id": "FTC", "name": "Financial Trust Company", "category": "trust"},
        {"id": "NWO", "name": "NWO LLC", "category": "entity"},
        {"id": "Maple Inc", "name": "Maple Inc (USVI)", "category": "entity"},
        {"id": "9E71st", "name": "9 E 71st Street", "category": "property"},
        {"id": "L Brands", "name": "L Brands / BBWI", "category": "company"},
        {"id": "Jeffrey Epstein", "name": "Jeffrey Epstein", "category": "person"},
        {"id": "Wexner Foundation", "name": "Wexner Foundation", "category": "nonprofit"},
    ]

    links = [
        {"source": "Les Wexner", "target": "FTC", "value": 78000000, "label": "FTC controlled 7.8M L Brands shares"},
        {"source": "Les Wexner", "target": "NWO", "value": 1, "label": "Ownership"},
        {"source": "Les Wexner", "target": "9E71st", "value": 1, "label": "Deeded to Maple Inc"},
        {"source": "9E71st", "target": "Maple Inc", "value": 1, "label": "Wexner → Maple USVI transfer"},
        {"source": "Maple Inc", "target": "Jeffrey Epstein", "value": 51000000, "label": "$51M sale"},
        {"source": "FTC", "target": "L Brands", "value": 78000000, "label": "7.8M shares"},
        {"source": "Les Wexner", "target": "Wexner Foundation", "value": 1, "label": "Founder"},
    ]

    return {
        "title": "Wexner Trust Architecture",
        "subtitle": "A masterclass in using trusts to obscure beneficial ownership",
        "nodes": nodes,
        "links": links,
    }


def main():
    parser = argparse.ArgumentParser(description="Export financial flow data")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--ds10-db", type=Path, default=LMSBAND_DB, help="Path to DS10 SQLite database")
    parser.add_argument("--inv-db", type=Path, default=INVESTIGATION_DB, help="Path to investigation SQLite DB")
    parser.add_argument("--diagram", choices=["ds10", "apollo", "wexner", "all"], default="all")
    parser.add_argument("--min-amount", type=float, default=50000, help="Min amount for DS10 flows")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    diagrams = {
        "ds10": ("ds10-flows.json", lambda: export_ds10_flows(args.min_amount, args.ds10_db, args.inv_db)),
        "apollo": ("apollo-pipeline.json", lambda: export_apollo_pipeline(args.inv_db)),
        "wexner": ("wexner-architecture.json", export_wexner_architecture),
    }

    to_export = diagrams.keys() if args.diagram == "all" else [args.diagram]

    for key in to_export:
        filename, fn = diagrams[key]
        data = fn()
        out_path = args.output_dir / filename
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Exported {key} → {out_path}")


if __name__ == "__main__":
    main()
