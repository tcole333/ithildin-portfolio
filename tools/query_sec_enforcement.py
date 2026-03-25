#!/usr/bin/env python3
"""Query SEC enforcement actions database.

Search, defendant lookup, co-defendant networks, repeat offender detection,
and cross-referencing against investigation.db and registry.db.

Database: datasets/sec_enforcement.db (built by ingest_sec_enforcement.py)

Usage:
    python tools/query_sec_enforcement.py search "insider trading"
    python tools/query_sec_enforcement.py search "Epstein" --source litigation
    python tools/query_sec_enforcement.py defendant "Leon Black"
    python tools/query_sec_enforcement.py defendant "JPMorgan" --fuzzy --threshold 80
    python tools/query_sec_enforcement.py action LR-26503
    python tools/query_sec_enforcement.py co-defendants LR-26489
    python tools/query_sec_enforcement.py network "Joseph Lewis" --depth 2
    python tools/query_sec_enforcement.py repeat-offenders --min-actions 2
    python tools/query_sec_enforcement.py stats --by-year
    python tools/query_sec_enforcement.py cross-ref
    python tools/query_sec_enforcement.py cross-ref --auto-leads --dry-run
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

# ---------------------------------------------------------------------------
# Database paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "datasets" / "sec_enforcement.db"
INVESTIGATION_DB = PROJECT_ROOT / "investigation.db"
REGISTRY_DB = PROJECT_ROOT / "registry.db"


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


def get_db():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        print("Run: uv run python tools/ingest_sec_enforcement.py ingest", file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def cmd_search(args):
    """FTS5 search across respondent text and release numbers."""
    db = get_db()
    query = args.query

    conditions = []
    params = []

    # Source filter
    if args.source:
        conditions.append("ea.source_type = ?")
        params.append(args.source)

    # Date filters
    if args.start:
        conditions.append("ea.date_published >= ?")
        params.append(args.start)
    if args.end:
        conditions.append("ea.date_published <= ?")
        params.append(args.end)

    where = ""
    if conditions:
        where = "AND " + " AND ".join(conditions)

    rows = db.execute(
        f"""SELECT ea.id, ea.release_number, ea.source_type, ea.date_published,
                   ea.respondent_text, ea.release_url, ea.file_number
            FROM enforcement_actions ea
            JOIN enforcement_actions_fts f ON ea.id = f.rowid
            WHERE enforcement_actions_fts MATCH ?
            {where}
            ORDER BY ea.date_published DESC
            LIMIT ?""",
        [query] + params + [args.limit],
    ).fetchall()

    results = [dict(r) for r in rows]
    _log(query, "sec_enforcement", len(results))
    db.close()

    if write_output(results, args, summary=f"SEC enforcement search '{query}'"):
        return

    if not results:
        print(f"No SEC enforcement actions matching '{query}'.")
        return

    print(f"Found {len(results)} SEC enforcement actions matching '{query}':\n")
    for r in results:
        print(f"  {r['release_number']:12s} {r['date_published']}  [{r['source_type']}]")
        print(f"    {r['respondent_text'][:100]}")
        if r.get("release_url"):
            print(f"    {r['release_url']}")
        print()


# ---------------------------------------------------------------------------
# defendant
# ---------------------------------------------------------------------------


def cmd_defendant(args):
    """Find all enforcement actions involving a specific defendant."""
    db = get_db()
    name = args.name

    if args.fuzzy:
        results = _fuzzy_defendant_search(db, name, args.threshold)
    else:
        results = _exact_defendant_search(db, name)

    _log(name, "sec_enforcement_defendant", len(results))
    db.close()

    if write_output(results, args, summary=f"SEC defendant '{name}'"):
        return

    if not results:
        print(f"No enforcement actions found for '{name}'.")
        if not args.fuzzy:
            print("  Tip: try --fuzzy for approximate matching")
        return

    print(f"Found {len(results)} enforcement actions for '{name}':\n")
    for r in results:
        score = f" (score: {r['match_score']})" if r.get("match_score") else ""
        print(f"  {r['release_number']:12s} {r['date_published']}  [{r['source_type']}]{score}")
        print(f"    Matched: {r['matched_name']}")
        print(f"    Full respondents: {r['respondent_text'][:100]}")
        if r.get("release_url"):
            print(f"    {r['release_url']}")
        print()


def _exact_defendant_search(db, name):
    """Search by exact normalized name match."""
    try:
        from tools.entity_resolution import normalize_entity_name, normalize_person_name
    except ImportError:
        try:
            from entity_resolution import normalize_entity_name, normalize_person_name
        except ImportError:
            normalize_entity_name = normalize_person_name = lambda x: x.lower().strip()

    norm_person = normalize_person_name(name)
    norm_entity = normalize_entity_name(name)

    rows = db.execute(
        """SELECT ea.*, ed.name_raw as matched_name, ed.defendant_type
           FROM enforcement_defendants ed
           JOIN enforcement_actions ea ON ed.action_id = ea.id
           WHERE ed.name_normalized IN (?, ?)
           ORDER BY ea.date_published DESC""",
        (norm_person, norm_entity),
    ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["match_score"] = 100
        d["match_type"] = "exact"
        results.append(d)
    return results


def _fuzzy_defendant_search(db, name, threshold):
    """Search using FTS + rapidfuzz confirmation."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        print("ERROR: rapidfuzz not installed. Run: uv pip install rapidfuzz", file=sys.stderr)
        return []

    try:
        from tools.entity_resolution import normalize_person_name
    except ImportError:
        try:
            from entity_resolution import normalize_person_name
        except ImportError:
            normalize_person_name = lambda x: x.lower().strip()

    norm = normalize_person_name(name)

    # FTS search for candidates
    # Use individual words as OR query for broad recall
    fts_query = " OR ".join(f'"{w}"' for w in norm.split() if len(w) > 2)
    if not fts_query:
        fts_query = f'"{norm}"'

    candidates = db.execute(
        """SELECT ed.id, ed.action_id, ed.name_raw, ed.name_normalized, ed.defendant_type
           FROM enforcement_defendants ed
           JOIN enforcement_defendants_fts f ON ed.id = f.rowid
           WHERE enforcement_defendants_fts MATCH ?
           LIMIT 500""",
        (fts_query,),
    ).fetchall()

    # Score each candidate
    matches = []
    for c in candidates:
        score = fuzz.token_sort_ratio(norm, c["name_normalized"])
        if score >= threshold:
            matches.append((c, score))

    # Fetch full action details for matches
    results = []
    for c, score in sorted(matches, key=lambda x: -x[1]):
        action = db.execute(
            "SELECT * FROM enforcement_actions WHERE id = ?", (c["action_id"],)
        ).fetchone()
        if action:
            d = dict(action)
            d["matched_name"] = c["name_raw"]
            d["defendant_type"] = c["defendant_type"]
            d["match_score"] = score
            d["match_type"] = "fuzzy"
            results.append(d)

    return results


