#!/usr/bin/env python3
"""
Trigger engine for scheduled and threshold-based job creation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from queue_system.queue import JobQueue


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "scripts" / "trigger_config.json"


class TriggerEngine:
    def __init__(
        self,
        queue: JobQueue,
        db_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.queue = queue
        self.db_path = Path(db_path) if db_path else Path(queue.db_path)
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _ensure_tables(self) -> None:
        db = self._connect()
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS trigger_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_name TEXT NOT NULL,
                    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('scheduled', 'threshold')),
                    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'failed')),
                    job_id TEXT,
                    details TEXT,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_trigger_runs_name
                    ON trigger_runs(trigger_name, completed_at);

                CREATE TABLE IF NOT EXISTS trigger_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            db.commit()
        finally:
            db.close()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {"scheduled": [], "thresholds": []}
        return json.loads(self.config_path.read_text())

    def _record_run(
        self,
        db: sqlite3.Connection,
        trigger_name: str,
        trigger_type: str,
        job_id: Optional[str],
        details: Dict[str, Any],
        status: str = "completed",
    ) -> None:
        db.execute(
            """
            INSERT INTO trigger_runs (trigger_name, trigger_type, status, job_id, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trigger_name, trigger_type, status, job_id, json.dumps(details)),
        )

    def _get_state_int(self, db: sqlite3.Connection, key: str, default: int = 0) -> int:
        row = db.execute("SELECT value FROM trigger_state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    def _set_state(self, db: sqlite3.Connection, key: str, value: Any) -> None:
        db.execute(
            """
            INSERT INTO trigger_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )

    def _cooldown_active(
        self,
        db: sqlite3.Connection,
        trigger_name: str,
        cooldown_minutes: Optional[int],
    ) -> bool:
        if not cooldown_minutes:
            return False
        row = db.execute(
            """
            SELECT COUNT(*) as n FROM trigger_runs
            WHERE trigger_name=?
              AND completed_at >= datetime('now', ?)
            """,
            (trigger_name, f"-{cooldown_minutes} minutes"),
        ).fetchone()
        return row["n"] > 0

    def _max_per_hour_reached(
        self,
        db: sqlite3.Connection,
        trigger_name: str,
        max_per_hour: Optional[int],
    ) -> bool:
        if not max_per_hour:
            return False
        row = db.execute(
            """
            SELECT COUNT(*) as n FROM trigger_runs
            WHERE trigger_name=?
              AND completed_at >= datetime('now', '-1 hour')
            """,
            (trigger_name,),
        ).fetchone()
        return row["n"] >= max_per_hour

    def _budget_available(
        self,
        db: sqlite3.Connection,
        budget_per_hour: Optional[int],
    ) -> bool:
        if not budget_per_hour:
            return True
        row = db.execute(
            """
            SELECT COUNT(*) as n FROM trigger_runs
            WHERE completed_at >= datetime('now', '-1 hour')
            """
        ).fetchone()
        return row["n"] < budget_per_hour

    def _get_metric_value(
        self,
        db: sqlite3.Connection,
        trigger_name: str,
        metric: str,
    ) -> Tuple[int, Dict[str, Any]]:
        if metric == "queue_pending":
            row = db.execute(
                "SELECT COUNT(*) as n FROM job_queue WHERE status='pending'"
            ).fetchone()
            return row["n"], {}
        if metric == "queue_failed":
            row = db.execute(
                "SELECT COUNT(*) as n FROM job_queue WHERE status='failed'"
            ).fetchone()
            return row["n"], {}
        if metric == "pending_triage":
            try:
                row = db.execute(
                    "SELECT COUNT(*) as n FROM leads WHERE status='pending_triage'"
                ).fetchone()
                return row["n"], {}
            except sqlite3.OperationalError:
                return 0, {}
        if metric == "findings_total":
            try:
                row = db.execute("SELECT COUNT(*) as n FROM findings").fetchone()
                return row["n"], {}
            except sqlite3.OperationalError:
                return 0, {}
        if metric == "findings_delta":
            try:
                row = db.execute("SELECT COUNT(*) as n FROM findings").fetchone()
                total = row["n"]
            except sqlite3.OperationalError:
                total = 0
            last_key = f"trigger:{trigger_name}:last_count"
            last = self._get_state_int(db, last_key, default=0)
            return total - last, {"total": total, "last": last}

        return 0, {}

    def _create_job(self, trigger: Dict[str, Any]) -> str:
        return self.queue.create_job(
            job_type=trigger["job_type"],
            domain=trigger["domain"],
            payload=trigger.get("payload", {}),
            priority=trigger.get("priority", 5),
            created_by=f"trigger:{trigger['name']}",
            source_trigger=trigger["name"],
        )

    def run_scheduled(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        config = self._load_config()
        triggers = config.get("scheduled", [])
        budget_per_hour = config.get("budget_per_hour")
        results: List[Dict[str, Any]] = []
        db = self._connect()
        try:
            for trigger in triggers:
                if not trigger.get("enabled", True):
                    continue
                name = trigger.get("name")
                interval_minutes = trigger.get("interval_minutes")
                if not name or not interval_minutes:
                    continue
                if self._cooldown_active(db, name, trigger.get("cooldown_minutes")):
                    continue
                if self._max_per_hour_reached(db, name, trigger.get("max_per_hour")):
                    continue
                if not self._budget_available(db, budget_per_hour):
                    if dry_run:
                        results.append({"name": name, "status": "budget_exceeded"})
                    continue

                row = db.execute(
                    """
                    SELECT completed_at FROM trigger_runs
                    WHERE trigger_name=?
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (name,),
                ).fetchone()
                if row:
                    last_run = row["completed_at"]
                    if last_run:
                        delta = db.execute(
                            "SELECT (julianday('now') - julianday(?)) * 1440 as minutes",
                            (last_run,),
                        ).fetchone()["minutes"]
                        if delta is not None and delta < interval_minutes:
                            continue

                if dry_run:
                    results.append({"name": name, "status": "dry_run"})
                    continue

                job_id = self._create_job(trigger)
                details = {
                    "interval_minutes": interval_minutes,
                    "job_type": trigger["job_type"],
                }
                self._record_run(db, name, "scheduled", job_id, details)
                results.append({"name": name, "job_id": job_id})
            db.commit()
        finally:
            db.close()
        return results

    def run_thresholds(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        config = self._load_config()
        triggers = config.get("thresholds", [])
        budget_per_hour = config.get("budget_per_hour")
        results: List[Dict[str, Any]] = []
        db = self._connect()
        try:
            for trigger in triggers:
                if not trigger.get("enabled", True):
                    continue
                name = trigger.get("name")
                metric = trigger.get("metric")
                threshold = trigger.get("threshold")
                if not name or not metric or threshold is None:
                    continue
                if self._cooldown_active(db, name, trigger.get("cooldown_minutes")):
                    continue
                if self._max_per_hour_reached(db, name, trigger.get("max_per_hour")):
                    continue
                if not self._budget_available(db, budget_per_hour):
                    if dry_run:
                        results.append({"name": name, "status": "budget_exceeded"})
                    continue

                value, meta = self._get_metric_value(db, name, metric)
                if value < threshold:
                    continue

                if dry_run:
                    results.append(
                        {"name": name, "status": "dry_run", "value": value}
                    )
                    continue

                job_id = self._create_job(trigger)
                details = {
                    "metric": metric,
                    "value": value,
                    "threshold": threshold,
                    **meta,
                }
                self._record_run(db, name, "threshold", job_id, details)
                if metric == "findings_delta":
                    self._set_state(db, f"trigger:{name}:last_count", meta.get("total", 0))
                results.append({"name": name, "job_id": job_id, "value": value})
            db.commit()
        finally:
            db.close()
        return results

    def list_runs(self, limit: int = 20, trigger_name: Optional[str] = None) -> List[Dict[str, Any]]:
        db = self._connect()
        try:
            clauses = []
            params: List[Any] = []
            if trigger_name:
                clauses.append("trigger_name = ?")
                params.append(trigger_name)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            query = (
                "SELECT * FROM trigger_runs "
                f"{where} "
                "ORDER BY completed_at DESC "
                "LIMIT ?"
            )
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            results = []
            for row in rows:
                data = dict(row)
                try:
                    data["details"] = json.loads(data.get("details") or "{}")
                except json.JSONDecodeError:
                    pass
                results.append(data)
            return results
        finally:
            db.close()
