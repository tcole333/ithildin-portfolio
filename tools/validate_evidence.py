#!/usr/bin/env python3
"""
Evidence reference validation for OSINT investigations.

Checks that EFTA IDs and other evidence references cited in findings
actually exist in the corpus databases (DOJ, LMSBAND, Unified).

Usage:
    python tools/validate_evidence.py scan
    python tools/validate_evidence.py scan --fix
    python tools/validate_evidence.py scan --output results.json
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import doj_db_path, investigation_db_path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

PROJECT_ROOT = Path(__file__).parent.parent
INVESTIGATION_DB = investigation_db_path()

# Corpus databases where EFTA IDs should resolve
CORPUS_DBS = {
    "doj": doj_db_path(),
    "lmsband": PROJECT_ROOT / "datasets" / "lmsband_epstein_files.db",
    "unified": PROJECT_ROOT / "datasets" / "unified_epstein.db",
}

# EFTA ID pattern: EFTA followed by digits (e.g., EFTA02336502)
EFTA_RE = re.compile(r"EFTA\d{5,}")


def _get_corpus_tables():
    """Build lookup of available corpus FTS tables."""
    tables = {}
    for name, path in CORPUS_DBS.items():
        if not path.exists():
            continue
        try:
            db = sqlite3.connect(str(path))
            # Check which tables exist for ID lookup
            cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            table_names = {r[0] for r in cursor.fetchall()}
            tables[name] = {"db": db, "tables": table_names, "path": path}
        except Exception:
            continue
    return tables


def _check_efta_in_corpus(efta_id, corpus_tables):
    """Check if an EFTA ID exists in any corpus database."""
    for name, info in corpus_tables.items():
        db = info["db"]
        # Try common table/column patterns
        for table, col in [
            ("documents", "doc_id"),
            ("documents", "efta_id"),
            ("files", "file_id"),
            ("files", "efta_id"),
            ("emails", "doc_id"),
        ]:
            if table not in info["tables"]:
                continue
            try:
                row = db.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (efta_id,)).fetchone()
                if row:
                    return True
            except sqlite3.OperationalError:
                continue
        # Try FTS search as fallback
        for fts_table in [t for t in info["tables"] if t.endswith("_fts")]:
            try:
                row = db.execute(f"SELECT 1 FROM {fts_table} WHERE {fts_table} MATCH ? LIMIT 1",
                                 (f'"{efta_id}"',)).fetchone()
                if row:
                    return True
            except sqlite3.OperationalError:
                continue
    return False


def scan_evidence(fix=False):
    """Scan all finding evidence refs and validate EFTA IDs.

    Returns dict with valid/invalid counts and list of invalid refs.
    """
    if not INVESTIGATION_DB.exists():
        return {"error": "investigation.db not found"}

    db = sqlite3.connect(str(INVESTIGATION_DB))
    db.row_factory = sqlite3.Row

    # Get all evidence refs
    rows = db.execute("""
        SELECT fe.finding_id, fe.evidence_type, fe.evidence_ref,
               f.target_name, f.summary
        FROM finding_evidence fe
        JOIN findings f ON f.id = fe.finding_id
        WHERE fe.evidence_type = 'efta' OR fe.evidence_ref LIKE 'EFTA%'
    """).fetchall()

    if not rows:
        db.close()
        return {"total_refs": 0, "valid": 0, "invalid": 0, "invalid_refs": []}

    corpus_tables = _get_corpus_tables()
    if not corpus_tables:
        db.close()
        return {"error": "No corpus databases available for validation"}

    valid = 0
    invalid_refs = []

    for row in rows:
        ref = row["evidence_ref"]
        # Extract EFTA ID from ref (may have prefixes/suffixes)
        match = EFTA_RE.search(ref)
        efta_id = match.group(0) if match else ref

        if _check_efta_in_corpus(efta_id, corpus_tables):
            valid += 1
        else:
            invalid_refs.append({
                "finding_id": row["finding_id"],
                "evidence_ref": ref,
                "target_name": row["target_name"],
                "summary": (row["summary"] or "")[:100],
            })

    # Close corpus connections
    for info in corpus_tables.values():
        info["db"].close()

    if fix and invalid_refs:
        # Mark findings with invalid refs as needing review
        for inv in invalid_refs:
            db.execute("""
                UPDATE findings SET quality_state = 'needs_review'
                WHERE id = ? AND quality_state != 'needs_review'
            """, (inv["finding_id"],))
        db.commit()

    db.close()

    return {
        "total_refs": len(rows),
        "valid": valid,
        "invalid": len(invalid_refs),
        "invalid_refs": invalid_refs,
        "corpus_dbs_checked": list(corpus_tables.keys()),
    }


def main():
    parser = argparse.ArgumentParser(description="Validate evidence references in findings")
    sub = parser.add_subparsers(dest="command")

    scan_p = sub.add_parser("scan", help="Scan all EFTA evidence refs for validity")
    scan_p.add_argument("--fix", action="store_true",
                        help="Mark findings with invalid refs as needs_review")
    add_output_args(scan_p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "scan":
        result = scan_evidence(fix=args.fix)
        if not write_output(result, args, summary=f"evidence validation: {result.get('valid', 0)} valid, {result.get('invalid', 0)} invalid"):
            if result.get("error"):
                print(f"ERROR: {result['error']}")
                sys.exit(1)
            print(f"\nEvidence Reference Validation")
            print(f"{'='*60}")
            print(f"  Total EFTA refs:  {result['total_refs']}")
            print(f"  Valid:            {result['valid']}")
            print(f"  Invalid:          {result['invalid']}")
            print(f"  Corpus DBs:       {', '.join(result['corpus_dbs_checked'])}")
            if result["invalid_refs"]:
                print(f"\nInvalid references:")
                for inv in result["invalid_refs"][:20]:
                    print(f"  Finding #{inv['finding_id']}: {inv['evidence_ref']}")
                    print(f"    {inv['target_name']} — {inv['summary']}")
                if len(result["invalid_refs"]) > 20:
                    print(f"  ... and {len(result['invalid_refs']) - 20} more")
            if args.fix and result["invalid_refs"]:
                print(f"\nMarked {len(result['invalid_refs'])} findings as needs_review.")


if __name__ == "__main__":
    main()
