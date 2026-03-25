#!/usr/bin/env python3
"""
Infrastructure request queue for OSINT investigations.

Tracks new data sources, registries, tool improvements, and feature requests
discovered by agents during investigation. Part of investigation.db.

Usage:
    python tools/infra_tracker.py add --title "..." --type new_source --description "..."
    python tools/infra_tracker.py list [--status open] [--type new_source] [--priority high]
    python tools/infra_tracker.py show 12
    python tools/infra_tracker.py claim 12
    python tools/infra_tracker.py evaluate 12 --probe-results "..." --proceed
    python tools/infra_tracker.py note 12 "Progress update"
    python tools/infra_tracker.py complete 12 --tool-file "tools/query_foo.py" --files-modified tools/query_foo.py CLAUDE.md --summary "..."
    python tools/infra_tracker.py reject 12 --reason "..."
    python tools/infra_tracker.py search "vessel"
    python tools/infra_tracker.py next [--type new_source]
    python tools/infra_tracker.py stats
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# Reuse lead_tracker's get_db() which ensures all schema including infra_requests
try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

VALID_TYPES = ["new_source", "new_registry", "tool_improvement", "tool_fix", "new_feature"]
VALID_PRIORITIES = ["critical", "high", "medium", "low"]
VALID_STATUSES = ["open", "evaluating", "in_progress", "completed", "blocked", "rejected"]
VALID_ACCESS_METHODS = ["rest_api", "graphql", "bulk_download", "sftp", "web_scrape", "soda_api", "manual", "sdk", "other"]
VALID_AUTH = ["none", "api_key_free", "api_key_paid", "login_required", "paid_subscription", "other"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── CRUD ────────────────────────────────────────────────────


def add_request(title, request_type, description, source_name=None, source_url=None,
                data_type=None, access_method=None, auth_requirements=None,
                estimated_coverage=None, priority="medium", discovered_by=None,
                discovered_during=None, related_lead_id=None, existing_tool=None):
    """Add a new infrastructure request. Returns the request ID."""
    db = get_db()
    cursor = db.execute("""
        INSERT INTO infra_requests (
            title, description, request_type, priority, source_name, source_url,
            data_type, access_method, auth_requirements, estimated_coverage,
            discovered_by, discovered_during, related_lead_id, tool_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        title, description, request_type, priority, source_name, source_url,
        data_type, access_method, auth_requirements, estimated_coverage,
        discovered_by, discovered_during, related_lead_id, existing_tool,
    ))
    req_id = cursor.lastrowid
    db.commit()
    db.close()
    return req_id