# ---------------------------------------------------------------------------
# action
# ---------------------------------------------------------------------------


def cmd_action(args):
    """Show details of a specific enforcement action."""
    db = get_db()
    release = args.release_number

    action = db.execute(
        "SELECT * FROM enforcement_actions WHERE release_number = ?", (release,)
    ).fetchone()

    if not action:
        # Try case-insensitive
        action = db.execute(
            "SELECT * FROM enforcement_actions WHERE UPPER(release_number) = UPPER(?)",
            (release,),
        ).fetchone()

    if not action:
        db.close()
        print(f"No enforcement action found with release number '{release}'.")
        sys.exit(1)

    # Get all defendants
    defendants = db.execute(
        """SELECT name_raw, name_normalized, defendant_type, is_et_al
           FROM enforcement_defendants WHERE action_id = ?
           ORDER BY defendant_type, name_raw""",
        (action["id"],),
    ).fetchall()

    result = dict(action)
    result["defendants"] = [dict(d) for d in defendants]

    db.close()

    if write_output(result, args, summary=f"SEC action {release}"):
        return

    print(f"SEC Enforcement Action: {result['release_number']}")
    print(f"  Source:      {result['source_type']}")
    print(f"  Date:        {result['date_published']}")
    print(f"  Respondents: {result['respondent_text']}")
    if result.get("file_number"):
        print(f"  File No:     {result['file_number']}")
    if result.get("release_url"):
        print(f"  URL:         {result['release_url']}")
    if result.get("see_also_text"):
        print(f"  See also:    {result['see_also_text']} ({result.get('see_also_url', '')})")
    print(f"\n  Defendants ({len(result['defendants'])}):")
    for d in result["defendants"]:
        et = " (et al.)" if d["is_et_al"] else ""
        print(f"    [{d['defendant_type']:7s}] {d['name_raw']}{et}")


