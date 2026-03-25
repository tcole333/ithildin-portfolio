#!/usr/bin/env python3
"""Lead deduplication for investigation.db.

Groups open leads into candidate duplicate clusters, infers missing target_names,
and applies subagent dedup decisions (dead-end duplicates, link via lead_relations).

Usage:
    python tools/lead_dedup.py fill-targets [--dry-run] [--batch-size 200]
    python tools/lead_dedup.py scan [--profile-id NAME] [--min-group-size 2]
    python tools/lead_dedup.py show-group <group_hash_or_lead_id>
    python tools/lead_dedup.py export-batch --batch-size 20 --offset 0 --output FILE
    python tools/lead_dedup.py apply --decisions-file FILE [--dry-run]
    python tools/lead_dedup.py verify [--sample-size 10]
    python tools/lead_dedup.py stats
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"

# ---------------------------------------------------------------------------
# Shared utilities (subset of finding_dedup.py patterns)
# ---------------------------------------------------------------------------

_GENERIC_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "by", "from", "is", "was", "were", "are",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall",
    "this", "that", "these", "those", "it", "its", "as", "not", "no",
    "cross", "ref", "officer", "search", "registry", "investigate",
    "entity", "find", "other", "roles", "check", "corporate", "all",
    "sources", "deep", "analyze", "trace", "review", "lead", "follow",
    "up", "new", "possible", "potential", "via", "also", "related",
}

_LEAD_STOP_WORDS = None


def _build_stop_words():
    global _LEAD_STOP_WORDS
    if _LEAD_STOP_WORDS is not None:
        return _LEAD_STOP_WORDS
    words = set(_GENERIC_STOP_WORDS)
    try:
        from tools.investigation_context import get_active_profile
        profile = get_active_profile()
        if profile.primary_subject:
            words |= set(profile.primary_subject.lower().split())
    except Exception:
        pass
    _LEAD_STOP_WORDS = words
    return words


def _tokens(text):
    """Extract meaningful tokens for similarity comparison."""
    stop = _build_stop_words()
    return {w for w in re.findall(r'\w+', text.lower())
            if w not in stop and len(w) > 2 and not w.isdigit()}


def jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def normalize_name(name):
    """Normalize a name for comparison: lowercase, strip punctuation, collapse whitespace."""
    if not name:
        return ""
    return re.sub(r'[^a-z0-9 ]', '', name.lower()).strip()


def group_hash(lead_ids):
    """Stable hash for a set of lead IDs."""
    key = ",".join(str(i) for i in sorted(lead_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    _ensure_dedup_schema(db)
    return db


def _ensure_dedup_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS lead_dedup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_hash TEXT NOT NULL UNIQUE,
            lead_ids TEXT NOT NULL,
            decision TEXT NOT NULL,
            keeper_id INTEGER,
            dead_ended_ids TEXT,
            rationale TEXT,
            decided_by TEXT DEFAULT 'agent:dedup',
            decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_dedup_log_hash ON lead_dedup_log(group_hash);
    """)


# ---------------------------------------------------------------------------
# Union-Find for grouping
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry

    def groups(self):
        """Return dict of root -> [members]."""
        result = {}
        for x in self.parent:
            root = self.find(x)
            result.setdefault(root, []).append(x)
        return result


# ---------------------------------------------------------------------------
# fill-targets: Infer missing target_name from title patterns
# ---------------------------------------------------------------------------

