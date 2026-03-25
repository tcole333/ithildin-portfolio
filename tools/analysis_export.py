#!/usr/bin/env python3
"""
Bulk data extraction for analysis skills in OSINT investigations.

Provides data export functions that analysis agents use to load investigation
data for graph analysis, timeline correlation, pattern detection, etc.
Tracks analysis runs for change detection.

Part of investigation.db.

Usage:
    python tools/analysis_export.py connections-graph [--output FILE]
    python tools/analysis_export.py findings-dump [--thread-id N] [--min-confidence medium] [--output FILE]
    python tools/analysis_export.py timeline-export [--start DATE] [--end DATE] [--output FILE]
    python tools/analysis_export.py entity-network [--output FILE]
    python tools/analysis_export.py coverage-matrix [--top N] [--output FILE]
    python tools/analysis_export.py thread-summary [--thread-id N] [--output FILE]
    python tools/analysis_export.py analysis-state [--output FILE]
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

try:
    from tools.investigation_context import get_active_profile_id
except ImportError:
    try:
        from investigation_context import get_active_profile_id
    except ImportError:
        def get_active_profile_id():
            return ""

CONFIDENCE_ORDER = {"confirmed": 4, "high": 3, "medium": 2, "low": 1, "unverified": 0}


def _resolve_profile(profile_id=None, all_profiles=False):
    """Resolve profile_id: explicit > active profile > None."""
    if all_profiles:
        return None
    if profile_id is not None:
        return profile_id
    return get_active_profile_id() or None


# ── Schema ────────────────────────────────────────────────────

def _ensure_analysis_schema(db):
    """Create analysis tracking tables if they don't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            findings_at_start INTEGER,
            connections_at_start INTEGER,
            findings_created INTEGER DEFAULT 0,
            hypotheses_created INTEGER DEFAULT 0,
            leads_created INTEGER DEFAULT 0,
            tags_created INTEGER DEFAULT 0,
            report_path TEXT,
            status TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_runs_skill ON analysis_runs(skill_name);
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_status ON analysis_runs(status);
    """)


def get_analysis_db():
    """Get DB connection with analysis schema ensured."""
    db = get_db()
    _ensure_analysis_schema(db)
    return db


# ── Analysis Run Tracking ────────────────────────────────────

def start_analysis_run(skill_name, profile_id=None):
    """Start an analysis run and return its ID."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id)
    if resolved:
        findings_count = db.execute("SELECT COUNT(*) FROM findings WHERE profile_id = ?", (resolved,)).fetchone()[0]
        connections_count = db.execute("SELECT COUNT(*) FROM connections WHERE profile_id = ?", (resolved,)).fetchone()[0]
    else:
        findings_count = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        connections_count = db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]

    cursor = db.execute("""
        INSERT INTO analysis_runs (skill_name, findings_at_start, connections_at_start)
        VALUES (?, ?, ?)
    """, (skill_name, findings_count, connections_count))
    run_id = cursor.lastrowid
    db.commit()
    db.close()
    return run_id


def complete_analysis_run(run_id, findings_created=0, hypotheses_created=0,
                          leads_created=0, tags_created=0, report_path=None, notes=None):
    """Mark an analysis run as completed with results."""
    db = get_analysis_db()
    db.execute("""
        UPDATE analysis_runs SET
            completed_at = CURRENT_TIMESTAMP, status = 'completed',
            findings_created = ?, hypotheses_created = ?,
            leads_created = ?, tags_created = ?,
            report_path = ?, notes = ?
        WHERE id = ?
    """, (findings_created, hypotheses_created, leads_created, tags_created,
          report_path, notes, run_id))
    db.commit()
    db.close()


def fail_analysis_run(run_id, error_msg):
    """Mark an analysis run as failed."""
    db = get_analysis_db()
    db.execute("""
        UPDATE analysis_runs SET
            completed_at = CURRENT_TIMESTAMP, status = 'failed', notes = ?
        WHERE id = ?
    """, (error_msg, run_id))
    db.commit()
    db.close()