# ---------------------------------------------------------------------------
# co-defendants
# ---------------------------------------------------------------------------


def cmd_co_defendants(args):
    """List all defendants in a specific enforcement action."""
    db = get_db()
    release = args.release_number

    action = db.execute(
        "SELECT id, respondent_text FROM enforcement_actions WHERE release_number = ?",
        (release,),
    ).fetchone()

    if not action:
        action = db.execute(
            "SELECT id, respondent_text FROM enforcement_actions WHERE UPPER(release_number) = UPPER(?)",
            (release,),
        ).fetchone()

    if not action:
        db.close()
        print(f"No enforcement action found with release number '{release}'.")
        sys.exit(1)

    defendants = db.execute(
        """SELECT name_raw, name_normalized, defendant_type, is_et_al
           FROM enforcement_defendants WHERE action_id = ?
           ORDER BY defendant_type, name_raw""",
        (action["id"],),
    ).fetchall()

    results = [dict(d) for d in defendants]
    db.close()

    if write_output(results, args, summary=f"co-defendants in {release}"):
        return

    print(f"Co-defendants in {release} ({len(results)}):")
    for d in results:
        et = " (et al.)" if d["is_et_al"] else ""
        print(f"  [{d['defendant_type']:7s}] {d['name_raw']}{et}")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