def list_requests(status=None, request_type=None, priority=None, limit=50):
    """List infra requests with optional filters."""
    db = get_db()
    conditions = []
    params = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if request_type:
        conditions.append("request_type = ?")
        params.append(request_type)
    if priority:
        conditions.append("priority = ?")
        params.append(priority)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT * FROM infra_requests {where}
        ORDER BY
            CASE priority
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 WHEN 'low' THEN 3
            END,
            created_at ASC
        LIMIT ?
    """
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_request(req_id):
    """Get a single request with notes and related lead info."""
    db = get_db()
    req = db.execute("SELECT * FROM infra_requests WHERE id = ?", (req_id,)).fetchone()
    if not req:
        db.close()
        return None

    result = dict(req)
    result["notes"] = [dict(n) for n in db.execute(
        "SELECT * FROM infra_notes WHERE infra_id = ? ORDER BY created_at", (req_id,)
    ).fetchall()]

    if result.get("related_lead_id"):
        lead = db.execute(
            "SELECT id, title, status, priority FROM leads WHERE id = ?",
            (result["related_lead_id"],)
        ).fetchone()
        result["related_lead"] = dict(lead) if lead else None

    # Find leads blocked by this infra request
    blocked_leads = db.execute(
        "SELECT id, title, status, priority FROM leads WHERE blocked_by_infra_id = ?",
        (req_id,)
    ).fetchall()
    result["blocked_leads"] = [dict(r) for r in blocked_leads]

    db.close()
    return result


def claim_request(req_id):
    """Set status to evaluating. Only works if currently open."""
    db = get_db()
    now = _utcnow().isoformat()
    cursor = db.execute(
        "UPDATE infra_requests SET status = 'evaluating', claimed_at = ? WHERE id = ? AND status = 'open'",
        (now, req_id)
    )
    db.commit()
    affected = cursor.rowcount
    db.close()
    return affected > 0


def evaluate_request(req_id, probe_results=None, notes=None, action=None):
    """Record evaluation results. action: proceed, reject, or block."""
    db = get_db()
    updates = []
    params = []

    if probe_results:
        updates.append("probe_results = ?")
        params.append(probe_results)
    if notes:
        updates.append("evaluation_notes = ?")
        params.append(notes)
    if action == "proceed":
        updates.append("status = 'in_progress'")
    elif action == "reject":
        updates.append("status = 'rejected'")
    elif action == "block":
        updates.append("status = 'blocked'")

    if not updates:
        db.close()
        return False

    params.append(req_id)
    db.execute(f"UPDATE infra_requests SET {', '.join(updates)} WHERE id = ?", params)

    if notes:
        db.execute(
            "INSERT INTO infra_notes (infra_id, note) VALUES (?, ?)",
            (req_id, f"Evaluation: {notes}")
        )

    db.commit()
    db.close()
    return True


def add_note(req_id, text):
    """Add a progress note."""
    db = get_db()
    db.execute("INSERT INTO infra_notes (infra_id, note) VALUES (?, ?)", (req_id, text))
    db.commit()
    db.close()


def complete_request(req_id, tool_file, files_modified, summary):
    """Mark request as completed."""
    db = get_db()
    now = _utcnow().isoformat()
    files_json = json.dumps(files_modified) if files_modified else None
    db.execute("""
        UPDATE infra_requests
        SET status = 'completed', tool_file = ?, files_modified = ?,
            evaluation_notes = COALESCE(evaluation_notes || '\n', '') || ?,
            completed_at = ?
        WHERE id = ?
    """, (tool_file, files_json, f"Completed: {summary}", now, req_id))

    # Unblock any leads that were waiting on this request
    blocked = db.execute(
        "SELECT id FROM leads WHERE blocked_by_infra_id = ? AND status = 'blocked'",
        (req_id,)
    ).fetchall()
    if blocked:
        for row in blocked:
            db.execute(
                "UPDATE leads SET status = 'open', blocked_by_infra_id = NULL, updated_at = ? WHERE id = ?",
                (now, row["id"])
            )
            db.execute(
                "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
                (row["id"], f"Unblocked: infra request #{req_id} completed ({summary})")
            )

    db.commit()
    unblocked_count = len(blocked)
    db.close()
    return unblocked_count


def reject_request(req_id, reason):
    """Mark request as rejected with reason."""
    db = get_db()
    db.execute(
        "UPDATE infra_requests SET status = 'rejected', evaluation_notes = COALESCE(evaluation_notes || '\\n', '') || ? WHERE id = ?",
        (f"Rejected: {reason}", req_id)
    )
    db.execute(
        "INSERT INTO infra_notes (infra_id, note) VALUES (?, ?)",
        (req_id, f"REJECTED: {reason}")
    )
    db.commit()
    db.close()


def search_requests(query):
    """Full-text search across infra requests."""
    db = get_db()
    safe_query = '"' + query.replace('"', '""') + '"'
    rows = db.execute("""
        SELECT infra_requests.*, infra_requests_fts.rank
        FROM infra_requests_fts
        JOIN infra_requests ON infra_requests.id = infra_requests_fts.rowid
        WHERE infra_requests_fts MATCH ?
        ORDER BY infra_requests_fts.rank
        LIMIT 30
    """, (safe_query,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_next_request(request_type=None):
    """Get highest priority open request."""
    db = get_db()
    conditions = ["status = 'open'"]
    params = []
    if request_type:
        conditions.append("request_type = ?")
        params.append(request_type)

    where = f"WHERE {' AND '.join(conditions)}"
    row = db.execute(f"""
        SELECT * FROM infra_requests {where}
        ORDER BY
            CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END,
            created_at ASC
        LIMIT 1
    """, params).fetchone()
    db.close()
    return dict(row) if row else None


def get_stats():
    """Get summary statistics for infra requests."""
    db = get_db()
    stats = {}

    rows = db.execute("SELECT status, COUNT(*) as cnt FROM infra_requests GROUP BY status").fetchall()
    stats["by_status"] = {r["status"]: r["cnt"] for r in rows}

    rows = db.execute("SELECT request_type, COUNT(*) as cnt FROM infra_requests GROUP BY request_type").fetchall()
    stats["by_type"] = {r["request_type"]: r["cnt"] for r in rows}

    rows = db.execute(
        "SELECT priority, COUNT(*) as cnt FROM infra_requests WHERE status IN ('open','evaluating','in_progress') GROUP BY priority"
    ).fetchall()
    stats["by_priority"] = {r["priority"]: r["cnt"] for r in rows}

    stats["total"] = db.execute("SELECT COUNT(*) FROM infra_requests").fetchone()[0]

    stats["completed_this_week"] = db.execute(
        "SELECT COUNT(*) FROM infra_requests WHERE completed_at > datetime('now', '-7 days')"
    ).fetchone()[0]

    # Leads blocked by infra
    stats["leads_blocked_by_infra"] = db.execute(
        "SELECT COUNT(*) FROM leads WHERE blocked_by_infra_id IS NOT NULL AND status = 'blocked'"
    ).fetchone()[0]

    db.close()
    return stats


def block_lead_on_infra(lead_id, infra_id, reason=None):
    """Block a lead because it depends on an infra request."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute(
        "UPDATE leads SET status = 'blocked', blocked_by_infra_id = ?, updated_at = ? WHERE id = ?",
        (infra_id, now, lead_id)
    )
    note = f"Blocked: waiting on infra request #{infra_id}"
    if reason:
        note += f" — {reason}"
    db.execute(
        "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
        (lead_id, note)
    )
    db.commit()
    db.close()