# ── Export Functions ────────────────────────────────────────

def export_connections_graph(profile_id=None, all_profiles=False):
    """Export all connections as an edge list with metadata."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    if resolved:
        rows = db.execute("""
            SELECT c.id, c.person_a, c.person_b, c.relationship_type,
                   c.description, c.strength, c.date_range, c.verification_status,
                   c.created_at
            FROM connections c
            WHERE c.profile_id = ?
            ORDER BY c.id
        """, (resolved,)).fetchall()
    else:
        rows = db.execute("""
            SELECT c.id, c.person_a, c.person_b, c.relationship_type,
                   c.description, c.strength, c.date_range, c.verification_status,
                   c.created_at
            FROM connections c
            ORDER BY c.id
        """).fetchall()

    edges = [dict(r) for r in rows]

    # Also get node metadata from findings
    if resolved:
        targets = db.execute("""
            SELECT target_name, COUNT(*) as finding_count,
                   thread_id, MAX(confidence) as max_confidence
            FROM findings
            WHERE profile_id = ?
            GROUP BY target_name
        """, (resolved,)).fetchall()
    else:
        targets = db.execute("""
            SELECT target_name, COUNT(*) as finding_count,
                   thread_id, MAX(confidence) as max_confidence
            FROM findings
            GROUP BY target_name
        """).fetchall()
    node_meta = {r["target_name"]: dict(r) for r in targets}

    db.close()
    return {
        "edges": edges,
        "edge_count": len(edges),
        "node_metadata": node_meta,
        "node_count": len(node_meta),
    }


def export_findings_dump(thread_id=None, min_confidence=None, profile_id=None,
                         all_profiles=False):
    """Export all findings with optional filters."""
    db = get_analysis_db()
    conditions = []
    params = []

    resolved = _resolve_profile(profile_id, all_profiles)
    if resolved:
        conditions.append("f.profile_id = ?")
        params.append(resolved)

    if thread_id:
        conditions.append("f.thread_id = ?")
        params.append(int(thread_id))
    if min_confidence:
        min_val = CONFIDENCE_ORDER.get(min_confidence, 0)
        confidence_list = [c for c, v in CONFIDENCE_ORDER.items() if v >= min_val]
        placeholders = ",".join("?" for _ in confidence_list)
        conditions.append(f"f.confidence IN ({placeholders})")
        params.extend(confidence_list)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(f"""
        SELECT f.id, f.target_name, f.finding_type, f.summary, f.detail,
               f.confidence, f.date_of_event, f.thread_id,
               f.claim_type, f.verification_status, f.created_at
        FROM findings f
        {where}
        ORDER BY f.id
    """, params).fetchall()

    findings = [dict(r) for r in rows]
    db.close()
    return {"findings": findings, "count": len(findings)}


def export_timeline(start_date=None, end_date=None, profile_id=None,
                    all_profiles=False):
    """Export findings and events on a timeline."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    # Findings with dates
    f_conditions = ["f.date_of_event IS NOT NULL"]
    f_params = []
    if resolved:
        f_conditions.append("f.profile_id = ?")
        f_params.append(resolved)
    if start_date:
        f_conditions.append("f.date_of_event >= ?")
        f_params.append(start_date)
    if end_date:
        f_conditions.append("f.date_of_event <= ?")
        f_params.append(end_date)

    findings = [dict(r) for r in db.execute(f"""
        SELECT f.id, f.target_name, f.summary, f.date_of_event,
               f.confidence, f.thread_id, f.finding_type
        FROM findings f
        WHERE {' AND '.join(f_conditions)}
        ORDER BY f.date_of_event
    """, f_params).fetchall()]

    # Events
    e_conditions = []
    e_params = []
    if start_date:
        e_conditions.append("event_date >= ?")
        e_params.append(start_date)
    if end_date:
        e_conditions.append("event_date <= ?")
        e_params.append(end_date)

    e_where = f"WHERE {' AND '.join(e_conditions)}" if e_conditions else ""
    try:
        events = [dict(r) for r in db.execute(f"""
            SELECT id, event_date, event_name, category, description, relevance
            FROM event_timeline
            {e_where}
            ORDER BY event_date
        """, e_params).fetchall()]
    except sqlite3.OperationalError:
        events = []  # table might not exist yet

    # Also get findings WITHOUT dates but with created_at as proxy
    if resolved:
        undated = db.execute(
            "SELECT COUNT(*) as n FROM findings WHERE date_of_event IS NULL AND profile_id = ?",
            (resolved,)
        ).fetchone()["n"]
    else:
        undated = db.execute(
            "SELECT COUNT(*) as n FROM findings WHERE date_of_event IS NULL"
        ).fetchone()["n"]

    db.close()
    return {
        "dated_findings": findings,
        "events": events,
        "undated_finding_count": undated,
        "dated_finding_count": len(findings),
        "event_count": len(events),
    }


