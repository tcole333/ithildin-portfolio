#!/usr/bin/env python3
"""
Queue management CLI (SQLite-first).

Usage:
  python scripts/queue_tools.py submit --type echo --domain system --payload '{"message":"hi"}'
  python scripts/queue_tools.py status
  python scripts/queue_tools.py list --status pending --limit 20
  python scripts/queue_tools.py show <job_id>
  python scripts/queue_tools.py pause --by "human"
  python scripts/queue_tools.py resume --by "human"
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queue_system.queue import DEFAULT_DB_PATH, JobQueue


def _queue(args) -> JobQueue:
    db_path = getattr(args, "db_path", None)
    if db_path:
        return JobQueue(db_path=Path(db_path))
    return JobQueue()


def _load_payload(payload_str: Optional[str], payload_file: Optional[str]) -> dict:
    if payload_file:
        raw = Path(payload_file).read_text()
        return json.loads(raw)
    if payload_str:
        return json.loads(payload_str)
    return {}


def cmd_submit(args):
    queue = _queue(args)
    payload = _load_payload(args.payload, args.payload_file)
    job_id = queue.create_job(
        job_type=args.type,
        domain=args.domain,
        payload=payload,
        priority=args.priority,
        created_by=args.created_by,
        scheduled_for=args.scheduled_for,
    )
    print(f"Job submitted: {job_id}")


def _priority_to_int(priority: str) -> int:
    mapping = {
        "critical": 10,
        "high": 7,
        "medium": 5,
        "low": 3,
    }
    return mapping.get(priority, 5)


def cmd_enqueue_triage(args):
    from tools import lead_tracker

    db = lead_tracker.get_db()
    total = db.execute(
        "SELECT COUNT(*) as n FROM leads WHERE status='pending_triage'"
    ).fetchone()["n"]
    db.close()

    if total == 0 and not args.force:
        print("No pending_triage leads found. Use --force to enqueue anyway.")
        return

    payload = {
        "batch_size": args.batch_size,
        "dry_run": args.dry_run,
        "triaged_by": args.triaged_by,
    }
    queue = _queue(args)
    job_id = queue.create_job(
        job_type="lead_triage",
        domain="discovery",
        payload=payload,
        priority=6,
        created_by=args.created_by,
    )
    print(f"Job submitted: {job_id}")


def cmd_enqueue_lead(args):
    from tools import lead_tracker

    db = lead_tracker.get_db()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
    db.close()

    if not lead:
        print("Lead not found.")
        return

    target = lead["target_name"] or lead["title"]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None

    payload = {
        "target_name": target,
        "lead_id": lead["id"],
        "limit": args.limit,
    }
    if sources is not None:
        payload["sources"] = sources

    queue = _queue(args)
    job_id = queue.create_job(
        job_type=args.job_type,
        domain=args.domain,
        payload=payload,
        priority=_priority_to_int(lead["priority"]),
        created_by=args.created_by,
        source_lead_id=lead["id"],
    )
    print(f"Job submitted: {job_id}")


def cmd_status(args):
    queue = _queue(args)
    paused = queue.is_paused()
    status_counts = queue.status_counts()
    domain_counts = queue.domain_counts()

    print(f"Paused: {'yes' if paused else 'no'}")
    print("\nSTATUS COUNTS:")
    for status, count in sorted(status_counts.items()):
        print(f"  {status:<14} {count}")

    print("\nPENDING BY DOMAIN:")
    if not domain_counts:
        print("  (none)")
    else:
        for domain, count in sorted(domain_counts.items()):
            print(f"  {domain:<14} {count}")


def cmd_list(args):
    queue = _queue(args)
    jobs = queue.list_jobs(
        status=args.status,
        domain=args.domain,
        job_type=args.type,
        limit=args.limit,
    )
    for job in jobs:
        print(f"{job['id']}  {job['status']:<12} {job['job_type']:<18} {job['domain']:<14} {job['created_at']}")


def cmd_show(args):
    queue = _queue(args)
    job = queue.get_job(args.job_id)
    if not job:
        print("Job not found.")
        return
    print(json.dumps(job, indent=2, default=str))

def cmd_agents(args):
    queue = _queue(args)
    agents = queue.list_agents(status=args.status, limit=args.limit)
    for agent in agents:
        current = agent.get("current_job_id") or "-"
        print(
            f"{agent['id']}  {agent['persona']:<14} {agent['status']:<8} "
            f"{current:<36} {agent.get('last_heartbeat')}"
        )


def cmd_metrics(args):
    queue = _queue(args)
    metrics = queue.sample_metrics()
    print(json.dumps(metrics, indent=2, default=str))


def cmd_mark_stale(args):
    queue = _queue(args)
    marked = queue.mark_stale_jobs(grace_seconds=args.grace_seconds)
    print(f"Marked stale jobs: {marked}")


def cmd_pause(args, paused: bool):
    queue = _queue(args)
    queue.set_paused(paused, updated_by=args.by)
    print(f"Paused set to {'true' if paused else 'false'}")


def main():
    parser = argparse.ArgumentParser(description="Queue management CLI")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to queue DB (default: {DEFAULT_DB_PATH})",
    )
    sub = parser.add_subparsers(dest="command")

    p_submit = sub.add_parser("submit", help="Submit a job")
    p_submit.add_argument("--type", required=True, help="Job type")
    p_submit.add_argument("--domain", required=True, help="Job domain")
    p_submit.add_argument("--payload", help="JSON payload")
    p_submit.add_argument("--payload-file", help="Path to JSON payload file")
    p_submit.add_argument("--priority", type=int, default=5, help="Priority 1-10")
    p_submit.add_argument("--created-by", help="Creator identifier")
    p_submit.add_argument("--scheduled-for", help="Schedule timestamp (YYYY-MM-DD HH:MM:SS)")
    p_submit.set_defaults(func=cmd_submit)

    p_triage = sub.add_parser("enqueue-triage", help="Create a lead_triage job")
    p_triage.add_argument("--batch-size", type=int, default=20)
    p_triage.add_argument("--dry-run", action="store_true")
    p_triage.add_argument("--triaged-by", default="agent:lead_triage")
    p_triage.add_argument("--created-by", help="Creator identifier")
    p_triage.add_argument("--force", action="store_true", help="Enqueue even if no pending leads")
    p_triage.set_defaults(func=cmd_enqueue_triage)

    p_lead = sub.add_parser("enqueue-lead", help="Create a job from a lead")
    p_lead.add_argument("lead_id", type=int)
    p_lead.add_argument("--job-type", default="deep_person")
    p_lead.add_argument("--domain", default="investigation")
    p_lead.add_argument("--limit", type=int, default=20)
    p_lead.add_argument("--sources", help="Comma-separated source list")
    p_lead.add_argument("--created-by", help="Creator identifier")
    p_lead.set_defaults(func=cmd_enqueue_lead)

    p_status = sub.add_parser("status", help="Show queue status")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="List jobs")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--domain", help="Filter by domain")
    p_list.add_argument("--type", help="Filter by job type")
    p_list.add_argument("--limit", type=int, default=50, help="Max results")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show job details")
    p_show.add_argument("job_id", help="Job ID")
    p_show.set_defaults(func=cmd_show)

    p_agents = sub.add_parser("agents", help="List agent instances")
    p_agents.add_argument("--status", help="Filter by status")
    p_agents.add_argument("--limit", type=int, default=50, help="Max results")
    p_agents.set_defaults(func=cmd_agents)

    p_metrics = sub.add_parser("metrics", help="Sample queue metrics")
    p_metrics.set_defaults(func=cmd_metrics)

    p_stale = sub.add_parser("mark-stale", help="Mark stale in-progress jobs")
    p_stale.add_argument("--grace-seconds", type=int, default=0)
    p_stale.set_defaults(func=cmd_mark_stale)

    p_pause = sub.add_parser("pause", help="Pause queue claiming")
    p_pause.add_argument("--by", help="Updated by")
    p_pause.set_defaults(func=lambda a: cmd_pause(a, True))

    p_resume = sub.add_parser("resume", help="Resume queue claiming")
    p_resume.add_argument("--by", help="Updated by")
    p_resume.set_defaults(func=lambda a: cmd_pause(a, False))

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
