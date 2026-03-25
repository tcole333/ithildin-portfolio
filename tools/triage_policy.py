#!/usr/bin/env python3
"""
Shared triage scheduling policy — depth tier assignment, skill recommendation,
stop conditions, and thread coverage balancing.

This module is the single source of truth for triage decisions. Both the
/triage-leads skill and the dispatcher reference these rules.

Usage:
    uv run python tools/triage_policy.py assess "Target Name"
    uv run python tools/triage_policy.py rules
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "investigation.db"

# ── Constants ────────────────────────────────────────────────

DEPTH_TIERS = ("scan", "standard", "deep_dive")

SKILL_RECOMMENDATION = {
    ("deep_dive", "person"): "/deep-investigate",
    ("deep_dive", "entity"): "/deep-investigate",
    ("deep_dive", "financial"): "/deep-investigate",
    ("standard", "person"): "/investigate-person",
    ("standard", "entity"): "/trace-entity",
    ("standard", "financial"): "/pursue-lead",
    ("standard", "connection"): "/pursue-lead",
    ("scan", "person"): "/pursue-lead",
    ("scan", "entity"): "/pursue-lead",
    ("scan", "financial"): "/pursue-lead",
    ("scan", "connection"): "/pursue-lead",
    # Depth-analysis skills: route specific source types to focused analyzers
    ("standard", "filing"): "/analyze-filing",
    ("standard", "contract"): "/analyze-contract",
    ("standard", "case"): "/analyze-case",
    ("deep_dive", "filing"): "/analyze-filing",
    ("deep_dive", "contract"): "/analyze-contract",
    ("deep_dive", "case"): "/analyze-case",
    ("scan", "filing"): "/analyze-filing",
    ("scan", "contract"): "/analyze-contract",
    ("scan", "case"): "/analyze-case",
    # Nonprofit/grant routing
    ("deep_dive", "nonprofit"): "/trace-grants",
    ("standard", "nonprofit"): "/trace-grants",
    ("scan", "nonprofit"): "/trace-grants",
    ("deep_dive", "grant"): "/trace-grants",
    ("standard", "grant"): "/trace-grants",
    ("scan", "grant"): "/trace-grants",
}

DEAD_END_THRESHOLDS = {
    "exhaustively_covered_findings": 10,
    "thread_queue_saturated": 30,
}

# Structural signals that trigger tier escalation
STANDARD_TIER_MIN_ROLES = 3
STANDARD_TIER_MIN_CONNECTIONS = 3
DEEP_DIVE_MIN_ROLES = 5
DEEP_DIVE_MIN_CONNECTIONS = 8


# ── Assessment Functions ─────────────────────────────────────

def _get_structural_signals(target_name, db):
    """Query entity_roles, connections, and findings counts for a target."""
    roles = db.execute(
        "SELECT COUNT(*) FROM entity_roles WHERE person_name LIKE ?",
        (f"%{target_name}%",)
    ).fetchone()[0]
    connections = db.execute(
        "SELECT COUNT(*) FROM connections WHERE person_a LIKE ? OR person_b LIKE ?",
        (f"%{target_name}%", f"%{target_name}%")
    ).fetchone()[0]
    findings = db.execute(
        "SELECT COUNT(*) FROM findings WHERE target_name LIKE ?",
        (f"%{target_name}%",)
    ).fetchone()[0]
    return {"roles": roles, "connections": connections, "findings": findings}


def assess_depth_tier(target_name, db, key_persons=None, known_addresses=None):
    """Assign a depth tier based on structural signals and profile context.

    Returns (tier, reason) tuple.
    """
    key_persons = key_persons or []
    known_addresses = known_addresses or []

    # Key person → deep_dive
    target_lower = target_name.lower()
    for kp in key_persons:
        if kp.lower() in target_lower or target_lower in kp.lower():
            return "deep_dive", f"key person match: {kp}"

    signals = _get_structural_signals(target_name, db)

    # High structural position → deep_dive
    if signals["roles"] >= DEEP_DIVE_MIN_ROLES or signals["connections"] >= DEEP_DIVE_MIN_CONNECTIONS:
        return "deep_dive", (
            f"{signals['roles']} roles, {signals['connections']} connections "
            f"(thresholds: {DEEP_DIVE_MIN_ROLES} roles or {DEEP_DIVE_MIN_CONNECTIONS} connections)"
        )

    # Moderate structural position → standard
    if signals["roles"] >= STANDARD_TIER_MIN_ROLES or signals["connections"] >= STANDARD_TIER_MIN_CONNECTIONS:
        return "standard", (
            f"{signals['roles']} roles, {signals['connections']} connections "
            f"(thresholds: {STANDARD_TIER_MIN_ROLES} roles or {STANDARD_TIER_MIN_CONNECTIONS} connections)"
        )

    # Known address → standard
    for addr in known_addresses:
        # Check if any entity at this address is linked to the target
        has_addr = db.execute(
            """SELECT COUNT(*) FROM entity_addresses ea
               JOIN entity_roles er ON ea.entity_id = er.entity_id
               WHERE ea.address LIKE ? AND er.person_name LIKE ?""",
            (f"%{addr}%", f"%{target_name}%")
        ).fetchone()[0]
        if has_addr:
            return "standard", f"entity at known address: {addr}"

    # Default → scan
    return "scan", (
        f"{signals['roles']} roles, {signals['connections']} connections, "
        f"{signals['findings']} findings — no escalation signals"
    )


def recommend_skill(depth_tier, category):
    """Return the recommended skill for a given depth tier and lead category.

    Falls back through: exact match → tier with None category → /pursue-lead.
    """
    # Exact match
    key = (depth_tier, category)
    if key in SKILL_RECOMMENDATION:
        return SKILL_RECOMMENDATION[key]

    # Tier fallback (any category at this tier)
    for (tier, _cat), skill in SKILL_RECOMMENDATION.items():
        if tier == depth_tier:
            return skill

    return "/pursue-lead"


def should_dead_end(target_name, depth_tier, thread_id, db):
    """Check stop conditions. Returns (should_stop, reason) tuple."""
    signals = _get_structural_signals(target_name, db)

    # Exhaustively covered
    threshold = DEAD_END_THRESHOLDS["exhaustively_covered_findings"]
    if signals["findings"] >= threshold:
        # Check if there's an existing open lead at same or higher depth
        existing = db.execute(
            """SELECT id, depth_tier FROM leads
               WHERE target_name LIKE ? AND status IN ('open', 'in_progress')
               AND depth_tier IS NOT NULL""",
            (f"%{target_name}%",)
        ).fetchone()
        if existing:
            return True, (
                f"exhaustively_covered: {signals['findings']} findings "
                f"(threshold: {threshold}), existing lead #{existing['id']} "
                f"at depth_tier={existing['depth_tier']}"
            )

    # Duplicate at same or higher depth
    if depth_tier:
        tier_rank = {"scan": 0, "standard": 1, "deep_dive": 2}
        my_rank = tier_rank.get(depth_tier, 0)
        existing = db.execute(
            """SELECT id, depth_tier FROM leads
               WHERE target_name LIKE ? AND status IN ('open', 'in_progress')
               AND depth_tier IS NOT NULL AND id != -1""",
            (f"%{target_name}%",)
        ).fetchall()
        for e in existing:
            e_rank = tier_rank.get(e["depth_tier"], 0)
            if e_rank >= my_rank:
                return True, (
                    f"covered_by_lead_#{e['id']}: existing lead at "
                    f"depth_tier={e['depth_tier']} (>= {depth_tier})"
                )

    # Thread queue saturated
    if thread_id:
        saturated_threshold = DEAD_END_THRESHOLDS["thread_queue_saturated"]
        active_in_thread = db.execute(
            """SELECT COUNT(*) FROM leads
               WHERE thread_id = ? AND status IN ('open', 'in_progress')""",
            (thread_id,)
        ).fetchone()[0]
        if active_in_thread >= saturated_threshold:
            return True, (
                f"thread_saturated: {active_in_thread} active leads in thread "
                f"{thread_id} (threshold: {saturated_threshold})"
            )

    return False, ""


def get_thread_priority_boost(thread_id, db):
    """Calculate priority adjustment based on thread coverage imbalance.

    Returns -1 (lower), 0 (keep), or +1 (raise) relative to other threads.
    """
    if not thread_id:
        return 0

    stats = db.execute(
        """SELECT
            (SELECT COUNT(*) FROM findings WHERE thread_id = ?) as my_findings,
            (SELECT AVG(cnt) FROM (
                SELECT COUNT(*) as cnt FROM findings
                WHERE thread_id IS NOT NULL
                GROUP BY thread_id
            )) as avg_findings,
            (SELECT COUNT(*) FROM leads
             WHERE thread_id = ? AND status IN ('open', 'in_progress')) as my_active
        """,
        (thread_id, thread_id)
    ).fetchone()

    my_findings = stats["my_findings"] or 0
    avg_findings = stats["avg_findings"] or 0
    my_active = stats["my_active"] or 0

    # Starved thread: below average findings AND few active leads
    if my_findings < avg_findings * 0.5 and my_active < 10:
        return 1  # boost

    # Saturated thread: well above average
    if my_findings > avg_findings * 2.0:
        return -1  # lower

    return 0


# ── CLI ──────────────────────────────────────────────────────

def _load_profile_config():
    """Load key_persons and known_addresses from active investigation profile."""
    try:
        import yaml
        from tools.investigation_context import get_active_profile_path
        profile_path = get_active_profile_path()
        if profile_path and profile_path.exists():
            with open(profile_path) as f:
                cfg = yaml.safe_load(f)
            return cfg.get("key_persons", []), cfg.get("known_addresses", [])
    except Exception:
        pass
    return [], []


def cmd_assess(args):
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    key_persons, known_addresses = _load_profile_config()

    tier, reason = assess_depth_tier(args.target, db, key_persons, known_addresses)
    signals = _get_structural_signals(args.target, db)
    category = args.category or "person"
    skill = recommend_skill(tier, category)

    print(f"Target: {args.target}")
    print(f"  depth_tier: {tier}")
    print(f"  recommended_skill: {skill}")
    print(f"  reason: {reason}")
    print(f"  signals: roles={signals['roles']} connections={signals['connections']} findings={signals['findings']}")

    stop, stop_reason = should_dead_end(args.target, tier, args.thread_id, db)
    if stop:
        print(f"  STOP: {stop_reason}")

    boost = get_thread_priority_boost(args.thread_id, db) if args.thread_id else 0
    if boost:
        print(f"  thread_boost: {'+' if boost > 0 else ''}{boost}")

    db.close()


def cmd_rules(_args):
    print("=== Depth Tier Thresholds ===")
    print(f"  deep_dive: key_person OR roles >= {DEEP_DIVE_MIN_ROLES} OR connections >= {DEEP_DIVE_MIN_CONNECTIONS}")
    print(f"  standard:  roles >= {STANDARD_TIER_MIN_ROLES} OR connections >= {STANDARD_TIER_MIN_CONNECTIONS} OR known_address")
    print(f"  scan:      default (no escalation signals)")
    print()
    print("=== Skill Recommendation ===")
    for (tier, cat), skill in sorted(SKILL_RECOMMENDATION.items()):
        print(f"  {tier:10} + {cat or 'any':12} -> {skill}")
    print()
    print("=== Dead-End Thresholds ===")
    for k, v in DEAD_END_THRESHOLDS.items():
        print(f"  {k}: {v}")


def main():
    parser = argparse.ArgumentParser(description="Triage scheduling policy")
    sub = parser.add_subparsers(dest="command")

    assess_p = sub.add_parser("assess", help="Assess a target's depth tier and recommended skill")
    assess_p.add_argument("target", help="Target name to assess")
    assess_p.add_argument("--category", default=None, help="Lead category (person, entity, financial)")
    assess_p.add_argument("--thread-id", type=int, default=None, help="Thread ID for coverage balancing")

    sub.add_parser("rules", help="Show decision tables and thresholds")

    args = parser.parse_args()
    if args.command == "assess":
        cmd_assess(args)
    elif args.command == "rules":
        cmd_rules(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