def export_entity_network(profile_id=None, all_profiles=False):
    """Export entity registry with roles, relations, and addresses.

    Entities are shared across profiles, but when profile-scoped, only
    returns entities referenced by findings in that profile.
    """
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    if resolved:
        # Scope to entities referenced by profile findings via person names
        entities = [dict(r) for r in db.execute("""
            SELECT DISTINCT e.id, e.name, e.entity_type, e.jurisdiction, e.status,
                   e.ein, e.address, e.source, e.notes
            FROM entities e
            JOIN entity_roles er ON er.entity_id = e.id
            WHERE er.person_name IN (
                SELECT DISTINCT target_name FROM findings WHERE profile_id = ?
            )
            ORDER BY e.id
        """, (resolved,)).fetchall()]
        entity_ids = {e["id"] for e in entities}

        roles = [dict(r) for r in db.execute("""
            SELECT entity_id, person_name, role, date_start, date_end, source
            FROM entity_roles WHERE entity_id IN (
                SELECT DISTINCT e.id FROM entities e
                JOIN entity_roles er ON er.entity_id = e.id
                WHERE er.person_name IN (
                    SELECT DISTINCT target_name FROM findings WHERE profile_id = ?
                )
            )
            ORDER BY entity_id
        """, (resolved,)).fetchall()]

        relations = [dict(r) for r in db.execute("""
            SELECT entity_a_id, entity_b_id, relation_type, description, source
            FROM entity_relations
            WHERE entity_a_id IN ({ids}) OR entity_b_id IN ({ids})
            ORDER BY entity_a_id
        """.format(ids=",".join("?" for _ in entity_ids)),
            list(entity_ids) + list(entity_ids)
        ).fetchall()] if entity_ids else []

        addresses = [dict(r) for r in db.execute("""
            SELECT entity_id, address, address_type, date_observed, source
            FROM entity_addresses
            WHERE entity_id IN ({ids})
            ORDER BY entity_id
        """.format(ids=",".join("?" for _ in entity_ids)),
            list(entity_ids)
        ).fetchall()] if entity_ids else []
    else:
        entities = [dict(r) for r in db.execute("""
            SELECT id, name, entity_type, jurisdiction, status, ein, address, source, notes
            FROM entities ORDER BY id
        """).fetchall()]

        roles = [dict(r) for r in db.execute("""
            SELECT entity_id, person_name, role, date_start, date_end, source
            FROM entity_roles ORDER BY entity_id
        """).fetchall()]

        relations = [dict(r) for r in db.execute("""
            SELECT entity_a_id, entity_b_id, relation_type, description, source
            FROM entity_relations ORDER BY entity_a_id
        """).fetchall()]

        addresses = [dict(r) for r in db.execute("""
            SELECT entity_id, address, address_type, date_observed, source
            FROM entity_addresses ORDER BY entity_id
        """).fetchall()]

    db.close()
    return {
        "entities": entities,
        "roles": roles,
        "relations": relations,
        "addresses": addresses,
        "entity_count": len(entities),
        "role_count": len(roles),
        "relation_count": len(relations),
    }