# Patterns for extracting target names from auto-generated and manual lead titles
_TARGET_PATTERNS = [
    # Auto-leads patterns
    (r'^Cross-ref officer:\s*(.+?)\s*[-—]', None),
    (r'^Cross-ref registry:\s*(.+?)\s*[-—]', None),
    (r'^Cross-ref address:\s*(.+?)\s*[-—]', None),
    (r'^Serial director:\s*(.+?)\s*[-—]', None),
    (r'^Filing cluster:\s*\d+\s+entities\s+by\s+(.+?)\s+within', None),
    (r'^Jurisdiction cluster:\s*(.+?)\s+has\s+\d+', None),
    (r'^Coverage gap:\s*(.+?)\s*[-—]', None),
    # Common manual patterns — more specific patterns first
    (r'^(?:Deep-)?[Ii]nvestigate\s+(.+?)(?:\s*[-—])', None),
    (r'^(?:Deep-)?[Ii]nvestigate\s+(.+?)(?:\s+10-K|\s+role\b|\s+connection\b|\s+financial\b|\s+corporate\b)', None),
    (r'^(?:Deep |Deep-)?[Dd]ive:?\s+(.+?)(?:\s*[-—]|$)', None),
    (r'^(?:Deep-)?[Ii]nvestigate\s+(.+?)$', None),
    (r'^Trace\s+(.+?)\s+(?:corporate|financial|representation|entity|through|13F|connection)', None),
    (r'^Analyze\s+(.+?)\s+(?:10-K|proxy|13[DF]|SEC|financial|contract|corporate|filing)', None),
    (r'^SEC EDGAR (?:deep-dive|search|analysis) (?:on|for)\s+(.+?)(?:\s+CIK|\s*[-—]|$)', None),
    (r'^(?:Identify|Map|Review)\s+(.+?)\s+(?:role|who|network|full|in\s+|as\s+|codename)', None),
    (r'^(.+?)\s+(?:master contact|investment vehicle|foreknowledge|financial compensation)', None),
    (r'^(.+?)\s+(?:10-K|proxy|13[DF]|Form ADV|13F)\b', None),
    (r'^(.+?)\s+SEC (?:Non-Enforcement|Filing|enforcement)', None),
]


def _clean_target(candidate):
    """Clean up an extracted target name."""
    if not candidate:
        return None
    # Remove trailing possessives
    candidate = re.sub(r"'s$", "", candidate)
    # Remove CIK numbers and parenthetical registry IDs
    candidate = re.sub(r'\s*\(CIK[^)]*\)', '', candidate)
    candidate = re.sub(r'\s*\([A-Z]\d{8,}\)', '', candidate)
    # Remove leading "the "
    candidate = re.sub(r'^[Tt]he\s+', '', candidate)
    # Remove trailing prepositions/articles/source tags
    candidate = re.sub(r'\s+(?:in|to|for|from|and|the|a|an|SEC|EDGAR|via|what)$', '', candidate)
    # Remove leading "what/remaining/the"
    candidate = re.sub(r'^(?:what|remaining|the)\s+', '', candidate, flags=re.IGNORECASE)
    return candidate.strip()


def _extract_target_from_title(title):
    """Try to extract a target name from a lead title using regex patterns."""
    for pattern, _ in _TARGET_PATTERNS:
        m = re.match(pattern, title)
        if m:
            candidate = _clean_target(m.group(1).strip())
            if not candidate:
                continue
            # Skip if it looks like a generic description, not a name
            if len(candidate) < 3 or len(candidate) > 80:
                continue
            # Skip if all lowercase (likely a description, not a proper noun)
            if candidate == candidate.lower() and not re.search(r'[A-Z]', title[:50]):
                continue
            return candidate
    return None


def cmd_fill_targets(args):
    """Infer missing target_name from title patterns."""
    db = get_db()
    leads = db.execute("""
        SELECT id, title, description, source FROM leads
        WHERE status IN ('open', 'pending_triage')
          AND (target_name IS NULL OR target_name = '')
        ORDER BY id
    """).fetchall()

    if not leads:
        print("No leads with missing target_name found.")
        db.close()
        return

    print(f"Leads with missing target_name: {len(leads)}")

    filled = 0
    unfilled = []
    for lead in leads:
        target = _extract_target_from_title(lead["title"])
        if target:
            filled += 1
            if args.dry_run:
                print(f"  #{lead['id']}: '{target}' <- {lead['title'][:80]}")
            else:
                db.execute(
                    "UPDATE leads SET target_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (target, lead["id"]),
                )
                db.execute(
                    "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
                    (lead["id"], f"target_name inferred from title: '{target}'"),
                )
        else:
            unfilled.append(lead)

    if not args.dry_run:
        db.commit()
        print(f"\nFilled: {filled} target_names")
    else:
        print(f"\n[DRY RUN] Would fill: {filled} target_names")

    if unfilled:
        print(f"Unfilled: {len(unfilled)} (need manual/subagent review)")
        if args.verbose:
            for lead in unfilled[:30]:
                print(f"  #{lead['id']}: {lead['title'][:100]}")

    db.close()


