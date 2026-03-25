#!/usr/bin/env python3
"""
Operational learning loop for OSINT investigations.

Tracks methodology observations from agent reports: tool friction, surprise
findings, process insights, source quality notes. Detects patterns across
observations and supports bulk ingestion from structured handoff reports.

Part of investigation.db.

Usage:
    python tools/methodology_tracker.py add --category friction --description "..." [--skill ...] [--lead-id N] [--agent ...] [--target "..."]
    python tools/methodology_tracker.py list [--category friction] [--status open] [--limit 50]
    python tools/methodology_tracker.py show <id>
    python tools/methodology_tracker.py acknowledge <id>
    python tools/methodology_tracker.py address <id> --resolution "..."
    python tools/methodology_tracker.py dismiss <id> --reason "..."
    python tools/methodology_tracker.py patterns [--min-count 3]
    python tools/methodology_tracker.py ingest-report <report.md> [--skill ...] [--lead-id N]
    python tools/methodology_tracker.py stats
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

VALID_CATEGORIES = ["friction", "surprise", "methodology", "process_gap", "source_quality"]
VALID_STATUSES = ["open", "acknowledged", "addressed", "dismissed", "duplicate"]

# Maps report [Category] labels to DB category values
CATEGORY_MAP = {
    "Friction": "friction",
    "Surprise": "surprise",
    "Methodology": "methodology",
    "Process gap": "process_gap",
    "Source quality": "source_quality",
}

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "not", "no", "it", "its",
    "this", "that", "these", "those", "i", "we", "you", "he", "she", "they",
    "me", "us", "him", "her", "them", "my", "our", "your", "his", "their",
    "what", "which", "who", "when", "where", "how", "why", "all", "each",
    "any", "some", "can", "will", "should", "would", "could", "may", "might",
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── CRUD ────────────────────────────────────────────────────


def add_observation(category, description, skill=None, lead_id=None,
                    agent=None, target=None):
    """Add a methodology observation. Returns the observation ID."""
    db = get_db()
    cursor = db.execute("""
        INSERT INTO methodology_observations
            (category, description, source_skill, source_lead_id, source_agent, target_name)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (category, description, skill, lead_id, agent, target))
    obs_id = cursor.lastrowid
    db.commit()
    db.close()
    return obs_id