# ── Display ─────────────────────────────────────────────────


TYPE_ABBREV = {
    "new_source": "new_source ",
    "new_registry": "new_registr",
    "tool_improvement": "tool_improv",
    "tool_fix": "tool_fix   ",
    "new_feature": "new_feature",
}

ACCESS_ABBREV = {
    "rest_api": "rest_api",
    "graphql": "graphql ",
    "bulk_download": "bulk_dl ",
    "sftp": "sftp    ",
    "web_scrape": "web_scrp",
    "soda_api": "soda_api",
    "manual": "manual  ",
    "sdk": "sdk     ",
    "other": "other   ",
    None: "n/a     ",
}


def format_request(req, verbose=False):
    """Format a request for display."""
    status_icons = {
        "open": "[ ]", "evaluating": "[?]", "in_progress": "[~]",
        "completed": "[x]", "blocked": "[!]", "rejected": "[-]",
    }
    icon = status_icons.get(req["status"], "[?]")
    prio = {"critical": "!!!!", "high": "!!!", "medium": "!!", "low": "!"}.get(req["priority"], "")
    rtype = TYPE_ABBREV.get(req["request_type"], req["request_type"])
    access = ACCESS_ABBREV.get(req.get("access_method"), "n/a     ")

    title = req["title"]
    if req.get("estimated_coverage"):
        title += f" — {req['estimated_coverage']}"

    line = f"{icon} #{req['id']:>4} {prio:<4} [{rtype}] {access}  {title}"

    if verbose:
        if req.get("description"):
            line += f"\n       Description: {req['description']}"
        if req.get("source_name"):
            line += f"\n       Source: {req['source_name']}"
        if req.get("source_url"):
            line += f"\n       URL: {req['source_url']}"
        if req.get("data_type"):
            line += f"\n       Data type: {req['data_type']}"
        if req.get("auth_requirements"):
            line += f"\n       Auth: {req['auth_requirements']}"
        if req.get("discovered_by"):
            line += f"\n       Discovered by: {req['discovered_by']}"
        if req.get("discovered_during"):
            line += f"\n       During: {req['discovered_during']}"
        if req.get("related_lead_id"):
            line += f"\n       Related lead: #{req['related_lead_id']}"
        if req.get("probe_results"):
            line += f"\n       Probe: {req['probe_results']}"
        if req.get("evaluation_notes"):
            line += f"\n       Notes: {req['evaluation_notes']}"
        if req.get("tool_file"):
            line += f"\n       Tool: {req['tool_file']}"
        if req.get("files_modified"):
            line += f"\n       Files: {req['files_modified']}"
        if req.get("notes"):
            for n in req["notes"]:
                line += f"\n       Note ({n['created_at']}): {n['note']}"
        if req.get("blocked_leads"):
            for bl in req["blocked_leads"]:
                line += f"\n       Blocks lead: #{bl['id']} {bl['title']} [{bl['status']}]"

    return line


