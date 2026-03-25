#!/usr/bin/env python3
"""
Lead prioritization engine for OSINT investigations.

Scores open leads by expected information value based on:
entity connectivity, source coverage gaps, thread importance,
upstream dependencies.

Part of investigation.db.

Usage:
    python tools/lead_prioritizer.py score [--top 30] [--thread-id N]
    python tools/lead_prioritizer.py explain --lead-id N
    python tools/lead_prioritizer.py cluster [--top 20]
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "investigation.db"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db


def _get_entity_connectivity(db):
    """Get degree centrality for each person in the connections graph."""
    rows = db.execute("""
        SELECT person_a, person_b FROM connections
    """).fetchall()
    degree = defaultdict(int)
    for r in rows:
        degree[r["person_a"]] += 1
        degree[r["person_b"]] += 1
    return degree


def _get_findings_count(db):
    """Get findings count per target_name."""
    rows = db.execute("""
        SELECT target_name, COUNT(*) as n FROM findings
        WHERE target_name IS NOT NULL
        GROUP BY target_name
    """).fetchall()
    return {r["target_name"]: r["n"] for r in rows}


def _get_source_coverage(db):
    """Get distinct source datasets per target."""
    rows = db.execute("""
        SELECT target_name, source_datasets FROM findings
        WHERE target_name IS NOT NULL AND source_datasets IS NOT NULL
    """).fetchall()
    coverage = defaultdict(set)
    for r in rows:
        if r["source_datasets"]:
            for src in r["source_datasets"].split(","):
                coverage[r["target_name"]].add(src.strip())
    return coverage


def _get_thread_stats(db):
    """Get open lead counts per thread."""
    rows = db.execute("""
        SELECT thread_id, COUNT(*) as n FROM leads
        WHERE status IN ('open', 'in_progress') AND thread_id IS NOT NULL
        GROUP BY thread_id
    """).fetchall()
    return {r["thread_id"]: r["n"] for r in rows}


def _get_upstream_references(db):
    """Find leads whose target_name is referenced by other open leads."""
    # Get all open lead target names
    leads = db.execute("""
        SELECT id, target_name, title, description FROM leads
        WHERE status IN ('open', 'pending_triage')
    """).fetchall()

    ref_count = defaultdict(int)
    target_names = {l["target_name"] for l in leads if l["target_name"]}

    for name in target_names:
        # Count other leads that mention this name
        count = db.execute("""
            SELECT COUNT(*) as n FROM leads
            WHERE status IN ('open', 'pending_triage', 'in_progress')
              AND target_name != ?
              AND (title LIKE ? OR description LIKE ?)
        """, (name, f"%{name[:30]}%", f"%{name[:30]}%")).fetchone()["n"]
        ref_count[name] = count

    return ref_count


def score_lead(db, lead, connectivity, findings_counts, source_coverage,
               thread_stats, upstream_refs):
    """Score a single lead. Returns (score, breakdown)."""
    target = lead["target_name"] or ""
    breakdown = {}

    # 1. Entity connectivity: high degree + few findings = high value
    degree = connectivity.get(target, 0)
    findings = findings_counts.get(target, 0)
    if degree > 0 and findings < 3:
        connectivity_score = min(degree / 10.0, 1.0) * (1 - min(findings / 5.0, 1.0))
    elif degree > 0:
        connectivity_score = min(degree / 20.0, 0.5)
    else:
        connectivity_score = 0.1  # Unknown entity, some baseline
    breakdown["connectivity"] = round(connectivity_score, 3)

    # 2. Coverage gap: few findings = high value
    if findings == 0:
        coverage_score = 1.0
    elif findings <= 2:
        coverage_score = 0.7
    elif findings <= 5:
        coverage_score = 0.3
    else:
        coverage_score = 0.1
    breakdown["coverage_gap"] = round(coverage_score, 3)

    # 3. Source coverage: fewer distinct sources = more to learn
    sources = source_coverage.get(target, set())
    ALL_SOURCES = {"web_search", "edgar", "registry", "990", "fec", "court", "acris", "aleph"}
    if sources:
        source_score = 1.0 - (len(sources) / len(ALL_SOURCES))
    else:
        source_score = 0.8  # No sources at all = lots to learn
    breakdown["source_diversity"] = round(source_score, 3)

    # 4. Thread balance: stale thread (few open leads) = boost
    thread_id = lead["thread_id"]
    if thread_id and thread_id in thread_stats:
        thread_leads = thread_stats[thread_id]
        if thread_leads <= 2:
            thread_score = 0.8  # Stale thread, needs attention
        elif thread_leads >= 10:
            thread_score = 0.3  # Already active
        else:
            thread_score = 0.5
    else:
        thread_score = 0.4  # No thread
    breakdown["thread_balance"] = round(thread_score, 3)

    # 5. Upstream value: other leads reference this target
    refs = upstream_refs.get(target, 0)
    upstream_score = min(refs / 5.0, 1.0) if refs > 0 else 0.0
    breakdown["upstream_value"] = round(upstream_score, 3)

    # 6. Manual priority bonus
    priority_bonus = {"critical": 0.3, "high": 0.2, "medium": 0.1, "low": 0.0}
    bonus = priority_bonus.get(lead["priority"], 0.0)
    breakdown["priority_bonus"] = bonus

    # Weighted total
    total = (
        0.25 * connectivity_score
        + 0.20 * coverage_score
        + 0.15 * source_score
        + 0.15 * thread_score
        + 0.15 * upstream_score
        + 0.10 * bonus / 0.3  # normalize bonus to 0-1 range
    )

    return round(total, 4), breakdown


def score_all_leads(db, thread_id=None, top=30):
    """Score and rank all open leads."""
    conditions = ["status IN ('open', 'pending_triage')"]
    params = []
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)

    leads = db.execute(f"""
        SELECT id, title, target_name, priority, thread_id, category
        FROM leads
        WHERE {' AND '.join(conditions)}
    """, params).fetchall()

    connectivity = _get_entity_connectivity(db)
    findings_counts = _get_findings_count(db)
    source_coverage = _get_source_coverage(db)
    thread_stats = _get_thread_stats(db)
    upstream_refs = _get_upstream_references(db)

    scored = []
    for lead in leads:
        score, breakdown = score_lead(
            db, dict(lead), connectivity, findings_counts,
            source_coverage, thread_stats, upstream_refs
        )
        scored.append({
            "lead_id": lead["id"],
            "title": lead["title"],
            "target": lead["target_name"],
            "priority": lead["priority"],
            "thread_id": lead["thread_id"],
            "score": score,
            "breakdown": breakdown,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


def cluster_leads(db, top=20):
    """Group related leads by entity overlap."""
    leads = db.execute("""
        SELECT id, title, target_name, category
        FROM leads
        WHERE status IN ('open', 'pending_triage') AND target_name IS NOT NULL
    """).fetchall()

    # Group by target_name
    groups = defaultdict(list)
    for l in leads:
        groups[l["target_name"]].append(dict(l))

    # Sort by cluster size
    clusters = []
    for target, cluster_leads in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True):
        if len(cluster_leads) >= 2:
            clusters.append({
                "target": target,
                "lead_count": len(cluster_leads),
                "leads": cluster_leads,
                "categories": list({l["category"] for l in cluster_leads if l.get("category")}),
            })

    return clusters[:top]


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Lead prioritization engine")
    sub = parser.add_subparsers(dest="command")

    p_score = sub.add_parser("score", help="Score and rank open leads")
    p_score.add_argument("--top", type=int, default=30)
    p_score.add_argument("--thread-id", type=int)
    add_output_args(p_score)

    p_explain = sub.add_parser("explain", help="Show scoring breakdown for a lead")
    p_explain.add_argument("--lead-id", type=int, required=True)
    add_output_args(p_explain)

    p_cluster = sub.add_parser("cluster", help="Group related leads by entity overlap")
    p_cluster.add_argument("--top", type=int, default=20)
    add_output_args(p_cluster)

    args = parser.parse_args()

    if args.command == "score":
        db = get_db()
        scored = score_all_leads(db, thread_id=args.thread_id, top=args.top)
        db.close()
        if write_output(scored, args, summary=f"lead scores (top {args.top})"):
            return
        print(f"\nLead Priority Scores (top {args.top}):")
        print(f"{'Rank':>4}  {'ID':>5}  {'Score':>6}  {'Pri':<8}  {'Target':<25}  Title")
        print("-" * 95)
        for i, s in enumerate(scored):
            target = (s["target"] or "")[:25]
            title = s["title"][:40]
            print(f"{i+1:>4}  {s['lead_id']:>5}  {s['score']:>6.3f}  {s['priority']:<8}  {target:<25}  {title}")

    elif args.command == "explain":
        db = get_db()
        lead = db.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
        if not lead:
            print(f"ERROR: Lead #{args.lead_id} not found")
            db.close()
            sys.exit(1)

        connectivity = _get_entity_connectivity(db)
        findings_counts = _get_findings_count(db)
        source_coverage = _get_source_coverage(db)
        thread_stats = _get_thread_stats(db)
        upstream_refs = _get_upstream_references(db)

        score, breakdown = score_lead(
            db, dict(lead), connectivity, findings_counts,
            source_coverage, thread_stats, upstream_refs
        )
        db.close()

        result = {
            "lead_id": lead["id"],
            "title": lead["title"],
            "target": lead["target_name"],
            "total_score": score,
            "breakdown": breakdown,
        }
        if write_output(result, args, summary=f"lead #{args.lead_id} scoring"):
            return

        target = lead["target_name"] or "(none)"
        print(f"\nLead #{lead['id']}: {lead['title']}")
        print(f"  Target: {target}")
        print(f"  Priority: {lead['priority']} | Thread: {lead['thread_id']}")
        print(f"\n  Total Score: {score:.4f}")
        print(f"\n  Breakdown:")
        weights = {
            "connectivity": 0.25, "coverage_gap": 0.20, "source_diversity": 0.15,
            "thread_balance": 0.15, "upstream_value": 0.15, "priority_bonus": 0.10,
        }
        for factor, value in breakdown.items():
            w = weights.get(factor, 0)
            print(f"    {factor:<20} {value:>6.3f}  (weight {w:.0%})")

        # Extra context
        findings = findings_counts.get(target, 0)
        degree = connectivity.get(target, 0)
        sources = source_coverage.get(target, set())
        refs = upstream_refs.get(target, 0)
        print(f"\n  Context:")
        print(f"    Findings: {findings} | Connections: {degree} | Sources: {len(sources)}")
        print(f"    Referenced by {refs} other leads")

    elif args.command == "cluster":
        db = get_db()
        clusters = cluster_leads(db, top=args.top)
        db.close()
        if write_output(clusters, args, summary=f"lead clusters"):
            return
        print(f"\nLead Clusters ({len(clusters)} targets with 2+ leads):")
        for c in clusters:
            print(f"\n  {c['target']} ({c['lead_count']} leads) — categories: {', '.join(c['categories'])}")
            for l in c["leads"][:5]:
                print(f"    #{l['id']}: {l['title'][:60]}")
            if len(c["leads"]) > 5:
                print(f"    ... +{len(c['leads']) - 5} more")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