# ---------------------------------------------------------------------------
# scan: Group open leads into candidate duplicate clusters
# ---------------------------------------------------------------------------

def _load_name_aliases(db):
    """Load name_aliases table into a lookup dict: alias -> canonical_name."""
    try:
        rows = db.execute("SELECT alias, canonical_name FROM name_aliases").fetchall()
        return {r["alias"]: r["canonical_name"] for r in rows}
    except Exception:
        return {}


def _resolve_target(target, aliases):
    """Resolve a target_name through aliases to its canonical form."""
    if not target:
        return target
    return aliases.get(target, target)


def _build_groups(db, profile_id=None, min_group_size=2):
    """Build candidate duplicate groups using 4 strategies + union-find."""
    uf = UnionFind()

    # Load open leads
    conditions = ["status = 'open'", "target_name IS NOT NULL", "target_name != ''"]
    params = []
    if profile_id:
        conditions.append("profile_id = ?")
        params.append(profile_id)

    where = " AND ".join(conditions)
    leads = db.execute(f"""
        SELECT id, title, description, category, priority, source,
               target_name, depth_tier, profile_id
        FROM leads WHERE {where}
        ORDER BY id
    """, params).fetchall()

    leads_by_id = {l["id"]: dict(l) for l in leads}
    aliases = _load_name_aliases(db)

    # Strategy 1: Exact target_name match (after alias resolution)
    by_target = {}
    for lead in leads:
        resolved = _resolve_target(lead["target_name"], aliases)
        normalized = normalize_name(resolved)
        if normalized:
            by_target.setdefault(normalized, []).append(lead["id"])

    s1_pairs = 0
    for norm_target, ids in by_target.items():
        if len(ids) >= 2:
            for i in range(1, len(ids)):
                uf.union(ids[0], ids[i])
                s1_pairs += 1

    # Strategy 2: Normalized name variants (prefix overlap)
    sorted_targets = sorted(by_target.keys())
    s2_pairs = 0
    for i, t1 in enumerate(sorted_targets):
        if len(t1) < 6:
            continue
        ids1 = by_target[t1]
        for t2 in sorted_targets[i + 1:]:
            if len(t2) < 6:
                continue
            # Check prefix overlap (one name is prefix of another)
            if t1.startswith(t2) or t2.startswith(t1):
                ids2 = by_target[t2]
                uf.union(ids1[0], ids2[0])
                s2_pairs += 1

    # Strategy 3: Title Jaccard similarity (within same category)
    by_category = {}
    for lead in leads:
        cat = lead["category"] or "unknown"
        by_category.setdefault(cat, []).append(lead)

    s3_pairs = 0
    for cat, cat_leads in by_category.items():
        # Only check leads that share a target group already or have similar titles
        # For efficiency, limit pairwise comparison to leads within same target group
        for norm_target, ids in by_target.items():
            if len(ids) < 2:
                continue
            cat_ids_in_group = [lid for lid in ids if lid in leads_by_id
                                and (leads_by_id[lid].get("category") or "unknown") == cat]
            if len(cat_ids_in_group) < 2:
                continue
            for i, id_a in enumerate(cat_ids_in_group):
                t_a = _tokens(leads_by_id[id_a]["title"])
                for id_b in cat_ids_in_group[i + 1:]:
                    t_b = _tokens(leads_by_id[id_b]["title"])
                    sim = jaccard(t_a, t_b)
                    if sim >= 0.5:
                        uf.union(id_a, id_b)
                        s3_pairs += 1

    # Build groups from union-find
    raw_groups = uf.groups()
    groups = []
    for root, members in raw_groups.items():
        if len(members) < min_group_size:
            continue
        member_ids = sorted(members)
        ghash = group_hash(member_ids)

        # Check if already processed
        existing = db.execute(
            "SELECT id FROM lead_dedup_log WHERE group_hash = ?", (ghash,)
        ).fetchone()
        if existing:
            continue

        # Get target names in group
        targets = set()
        for lid in member_ids:
            if lid in leads_by_id:
                targets.add(leads_by_id[lid].get("target_name", ""))

        groups.append({
            "group_hash": ghash,
            "lead_ids": member_ids,
            "size": len(member_ids),
            "targets": sorted(t for t in targets if t),
            "primary_target": max(targets, key=lambda t: sum(
                1 for lid in member_ids
                if leads_by_id.get(lid, {}).get("target_name") == t
            )) if targets else None,
        })

    groups.sort(key=lambda g: g["size"], reverse=True)

    return groups, {"s1_pairs": s1_pairs, "s2_pairs": s2_pairs, "s3_pairs": s3_pairs}