# ── CLI ─────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Infrastructure request tracker")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # add
    add_p = subparsers.add_parser("add", help="Add a new infra request")
    add_p.add_argument("--title", "-t", required=True)
    add_p.add_argument("--type", dest="request_type", required=True, choices=VALID_TYPES)
    add_p.add_argument("--description", "-d", required=True)
    add_p.add_argument("--source-name")
    add_p.add_argument("--source-url")
    add_p.add_argument("--data-type")
    add_p.add_argument("--access-method", choices=VALID_ACCESS_METHODS)
    add_p.add_argument("--auth", choices=VALID_AUTH)
    add_p.add_argument("--coverage")
    add_p.add_argument("--priority", "-p", choices=VALID_PRIORITIES, default="medium")
    add_p.add_argument("--discovered-by")
    add_p.add_argument("--discovered-during")
    add_p.add_argument("--related-lead", type=int)
    add_p.add_argument("--existing-tool", help="Existing tool file to improve (for tool_improvement/tool_fix)")

    # list
    list_p = subparsers.add_parser("list", help="List infra requests")
    list_p.add_argument("--status", choices=VALID_STATUSES)
    list_p.add_argument("--type", dest="request_type", choices=VALID_TYPES)
    list_p.add_argument("--priority", choices=VALID_PRIORITIES)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("-v", "--verbose", action="store_true")
    add_output_args(list_p)

    # show
    show_p = subparsers.add_parser("show", help="Show request details")
    show_p.add_argument("id", type=int)
    add_output_args(show_p)

    # claim
    claim_p = subparsers.add_parser("claim", help="Claim an open request for evaluation")
    claim_p.add_argument("id", type=int)

    # evaluate
    eval_p = subparsers.add_parser("evaluate", help="Record evaluation results")
    eval_p.add_argument("id", type=int)
    eval_p.add_argument("--probe-results", help="Results of endpoint probing")
    eval_p.add_argument("--notes", help="Evaluation notes")
    action_group = eval_p.add_mutually_exclusive_group()
    action_group.add_argument("--proceed", action="store_const", const="proceed", dest="action", help="Move to in_progress")
    action_group.add_argument("--reject", action="store_const", const="reject", dest="action", help="Reject the request")
    action_group.add_argument("--block", action="store_const", const="block", dest="action", help="Mark as blocked")

    # note
    note_p = subparsers.add_parser("note", help="Add a progress note")
    note_p.add_argument("id", type=int)
    note_p.add_argument("text")

    # complete
    comp_p = subparsers.add_parser("complete", help="Mark request as completed")
    comp_p.add_argument("id", type=int)
    comp_p.add_argument("--tool-file", required=True, help="Primary tool file created/modified")
    comp_p.add_argument("--files-modified", nargs="+", help="All files created or modified")
    comp_p.add_argument("--summary", required=True, help="Completion summary")

    # reject
    rej_p = subparsers.add_parser("reject", help="Reject a request")
    rej_p.add_argument("id", type=int)
    rej_p.add_argument("--reason", required=True)

    # search
    search_p = subparsers.add_parser("search", help="Full-text search")
    search_p.add_argument("query")
    add_output_args(search_p)

    # next
    next_p = subparsers.add_parser("next", help="Get next request to work on")
    next_p.add_argument("--type", dest="request_type", choices=VALID_TYPES)
    add_output_args(next_p)

    # stats
    stats_p = subparsers.add_parser("stats", help="Show statistics")
    add_output_args(stats_p)

    # block-lead — link a lead to an infra request
    bl_p = subparsers.add_parser("block-lead", help="Block a lead pending an infra request")
    bl_p.add_argument("lead_id", type=int, help="Lead to block")
    bl_p.add_argument("infra_id", type=int, help="Infra request it depends on")
    bl_p.add_argument("--reason", help="Why the lead is blocked")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "add":
        # Validation
        if len(args.description) < 20:
            print("Error: description must be at least 20 characters.", file=sys.stderr)
            sys.exit(1)
        if args.request_type in ("new_source", "new_registry") and not args.source_name:
            print(f"Warning: --source-name recommended for {args.request_type} requests.", file=sys.stderr)

        req_id = add_request(
            title=args.title, request_type=args.request_type, description=args.description,
            source_name=args.source_name, source_url=args.source_url,
            data_type=args.data_type, access_method=args.access_method,
            auth_requirements=args.auth, estimated_coverage=args.coverage,
            priority=args.priority, discovered_by=args.discovered_by,
            discovered_during=args.discovered_during, related_lead_id=args.related_lead,
            existing_tool=args.existing_tool,
        )
        print(f"Created infra request #{req_id}: {args.title}")

    elif args.command == "list":
        reqs = list_requests(
            status=args.status, request_type=args.request_type,
            priority=args.priority, limit=args.limit,
        )
        if not write_output(reqs, args, summary=f"infra list: {len(reqs)} requests"):
            if not reqs:
                print("No infra requests found matching filters.")
            else:
                for r in reqs:
                    print(format_request(r, verbose=args.verbose))

    elif args.command == "show":
        req = get_request(args.id)
        if not req:
            print(f"Infra request #{args.id} not found.", file=sys.stderr)
            sys.exit(1)
        if not write_output(req, args, summary=f"infra #{args.id}"):
            print(format_request(req, verbose=True))

    elif args.command == "claim":
        if claim_request(args.id):
            print(f"Claimed infra request #{args.id} (status → evaluating)")
        else:
            print(f"Could not claim #{args.id} (may not be open)", file=sys.stderr)

    elif args.command == "evaluate":
        if not args.probe_results and not args.notes and not args.action:
            print("Error: provide at least --probe-results, --notes, or an action flag.", file=sys.stderr)
            sys.exit(1)
        if evaluate_request(args.id, probe_results=args.probe_results, notes=args.notes, action=args.action):
            status_msg = f" → {args.action}" if args.action else ""
            print(f"Updated infra request #{args.id}{status_msg}")
        else:
            print(f"No changes made to #{args.id}", file=sys.stderr)

    elif args.command == "note":
        add_note(args.id, args.text)
        print(f"Added note to infra request #{args.id}")

    elif args.command == "complete":
        unblocked = complete_request(args.id, args.tool_file, args.files_modified, args.summary)
        print(f"Completed infra request #{args.id}: {args.summary}")
        if unblocked:
            print(f"  Unblocked {unblocked} lead(s)")

    elif args.command == "reject":
        reject_request(args.id, args.reason)
        print(f"Rejected infra request #{args.id}: {args.reason}")

    elif args.command == "search":
        results = search_requests(args.query)
        if not write_output(results, args, summary=f"infra search '{args.query}': {len(results)} results"):
            if not results:
                print(f"No infra requests matching '{args.query}'")
            else:
                print(f"Found {len(results)} infra requests matching '{args.query}':")
                for r in results:
                    print(format_request(r))

    elif args.command == "next":
        req = get_next_request(args.request_type)
        if req:
            full = get_request(req["id"])
            if not write_output(full, args, summary=f"next infra: #{req['id']}"):
                print("Next infra request:")
                print(format_request(full, verbose=True))
        else:
            type_msg = f" of type '{args.request_type}'" if args.request_type else ""
            print(f"No open infra requests{type_msg}.")

    elif args.command == "stats":
        stats = get_stats()
        if not write_output(stats, args, summary=f"infra stats: {stats['total']} total"):
            print(f"Total infra requests: {stats['total']}")
            if stats.get("by_status"):
                print("\nBy status:")
                for s, c in sorted(stats["by_status"].items()):
                    print(f"  {s}: {c}")
            if stats.get("by_type"):
                print("\nBy type:")
                for t, c in sorted(stats["by_type"].items()):
                    print(f"  {t}: {c}")
            if stats.get("by_priority"):
                print("\nOpen/active by priority:")
                for p, c in sorted(stats["by_priority"].items()):
                    print(f"  {p}: {c}")
            print(f"\nCompleted this week: {stats['completed_this_week']}")
            if stats["leads_blocked_by_infra"] > 0:
                print(f"\n** {stats['leads_blocked_by_infra']} leads blocked waiting on infra **")

    elif args.command == "block-lead":
        block_lead_on_infra(args.lead_id, args.infra_id, args.reason)
        print(f"Lead #{args.lead_id} blocked on infra request #{args.infra_id}")


if __name__ == "__main__":
    main()