def cmd_network(args):
    """Find co-defendant network for a person — everyone who has been
    co-defendants with this person, and optionally their co-defendants too."""
    db = get_db()
    name = args.name
    depth = args.depth

    try:
        from tools.entity_resolution import normalize_person_name, normalize_entity_name
    except ImportError:
        try:
            from entity_resolution import normalize_person_name, normalize_entity_name
        except ImportError:
            normalize_person_name = normalize_entity_name = lambda x: x.lower().strip()

    norm_p = normalize_person_name(name)
    norm_e = normalize_entity_name(name)

    # BFS through co-defendant graph
    visited = set()
    queue = [(norm_p, 0), (norm_e, 0)]
    network = defaultdict(lambda: {"actions": set(), "depth": float("inf")})

    while queue:
        current_name, current_depth = queue.pop(0)
        if current_name in visited or current_depth > depth:
            continue
        visited.add(current_name)

        # Find all actions this person is in
        action_rows = db.execute(
            """SELECT DISTINCT action_id
               FROM enforcement_defendants
               WHERE name_normalized = ?""",
            (current_name,),
        ).fetchall()

        for ar in action_rows:
            # Find all co-defendants in those actions
            co_defs = db.execute(
                """SELECT ed.name_raw, ed.name_normalized, ed.defendant_type,
                          ea.release_number, ea.date_published
                   FROM enforcement_defendants ed
                   JOIN enforcement_actions ea ON ed.action_id = ea.id
                   WHERE ed.action_id = ? AND ed.name_normalized != ?""",
                (ar["action_id"], current_name),
            ).fetchall()

            for cd in co_defs:
                norm_cd = cd["name_normalized"]
                network[norm_cd]["name_raw"] = cd["name_raw"]
                network[norm_cd]["defendant_type"] = cd["defendant_type"]
                network[norm_cd]["actions"].add(
                    f"{cd['release_number']} ({cd['date_published']})"
                )
                network[norm_cd]["depth"] = min(
                    network[norm_cd]["depth"], current_depth + 1
                )

                if current_depth + 1 < depth and norm_cd not in visited:
                    queue.append((norm_cd, current_depth + 1))

    db.close()

    # Format results
    results = []
    for norm, info in sorted(network.items(), key=lambda x: (x[1]["depth"], x[0])):
        results.append(
            {
                "name": info.get("name_raw", norm),
                "name_normalized": norm,
                "defendant_type": info.get("defendant_type", "unknown"),
                "depth": info["depth"],
                "shared_actions": sorted(info["actions"]),
                "action_count": len(info["actions"]),
            }
        )

    _log(name, "sec_enforcement_network", len(results))

    if write_output(results, args, summary=f"co-defendant network for '{name}'"):
        return

    if not results:
        print(f"No co-defendant network found for '{name}'.")
        return

    print(f"Co-defendant network for '{name}' (depth {depth}):\n")
    for r in results:
        indent = "  " * r["depth"]
        print(
            f"{indent}[{r['defendant_type']:7s}] {r['name']} "
            f"({r['action_count']} shared actions, depth {r['depth']})"
        )
        for a in r["shared_actions"][:3]:
            print(f"{indent}           {a}")
        if len(r["shared_actions"]) > 3:
            print(f"{indent}           ... and {len(r['shared_actions']) - 3} more")


# ---------------------------------------------------------------------------
# repeat-offenders
# ---------------------------------------------------------------------------