def list_observations(category=None, status=None, limit=50):
    """List observations with optional filters."""
    db = get_db()
    conditions = []
    params = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(f"""
        SELECT id, category, description, source_skill, source_lead_id,
               source_agent, target_name, status, resolution,
               related_infra_id, created_at
        FROM methodology_observations
        {where}
        ORDER BY created_at DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_observation(obs_id):
    """Get a single observation by ID."""
    db = get_db()
    row = db.execute("""
        SELECT id, category, description, source_skill, source_lead_id,
               source_agent, target_name, status, resolution,
               related_infra_id, created_at
        FROM methodology_observations WHERE id = ?
    """, (obs_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def update_status(obs_id, new_status, resolution=None):
    """Update observation status and optional resolution."""
    db = get_db()
    if resolution:
        db.execute("""
            UPDATE methodology_observations SET status = ?, resolution = ?
            WHERE id = ?
        """, (new_status, resolution, obs_id))
    else:
        db.execute("""
            UPDATE methodology_observations SET status = ? WHERE id = ?
        """, (new_status, obs_id))
    db.commit()
    db.close()


def get_stats():
    """Get observation counts by category and status."""
    db = get_db()
    by_category = db.execute("""
        SELECT category, count(*) as cnt
        FROM methodology_observations GROUP BY category ORDER BY cnt DESC
    """).fetchall()
    by_status = db.execute("""
        SELECT status, count(*) as cnt
        FROM methodology_observations GROUP BY status ORDER BY cnt DESC
    """).fetchall()
    total = db.execute("SELECT count(*) FROM methodology_observations").fetchone()[0]
    by_skill = db.execute("""
        SELECT source_skill, count(*) as cnt
        FROM methodology_observations
        WHERE source_skill IS NOT NULL
        GROUP BY source_skill ORDER BY cnt DESC
    """).fetchall()
    db.close()
    return {
        "total": total,
        "by_category": {r["category"]: r["cnt"] for r in by_category},
        "by_status": {r["status"]: r["cnt"] for r in by_status},
        "by_skill": {r["source_skill"]: r["cnt"] for r in by_skill},
    }


# ── Pattern Detection ────────────────────────────────────────


def _tokenize(text):
    """Simple word tokenization with stopword removal."""
    words = re.findall(r'[a-z_]+', text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def detect_patterns(min_count=3):
    """Detect patterns by grouping observations with high word overlap.

    Within each category, clusters observations that share 50%+ words.
    Reports clusters with min_count or more entries.
    """
    db = get_db()
    rows = db.execute("""
        SELECT id, category, description, status
        FROM methodology_observations
        WHERE status IN ('open', 'acknowledged')
        ORDER BY category, id
    """).fetchall()
    db.close()

    # Group by category
    by_cat = defaultdict(list)
    for r in rows:
        tokens = _tokenize(r["description"])
        by_cat[r["category"]].append({
            "id": r["id"],
            "description": r["description"],
            "status": r["status"],
            "tokens": tokens,
        })

    patterns = []
    for category, items in by_cat.items():
        # Simple greedy clustering: assign each item to first matching cluster
        clusters = []
        for item in items:
            placed = False
            for cluster in clusters:
                # Check overlap with cluster centroid (union of all tokens)
                centroid = cluster["centroid"]
                if not item["tokens"] or not centroid:
                    continue
                overlap = len(item["tokens"] & centroid) / max(len(item["tokens"]), 1)
                if overlap >= 0.5:
                    cluster["items"].append(item)
                    cluster["centroid"] = centroid | item["tokens"]
                    placed = True
                    break
            if not placed:
                clusters.append({
                    "centroid": set(item["tokens"]),
                    "items": [item],
                })

        for cluster in clusters:
            if len(cluster["items"]) >= min_count:
                # Find most common words as cluster label
                word_counts = Counter()
                for item in cluster["items"]:
                    word_counts.update(item["tokens"])
                top_words = [w for w, _ in word_counts.most_common(5)]
                patterns.append({
                    "category": category,
                    "count": len(cluster["items"]),
                    "keywords": top_words,
                    "observation_ids": [i["id"] for i in cluster["items"]],
                    "samples": [i["description"][:120] for i in cluster["items"][:3]],
                })

    patterns.sort(key=lambda x: x["count"], reverse=True)
    return patterns


# ── Report Ingestion ────────────────────────────────────────


def ingest_report(filepath, skill=None, lead_id=None):
    """Parse a structured handoff report and bulk-insert Learnings as observations.

    Returns list of inserted observation IDs.
    """
    text = Path(filepath).read_text(encoding="utf-8")

    # Parse frontmatter for agent/target/skill metadata
    fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    fm = {}
    if fm_match:
        for line in fm_match.group(1).strip().splitlines():
            if ':' in line:
                key, _, value = line.partition(':')
                fm[key.strip()] = value.strip().strip('"').strip("'")

    agent = fm.get("agent")
    target = fm.get("target")
    report_skill = skill or fm.get("skill")
    report_lead_id = lead_id

    # Parse Learnings section
    learn_match = re.search(r'^## Learnings\s*\n(.*?)(?=^## |\Z)', text,
                            re.MULTILINE | re.DOTALL)
    if not learn_match:
        return []

    inserted = []
    for match in re.finditer(r'^- \[([^\]]+)\]\s*(.+)$', learn_match.group(1),
                             re.MULTILINE):
        label = match.group(1).strip()
        description = match.group(2).strip()
        category = CATEGORY_MAP.get(label)
        if not category:
            continue

        obs_id = add_observation(
            category=category,
            description=description,
            skill=report_skill,
            lead_id=report_lead_id,
            agent=agent,
            target=target,
        )
        inserted.append(obs_id)

    return inserted


# ── CLI ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Methodology observation tracker for investigation learning loop")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Record a new observation")
    p_add.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--skill", help="Source skill (e.g., pursue-lead)")
    p_add.add_argument("--lead-id", type=int, help="Related lead ID")
    p_add.add_argument("--agent", help="Agent identifier")
    p_add.add_argument("--target", help="Investigation target name")

    # list
    p_list = sub.add_parser("list", help="List observations")
    p_list.add_argument("--category", choices=VALID_CATEGORIES)
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--limit", type=int, default=50)
    add_output_args(p_list)

    # show
    p_show = sub.add_parser("show", help="Show observation detail")
    p_show.add_argument("id", type=int)

    # acknowledge
    p_ack = sub.add_parser("acknowledge", help="Mark observation as acknowledged")
    p_ack.add_argument("id", type=int)

    # address
    p_addr = sub.add_parser("address", help="Mark observation as addressed")
    p_addr.add_argument("id", type=int)
    p_addr.add_argument("--resolution", required=True, help="How it was resolved")

    # dismiss
    p_dis = sub.add_parser("dismiss", help="Dismiss observation")
    p_dis.add_argument("id", type=int)
    p_dis.add_argument("--reason", required=True, help="Why dismissed")

    # patterns
    p_pat = sub.add_parser("patterns", help="Detect recurring patterns")
    p_pat.add_argument("--min-count", type=int, default=3)
    add_output_args(p_pat)

    # ingest-report
    p_ing = sub.add_parser("ingest-report", help="Ingest learnings from a handoff report")
    p_ing.add_argument("file", help="Report file path")
    p_ing.add_argument("--skill", help="Override source skill")
    p_ing.add_argument("--lead-id", type=int, help="Related lead ID")

    # stats
    sub.add_parser("stats", help="Observation statistics")

    args = parser.parse_args()

    if args.command == "add":
        obs_id = add_observation(
            category=args.category,
            description=args.description,
            skill=args.skill,
            lead_id=args.lead_id,
            agent=args.agent,
            target=args.target,
        )
        print(f"Observation #{obs_id} added [{args.category}]")

    elif args.command == "list":
        obs = list_observations(
            category=args.category,
            status=args.status,
            limit=args.limit,
        )
        if write_output(obs, args, summary=f"observations ({len(obs)})"):
            return
        if not obs:
            print("No observations found")
            return
        print(f"{'ID':>4}  {'Category':<14} {'Status':<12} {'Description':<60} {'Skill':<16}")
        print("-" * 110)
        for o in obs:
            desc = o["description"][:58] + ".." if len(o["description"]) > 60 else o["description"]
            skill = o.get("source_skill") or ""
            print(f"{o['id']:>4}  {o['category']:<14} {o['status']:<12} {desc:<60} {skill:<16}")

    elif args.command == "show":
        obs = get_observation(args.id)
        if not obs:
            print(f"Observation #{args.id} not found")
            sys.exit(1)
        print(f"Observation #{obs['id']}")
        print(f"  Category:    {obs['category']}")
        print(f"  Status:      {obs['status']}")
        print(f"  Description: {obs['description']}")
        if obs.get("source_skill"):
            print(f"  Skill:       {obs['source_skill']}")
        if obs.get("source_agent"):
            print(f"  Agent:       {obs['source_agent']}")
        if obs.get("target_name"):
            print(f"  Target:      {obs['target_name']}")
        if obs.get("source_lead_id"):
            print(f"  Lead ID:     {obs['source_lead_id']}")
        if obs.get("resolution"):
            print(f"  Resolution:  {obs['resolution']}")
        if obs.get("related_infra_id"):
            print(f"  Infra Req:   #{obs['related_infra_id']}")
        print(f"  Created:     {obs['created_at']}")

    elif args.command == "acknowledge":
        update_status(args.id, "acknowledged")
        print(f"Observation #{args.id} acknowledged")

    elif args.command == "address":
        update_status(args.id, "addressed", resolution=args.resolution)
        print(f"Observation #{args.id} addressed: {args.resolution}")

    elif args.command == "dismiss":
        update_status(args.id, "dismissed", resolution=args.reason)
        print(f"Observation #{args.id} dismissed: {args.reason}")

    elif args.command == "patterns":
        pats = detect_patterns(min_count=args.min_count)
        if write_output(pats, args, summary=f"patterns ({len(pats)})"):
            return
        if not pats:
            print(f"No patterns found (min_count={args.min_count})")
            return
        print(f"Detected Patterns ({len(pats)}):\n")
        for p in pats:
            print(f"  [{p['category']}] {p['count']} observations — keywords: {', '.join(p['keywords'])}")
            print(f"    IDs: {p['observation_ids']}")
            for s in p["samples"]:
                print(f"    - {s}")
            print()

    elif args.command == "ingest-report":
        filepath = Path(args.file)
        if not filepath.exists():
            print(f"File not found: {filepath}")
            sys.exit(1)
        inserted = ingest_report(
            filepath,
            skill=args.skill,
            lead_id=args.lead_id,
        )
        if inserted:
            print(f"Ingested {len(inserted)} observations from {filepath.name} (IDs: {inserted})")
        else:
            print(f"No learnings found in {filepath.name}")

    elif args.command == "stats":
        s = get_stats()
        print("Methodology Observations")
        print("=" * 40)
        print(f"  Total: {s['total']}")
        print()
        print("  By Category:")
        for cat, cnt in s["by_category"].items():
            print(f"    {cat:<16} {cnt}")
        print()
        print("  By Status:")
        for status, cnt in s["by_status"].items():
            print(f"    {status:<16} {cnt}")
        if s["by_skill"]:
            print()
            print("  By Skill:")
            for skill, cnt in s["by_skill"].items():
                print(f"    {skill:<16} {cnt}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