def export_coverage_matrix(top_n=50, profile_id=None, all_profiles=False):
    """Export coverage matrix: findings per target, with gaps highlighted."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    f_cond = " WHERE profile_id = ?" if resolved else ""
    c_cond = " WHERE profile_id = ?" if resolved else ""
    f_params = [resolved] if resolved else []
    c_params = [resolved] if resolved else []

    # Top targets by finding count
    targets = db.execute(f"""
        SELECT target_name, COUNT(*) as finding_count,
               thread_id,
               SUM(CASE WHEN confidence IN ('confirmed','high') THEN 1 ELSE 0 END) as high_conf,
               SUM(CASE WHEN confidence IN ('low','unverified') THEN 1 ELSE 0 END) as low_conf,
               MIN(created_at) as first_finding,
               MAX(created_at) as last_finding
        FROM findings{f_cond}
        GROUP BY target_name
        ORDER BY finding_count DESC
        LIMIT ?
    """, f_params + [top_n]).fetchall()

    # Connection counts per target
    conn_counts = {}
    for row in db.execute(f"""
        SELECT name, COUNT(*) as conn_count FROM (
            SELECT person_a as name FROM connections{c_cond}
            UNION ALL
            SELECT person_b as name FROM connections{c_cond}
        ) GROUP BY name
    """, c_params + c_params):
        conn_counts[row["name"]] = row["conn_count"]

    matrix = []
    for t in targets:
        name = t["target_name"]
        matrix.append({
            "target": name,
            "findings": t["finding_count"],
            "high_confidence": t["high_conf"],
            "low_confidence": t["low_conf"],
            "connections": conn_counts.get(name, 0),
            "thread_id": t["thread_id"],
            "first_finding": t["first_finding"],
            "last_finding": t["last_finding"],
        })

    # Also find high-connectivity nodes with zero/few findings
    gaps = []
    for name, conn_count in sorted(conn_counts.items(), key=lambda x: x[1], reverse=True)[:100]:
        if resolved:
            finding_count = db.execute(
                "SELECT COUNT(*) FROM findings WHERE target_name = ? AND profile_id = ?",
                (name, resolved)
            ).fetchone()[0]
        else:
            finding_count = db.execute(
                "SELECT COUNT(*) FROM findings WHERE target_name = ?", (name,)
            ).fetchone()[0]
        if conn_count >= 3 and finding_count <= 2:
            gaps.append({
                "target": name,
                "connections": conn_count,
                "findings": finding_count,
                "gap_ratio": round(conn_count / max(finding_count, 1), 1),
            })

    gaps.sort(key=lambda x: x["gap_ratio"], reverse=True)

    db.close()
    return {
        "coverage": matrix,
        "gaps": gaps[:30],
        "total_targets": len(matrix),
        "total_gaps": len(gaps),
    }


def export_thread_summary(thread_id=None, profile_id=None, all_profiles=False):
    """Export per-thread summary statistics."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    conditions = []
    params = []
    if resolved:
        conditions.append("f.profile_id = ?")
        params.append(resolved)
    if thread_id:
        conditions.append("f.thread_id = ?")
        params.append(int(thread_id))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    threads = {}
    rows = db.execute(f"""
        SELECT f.thread_id,
               COUNT(*) as finding_count,
               COUNT(DISTINCT f.target_name) as unique_targets,
               SUM(CASE WHEN f.confidence IN ('confirmed','high') THEN 1 ELSE 0 END) as high_conf,
               SUM(CASE WHEN f.verification_status = 'verified' THEN 1 ELSE 0 END) as verified,
               MIN(f.created_at) as first_finding,
               MAX(f.created_at) as last_finding
        FROM findings f
        {where}
        GROUP BY f.thread_id
    """, params).fetchall()

    for r in rows:
        tid = r["thread_id"] or 0
        threads[tid] = dict(r)

    # Thread names
    thread_names = {}
    for row in db.execute("SELECT id, title FROM investigation_threads"):
        thread_names[row["id"]] = row["title"]

    for tid in threads:
        threads[tid]["thread_name"] = thread_names.get(tid, "Unassigned")

    # Connection counts per thread (approximate via finding targets)
    profile_filter = " AND profile_id = ?" if resolved else ""
    for tid, data in threads.items():
        f_query_params = [tid] + ([resolved] if resolved else [])
        target_list = [r[0] for r in db.execute(
            f"SELECT DISTINCT target_name FROM findings WHERE thread_id = ?{profile_filter}",
            f_query_params
        ).fetchall()]
        if target_list:
            placeholders = ",".join("?" for _ in target_list)
            conn_cond = f" AND profile_id = ?" if resolved else ""
            conn_params = target_list + target_list + ([resolved] if resolved else [])
            conn_count = db.execute(f"""
                SELECT COUNT(DISTINCT id) FROM connections
                WHERE (person_a IN ({placeholders}) OR person_b IN ({placeholders})){conn_cond}
            """, conn_params).fetchone()[0]
        else:
            conn_count = 0
        threads[tid]["connections"] = conn_count

    # Lead counts per thread
    for tid, data in threads.items():
        leads = db.execute("""
            SELECT status, COUNT(*) as n FROM leads WHERE thread_id = ?
            GROUP BY status
        """, (tid,)).fetchall()
        data["leads"] = {r["status"]: r["n"] for r in leads}

    db.close()
    return {"threads": threads}