def cmd_repeat_offenders(args):
    """Find defendants appearing in multiple enforcement actions."""
    db = get_db()

    rows = db.execute(
        """SELECT ed.name_normalized, ed.defendant_type,
                  GROUP_CONCAT(DISTINCT ea.release_number) as releases,
                  GROUP_CONCAT(DISTINCT ea.source_type) as source_types,
                  COUNT(DISTINCT ed.action_id) as action_count,
                  MIN(ea.date_published) as first_action,
                  MAX(ea.date_published) as last_action,
                  MAX(ed.name_raw) as name_display
           FROM enforcement_defendants ed
           JOIN enforcement_actions ea ON ed.action_id = ea.id
           GROUP BY ed.name_normalized
           HAVING action_count >= ?
           ORDER BY action_count DESC, ed.name_normalized""",
        (args.min_actions,),
    ).fetchall()

    results = [dict(r) for r in rows]
    db.close()

    if write_output(results, args, summary=f"repeat offenders (min {args.min_actions})"):
        return

    if not results:
        print(f"No defendants found with {args.min_actions}+ enforcement actions.")
        return

    print(f"Repeat offenders ({len(results)} defendants with {args.min_actions}+ actions):\n")
    for r in results:
        span = ""
        if r["first_action"] != r["last_action"]:
            span = f" ({r['first_action']} to {r['last_action']})"
        print(
            f"  {r['name_display']:45s} [{r['defendant_type']:7s}] "
            f"{r['action_count']} actions{span}"
        )
        releases = r["releases"].split(",")
        for rel in releases[:5]:
            print(f"    {rel}")
        if len(releases) > 5:
            print(f"    ... and {len(releases) - 5} more")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def cmd_stats(args):
    """Show database summary statistics."""
    db = get_db()
    results = {}

    # Totals
    results["total_actions"] = db.execute(
        "SELECT COUNT(*) FROM enforcement_actions"
    ).fetchone()[0]
    results["total_defendants"] = db.execute(
        "SELECT COUNT(*) FROM enforcement_defendants"
    ).fetchone()[0]

    # By source
    rows = db.execute(
        """SELECT source_type, COUNT(*) as cnt
           FROM enforcement_actions GROUP BY source_type"""
    ).fetchall()
    results["by_source"] = {r["source_type"]: r["cnt"] for r in rows}

    # By year
    if args.by_year:
        rows = db.execute(
            """SELECT SUBSTR(date_published, 1, 4) as year, COUNT(*) as cnt
               FROM enforcement_actions GROUP BY year ORDER BY year DESC"""
        ).fetchall()
        results["by_year"] = {r["year"]: r["cnt"] for r in rows}

    # By defendant type
    rows = db.execute(
        """SELECT defendant_type, COUNT(*) as cnt
           FROM enforcement_defendants GROUP BY defendant_type ORDER BY cnt DESC"""
    ).fetchall()
    results["by_defendant_type"] = {r["defendant_type"]: r["cnt"] for r in rows}

    # Repeat offenders
    results["repeat_offenders"] = db.execute(
        """SELECT COUNT(*) FROM (
            SELECT name_normalized FROM enforcement_defendants
            GROUP BY name_normalized HAVING COUNT(DISTINCT action_id) >= 2
        )"""
    ).fetchone()[0]

    # Date range
    row = db.execute(
        "SELECT MIN(date_published), MAX(date_published) FROM enforcement_actions"
    ).fetchone()
    results["date_range"] = [row[0], row[1]]

    db.close()

    if write_output(results, args, summary="SEC enforcement stats"):
        return

    print(f"SEC Enforcement Database")
    print(f"  Actions:          {results['total_actions']:,}")
    for src, cnt in sorted(results["by_source"].items()):
        print(f"    {src:12s} {cnt:,}")
    print(f"  Defendants:       {results['total_defendants']:,}")
    for dtype, cnt in sorted(results["by_defendant_type"].items(), key=lambda x: -x[1]):
        print(f"    {dtype or 'null':12s} {cnt:,}")
    print(f"  Repeat offenders: {results['repeat_offenders']:,}")
    print(f"  Date range:       {results['date_range'][0]} to {results['date_range'][1]}")

    if args.by_year and "by_year" in results:
        print(f"\n  Actions by year:")
        for year, cnt in sorted(results["by_year"].items(), reverse=True):
            print(f"    {year}: {cnt:,}")


# ---------------------------------------------------------------------------
# cross-ref
# ---------------------------------------------------------------------------