def cmd_scan(args):
    """Group open leads into candidate duplicate clusters."""
    db = get_db()
    groups, pair_stats = _build_groups(db, profile_id=args.profile_id,
                                        min_group_size=args.min_group_size)

    print(f"=== Lead Dedup Scan ===")
    print(f"Strategy 1 (exact target match): {pair_stats['s1_pairs']} pairs")
    print(f"Strategy 2 (name variants): {pair_stats['s2_pairs']} pairs")
    print(f"Strategy 3 (title similarity): {pair_stats['s3_pairs']} pairs")
    print(f"\nCandidate groups: {len(groups)}")

    total_leads = sum(g["size"] for g in groups)
    print(f"Total leads in groups: {total_leads}")

    # Already processed
    processed = db.execute("SELECT COUNT(*) FROM lead_dedup_log").fetchone()[0]
    print(f"Already processed groups: {processed}")

    print(f"\nTop groups by size:")
    for g in groups[:30]:
        targets_str = ", ".join(g["targets"][:3])
        if len(g["targets"]) > 3:
            targets_str += f" +{len(g['targets']) - 3} more"
        print(f"  [{g['group_hash']}] {g['size']} leads — {targets_str}")

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(groups, indent=2))
        print(f"\nFull results written to {output_path}")

    db.close()


# ---------------------------------------------------------------------------
# show-group: Display all leads in a group with context
# ---------------------------------------------------------------------------

def _get_group_by_hash_or_lead(db, identifier):
    """Find a group by hash prefix or lead ID."""
    # Try as group_hash prefix in dedup log
    row = db.execute(
        "SELECT * FROM lead_dedup_log WHERE group_hash LIKE ?",
        (f"{identifier}%",)
    ).fetchone()
    if row:
        return json.loads(row["lead_ids"]), row

    # Run scan to find groups
    groups, _ = _build_groups(db, min_group_size=2)

    # Try matching by hash prefix in scan results
    for g in groups:
        if g["group_hash"].startswith(identifier):
            return g["lead_ids"], None

    # Try as lead ID
    try:
        lead_id = int(identifier)
    except ValueError:
        return None, None

    for g in groups:
        if lead_id in g["lead_ids"]:
            return g["lead_ids"], None

    # Also check processed groups in log
    rows = db.execute("SELECT lead_ids FROM lead_dedup_log").fetchall()
    for row in rows:
        ids = json.loads(row["lead_ids"])
        if lead_id in ids:
            return ids, row

    return None, None