def export_pillar_dump():
    """Export institutional pillars, career arcs, persons, events, and scores."""
    db = get_analysis_db()

    try:
        pillars = [dict(r) for r in db.execute(
            "SELECT * FROM institutional_pillars ORDER BY pillar_type, name"
        ).fetchall()]
        persons = [dict(r) for r in db.execute(
            "SELECT * FROM persons ORDER BY canonical_name"
        ).fetchall()]
        arcs = [dict(r) for r in db.execute("""
            SELECT ca.*, ip.name as pillar_name, ip.pillar_type
            FROM career_arcs ca
            JOIN institutional_pillars ip ON ca.pillar_id = ip.id
            ORDER BY ca.person_name, COALESCE(ca.date_start, '0000')
        """).fetchall()]
        events = [dict(r) for r in db.execute("""
            SELECT pe.*, ip.name as pillar_name
            FROM pillar_events pe
            JOIN institutional_pillars ip ON pe.pillar_id = ip.id
            ORDER BY pe.event_date
        """).fetchall()]
        scores = [dict(r) for r in db.execute(
            "SELECT * FROM pillar_scores ORDER BY score_value DESC"
        ).fetchall()]
    except sqlite3.OperationalError:
        return {"error": "Pillar tables not found. Run pillar_tracker.py seed first.",
                "pillar_count": 0, "person_count": 0, "arc_count": 0,
                "event_count": 0, "score_count": 0}

    db.close()
    return {
        "pillars": pillars,
        "persons": persons,
        "career_arcs": arcs,
        "pillar_events": events,
        "pillar_scores": scores,
        "pillar_count": len(pillars),
        "person_count": len(persons),
        "arc_count": len(arcs),
        "event_count": len(events),
        "score_count": len(scores),
    }