def cmd_cross_ref(args):
    """Cross-reference enforcement defendants against investigation.db and registry.db."""
    db = get_db()
    threshold = args.threshold
    dry_run = args.dry_run

    # Gather names to check from investigation.db and registry.db
    names_to_check = _gather_investigation_names()
    print(f"Gathered {len(names_to_check)} names from investigation.db and registry.db")

    # Load all enforcement defendants
    all_defendants = db.execute(
        """SELECT id, action_id, name_raw, name_normalized, defendant_type
           FROM enforcement_defendants"""
    ).fetchall()

    # Build normalized name index
    def_by_norm = defaultdict(list)
    for d in all_defendants:
        def_by_norm[d["name_normalized"]].append(d)

    matches = []
    for check_name, (source, source_id) in names_to_check.items():
        try:
            from tools.entity_resolution import normalize_person_name, normalize_entity_name
        except ImportError:
            try:
                from entity_resolution import normalize_person_name, normalize_entity_name
            except ImportError:
                normalize_person_name = normalize_entity_name = lambda x: x.lower().strip()

        norm_p = normalize_person_name(check_name)
        norm_e = normalize_entity_name(check_name)

        # Exact match (deduplicate across person/entity normalization)
        seen_def_ids = set()
        for norm in [norm_p, norm_e]:
            if norm in def_by_norm:
                for d in def_by_norm[norm]:
                    if d["id"] in seen_def_ids:
                        continue
                    seen_def_ids.add(d["id"])
                    action = db.execute(
                        "SELECT release_number, source_type, date_published, respondent_text "
                        "FROM enforcement_actions WHERE id = ?",
                        (d["action_id"],),
                    ).fetchone()
                    matches.append(
                        {
                            "check_name": check_name,
                            "check_source": source,
                            "check_source_id": source_id,
                            "defendant_id": d["id"],
                            "defendant_name": d["name_raw"],
                            "defendant_type": d["defendant_type"],
                            "match_type": "exact",
                            "match_score": 1.0,
                            "release_number": action["release_number"] if action else None,
                            "source_type": action["source_type"] if action else None,
                            "date_published": action["date_published"] if action else None,
                            "respondent_text": action["respondent_text"][:200] if action else None,
                        }
                    )

    # Store matches (unless dry run)
    if not dry_run and matches:
        for m in matches:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO enforcement_matches
                       (defendant_id, match_source, match_source_id, match_name, match_type, match_score)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        m["defendant_id"],
                        m["check_source"],
                        m["check_source_id"],
                        m["check_name"],
                        m["match_type"],
                        m["match_score"],
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        db.commit()

    # Auto-lead generation
    if args.auto_leads and matches and not dry_run:
        leads_created = _create_enforcement_leads(matches)
        print(f"Created {leads_created} investigation leads")

    db.close()

    if write_output(matches, args, summary=f"SEC enforcement cross-ref"):
        return

    if not matches:
        print("No cross-reference matches found.")
        return

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Found {len(matches)} cross-reference matches:\n")
    for m in matches:
        print(
            f"  {m['check_name']} ({m['check_source']})"
            f" <-> {m['defendant_name']} ({m['defendant_type']})"
        )
        print(
            f"    {m['release_number']} [{m['source_type']}] {m['date_published']}"
            f" — score: {m['match_score']}"
        )
        print(f"    {m.get('respondent_text', '')[:100]}")
        print()


def _gather_investigation_names():
    """Gather entity/person names from investigation.db and registry.db."""
    names = {}  # name -> (source, source_id)

    # Investigation.db entities
    if INVESTIGATION_DB.exists():
        try:
            inv_db = sqlite3.connect(str(INVESTIGATION_DB))
            inv_db.row_factory = sqlite3.Row

            for r in inv_db.execute("SELECT id, name FROM entities").fetchall():
                if r["name"] and len(r["name"]) > 2:
                    names[r["name"]] = ("investigation_entity", r["id"])

            # Connections
            for col in ["person_a", "person_b"]:
                for r in inv_db.execute(
                    f"SELECT DISTINCT {col} as name FROM connections WHERE {col} IS NOT NULL"
                ).fetchall():
                    if r["name"] and len(r["name"]) > 2 and r["name"] not in names:
                        names[r["name"]] = ("investigation_connection", None)

            # Entity roles
            for r in inv_db.execute(
                "SELECT DISTINCT person_name as name FROM entity_roles WHERE person_name IS NOT NULL"
            ).fetchall():
                if r["name"] and len(r["name"]) > 2 and r["name"] not in names:
                    names[r["name"]] = ("investigation_role", None)

            inv_db.close()
        except Exception as e:
            print(f"  Warning: could not read investigation.db: {e}", file=sys.stderr)

    # Registry.db officers
    if REGISTRY_DB.exists():
        try:
            reg_db = sqlite3.connect(str(REGISTRY_DB))
            reg_db.row_factory = sqlite3.Row

            for r in reg_db.execute(
                "SELECT id, officer_name FROM registry_officers WHERE officer_name IS NOT NULL LIMIT 50000"
            ).fetchall():
                if r["officer_name"] and len(r["officer_name"]) > 2 and r["officer_name"] not in names:
                    names[r["officer_name"]] = ("registry_officer", r["id"])

            reg_db.close()
        except Exception as e:
            print(f"  Warning: could not read registry.db: {e}", file=sys.stderr)

    return names