def cmd_show_group(args):
    """Show all leads in a group with full context."""
    db = get_db()
    lead_ids, log_entry = _get_group_by_hash_or_lead(db, args.identifier)

    if lead_ids is None:
        print(f"No group found for '{args.identifier}'")
        db.close()
        return

    if log_entry:
        print(f"Group {log_entry['group_hash']} — {log_entry['decision']} "
              f"(decided {log_entry['decided_at']})")
        if log_entry["rationale"]:
            print(f"Rationale: {log_entry['rationale']}")
        print()

    for lid in sorted(lead_ids):
        lead = db.execute("""
            SELECT id, title, description, category, priority, status, source,
                   target_name, depth_tier, stop_reason, created_at
            FROM leads WHERE id = ?
        """, (lid,)).fetchone()
        if not lead:
            print(f"  #{lid}: NOT FOUND")
            continue

        status_mark = "X" if lead["status"] == "dead_end" else " "
        print(f"  [{status_mark}] #{lead['id']} [{lead['priority']}] ({lead['category'] or '?'})")
        print(f"      Title: {lead['title'][:120]}")
        if lead["target_name"]:
            print(f"      Target: {lead['target_name']}")
        if lead["description"]:
            print(f"      Desc: {lead['description'][:150]}")
        if lead["source"]:
            print(f"      Source: {lead['source'][:80]}")
        if lead["depth_tier"]:
            print(f"      Tier: {lead['depth_tier']}")
        if lead["stop_reason"]:
            print(f"      Stop: {lead['stop_reason'][:100]}")

        # Notes
        notes = db.execute(
            "SELECT note, created_at FROM lead_notes WHERE lead_id = ? ORDER BY created_at DESC LIMIT 3",
            (lid,)
        ).fetchall()
        for note in notes:
            print(f"      Note: {note['note'][:100]}")

        # Related findings count
        findings_count = db.execute(
            "SELECT COUNT(*) FROM findings WHERE target_name = ? AND verification_status != 'retracted'",
            (lead["target_name"],)
        ).fetchone()[0] if lead["target_name"] else 0
        if findings_count:
            print(f"      Findings for target: {findings_count}")

        # Existing lead_relations
        rels = db.execute("""
            SELECT lr.related_lead_id, lr.relation_type, l.title
            FROM lead_relations lr
            JOIN leads l ON l.id = lr.related_lead_id
            WHERE lr.lead_id = ?
            UNION
            SELECT lr.lead_id, lr.relation_type, l.title
            FROM lead_relations lr
            JOIN leads l ON l.id = lr.lead_id
            WHERE lr.related_lead_id = ?
        """, (lid, lid)).fetchall()
        for rel in rels:
            print(f"      Rel: {rel['relation_type']} -> #{rel['related_lead_id']} {rel['title'][:60]}")

        print()

    db.close()


# ---------------------------------------------------------------------------
# export-batch: Export unprocessed groups for subagent review
# ---------------------------------------------------------------------------

def cmd_export_batch(args):
    """Export unprocessed groups with full context for subagent review."""
    db = get_db()
    groups, _ = _build_groups(db, profile_id=args.profile_id,
                               min_group_size=args.min_group_size)

    # Apply offset and batch_size
    batch = groups[args.offset:args.offset + args.batch_size]

    if not batch:
        print(f"No unprocessed groups at offset={args.offset}")
        db.close()
        return

    # Enrich each group with full lead context
    enriched = []
    for g in batch:
        group_leads = []
        for lid in g["lead_ids"]:
            lead = db.execute("""
                SELECT id, title, description, category, priority, status, source,
                       target_name, depth_tier, recommended_skill, created_at
                FROM leads WHERE id = ?
            """, (lid,)).fetchone()
            if not lead:
                continue

            lead_data = dict(lead)

            # Notes (top 3)
            notes = db.execute(
                "SELECT note FROM lead_notes WHERE lead_id = ? ORDER BY created_at DESC LIMIT 3",
                (lid,)
            ).fetchall()
            lead_data["notes"] = [n["note"] for n in notes]

            # Findings count + top summaries for this target
            if lead["target_name"]:
                findings = db.execute("""
                    SELECT id, summary FROM findings
                    WHERE target_name = ? AND verification_status != 'retracted'
                    ORDER BY id DESC LIMIT 5
                """, (lead["target_name"],)).fetchall()
                lead_data["findings_count"] = len(findings)
                lead_data["finding_summaries"] = [f["summary"][:200] for f in findings[:3]]
            else:
                lead_data["findings_count"] = 0
                lead_data["finding_summaries"] = []

            group_leads.append(lead_data)

        enriched.append({
            "group_hash": g["group_hash"],
            "lead_ids": g["lead_ids"],
            "size": g["size"],
            "targets": g["targets"],
            "leads": group_leads,
        })

    output_path = Path(args.output)
    output_path.write_text(json.dumps(enriched, indent=2, default=str))
    print(f"Exported {len(enriched)} groups ({sum(g['size'] for g in enriched)} leads) to {output_path}")
    print(f"  Offset: {args.offset}, Batch size: {args.batch_size}")
    print(f"  Remaining: {max(0, len(groups) - args.offset - args.batch_size)} groups")

    db.close()


# ---------------------------------------------------------------------------
# apply: Execute subagent dedup decisions
# ---------------------------------------------------------------------------