def export_analysis_state(profile_id=None, all_profiles=False):
    """Export current analysis run history and change detection state."""
    db = get_analysis_db()
    resolved = _resolve_profile(profile_id, all_profiles)

    runs = [dict(r) for r in db.execute("""
        SELECT * FROM analysis_runs ORDER BY started_at DESC LIMIT 50
    """).fetchall()]

    # Current counts
    if resolved:
        findings_count = db.execute("SELECT COUNT(*) FROM findings WHERE profile_id = ?", (resolved,)).fetchone()[0]
        connections_count = db.execute("SELECT COUNT(*) FROM connections WHERE profile_id = ?", (resolved,)).fetchone()[0]
    else:
        findings_count = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        connections_count = db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]

    try:
        hypotheses_count = db.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    except sqlite3.OperationalError:
        hypotheses_count = 0

    try:
        tags_count = db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    except sqlite3.OperationalError:
        tags_count = 0

    # Changes since last run of each skill
    changes_since = {}
    for skill in ["analyze-network", "generate-hunches", "timeline-analysis", "systemic-analysis"]:
        last_run = db.execute("""
            SELECT findings_at_start, connections_at_start, completed_at
            FROM analysis_runs WHERE skill_name = ? AND status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
        """, (skill,)).fetchone()

        if last_run:
            changes_since[skill] = {
                "last_run": last_run["completed_at"],
                "new_findings": findings_count - (last_run["findings_at_start"] or 0),
                "new_connections": connections_count - (last_run["connections_at_start"] or 0),
            }
        else:
            changes_since[skill] = {
                "last_run": None,
                "new_findings": findings_count,
                "new_connections": connections_count,
            }

    db.close()
    return {
        "runs": runs,
        "current_counts": {
            "findings": findings_count,
            "connections": connections_count,
            "hypotheses": hypotheses_count,
            "tags": tags_count,
        },
        "changes_since_last": changes_since,
    }


# ── CLI ────────────────────────────────────────────────────

def _add_profile_args(subparser):
    """Add --profile and --all-profiles to a subparser."""
    subparser.add_argument("--profile", default=None, help="Investigation profile (default: active)")
    subparser.add_argument("--all-profiles", action="store_true", help="Include all profiles")


