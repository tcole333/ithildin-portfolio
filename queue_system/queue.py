#!/usr/bin/env python3
"""
SQLite-backed job queue for the Ithildin platform.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from ithildin.core.paths import investigation_db_path

DEFAULT_DB_PATH = investigation_db_path()
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF = 0.1
DEFAULT_JOB_MAX_ATTEMPTS = 3
DEFAULT_JOB_RETRY_DELAY_SECONDS = 300
DEFAULT_JOB_TIMEOUT_SECONDS = 1800
DEFAULT_RETRY_DELAY_MULTIPLIER = 5


class JobQueue:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        retry_delay_multiplier: int = DEFAULT_RETRY_DELAY_MULTIPLIER,
    ) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self.busy_timeout_ms = busy_timeout_ms
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.retry_delay_multiplier = retry_delay_multiplier
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return db

    def _with_retry(self, func):
        last_err = None
        for attempt in range(self.retry_attempts):
            try:
                return func()
            except sqlite3.OperationalError as exc:
                last_err = exc
                msg = str(exc).lower()
                if "database is locked" in msg or "database is busy" in msg:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise
        if last_err:
            raise last_err

    def _ensure_schema(self) -> None:
        def _apply():
            db = self._connect()
            try:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS job_queue (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        domain TEXT NOT NULL CHECK (domain IN (
                            'discovery', 'investigation', 'analysis', 'understanding',
                            'curation', 'infrastructure', 'system'
                        )),
                        priority INTEGER NOT NULL DEFAULT 5 CHECK (priority >= 1 AND priority <= 10),
                        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending', 'claimed', 'in_progress', 'awaiting_review',
                            'completed', 'failed', 'blocked', 'stale', 'cancelled'
                        )),
                        payload TEXT NOT NULL DEFAULT '{}',
                        output TEXT DEFAULT NULL,
                        error_message TEXT DEFAULT NULL,
                        error_traceback TEXT DEFAULT NULL,
                        parent_job_id TEXT REFERENCES job_queue(id) ON DELETE SET NULL,
                        thread_id TEXT REFERENCES job_queue(id) ON DELETE SET NULL,
                        claimed_by TEXT DEFAULT NULL,
                        claimed_at TIMESTAMP DEFAULT NULL,
                        started_at TIMESTAMP DEFAULT NULL,
                        completed_at TIMESTAMP DEFAULT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        retry_delay_seconds INTEGER DEFAULT 300,
                        timeout_seconds INTEGER DEFAULT 1800,
                        stale_after TIMESTAMP DEFAULT NULL,
                        scheduled_for TIMESTAMP DEFAULT NULL,
                        cron_expression TEXT DEFAULT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_by TEXT DEFAULT NULL,
                        tags TEXT DEFAULT '[]',
                        workdir_path TEXT DEFAULT NULL,
                        source_trigger TEXT DEFAULT NULL,
                        source_finding_id INTEGER DEFAULT NULL,
                        source_lead_id INTEGER DEFAULT NULL,
                        search_queries TEXT DEFAULT '[]'
                    );

                    CREATE TABLE IF NOT EXISTS job_dependencies (
                        job_id TEXT NOT NULL REFERENCES job_queue(id) ON DELETE CASCADE,
                        depends_on_job_id TEXT NOT NULL REFERENCES job_queue(id) ON DELETE CASCADE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (job_id, depends_on_job_id)
                    );

                    CREATE TABLE IF NOT EXISTS job_events (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES job_queue(id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL CHECK (event_type IN (
                            'created', 'claimed', 'started', 'progress', 'completed',
                            'failed', 'blocked', 'unblocked', 'stale', 'cancelled',
                            'retry_scheduled', 'spawned_child', 'dependency_added'
                        )),
                        payload TEXT DEFAULT NULL,
                        agent_id TEXT DEFAULT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS agent_instances (
                        id TEXT PRIMARY KEY,
                        persona TEXT NOT NULL,
                        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'paused', 'stopped')),
                        current_job_id TEXT REFERENCES job_queue(id) ON DELETE SET NULL,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        jobs_completed INTEGER DEFAULT 0,
                        jobs_failed INTEGER DEFAULT 0,
                        capabilities TEXT DEFAULT '[]',
                        version TEXT DEFAULT '1.0.0'
                    );

                    CREATE TABLE IF NOT EXISTS queue_metrics (
                        id TEXT PRIMARY KEY,
                        sampled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        pending_count INTEGER DEFAULT 0,
                        claimed_count INTEGER DEFAULT 0,
                        in_progress_count INTEGER DEFAULT 0,
                        awaiting_review_count INTEGER DEFAULT 0,
                        failed_count INTEGER DEFAULT 0,
                        discovery_pending INTEGER DEFAULT 0,
                        investigation_pending INTEGER DEFAULT 0,
                        analysis_pending INTEGER DEFAULT 0,
                        understanding_pending INTEGER DEFAULT 0,
                        infrastructure_pending INTEGER DEFAULT 0,
                        jobs_completed_1h INTEGER DEFAULT 0,
                        jobs_failed_1h INTEGER DEFAULT 0,
                        avg_processing_time_seconds REAL DEFAULT 0,
                        active_agents INTEGER DEFAULT 0,
                        idle_agents INTEGER DEFAULT 0,
                        has_stuck_jobs INTEGER DEFAULT 0,
                        has_failed_jobs INTEGER DEFAULT 0,
                        queue_depth_critical INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS system_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_by TEXT DEFAULT NULL
                    );

                    INSERT OR IGNORE INTO system_state (key, value) VALUES ('paused', 'false');

                    CREATE INDEX IF NOT EXISTS idx_job_queue_status_priority
                        ON job_queue(status, priority DESC, created_at);
                    CREATE INDEX IF NOT EXISTS idx_job_queue_type_pending
                        ON job_queue(job_type) WHERE status = 'pending';
                    CREATE INDEX IF NOT EXISTS idx_job_queue_domain_pending
                        ON job_queue(domain) WHERE status = 'pending';
                    CREATE INDEX IF NOT EXISTS idx_job_queue_claimed
                        ON job_queue(claimed_by) WHERE status = 'in_progress';
                    CREATE INDEX IF NOT EXISTS idx_job_queue_parent
                        ON job_queue(parent_job_id);
                    CREATE INDEX IF NOT EXISTS idx_job_queue_thread
                        ON job_queue(thread_id);
                    CREATE INDEX IF NOT EXISTS idx_job_queue_scheduled
                        ON job_queue(scheduled_for) WHERE scheduled_for IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS idx_job_queue_pending
                        ON job_queue(status, priority DESC, created_at)
                        WHERE status = 'pending';

                    CREATE INDEX IF NOT EXISTS idx_job_dependencies_blocked
                        ON job_dependencies(depends_on_job_id);

                    CREATE INDEX IF NOT EXISTS idx_job_events_job
                        ON job_events(job_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_job_events_type
                        ON job_events(event_type, created_at DESC);

                    CREATE INDEX IF NOT EXISTS idx_agent_instances_status
                        ON agent_instances(status, last_heartbeat);
                    """
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_apply)

    def _parse_json(self, value: Optional[str]) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("payload", "output", "tags", "search_queries", "capabilities"):
            data[key] = self._parse_json(data.get(key))
        return data

    def is_paused(self) -> bool:
        def _check():
            db = self._connect()
            try:
                row = db.execute(
                    "SELECT value FROM system_state WHERE key='paused'"
                ).fetchone()
                return row and row["value"] == "true"
            finally:
                db.close()

        return bool(self._with_retry(_check))

    def set_paused(self, paused: bool, updated_by: Optional[str] = None) -> None:
        def _set():
            db = self._connect()
            try:
                db.execute(
                    "UPDATE system_state SET value=?, updated_at=CURRENT_TIMESTAMP, updated_by=? "
                    "WHERE key='paused'",
                    ("true" if paused else "false", updated_by),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_set)

    def create_job(
        self,
        job_type: str,
        domain: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: int = 5,
        status: str = "pending",
        created_by: Optional[str] = None,
        tags: Optional[List[str]] = None,
        scheduled_for: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        source_trigger: Optional[str] = None,
        source_finding_id: Optional[int] = None,
        source_lead_id: Optional[int] = None,
        depends_on: Optional[Iterable[str]] = None,
        max_attempts: Optional[int] = None,
        retry_delay_seconds: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
    ) -> str:
        job_id = str(uuid4())
        payload_json = json.dumps(payload or {})
        tags_json = json.dumps(tags or [])
        dependencies = list(dict.fromkeys(depends_on or []))
        max_attempts = max_attempts if max_attempts is not None else DEFAULT_JOB_MAX_ATTEMPTS
        retry_delay_seconds = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else DEFAULT_JOB_RETRY_DELAY_SECONDS
        )
        timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else DEFAULT_JOB_TIMEOUT_SECONDS
        )

        def _insert():
            db = self._connect()
            try:
                status_to_use = status
                error_message = None

                depth_limit = max_depth
                if depth_limit is None:
                    env_val = os.environ.get("ITHILDIN_MAX_DEPTH")
                    depth_limit = int(env_val) if env_val else None

                child_limit = max_children
                if child_limit is None:
                    env_val = os.environ.get("ITHILDIN_MAX_CHILDREN")
                    child_limit = int(env_val) if env_val else None

                if parent_job_id:
                    if depth_limit is not None:
                        depth = self._job_depth(db, parent_job_id)
                        if depth + 1 > depth_limit:
                            status_to_use = "cancelled"
                            error_message = f"max_depth_exceeded:{depth_limit}"
                    if child_limit is not None and status_to_use != "cancelled":
                        child_count = self._child_count(db, parent_job_id)
                        if child_count >= child_limit:
                            status_to_use = "cancelled"
                            error_message = f"max_children_exceeded:{child_limit}"

                if dependencies and status == "pending":
                    if not self._dependencies_complete(db, dependencies):
                        status_to_use = "blocked"

                db.execute(
                    """
                    INSERT INTO job_queue (
                        id, job_type, domain, priority, status, payload, created_by,
                        tags, scheduled_for, parent_job_id, thread_id,
                        max_attempts, retry_delay_seconds, timeout_seconds,
                        source_trigger, source_finding_id, source_lead_id,
                        error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        domain,
                        priority,
                        status_to_use,
                        payload_json,
                        created_by,
                        tags_json,
                        scheduled_for,
                        parent_job_id,
                        thread_id,
                        max_attempts,
                        retry_delay_seconds,
                        timeout_seconds,
                        source_trigger,
                        source_finding_id,
                        source_lead_id,
                        error_message,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO job_events (id, job_id, event_type, payload, agent_id)
                    VALUES (?, ?, 'created', ?, ?)
                    """,
                    (str(uuid4()), job_id, payload_json, created_by),
                )
                if status_to_use == "cancelled":
                    db.execute(
                        """
                        INSERT INTO job_events (id, job_id, event_type, payload)
                        VALUES (?, ?, 'cancelled', ?)
                        """,
                        (str(uuid4()), job_id, error_message or "cancelled"),
                    )
                for dep_id in dependencies:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO job_dependencies (job_id, depends_on_job_id)
                        VALUES (?, ?)
                        """,
                        (job_id, dep_id),
                    )
                    db.execute(
                        """
                        INSERT INTO job_events (id, job_id, event_type, payload)
                        VALUES (?, ?, 'dependency_added', ?)
                        """,
                        (
                            str(uuid4()),
                            job_id,
                            json.dumps({"depends_on": dep_id}),
                        ),
                    )
                db.commit()
            finally:
                db.close()

        self._with_retry(_insert)
        return job_id

    def add_dependencies(self, job_id: str, depends_on: Iterable[str]) -> None:
        dependencies = list(dict.fromkeys(depends_on))
        if not dependencies:
            return

        def _add():
            db = self._connect()
            try:
                for dep_id in dependencies:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO job_dependencies (job_id, depends_on_job_id)
                        VALUES (?, ?)
                        """,
                        (job_id, dep_id),
                    )
                    db.execute(
                        """
                        INSERT INTO job_events (id, job_id, event_type, payload)
                        VALUES (?, ?, 'dependency_added', ?)
                        """,
                        (
                            str(uuid4()),
                            job_id,
                            json.dumps({"depends_on": dep_id}),
                        ),
                    )

                remaining = self._dependencies_remaining(db, job_id)
                if remaining > 0:
                    db.execute(
                        "UPDATE job_queue SET status='blocked' WHERE id=? AND status='pending'",
                        (job_id,),
                    )
                db.commit()
            finally:
                db.close()

        self._with_retry(_add)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        def _fetch():
            db = self._connect()
            try:
                row = db.execute(
                    "SELECT * FROM job_queue WHERE id=?",
                    (job_id,),
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                db.close()

        return self._with_retry(_fetch)

    def list_jobs(
        self,
        status: Optional[str] = None,
        domain: Optional[str] = None,
        job_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        def _fetch():
            db = self._connect()
            try:
                clauses = []
                params: List[Any] = []
                if status:
                    clauses.append("status = ?")
                    params.append(status)
                if domain:
                    clauses.append("domain = ?")
                    params.append(domain)
                if job_type:
                    clauses.append("job_type = ?")
                    params.append(job_type)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                query = (
                    "SELECT * FROM job_queue "
                    f"{where} "
                    "ORDER BY created_at DESC "
                    "LIMIT ?"
                )
                params.append(limit)
                rows = db.execute(query, params).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                db.close()

        return self._with_retry(_fetch)

    def _select_next(self, db: sqlite3.Connection, capabilities: Iterable[str]) -> Optional[sqlite3.Row]:
        clauses = [
            "status = 'pending'",
            "(scheduled_for IS NULL OR scheduled_for <= CURRENT_TIMESTAMP)",
            """
            NOT EXISTS (
                SELECT 1 FROM job_dependencies jd
                LEFT JOIN job_queue jq ON jd.depends_on_job_id = jq.id
                WHERE jd.job_id = job_queue.id
                  AND (jq.id IS NULL OR jq.status != 'completed')
            )
            """,
        ]
        params: List[Any] = []
        caps = list(capabilities) if capabilities else []
        if caps:
            placeholders = ",".join("?" for _ in caps)
            clauses.append(f"job_type IN ({placeholders})")
            params.extend(caps)
        where = " AND ".join(clauses)
        query = f"""
            SELECT * FROM job_queue
            WHERE {where}
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
        """
        return db.execute(query, params).fetchone()

    def claim_next(self, agent_id: str, capabilities: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
        if self.is_paused():
            return None

        def _claim():
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = self._select_next(db, capabilities or [])
                if not row:
                    db.commit()
                    return None
                updated = db.execute(
                    """
                    UPDATE job_queue
                    SET status='claimed', claimed_by=?, claimed_at=CURRENT_TIMESTAMP, attempts=attempts+1
                    WHERE id=? AND status='pending'
                    """,
                    (agent_id, row["id"]),
                )
                if updated.rowcount != 1:
                    db.commit()
                    return None
                db.execute(
                    "INSERT INTO job_events (id, job_id, event_type, agent_id) "
                    "VALUES (?, ?, 'claimed', ?)",
                    (str(uuid4()), row["id"], agent_id),
                )
                db.commit()
                return self._row_to_dict(row)
            finally:
                db.close()

        return self._with_retry(_claim)

    def start_job(self, job_id: str, agent_id: Optional[str] = None) -> None:
        def _start():
            db = self._connect()
            try:
                db.execute(
                    """
                    UPDATE job_queue
                    SET status='in_progress',
                        started_at=CURRENT_TIMESTAMP,
                        stale_after=datetime('now', '+' || timeout_seconds || ' seconds')
                    WHERE id=?
                    """,
                    (job_id,),
                )
                db.execute(
                    "INSERT INTO job_events (id, job_id, event_type, agent_id) "
                    "VALUES (?, ?, 'started', ?)",
                    (str(uuid4()), job_id, agent_id),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_start)

    def complete_job(
        self,
        job_id: str,
        output: Optional[Dict[str, Any]] = None,
        status: str = "completed",
    ) -> None:
        output_json = json.dumps(output or {})

        def _complete():
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                if status == "completed":
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status='completed', completed_at=CURRENT_TIMESTAMP, output=?
                        WHERE id=?
                        """,
                        (output_json, job_id),
                    )
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'completed', ?)",
                        (str(uuid4()), job_id, output_json),
                    )
                    self._unblock_dependents(db, job_id)
                else:
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status=?, output=?
                        WHERE id=?
                        """,
                        (status, output_json, job_id),
                    )
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'progress', ?)",
                        (str(uuid4()), job_id, json.dumps({"status": status, "output": output})),
                    )
                db.commit()
            finally:
                db.close()

        self._with_retry(_complete)

    def set_status(
        self,
        job_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        def _set():
            db = self._connect()
            try:
                completed_at = None
                if status in {"completed", "failed", "cancelled", "stale"}:
                    completed_at = "CURRENT_TIMESTAMP"
                if completed_at:
                    db.execute(
                        f"""
                        UPDATE job_queue
                        SET status=?, completed_at={completed_at}, error_message=?
                        WHERE id=?
                        """,
                        (status, error_message, job_id),
                    )
                else:
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status=?, error_message=?
                        WHERE id=?
                        """,
                        (status, error_message, job_id),
                    )

                event_type = status if status in {"failed", "blocked", "cancelled", "stale"} else "progress"
                db.execute(
                    "INSERT INTO job_events (id, job_id, event_type, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (str(uuid4()), job_id, event_type, error_message or ""),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_set)

    def fail_job(
        self,
        job_id: str,
        error_message: str,
        error_traceback: Optional[str] = None,
    ) -> None:
        def _fail():
            db = self._connect()
            try:
                row = db.execute(
                    """
                    SELECT attempts, max_attempts, retry_delay_seconds
                    FROM job_queue WHERE id=?
                    """,
                    (job_id,),
                ).fetchone()
                if not row:
                    return

                attempts = row["attempts"] or 0
                max_attempts = row["max_attempts"] or DEFAULT_JOB_MAX_ATTEMPTS
                retry_delay_seconds = row["retry_delay_seconds"] or DEFAULT_JOB_RETRY_DELAY_SECONDS

                if attempts < max_attempts:
                    attempt_index = max(attempts - 1, 0)
                    delay = retry_delay_seconds * (self.retry_delay_multiplier ** attempt_index)
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status='pending',
                            scheduled_for=datetime('now', ?),
                            error_message=?,
                            error_traceback=?,
                            claimed_by=NULL,
                            claimed_at=NULL,
                            started_at=NULL
                        WHERE id=?
                        """,
                        (f"+{delay} seconds", error_message, error_traceback, job_id),
                    )
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'retry_scheduled', ?)",
                        (
                            str(uuid4()),
                            job_id,
                            json.dumps(
                                {
                                    "attempt": attempts,
                                    "delay_seconds": delay,
                                }
                            ),
                        ),
                    )
                else:
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status='failed', completed_at=CURRENT_TIMESTAMP,
                            error_message=?, error_traceback=?
                        WHERE id=?
                        """,
                        (error_message, error_traceback, job_id),
                    )
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'failed', ?)",
                        (str(uuid4()), job_id, error_message),
                    )
                db.commit()
            finally:
                db.close()

        self._with_retry(_fail)

    def register_agent(self, agent_id: str, persona: str, capabilities: Optional[List[str]] = None) -> None:
        caps_json = json.dumps(capabilities or [])

        def _register():
            db = self._connect()
            try:
                db.execute(
                    """
                    INSERT INTO agent_instances (id, persona, capabilities)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        persona=excluded.persona,
                        capabilities=excluded.capabilities,
                        status='active',
                        last_heartbeat=CURRENT_TIMESTAMP
                    """,
                    (agent_id, persona, caps_json),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_register)

    def update_agent_job(self, agent_id: str, job_id: Optional[str]) -> None:
        def _update():
            db = self._connect()
            try:
                db.execute(
                    """
                    UPDATE agent_instances
                    SET current_job_id=?, last_heartbeat=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (job_id, agent_id),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_update)

    def set_workdir(self, job_id: str, workdir_path: str) -> None:
        def _set():
            db = self._connect()
            try:
                db.execute(
                    "UPDATE job_queue SET workdir_path=? WHERE id=?",
                    (workdir_path, job_id),
                )
                db.execute(
                    "INSERT INTO job_events (id, job_id, event_type, payload) "
                    "VALUES (?, ?, 'progress', ?)",
                    (str(uuid4()), job_id, json.dumps({"workdir_path": workdir_path})),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_set)

    def heartbeat_agent(self, agent_id: str) -> None:
        def _heartbeat():
            db = self._connect()
            try:
                db.execute(
                    "UPDATE agent_instances SET last_heartbeat=CURRENT_TIMESTAMP WHERE id=?",
                    (agent_id,),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_heartbeat)

    def update_agent_stats(self, agent_id: str, completed: bool) -> None:
        field = "jobs_completed" if completed else "jobs_failed"

        def _update():
            db = self._connect()
            try:
                db.execute(
                    f"""
                    UPDATE agent_instances
                    SET {field} = {field} + 1, last_heartbeat=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (agent_id,),
                )
                db.commit()
            finally:
                db.close()

        self._with_retry(_update)

    def status_counts(self) -> Dict[str, int]:
        def _counts():
            db = self._connect()
            try:
                rows = db.execute(
                    "SELECT status, COUNT(*) as n FROM job_queue GROUP BY status"
                ).fetchall()
                return {row["status"]: row["n"] for row in rows}
            finally:
                db.close()

        return self._with_retry(_counts)

    def domain_counts(self) -> Dict[str, int]:
        def _counts():
            db = self._connect()
            try:
                rows = db.execute(
                    "SELECT domain, COUNT(*) as n FROM job_queue WHERE status='pending' GROUP BY domain"
                ).fetchall()
                return {row["domain"]: row["n"] for row in rows}
            finally:
                db.close()

        return self._with_retry(_counts)

    def list_agents(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        def _fetch():
            db = self._connect()
            try:
                clauses = []
                params: List[Any] = []
                if status:
                    clauses.append("status = ?")
                    params.append(status)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                query = (
                    "SELECT * FROM agent_instances "
                    f"{where} "
                    "ORDER BY last_heartbeat DESC "
                    "LIMIT ?"
                )
                params.append(limit)
                rows = db.execute(query, params).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                db.close()

        return self._with_retry(_fetch)

    def mark_stale_jobs(self, grace_seconds: int = 0) -> int:
        def _mark():
            db = self._connect()
            try:
                modifier = f"-{grace_seconds} seconds" if grace_seconds else "0 seconds"
                rows = db.execute(
                    """
                    SELECT id FROM job_queue
                    WHERE status='in_progress' AND (
                        (stale_after IS NOT NULL AND stale_after <= datetime('now', ?))
                        OR (
                            stale_after IS NULL
                            AND started_at IS NOT NULL
                            AND timeout_seconds IS NOT NULL
                            AND (strftime('%s','now') - strftime('%s', started_at))
                                >= (timeout_seconds + ?)
                        )
                    )
                    """,
                    (modifier, grace_seconds),
                ).fetchall()
                job_ids = [row["id"] for row in rows]
                for job_id in job_ids:
                    db.execute(
                        """
                        UPDATE job_queue
                        SET status='stale',
                            completed_at=CURRENT_TIMESTAMP,
                            error_message='stale job timeout'
                        WHERE id=?
                        """,
                        (job_id,),
                    )
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'stale', ?)",
                        (str(uuid4()), job_id, "stale job timeout"),
                    )
                db.commit()
                return len(job_ids)
            finally:
                db.close()

        return self._with_retry(_mark)

    def sample_metrics(self, critical_threshold: int = 50) -> Dict[str, Any]:
        def _sample():
            db = self._connect()
            try:
                status_rows = db.execute(
                    "SELECT status, COUNT(*) as n FROM job_queue GROUP BY status"
                ).fetchall()
                statuses = {row["status"]: row["n"] for row in status_rows}

                domain_rows = db.execute(
                    "SELECT domain, COUNT(*) as n FROM job_queue WHERE status='pending' GROUP BY domain"
                ).fetchall()
                domains = {row["domain"]: row["n"] for row in domain_rows}

                completed_1h = db.execute(
                    """
                    SELECT COUNT(*) as n FROM job_queue
                    WHERE status='completed'
                      AND completed_at >= datetime('now', '-1 hour')
                    """
                ).fetchone()["n"]
                failed_1h = db.execute(
                    """
                    SELECT COUNT(*) as n FROM job_queue
                    WHERE status='failed'
                      AND completed_at >= datetime('now', '-1 hour')
                    """
                ).fetchone()["n"]
                avg_processing = db.execute(
                    """
                    SELECT AVG(strftime('%s', completed_at) - strftime('%s', started_at)) as avg
                    FROM job_queue
                    WHERE status='completed'
                      AND completed_at >= datetime('now', '-1 hour')
                      AND started_at IS NOT NULL
                    """
                ).fetchone()["avg"]
                avg_processing = avg_processing if avg_processing is not None else 0

                active_agents = db.execute(
                    "SELECT COUNT(*) as n FROM agent_instances WHERE status='active'"
                ).fetchone()["n"]
                idle_agents = db.execute(
                    """
                    SELECT COUNT(*) as n FROM agent_instances
                    WHERE status='active' AND current_job_id IS NULL
                    """
                ).fetchone()["n"]

                stuck_count = db.execute(
                    """
                    SELECT COUNT(*) as n FROM job_queue
                    WHERE status='in_progress'
                      AND stale_after IS NOT NULL
                      AND stale_after <= CURRENT_TIMESTAMP
                    """
                ).fetchone()["n"]

                pending_count = statuses.get("pending", 0)

                metrics = {
                    "pending_count": pending_count,
                    "claimed_count": statuses.get("claimed", 0),
                    "in_progress_count": statuses.get("in_progress", 0),
                    "awaiting_review_count": statuses.get("awaiting_review", 0),
                    "failed_count": statuses.get("failed", 0),
                    "discovery_pending": domains.get("discovery", 0),
                    "investigation_pending": domains.get("investigation", 0),
                    "analysis_pending": domains.get("analysis", 0),
                    "understanding_pending": domains.get("understanding", 0),
                    "infrastructure_pending": domains.get("infrastructure", 0),
                    "jobs_completed_1h": completed_1h,
                    "jobs_failed_1h": failed_1h,
                    "avg_processing_time_seconds": avg_processing,
                    "active_agents": active_agents,
                    "idle_agents": idle_agents,
                    "has_stuck_jobs": 1 if stuck_count > 0 else 0,
                    "has_failed_jobs": 1 if statuses.get("failed", 0) > 0 else 0,
                    "queue_depth_critical": 1 if pending_count >= critical_threshold else 0,
                }

                db.execute(
                    """
                    INSERT INTO queue_metrics (
                        id,
                        pending_count,
                        claimed_count,
                        in_progress_count,
                        awaiting_review_count,
                        failed_count,
                        discovery_pending,
                        investigation_pending,
                        analysis_pending,
                        understanding_pending,
                        infrastructure_pending,
                        jobs_completed_1h,
                        jobs_failed_1h,
                        avg_processing_time_seconds,
                        active_agents,
                        idle_agents,
                        has_stuck_jobs,
                        has_failed_jobs,
                        queue_depth_critical
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        metrics["pending_count"],
                        metrics["claimed_count"],
                        metrics["in_progress_count"],
                        metrics["awaiting_review_count"],
                        metrics["failed_count"],
                        metrics["discovery_pending"],
                        metrics["investigation_pending"],
                        metrics["analysis_pending"],
                        metrics["understanding_pending"],
                        metrics["infrastructure_pending"],
                        metrics["jobs_completed_1h"],
                        metrics["jobs_failed_1h"],
                        metrics["avg_processing_time_seconds"],
                        metrics["active_agents"],
                        metrics["idle_agents"],
                        metrics["has_stuck_jobs"],
                        metrics["has_failed_jobs"],
                        metrics["queue_depth_critical"],
                    ),
                )
                db.commit()
                return metrics
            finally:
                db.close()

        return self._with_retry(_sample)

    def _dependencies_complete(self, db: sqlite3.Connection, dependencies: Iterable[str]) -> bool:
        deps = list(dependencies)
        if not deps:
            return True
        placeholders = ",".join("?" for _ in deps)
        row = db.execute(
            f"""
            SELECT COUNT(*) as n FROM job_queue
            WHERE id IN ({placeholders}) AND status='completed'
            """,
            deps,
        ).fetchone()
        return row["n"] == len(deps)

    def _job_depth(self, db: sqlite3.Connection, job_id: str) -> int:
        depth = 0
        current_id = job_id
        while current_id:
            row = db.execute(
                "SELECT parent_job_id FROM job_queue WHERE id=?",
                (current_id,),
            ).fetchone()
            if not row:
                break
            parent_id = row["parent_job_id"]
            if not parent_id:
                break
            depth += 1
            current_id = parent_id
        return depth

    def _child_count(self, db: sqlite3.Connection, parent_job_id: str) -> int:
        row = db.execute(
            "SELECT COUNT(*) as n FROM job_queue WHERE parent_job_id=?",
            (parent_job_id,),
        ).fetchone()
        return row["n"]

    def _dependencies_remaining(self, db: sqlite3.Connection, job_id: str) -> int:
        row = db.execute(
            """
            SELECT COUNT(*) as n
            FROM job_dependencies jd
            LEFT JOIN job_queue jq ON jd.depends_on_job_id = jq.id
            WHERE jd.job_id=?
              AND (jq.id IS NULL OR jq.status != 'completed')
            """,
            (job_id,),
        ).fetchone()
        return row["n"]

    def _unblock_dependents(self, db: sqlite3.Connection, completed_job_id: str) -> None:
        rows = db.execute(
            "SELECT job_id FROM job_dependencies WHERE depends_on_job_id=?",
            (completed_job_id,),
        ).fetchall()
        for row in rows:
            job_id = row["job_id"]
            if self._dependencies_remaining(db, job_id) == 0:
                updated = db.execute(
                    """
                    UPDATE job_queue
                    SET status='pending'
                    WHERE id=? AND status='blocked'
                    """,
                    (job_id,),
                )
                if updated.rowcount:
                    db.execute(
                        "INSERT INTO job_events (id, job_id, event_type, payload) "
                        "VALUES (?, ?, 'unblocked', ?)",
                        (str(uuid4()), job_id, json.dumps({"by": completed_job_id})),
                    )