def cmd_apply(args):
    """Apply subagent dedup decisions from a JSON file."""
    decisions_path = Path(args.decisions_file)
    if not decisions_path.exists():
        print(f"ERROR: decisions file not found: {decisions_path}")
        return

    decisions = json.loads(decisions_path.read_text())
    if not isinstance(decisions, list):
        print("ERROR: decisions file must contain a JSON array")
        return

    db = get_db()

    applied = 0
    skipped = 0
    dead_ended = 0
    errors = 0

    for decision in decisions:
        ghash = decision.get("group_hash")
        action = decision.get("decision")
        keeper_id = decision.get("keeper_id")
        dead_end_ids = decision.get("dead_end_ids", [])
        rationale = decision.get("rationale", "")
        target_fills = decision.get("target_name_fills", {})

        if not ghash or not action:
            print(f"  SKIP: missing group_hash or decision")
            errors += 1
            continue

        # Check if already processed
        existing = db.execute(
            "SELECT id FROM lead_dedup_log WHERE group_hash = ?", (ghash,)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        # Validate keeper exists and is open (for merge/consolidate)
        if action in ("merge", "consolidate") and keeper_id:
            keeper = db.execute(
                "SELECT id, status FROM leads WHERE id = ?", (keeper_id,)
            ).fetchone()
            if not keeper:
                print(f"  ERROR [{ghash[:8]}]: keeper #{keeper_id} not found")
                errors += 1
                continue
            if keeper["status"] not in ("open", "in_progress", "pending_triage"):
                print(f"  ERROR [{ghash[:8]}]: keeper #{keeper_id} is {keeper['status']}")
                errors += 1
                continue

            # Validate keeper is not in dead_end_ids
            if keeper_id in dead_end_ids:
                print(f"  ERROR [{ghash[:8]}]: keeper #{keeper_id} is also in dead_end_ids")
                errors += 1
                continue

        # Apply target_name fills
        for lid_str, target in target_fills.items():
            lid = int(lid_str)
            if not args.dry_run:
                db.execute(
                    "UPDATE leads SET target_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND (target_name IS NULL OR target_name = '')",
                    (target, lid),
                )

        # Execute dead-ends
        if action in ("merge", "consolidate") and dead_end_ids:
            relation_type = "duplicate" if action == "merge" else "supersedes"
            stop_prefix = "Duplicate of" if action == "merge" else "Consolidated into"

            for lid in dead_end_ids:
                lead = db.execute(
                    "SELECT id, status FROM leads WHERE id = ?", (lid,)
                ).fetchone()
                if not lead:
                    print(f"  WARN [{ghash[:8]}]: lead #{lid} not found, skipping")
                    continue
                if lead["status"] not in ("open", "pending_triage"):
                    print(f"  WARN [{ghash[:8]}]: lead #{lid} is {lead['status']}, skipping")
                    continue

                stop_reason = f"{stop_prefix} lead #{keeper_id}"
                if not args.dry_run:
                    db.execute("""
                        UPDATE leads SET status = 'dead_end',
                            stop_reason = ?,
                            updated_at = CURRENT_TIMESTAMP,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status IN ('open', 'pending_triage')
                    """, (stop_reason, lid))

                    # Create lead_relation
                    db.execute("""
                        INSERT OR IGNORE INTO lead_relations
                            (lead_id, related_lead_id, relation_type)
                        VALUES (?, ?, ?)
                    """, (lid, keeper_id, relation_type))

                dead_ended += 1

            if not args.dry_run and action == "consolidate" and keeper_id:
                # Append notes from dead-ended leads to keeper
                for lid in dead_end_ids:
                    old_lead = db.execute(
                        "SELECT title, description FROM leads WHERE id = ?", (lid,)
                    ).fetchone()
                    if old_lead:
                        note = f"Consolidated from lead #{lid}: {old_lead['title']}"
                        if old_lead["description"]:
                            note += f" — {old_lead['description'][:200]}"
                        db.execute(
                            "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
                            (keeper_id, note),
                        )

        # Log the decision
        if not args.dry_run:
            db.execute("""
                INSERT OR IGNORE INTO lead_dedup_log
                    (group_hash, lead_ids, decision, keeper_id, dead_ended_ids, rationale)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                ghash,
                json.dumps(decision.get("lead_ids", dead_end_ids + ([keeper_id] if keeper_id else []))),
                action,
                keeper_id,
                json.dumps(dead_end_ids),
                rationale,
            ))

        applied += 1

        action_str = f"{action}"
        if keeper_id:
            action_str += f" (keeper=#{keeper_id}, dead-ended={len(dead_end_ids)})"
        if args.dry_run:
            print(f"  [DRY RUN] {ghash[:8]}: {action_str}")
        else:
            print(f"  Applied {ghash[:8]}: {action_str}")

    if not args.dry_run:
        db.commit()

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Summary:")
    print(f"  Applied: {applied}")
    print(f"  Skipped (already processed): {skipped}")
    print(f"  Dead-ended: {dead_ended}")
    print(f"  Errors: {errors}")

    db.close()


# ---------------------------------------------------------------------------
# verify: Spot-check recent dedup decisions
# ---------------------------------------------------------------------------

def cmd_verify(args):
    """Spot-check recent dedup decisions for consistency."""
    db = get_db()

    recent = db.execute("""
        SELECT * FROM lead_dedup_log
        ORDER BY decided_at DESC
        LIMIT ?
    """, (args.sample_size,)).fetchall()

    if not recent:
        print("No dedup decisions found.")
        db.close()
        return

    print(f"Verifying {len(recent)} most recent decisions:\n")
    issues = 0

    for entry in recent:
        ghash = entry["group_hash"]
        decision = entry["decision"]
        keeper_id = entry["keeper_id"]
        dead_ended_ids = json.loads(entry["dead_ended_ids"]) if entry["dead_ended_ids"] else []

        status_ok = True

        # Check keeper is still open (for merge/consolidate)
        if keeper_id and decision in ("merge", "consolidate"):
            keeper = db.execute(
                "SELECT id, status FROM leads WHERE id = ?", (keeper_id,)
            ).fetchone()
            if not keeper:
                print(f"  [{ghash[:8]}] ISSUE: keeper #{keeper_id} not found")
                status_ok = False
                issues += 1
            elif keeper["status"] in ("dead_end", "completed"):
                # Check if it was dead-ended by another dedup decision (chain problem)
                chained = db.execute(
                    "SELECT group_hash FROM lead_dedup_log WHERE dead_ended_ids LIKE ?",
                    (f"%{keeper_id}%",)
                ).fetchone()
                if chained:
                    print(f"  [{ghash[:8]}] CHAIN: keeper #{keeper_id} was dead-ended by {chained['group_hash'][:8]}")
                    issues += 1
                    status_ok = False

        # Check dead-ended leads are actually dead-ended
        for lid in dead_ended_ids:
            lead = db.execute(
                "SELECT id, status, stop_reason FROM leads WHERE id = ?", (lid,)
            ).fetchone()
            if lead and lead["status"] != "dead_end":
                print(f"  [{ghash[:8]}] ISSUE: lead #{lid} should be dead_end but is {lead['status']}")
                status_ok = False
                issues += 1

            # Check lead_relation exists
            rel = db.execute(
                "SELECT * FROM lead_relations WHERE lead_id = ? AND related_lead_id = ?",
                (lid, keeper_id)
            ).fetchone()
            if not rel and keeper_id:
                print(f"  [{ghash[:8]}] ISSUE: missing lead_relation #{lid} -> #{keeper_id}")
                status_ok = False
                issues += 1

        if status_ok:
            print(f"  [{ghash[:8]}] OK — {decision} (keeper=#{keeper_id}, dead-ended={len(dead_ended_ids)})")

    print(f"\nVerified: {len(recent)} decisions, {issues} issues found")
    db.close()


# ---------------------------------------------------------------------------
# stats: Dedup metrics
# ---------------------------------------------------------------------------

def cmd_stats(args):
    """Show dedup metrics."""
    db = get_db()

    # Open leads overview
    total_open = db.execute("SELECT COUNT(*) FROM leads WHERE status = 'open'").fetchone()[0]
    no_target = db.execute(
        "SELECT COUNT(*) FROM leads WHERE status = 'open' AND (target_name IS NULL OR target_name = '')"
    ).fetchone()[0]

    print(f"=== Lead Dedup Stats ===")
    print(f"Open leads: {total_open}")
    print(f"Open leads without target_name: {no_target}")

    # Targets with multiple open leads
    multi = db.execute("""
        SELECT COUNT(*) FROM (
            SELECT target_name FROM leads
            WHERE status = 'open' AND target_name IS NOT NULL AND target_name != ''
            GROUP BY target_name HAVING COUNT(*) >= 2
        )
    """).fetchone()[0]
    print(f"Targets with 2+ open leads: {multi}")

    # Dedup log stats
    total_decisions = db.execute("SELECT COUNT(*) FROM lead_dedup_log").fetchone()[0]
    by_decision = db.execute("""
        SELECT decision, COUNT(*) as cnt FROM lead_dedup_log GROUP BY decision
    """).fetchall()

    print(f"\nDedup decisions: {total_decisions}")
    for row in by_decision:
        print(f"  {row['decision']}: {row['cnt']}")

    # Count dead-ended by dedup
    dedup_dead = db.execute("""
        SELECT COUNT(*) FROM leads
        WHERE status = 'dead_end'
          AND (stop_reason LIKE 'Duplicate of lead%'
               OR stop_reason LIKE 'Consolidated into lead%')
    """).fetchone()[0]
    print(f"\nLeads dead-ended by dedup: {dedup_dead}")

    # Lead relations from dedup
    dup_rels = db.execute(
        "SELECT COUNT(*) FROM lead_relations WHERE relation_type = 'duplicate'"
    ).fetchone()[0]
    sup_rels = db.execute(
        "SELECT COUNT(*) FROM lead_relations WHERE relation_type = 'supersedes'"
    ).fetchone()[0]
    print(f"Lead relations (duplicate): {dup_rels}")
    print(f"Lead relations (supersedes): {sup_rels}")

    # Scan preview (groups remaining)
    groups, _ = _build_groups(db, min_group_size=2)
    print(f"\nUnprocessed candidate groups: {len(groups)}")
    if groups:
        total_in_groups = sum(g["size"] for g in groups)
        print(f"Total leads in unprocessed groups: {total_in_groups}")

    db.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Lead deduplication for investigation.db"
    )
    sub = parser.add_subparsers(dest="command")

    # fill-targets
    p_fill = sub.add_parser("fill-targets", help="Infer missing target_name from title patterns")
    p_fill.add_argument("--dry-run", action="store_true")
    p_fill.add_argument("--verbose", "-v", action="store_true")

    # scan
    p_scan = sub.add_parser("scan", help="Group open leads into duplicate clusters")
    p_scan.add_argument("--profile-id", help="Filter by investigation profile")
    p_scan.add_argument("--min-group-size", type=int, default=2)
    p_scan.add_argument("--output", "-o", help="Write full results to JSON file")

    # show-group
    p_show = sub.add_parser("show-group", help="Show all leads in a group")
    p_show.add_argument("identifier", help="Group hash prefix or lead ID")

    # export-batch
    p_export = sub.add_parser("export-batch", help="Export groups for subagent review")
    p_export.add_argument("--batch-size", type=int, default=20)
    p_export.add_argument("--offset", type=int, default=0)
    p_export.add_argument("--output", "-o", required=True)
    p_export.add_argument("--profile-id", help="Filter by investigation profile")
    p_export.add_argument("--min-group-size", type=int, default=2)

    # apply
    p_apply = sub.add_parser("apply", help="Apply subagent dedup decisions")
    p_apply.add_argument("--decisions-file", required=True)
    p_apply.add_argument("--dry-run", action="store_true")

    # verify
    p_verify = sub.add_parser("verify", help="Spot-check recent dedup decisions")
    p_verify.add_argument("--sample-size", type=int, default=10)

    # stats
    sub.add_parser("stats", help="Dedup metrics")

    args = parser.parse_args()

    commands = {
        "fill-targets": cmd_fill_targets,
        "scan": cmd_scan,
        "show-group": cmd_show_group,
        "export-batch": cmd_export_batch,
        "apply": cmd_apply,
        "verify": cmd_verify,
        "stats": cmd_stats,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