def main():
    parser = argparse.ArgumentParser(description="Bulk data extraction for analysis skills")
    sub = parser.add_subparsers(dest="command")

    # connections-graph
    p_cg = sub.add_parser("connections-graph", help="Export connections as edge list")
    _add_profile_args(p_cg)
    add_output_args(p_cg)

    # findings-dump
    p_fd = sub.add_parser("findings-dump", help="Export all findings")
    p_fd.add_argument("--thread-id", type=int)
    p_fd.add_argument("--min-confidence", choices=list(CONFIDENCE_ORDER.keys()))
    _add_profile_args(p_fd)
    add_output_args(p_fd)

    # timeline-export
    p_te = sub.add_parser("timeline-export", help="Export timeline data")
    p_te.add_argument("--start", help="Start date YYYY-MM-DD")
    p_te.add_argument("--end", help="End date YYYY-MM-DD")
    _add_profile_args(p_te)
    add_output_args(p_te)

    # entity-network
    p_en = sub.add_parser("entity-network", help="Export entity registry")
    _add_profile_args(p_en)
    add_output_args(p_en)

    # coverage-matrix
    p_cm = sub.add_parser("coverage-matrix", help="Export coverage matrix")
    p_cm.add_argument("--top", type=int, default=50)
    _add_profile_args(p_cm)
    add_output_args(p_cm)

    # thread-summary
    p_ts = sub.add_parser("thread-summary", help="Export thread summaries")
    p_ts.add_argument("--thread-id", type=int)
    _add_profile_args(p_ts)
    add_output_args(p_ts)

    # analysis-state
    p_as = sub.add_parser("analysis-state", help="Export analysis run history")
    _add_profile_args(p_as)
    add_output_args(p_as)

    # pillar-dump
    p_pd = sub.add_parser("pillar-dump", help="Export institutional pillars, career arcs, and scores")
    add_output_args(p_pd)

    args = parser.parse_args()
    p_id = getattr(args, "profile", None)
    p_all = getattr(args, "all_profiles", False)

    if args.command == "connections-graph":
        result = export_connections_graph(profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary="connections graph"):
            return
        print(f"Connections Graph: {result['edge_count']} edges, {result['node_count']} nodes")
        for e in result["edges"][:10]:
            print(f"  {e['person_a']} --[{e['relationship_type']}]--> {e['person_b']}")
        if result["edge_count"] > 10:
            print(f"  ... ({result['edge_count'] - 10} more)")

    elif args.command == "findings-dump":
        result = export_findings_dump(
            thread_id=args.thread_id,
            min_confidence=args.min_confidence,
            profile_id=p_id, all_profiles=p_all,
        )
        if write_output(result, args, summary=f"findings dump ({result['count']})"):
            return
        print(f"Findings Dump: {result['count']} findings")

    elif args.command == "timeline-export":
        result = export_timeline(start_date=args.start, end_date=args.end,
                                 profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary="timeline export"):
            return
        print(f"Timeline Export:")
        print(f"  {result['dated_finding_count']} dated findings")
        print(f"  {result['event_count']} events")
        print(f"  {result['undated_finding_count']} undated findings")

    elif args.command == "entity-network":
        result = export_entity_network(profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary="entity network"):
            return
        print(f"Entity Network:")
        print(f"  {result['entity_count']} entities")
        print(f"  {result['role_count']} roles")
        print(f"  {result['relation_count']} relations")

    elif args.command == "coverage-matrix":
        result = export_coverage_matrix(top_n=args.top, profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary=f"coverage matrix (top {args.top})"):
            return
        print(f"Coverage Matrix (top {args.top}):")
        print(f"{'Target':<40} {'Findings':>8} {'Hi-Conf':>7} {'Conns':>5}")
        print("-" * 65)
        for c in result["coverage"][:20]:
            print(f"{c['target']:<40} {c['findings']:>8} {c['high_confidence']:>7} {c['connections']:>5}")
        if result["gaps"]:
            print(f"\nCoverage Gaps (high connectivity, low findings):")
            for g in result["gaps"][:10]:
                print(f"  {g['target']:<40} conns={g['connections']} findings={g['findings']}")

    elif args.command == "thread-summary":
        result = export_thread_summary(thread_id=args.thread_id,
                                       profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary="thread summary"):
            return
        print("Thread Summary:")
        for tid, data in sorted(result["threads"].items()):
            print(f"\n  Thread {tid}: {data['thread_name']}")
            print(f"    Findings: {data['finding_count']} ({data['unique_targets']} targets)")
            print(f"    High confidence: {data['high_conf']}")
            print(f"    Verified: {data['verified']}")
            print(f"    Connections: {data['connections']}")
            if data.get("leads"):
                leads_str = ", ".join(f"{s}={n}" for s, n in data["leads"].items())
                print(f"    Leads: {leads_str}")

    elif args.command == "analysis-state":
        result = export_analysis_state(profile_id=p_id, all_profiles=p_all)
        if write_output(result, args, summary="analysis state"):
            return
        counts = result["current_counts"]
        print(f"Analysis State:")
        print(f"  Findings: {counts['findings']}, Connections: {counts['connections']}")
        print(f"  Hypotheses: {counts['hypotheses']}, Tags: {counts['tags']}")
        print(f"\nChanges Since Last Run:")
        for skill, changes in result["changes_since_last"].items():
            last = changes["last_run"] or "never"
            print(f"  {skill:<25} last={last}  "
                  f"+{changes['new_findings']} findings, +{changes['new_connections']} connections")

    elif args.command == "pillar-dump":
        result = export_pillar_dump()
        if write_output(result, args, summary="pillar dump"):
            return
        print(f"Pillar Dump:")
        print(f"  {result['pillar_count']} institutions")
        print(f"  {result['person_count']} persons")
        print(f"  {result['arc_count']} career arcs")
        print(f"  {result['event_count']} pillar events")
        print(f"  {result['score_count']} scores")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