def _create_enforcement_leads(matches):
    """Create investigation leads from enforcement cross-reference matches."""
    if not INVESTIGATION_DB.exists():
        return 0

    try:
        from tools.lead_tracker import get_db as get_inv_db
    except ImportError:
        try:
            from lead_tracker import get_db as get_inv_db
        except ImportError:
            return 0

    inv_db = get_inv_db()
    created = 0

    for m in matches:
        if m["match_score"] < 0.85:
            continue

        title = (
            f"SEC enforcement match: {m['defendant_name']} "
            f"({m['release_number']}, {m['source_type']})"
        )

        # Check for existing lead with similar title
        existing = inv_db.execute(
            "SELECT id FROM leads WHERE title LIKE ? LIMIT 1",
            (f"%{m['defendant_name'][:30]}%enforcement%",),
        ).fetchone()

        if existing:
            continue

        priority = "high" if m["match_score"] >= 0.95 else "medium"
        notes = (
            f"Cross-reference match (score: {m['match_score']:.0%})\n"
            f"Investigation source: {m['check_source']} — {m['check_name']}\n"
            f"SEC action: {m['release_number']} ({m['date_published']})\n"
            f"Respondents: {m.get('respondent_text', '')[:300]}"
        )

        try:
            inv_db.execute(
                """INSERT INTO leads (title, category, priority, status, source, notes, created_at)
                   VALUES (?, 'enforcement', ?, 'pending_triage', 'agent:sec_enforcement_crossref', ?, datetime('now'))""",
                (title, priority, notes),
            )
            created += 1
        except Exception:
            pass

        if created >= 100:
            break

    inv_db.commit()
    inv_db.close()
    return created


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Query SEC enforcement actions")
    sub = parser.add_subparsers(dest="command")

    # search
    p = sub.add_parser("search", help="FTS5 search across enforcement actions")
    p.add_argument("query", help="Search query")
    p.add_argument("--source", choices=["litigation", "admin", "aaer"])
    p.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", help="End date (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # defendant
    p = sub.add_parser("defendant", help="Find actions involving a defendant")
    p.add_argument("name", help="Defendant name")
    p.add_argument("--fuzzy", action="store_true", help="Use fuzzy matching")
    p.add_argument("--threshold", type=int, default=80, help="Fuzzy threshold (0-100)")
    add_output_args(p)

    # action
    p = sub.add_parser("action", help="Show details of a specific action")
    p.add_argument("release_number", help="Release number (e.g. LR-26503)")
    add_output_args(p)

    # co-defendants
    p = sub.add_parser("co-defendants", help="List co-defendants in an action")
    p.add_argument("release_number", help="Release number")
    add_output_args(p)

    # network
    p = sub.add_parser("network", help="Co-defendant network analysis")
    p.add_argument("name", help="Starting person/entity name")
    p.add_argument("--depth", type=int, default=1, help="Hop depth (default: 1, max: 3)")
    add_output_args(p)

    # repeat-offenders
    p = sub.add_parser("repeat-offenders", help="Find multi-action defendants")
    p.add_argument("--min-actions", type=int, default=2, help="Minimum action count")
    add_output_args(p)

    # stats
    p = sub.add_parser("stats", help="Database statistics")
    p.add_argument("--by-year", action="store_true", help="Breakdown by year")
    add_output_args(p)

    # cross-ref
    p = sub.add_parser("cross-ref", help="Match against investigation.db and registry.db")
    p.add_argument("--threshold", type=int, default=85, help="Match threshold (0-100)")
    p.add_argument("--auto-leads", action="store_true", help="Generate investigation leads")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    add_output_args(p)

    args = parser.parse_args()

    commands = {
        "search": cmd_search,
        "defendant": cmd_defendant,
        "action": cmd_action,
        "co-defendants": cmd_co_defendants,
        "network": cmd_network,
        "repeat-offenders": cmd_repeat_offenders,
        "stats": cmd_stats,
        "cross-ref": cmd_cross_ref,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
