#!/usr/bin/env python3
"""
Minimal agent worker loop for SQLite queue processing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ithildin.core.paths import content_root, pipeline_root, workdir_base
from queue_system.queue import JobQueue

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AgentWorker:
    JOB_TYPES: List[str] = []

    def __init__(
        self,
        queue: JobQueue,
        agent_id: str,
        persona: str,
        capabilities: Optional[List[str]] = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.queue = queue
        self.agent_id = agent_id
        self.persona = persona
        self.capabilities = capabilities or []
        self.poll_interval = poll_interval

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def run_forever(self) -> None:
        self.queue.register_agent(self.agent_id, self.persona, self.capabilities)
        while True:
            self.queue.heartbeat_agent(self.agent_id)
            if self.queue.is_paused():
                time.sleep(self.poll_interval)
                continue

            job = self.queue.claim_next(self.agent_id, self.capabilities)
            if not job:
                time.sleep(self.poll_interval)
                continue

            job_id = job["id"]
            self.queue.update_agent_job(self.agent_id, job_id)
            self.queue.start_job(job_id, self.agent_id)
            base = workdir_base()
            self.queue.set_workdir(job_id, str(base / job_id))

            try:
                output = self.execute(job)
                if output and isinstance(output, dict):
                    status = output.pop("job_status", "completed")
                else:
                    status = "completed"
                self.queue.complete_job(job_id, output, status=status)
                self.queue.update_agent_stats(self.agent_id, completed=True)
            except Exception as exc:
                self.queue.fail_job(job_id, str(exc), traceback.format_exc())
                self.queue.update_agent_stats(self.agent_id, completed=False)
            finally:
                self.queue.update_agent_job(self.agent_id, None)


class EchoWorker(AgentWorker):
    JOB_TYPES = ["echo"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "echo": job.get("payload", {}),
            "job_id": job["id"],
            "persona": self.persona,
        }

def _ensure_workdir(job_id: str) -> Path:
    base = workdir_base()
    workdir = base / job_id
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return workdir


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _max_datetime(left: Optional[datetime], right: Optional[datetime]) -> Optional[datetime]:
    if left is None:
        return right
    if right is None:
        return left
    return left if left >= right else right


def _load_alias_groups(db: sqlite3.Connection) -> tuple[Dict[str, str], Dict[str, set]]:
    raw_to_canonical: Dict[str, str] = {}
    canonical_to_aliases: Dict[str, set] = {}
    try:
        rows = db.execute("SELECT canonical_name, alias FROM name_aliases").fetchall()
    except sqlite3.OperationalError:
        return raw_to_canonical, canonical_to_aliases

    for row in rows:
        canonical = row["canonical_name"]
        alias = row["alias"]
        raw_to_canonical[alias.lower()] = canonical
        canonical_to_aliases.setdefault(canonical, set()).add(alias)

    return raw_to_canonical, canonical_to_aliases


def _content_root() -> Path:
    return content_root()


def _load_model_index() -> list[dict]:
    """Load analytical model index for agent context."""
    index_path = _content_root() / "models" / "_index.json"
    if not index_path.exists():
        return []
    return json.loads(index_path.read_text())


def _slugify(value: str) -> str:
    slug = value.lower().strip()
    slug = re.sub(r"[^a-z0-9\\s-]", "", slug)
    slug = re.sub(r"[\\s-]+", "-", slug)
    return slug.strip("-")


def _unique_content_path(content_dir: Path, slug: str, suffix: str, job_id: str) -> Path:
    path = content_dir / f"{slug}.{suffix}"
    if path.exists():
        path = content_dir / f"{slug}-{job_id[:8]}.{suffix}"
    return path


def _result_count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("results", "hits", "articles", "items", "records", "data"):
            if key in data and isinstance(data[key], list):
                return len(data[key])
    return 1 if data is not None else 0


def _run_tool(tool_path: Path, args: List[str], output_path: Path) -> Dict[str, Any]:
    cmd = [sys.executable, str(tool_path)] + args + ["--output", str(output_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    result = {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "output_path": str(output_path),
    }
    if proc.returncode == 0 and output_path.exists():
        try:
            data = json.loads(output_path.read_text())
            result["count"] = _result_count(data)
        except json.JSONDecodeError:
            result["count"] = None
    return result


def _run_script(script_path: Path, args: List[str]) -> Dict[str, Any]:
    cmd = [sys.executable, str(script_path)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _run_infra_action(action: str, infra_id: int, args: List[str]) -> Dict[str, Any]:
    tool_root = Path(__file__).resolve().parent.parent / "tools"
    script = tool_root / "infra_tracker.py"
    return _run_script(script, [action, str(infra_id)] + args)


def _run_query_sources(
    query: str,
    sources: List[str],
    limit: int,
    output_dir: Path,
    context: int = 120,
    dry_run: bool = False,
) -> Dict[str, Any]:
    tool_results: Dict[str, Any] = {}
    if dry_run:
        return tool_results

    tool_root = Path(__file__).resolve().parent.parent / "tools"

    if "doj" in sources:
        out = output_dir / "doj-search.json"
        tool_results["doj"] = _run_tool(
            tool_root / "query_doj.py",
            ["search", query, "--limit", str(limit), "--context", str(context)],
            out,
        )

    if "lmsband" in sources:
        out = output_dir / "lmsband-search.json"
        tool_results["lmsband"] = _run_tool(
            tool_root / "query_lmsband.py",
            ["search", query, "--limit", str(limit)],
            out,
        )

    if "unified_docs" in sources:
        out = output_dir / "unified-docs.json"
        tool_results["unified_docs"] = _run_tool(
            tool_root / "query_unified.py",
            ["docs", query, "--limit", str(limit)],
            out,
        )

    if "unified_emails" in sources:
        out = output_dir / "unified-emails.json"
        tool_results["unified_emails"] = _run_tool(
            tool_root / "query_unified.py",
            ["emails", query, "--limit", str(limit)],
            out,
        )

    if "unified_entities" in sources:
        out = output_dir / "unified-entities.json"
        tool_results["unified_entities"] = _run_tool(
            tool_root / "query_unified.py",
            ["entities", query, "--limit", str(limit)],
            out,
        )

    if "gdelt" in sources:
        out = output_dir / "gdelt-articles.json"
        tool_results["gdelt"] = _run_tool(
            tool_root / "query_gdelt.py",
            ["articles", query, "--limit", str(limit)],
            out,
        )

    if "findings" in sources:
        out = output_dir / "findings-search.json"
        tool_results["findings"] = _run_tool(
            tool_root / "findings_tracker.py",
            ["search", query],
            out,
        )

    return tool_results


class LeadTriageWorker(AgentWorker):
    JOB_TYPES = ["lead_triage"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        from tools import lead_tracker

        payload = job.get("payload", {})
        batch_size = int(payload.get("batch_size", 20))
        dry_run = bool(payload.get("dry_run", False))
        triaged_by = payload.get("triaged_by", "agent:lead_triage")

        db = lead_tracker.get_db()
        rows = db.execute(
            """
            SELECT * FROM leads
            WHERE status = 'pending_triage'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        leads = [dict(r) for r in rows]
        db.close()

        results = {
            "total": len(leads),
            "opened": [],
            "duplicates": [],
            "dry_run": dry_run,
        }

        now = _utcnow().isoformat()
        for lead in leads:
            lead_id = lead["id"]
            target_name = lead.get("target_name") or ""
            title = lead.get("title") or ""

            dup_id = None
            db = lead_tracker.get_db()
            try:
                if target_name:
                    dup = db.execute(
                        """
                        SELECT id FROM leads
                        WHERE LOWER(target_name) = LOWER(?)
                          AND id != ?
                          AND status != 'pending_triage'
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (target_name, lead_id),
                    ).fetchone()
                    if dup:
                        dup_id = dup["id"]
                if not dup_id and title:
                    dup = db.execute(
                        """
                        SELECT id FROM leads
                        WHERE LOWER(title) = LOWER(?)
                          AND id != ?
                          AND status != 'pending_triage'
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (title, lead_id),
                    ).fetchone()
                    if dup:
                        dup_id = dup["id"]
            finally:
                db.close()

            if dup_id:
                results["duplicates"].append({"lead_id": lead_id, "duplicate_of": dup_id})
                if not dry_run:
                    lead_tracker.dead_end_lead(lead_id, f"Duplicate of lead #{dup_id}")
                    lead_tracker.add_note(lead_id, f"Triage: duplicate of lead #{dup_id}")
                    db = lead_tracker.get_db()
                    try:
                        db.execute(
                            """
                            INSERT OR IGNORE INTO lead_relations
                            (lead_id, related_lead_id, relation_type)
                            VALUES (?, ?, 'duplicate')
                            """,
                            (lead_id, dup_id),
                        )
                        db.execute(
                            """
                            UPDATE leads
                            SET triaged_by = ?, triaged_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (triaged_by, now, now, lead_id),
                        )
                        db.commit()
                    finally:
                        db.close()
                continue

            results["opened"].append(lead_id)
            if not dry_run:
                db = lead_tracker.get_db()
                try:
                    db.execute(
                        """
                        UPDATE leads
                        SET status = 'open',
                            triaged_by = ?,
                            triaged_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (triaged_by, now, now, lead_id),
                    )
                    db.execute(
                        "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
                        (lead_id, "Triage: promoted to open"),
                    )
                    db.commit()
                finally:
                    db.close()

        return results


class DeepPersonWorker(AgentWorker):
    JOB_TYPES = ["deep_person"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        from tools import lead_tracker

        payload = job.get("payload", {})
        target = payload.get("target_name") or payload.get("query")
        if not target:
            raise ValueError("payload.target_name is required")

        lead_id = payload.get("lead_id")
        limit = int(payload.get("limit", 20))
        if "sources" in payload:
            sources = payload.get("sources") or []
        else:
            sources = [
                "doj",
                "lmsband",
                "unified_docs",
                "unified_emails",
                "unified_entities",
                "findings",
            ]

        if lead_id:
            lead_tracker.claim_lead(lead_id)
            lead_tracker.add_note(lead_id, f"Deep investigation started for '{target}'")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"

        tool_results: Dict[str, Any] = {}

        def add_tool_result(name: str, result: Dict[str, Any]) -> None:
            tool_results[name] = result

        tool_root = Path(__file__).resolve().parent.parent / "tools"

        if "doj" in sources:
            out = output_dir / "doj-search.json"
            result = _run_tool(
                tool_root / "query_doj.py",
                ["search", target, "--limit", str(limit), "--context", "120"],
                out,
            )
            add_tool_result("doj", result)

        if "lmsband" in sources:
            out = output_dir / "lmsband-search.json"
            result = _run_tool(
                tool_root / "query_lmsband.py",
                ["search", target, "--limit", str(limit)],
                out,
            )
            add_tool_result("lmsband", result)

        if "unified_docs" in sources:
            out = output_dir / "unified-docs.json"
            result = _run_tool(
                tool_root / "query_unified.py",
                ["docs", target, "--limit", str(limit)],
                out,
            )
            add_tool_result("unified_docs", result)

        if "unified_emails" in sources:
            out = output_dir / "unified-emails.json"
            result = _run_tool(
                tool_root / "query_unified.py",
                ["emails", target, "--limit", str(limit)],
                out,
            )
            add_tool_result("unified_emails", result)

        if "unified_entities" in sources:
            out = output_dir / "unified-entities.json"
            result = _run_tool(
                tool_root / "query_unified.py",
                ["entities", target, "--limit", str(limit)],
                out,
            )
            add_tool_result("unified_entities", result)

        if "findings" in sources:
            out = output_dir / "findings-search.json"
            result = _run_tool(
                tool_root / "findings_tracker.py",
                ["search", target],
                out,
            )
            add_tool_result("findings", result)

        report_lines = [
            f"# Investigation Report: {target}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")

        report_path.write_text("\n".join(report_lines))

        summary = f"Deep investigation completed for '{target}'."
        if lead_id:
            lead_tracker.complete_lead(lead_id, summary)
            lead_tracker.add_note(lead_id, f"Report: {report_path}")

        return {
            "target": target,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class SurveyorWorker(AgentWorker):
    JOB_TYPES = ["source_scan"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        query = payload.get("query")
        if not query:
            raise ValueError("payload.query is required")

        sources = payload.get("sources") or [
            "doj",
            "lmsband",
            "unified_docs",
            "unified_emails",
            "gdelt",
        ]
        limit = int(payload.get("limit", 20))
        context = int(payload.get("context", 120))
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"

        tool_results = _run_query_sources(
            query=query,
            sources=sources,
            limit=limit,
            output_dir=output_dir,
            context=context,
            dry_run=dry_run,
        )

        report_lines = [
            f"# Source Scan Report: {query}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "query": query,
            "report_path": str(report_path),
            "tools": tool_results,
            "sources": sources,
        }


class DocumentMineWorker(AgentWorker):
    JOB_TYPES = ["document_mine"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        query = payload.get("query")
        if not query:
            raise ValueError("payload.query is required")

        sources = payload.get("sources") or [
            "doj",
            "lmsband",
            "unified_docs",
            "unified_emails",
            "unified_entities",
        ]
        limit = int(payload.get("limit", 20))
        context = int(payload.get("context", 120))
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"

        tool_results = _run_query_sources(
            query=query,
            sources=sources,
            limit=limit,
            output_dir=output_dir,
            context=context,
            dry_run=dry_run,
        )

        report_lines = [
            f"# Document Mine Report: {query}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "query": query,
            "report_path": str(report_path),
            "tools": tool_results,
            "sources": sources,
        }


class EntityTracerWorker(AgentWorker):
    JOB_TYPES = ["trace_entity"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        entity_name = payload.get("entity_name") or payload.get("target_name")
        if not entity_name:
            raise ValueError("payload.entity_name is required")

        sources = payload.get("sources") or ["registry", "opensanctions", "littlesis"]
        jurisdictions = payload.get("jurisdictions") or []
        limit = int(payload.get("limit", 20))
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            if "registry" in sources:
                if jurisdictions:
                    for code in jurisdictions:
                        out = output_dir / f"registry-{code}.json"
                        tool_results[f"registry_{code}"] = _run_tool(
                            tool_root / "query_registry.py",
                            ["search", entity_name, "--jurisdiction", code, "--limit", str(limit)],
                            out,
                        )
                else:
                    out = output_dir / "registry-search.json"
                    tool_results["registry"] = _run_tool(
                        tool_root / "query_registry.py",
                        ["search", entity_name, "--limit", str(limit)],
                        out,
                    )

            if "opensanctions" in sources:
                out = output_dir / "opensanctions-search.json"
                tool_results["opensanctions"] = _run_tool(
                    tool_root / "query_opensanctions.py",
                    ["search", entity_name, "--limit", str(limit)],
                    out,
                )

            if "littlesis" in sources:
                out = output_dir / "littlesis-search.json"
                tool_results["littlesis"] = _run_tool(
                    tool_root / "query_littlesis.py",
                    ["search", entity_name, "--limit", str(limit)],
                    out,
                )

        report_lines = [
            f"# Entity Trace Report: {entity_name}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "entity_name": entity_name,
            "report_path": str(report_path),
            "tools": tool_results,
            "sources": sources,
        }


class PatternSpotterWorker(AgentWorker):
    JOB_TYPES = ["pattern_trigger"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        analyses = payload.get("analyses") or ["stats", "bridges"]
        dry_run = bool(payload.get("dry_run", False))
        centrality_metric = payload.get("centrality_metric", "betweenness")
        centrality_top = int(payload.get("centrality_top", 25))
        min_size = int(payload.get("min_size", 3))
        min_degree = int(payload.get("min_degree", 5))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            for analysis in analyses:
                if analysis == "stats":
                    out = output_dir / "graph-stats.json"
                    tool_results["stats"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["stats"],
                        out,
                    )
                elif analysis == "bridges":
                    out = output_dir / "graph-bridges.json"
                    tool_results["bridges"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["bridges"],
                        out,
                    )
                elif analysis == "centrality":
                    out = output_dir / "graph-centrality.json"
                    tool_results["centrality"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["centrality", "--metric", centrality_metric, "--top", str(centrality_top)],
                        out,
                    )
                elif analysis == "components":
                    out = output_dir / "graph-components.json"
                    tool_results["components"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["components", "--min-size", str(min_size)],
                        out,
                    )
                elif analysis == "holes":
                    out = output_dir / "graph-holes.json"
                    tool_results["holes"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["holes", "--min-degree", str(min_degree)],
                        out,
                    )

        report_lines = [
            "# Pattern Spotter Report",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "report_path": str(report_path),
            "tools": tool_results,
            "analyses": analyses,
        }


class SynthesistWorker(AgentWorker):
    JOB_TYPES = ["synthesis"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        query = payload.get("query") or payload.get("target_name")
        finding_ids = payload.get("finding_ids") or []
        dry_run = bool(payload.get("dry_run", False))

        if not query and not finding_ids:
            raise ValueError("payload.query or payload.finding_ids is required")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            if finding_ids:
                for finding_id in finding_ids:
                    out = output_dir / f"finding-{finding_id}.json"
                    tool_results[f"finding_{finding_id}"] = _run_tool(
                        tool_root / "findings_tracker.py",
                        ["show", str(finding_id)],
                        out,
                    )
            else:
                out = output_dir / "findings-search.json"
                tool_results["findings_search"] = _run_tool(
                    tool_root / "findings_tracker.py",
                    ["search", query],
                    out,
                )

        report_lines = [
            f"# Synthesis Report: {query or 'selected findings'}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "query": query,
            "report_path": str(report_path),
            "tools": tool_results,
            "finding_ids": finding_ids,
        }


class NetworkAnalystWorker(AgentWorker):
    JOB_TYPES = ["network_analysis"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        analyses = payload.get("analyses") or ["stats", "centrality", "bridges"]
        dry_run = bool(payload.get("dry_run", False))
        centrality_metric = payload.get("centrality_metric", "betweenness")
        centrality_top = int(payload.get("centrality_top", 25))
        min_size = int(payload.get("min_size", 3))
        min_degree = int(payload.get("min_degree", 5))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            for analysis in analyses:
                if analysis == "stats":
                    out = output_dir / "graph-stats.json"
                    tool_results["stats"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["stats"],
                        out,
                    )
                elif analysis == "bridges":
                    out = output_dir / "graph-bridges.json"
                    tool_results["bridges"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["bridges"],
                        out,
                    )
                elif analysis == "centrality":
                    out = output_dir / "graph-centrality.json"
                    tool_results["centrality"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["centrality", "--metric", centrality_metric, "--top", str(centrality_top)],
                        out,
                    )
                elif analysis == "components":
                    out = output_dir / "graph-components.json"
                    tool_results["components"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["components", "--min-size", str(min_size)],
                        out,
                    )
                elif analysis == "holes":
                    out = output_dir / "graph-holes.json"
                    tool_results["holes"] = _run_tool(
                        tool_root / "graph_tools.py",
                        ["holes", "--min-degree", str(min_degree)],
                        out,
                    )

        report_lines = [
            "# Network Analysis Report",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "report_path": str(report_path),
            "tools": tool_results,
            "analyses": analyses,
        }


class TimelineAnalystWorker(AgentWorker):
    JOB_TYPES = ["timeline_correlation"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        start = payload.get("start")
        end = payload.get("end")
        finding_id = payload.get("finding_id")
        date = payload.get("date")
        days = int(payload.get("days", 14))
        list_category = payload.get("category")
        list_year = payload.get("year")
        limit = int(payload.get("limit", 100))
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            if start and end:
                out = output_dir / "timeline-window.json"
                tool_results["window"] = _run_tool(
                    tool_root / "event_timeline.py",
                    ["window", "--start", start, "--end", end],
                    out,
                )
            elif finding_id or date:
                out = output_dir / "timeline-near.json"
                args = ["near", "--days", str(days)]
                if finding_id:
                    args.extend(["--finding-id", str(finding_id)])
                if date:
                    args.extend(["--date", date])
                tool_results["near"] = _run_tool(
                    tool_root / "event_timeline.py",
                    args,
                    out,
                )
            elif list_category or list_year:
                out = output_dir / "timeline-list.json"
                args = ["list", "--limit", str(limit)]
                if list_category:
                    args.extend(["--category", list_category])
                if list_year:
                    args.extend(["--year", str(list_year)])
                tool_results["list"] = _run_tool(
                    tool_root / "event_timeline.py",
                    args,
                    out,
                )
            else:
                out = output_dir / "timeline-stats.json"
                tool_results["stats"] = _run_tool(
                    tool_root / "event_timeline.py",
                    ["stats"],
                    out,
                )

        report_lines = [
            "# Timeline Analysis Report",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "report_path": str(report_path),
            "tools": tool_results,
        }


class SystemicAnalystWorker(AgentWorker):
    JOB_TYPES = ["systemic_analysis"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        top = int(payload.get("top", 50))
        thread_id = payload.get("thread_id")
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            out = output_dir / "coverage-matrix.json"
            tool_results["coverage_matrix"] = _run_tool(
                tool_root / "analysis_export.py",
                ["coverage-matrix", "--top", str(top)],
                out,
            )
            out = output_dir / "thread-summary.json"
            args = ["thread-summary"]
            if thread_id is not None:
                args.extend(["--thread-id", str(thread_id)])
            tool_results["thread_summary"] = _run_tool(
                tool_root / "analysis_export.py",
                args,
                out,
            )
            out = output_dir / "analysis-state.json"
            tool_results["analysis_state"] = _run_tool(
                tool_root / "analysis_export.py",
                ["analysis-state"],
                out,
            )

        report_lines = [
            "# Systemic Analysis Report",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tools executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            count = result.get("count")
            count_txt = f"{count} results" if count is not None else "unknown results"
            report_lines.append(f"- {name}: {status} ({count_txt}) -> {result.get('output_path')}")
        report_path.write_text("\n".join(report_lines))

        return {
            "report_path": str(report_path),
            "tools": tool_results,
        }


class ExplainerWriterWorker(AgentWorker):
    JOB_TYPES = ["mechanism_explainer"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        mechanism = payload.get("mechanism_type") or payload.get("mechanism")
        title = payload.get("title") or (f"Mechanism: {mechanism}" if mechanism else None)
        subtitle = payload.get("subtitle")
        targets = payload.get("targets") or []
        date_str = payload.get("date") or _utcnow().date().isoformat()
        status = payload.get("status", "draft")
        dry_run = bool(payload.get("dry_run", False))

        if not title:
            raise ValueError("payload.title or payload.mechanism_type is required")

        slug = _slugify(title) or job["id"][:8]
        content_dir = _content_root() / "articles"
        content_dir.mkdir(parents=True, exist_ok=True)
        content_path = _unique_content_path(content_dir, slug, "mdx", job["id"])

        targets_text = ", ".join(targets) if isinstance(targets, list) else str(targets)
        frontmatter = [
            "---",
            f"title: \"{title}\"",
            f"subtitle: \"{subtitle}\"" if subtitle else None,
            f"cluster: {slug}",
            f"targets: \"{targets_text}\"" if targets_text else "targets: \"\"",
            f"date: \"{date_str}\"",
            f"status: {status}",
            "modality: mechanism_explainer",
            "---",
            "",
        ]
        frontmatter = [line for line in frontmatter if line is not None]

        # Include applicable analytical models
        model_index = _load_model_index()
        model_refs = [m for m in model_index if m["id"] in (payload.get("models") or [])]

        body = [
            "## Hook",
            "",
            "[What is the most counterintuitive finding about this mechanism? Open with a structural",
            "truth that contradicts naive assumptions. The reader should think 'wait, that can't be right'",
            "and then spend the rest of the article discovering that it is.]",
            "",
        ]
        if model_refs:
            body.append("## Applicable Models")
            for m in model_refs:
                body.append(f"- **[{m['title']}](/models/{m['id']})** — {m['subtitle']}")
            body.append("")
        body.extend([
            "## The Mechanism",
            "",
            "[Use Three-Part Architecture for each section:",
            "(1) Conceptual frame — what is this thing and what role does it play?",
            "(2) Specific evidence — exact dates, amounts, parties, EFTA references",
            "(3) Analysis connecting them — why does this instance illuminate the mechanism?]",
            "",
            "## What Should Have Happened",
            "",
            "[Explain the regulatory/compliance framework. Use Perspective Internalization:",
            "put the reader inside the compliance desk, the registrar's office, the bank's KYC team.",
            "Show the gap between design and reality at each step.]",
            "",
            "[NOTE: Ideally the counterfactual is woven throughout the Mechanism section,",
            "not isolated here. This section is a fallback.]",
            "",
            "## Why It Works This Way",
            "",
            "[Evolutionary Explanation: (1) How it works today, (2) Why that seems weird,",
            "(3) Historical/structural reason it evolved this way, (4) What would happen",
            "if you tried to change it, (5) Now you understand why it persists.]",
            "",
            "## What We Don't Know",
            "",
            "[Honest about gaps. Include missing documents: what records should exist but don't?",
            "Calibrated imprecision increases credibility.]",
            "",
            "## Evidence Index",
            "",
            "- [ ] Add EFTA references and source citations",
            "",
            "## Craft Notes",
            "",
            "Key principles for this explainer (from research/craft-principles.md):",
            "- Three-Part Structure: frame + evidence + analysis for every mechanism section",
            "- Infrastructure Reveal: peel back layers, show the invisible cascades",
            "- Stakes Before Mechanism: establish consequences before explaining how it works",
            "- Calibrated Precision: exact figures when available, honest ranges when uncertain",
            "- No 'shocking' language — let the facts do the work",
        ])
        content_text = "\n".join(frontmatter + body)

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        report_lines = [
            f"# Mechanism Explainer Draft: {title}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            f"## Content Path: {content_path}",
        ]

        review_job_id = None
        if not dry_run:
            content_path.write_text(content_text)
            if payload.get("spawn_review", True):
                review_job_id = self.queue.create_job(
                    job_type="editor_review",
                    domain="curation",
                    payload={
                        "content_path": str(content_path),
                        "modality": "mechanism_explainer",
                        "source_job_id": job["id"],
                    },
                    priority=payload.get("review_priority", 5),
                    created_by=f"agent:{self.persona}",
                    source_trigger="mechanism_explainer",
                )
        else:
            report_lines.append("## Dry Run: content not written")

        report_path.write_text("\n".join(report_lines))
        return {
            "title": title,
            "content_path": str(content_path) if not dry_run else None,
            "report_path": str(report_path),
            "review_job_id": review_job_id,
            "job_status": "awaiting_review" if review_job_id else "completed",
        }


class ContextualAnalystWorker(AgentWorker):
    JOB_TYPES = ["analytical_article"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        title = payload.get("title") or payload.get("target_name") or "Analytical Article"
        lens = payload.get("lens", "general")
        subtitle = payload.get("subtitle")
        targets = payload.get("targets") or []
        date_str = payload.get("date") or _utcnow().date().isoformat()
        status = payload.get("status", "draft")
        dry_run = bool(payload.get("dry_run", False))

        slug = _slugify(title) or job["id"][:8]
        content_dir = _content_root() / "articles"
        content_dir.mkdir(parents=True, exist_ok=True)
        content_path = _unique_content_path(content_dir, slug, "mdx", job["id"])

        targets_text = ", ".join(targets) if isinstance(targets, list) else str(targets)
        frontmatter = [
            "---",
            f"title: \"{title}\"",
            f"subtitle: \"{subtitle}\"" if subtitle else None,
            f"cluster: {slug}",
            f"targets: \"{targets_text}\"" if targets_text else "targets: \"\"",
            f"date: \"{date_str}\"",
            f"status: {status}",
            f"lens: \"{lens}\"",
            "modality: analytical_article",
            "---",
            "",
        ]
        frontmatter = [line for line in frontmatter if line is not None]

        # Detect applicable analytical models from title/targets
        model_index = _load_model_index()
        detect_text = f"{title} {' '.join(targets) if isinstance(targets, list) else targets}"
        try:
            from tools.model_detector import detect_models
            detected = detect_models(detect_text)
        except Exception:
            detected = []
        applicable_models = [d for d in detected if d.get("confidence") in ("high", "medium")]

        body = [
            "## Opening",
            "",
            "[Counterintuitive hook — what structural truth contradicts naive assumptions?",
            "Not 'Epstein was connected to powerful people' but something about HOW the system",
            "worked that would surprise an intelligent person in finance/law/compliance.]",
            "",
        ]
        if applicable_models:
            body.append("## Analytical Framework")
            body.append("")
            body.append("[Use these detected models as narrative scaffolding — not just a list,")
            body.append("but recurring characters that appear throughout the analysis:]")
            body.append("")
            for m in applicable_models:
                body.append(f"- **[{m['title']}](/models/{m['model_id']})** ({m['confidence']}) — {', '.join(m.get('reasons', [])[:2])}")
            body.append("")
        body.extend([
            "## Analysis",
            "",
            "[Perspective: write from INSIDE the system, not about it. Explain WHY it evolved",
            "this way using Evolutionary Explanation: (1) how it works, (2) why that seems weird,",
            "(3) historical reason, (4) what happens if you try to change it.]",
            "",
            "[Use the Dual-Spine technique: one holding spine (timeline, person, transaction chain)",
            "provides forward momentum; one depth spine (system explanation, regulatory framework)",
            "provides meaning. Weave them together.]",
            "",
            "## Evidence",
            "",
            "[Apply the evidence budget: select the findings that reveal mechanisms, connect threads,",
            "or contradict the public narrative. Use documents as plot points — paraphrase context,",
            "then quote the devastating line.]",
            "",
            "## What We Don't Know",
            "",
            "[Honest about gaps. Missing documents are evidence. Calibrated imprecision",
            "increases credibility.]",
            "",
            "## Sources",
            "- [ ] Add EFTA references and source citations",
        ])
        content_text = "\n".join(frontmatter + body)

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        report_lines = [
            f"# Analytical Article Draft: {title}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            f"## Content Path: {content_path}",
        ]

        review_job_id = None
        if not dry_run:
            content_path.write_text(content_text)
            if payload.get("spawn_review", True):
                review_job_id = self.queue.create_job(
                    job_type="editor_review",
                    domain="curation",
                    payload={
                        "content_path": str(content_path),
                        "modality": "analytical_article",
                        "source_job_id": job["id"],
                    },
                    priority=payload.get("review_priority", 5),
                    created_by=f"agent:{self.persona}",
                    source_trigger="analytical_article",
                )
        else:
            report_lines.append("## Dry Run: content not written")

        report_path.write_text("\n".join(report_lines))
        return {
            "title": title,
            "content_path": str(content_path) if not dry_run else None,
            "report_path": str(report_path),
            "review_job_id": review_job_id,
            "job_status": "awaiting_review" if review_job_id else "completed",
        }


class EditorReviewWorker(AgentWorker):
    JOB_TYPES = ["editor_review", "fact_check"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        content_path = payload.get("content_path")
        slug = payload.get("slug")
        min_words = int(payload.get("min_words", 300))
        modality = payload.get("modality")
        required_fields = payload.get("required_fields") or ["title", "date", "status"]

        if not content_path and slug:
            if modality == "wiki_dossier_update":
                content_path = str(_content_root() / "dossiers" / f"{slug}.json")
            else:
                content_path = str(_content_root() / "articles" / f"{slug}.mdx")
        if not content_path:
            raise ValueError("payload.content_path or payload.slug is required")

        content_file = Path(content_path)
        if not content_file.exists():
            raise FileNotFoundError(f"Content not found: {content_path}")

        if content_file.suffix == ".json" or modality == "wiki_dossier_update":
            dossier = json.loads(content_file.read_text())
            required_fields = payload.get("required_fields") or [
                "name",
                "slug",
                "findings",
                "connections",
                "stats",
            ]
            missing = [field for field in required_fields if field not in dossier]
            stats = dossier.get("stats") or {}
            total_findings = stats.get("total_findings", len(dossier.get("findings", [])))
            min_findings = int(payload.get("min_findings", 5))
            evidence_count = sum(1 for f in dossier.get("findings", []) if f.get("evidence"))

            issues = []
            if missing:
                issues.append(f"Missing dossier fields: {', '.join(missing)}")
            if total_findings < min_findings:
                issues.append(f"Findings below minimum ({total_findings} < {min_findings})")
            if evidence_count == 0:
                issues.append("No evidence attached to findings")

            decision = "approve" if not issues else "revise"
            report = {
                "content_path": content_path,
                "review_type": "dossier",
                "decision": decision,
                "issues": issues,
                "missing_fields": missing,
                "total_findings": total_findings,
                "evidence_count": evidence_count,
            }
        else:
            raw = content_file.read_text()
            frontmatter: Dict[str, str] = {}
            body = raw
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    fm_text = parts[1]
                    body = parts[2].lstrip("\n")
                    for line in fm_text.splitlines():
                        if ":" not in line:
                            continue
                        key, value = line.split(":", 1)
                        frontmatter[key.strip()] = value.strip().strip("\"")

            missing = [field for field in required_fields if not frontmatter.get(field)]
            word_count = len(re.findall(r"\b\w+\b", body))
            citations = sorted(set(re.findall(r"\[[^\]]+\]", body)))

            # Check for model references in article content
            model_index = _load_model_index()
            model_slugs = {m["id"] for m in model_index}
            referenced_models = [mid for mid in model_slugs if f"/models/{mid}" in body]

            # Detect if models *should* be referenced but aren't
            suggested_models = []
            try:
                from tools.model_detector import detect_models
                detected = detect_models(body[:2000])
                suggested_models = [
                    d["model_id"] for d in detected
                    if d.get("confidence") in ("high", "medium") and d["model_id"] not in referenced_models
                ]
            except Exception:
                pass

            issues = []
            if missing:
                issues.append(f"Missing frontmatter fields: {', '.join(missing)}")
            if word_count < min_words:
                issues.append(f"Word count below minimum ({word_count} < {min_words})")
            if not citations:
                issues.append("No citations detected")
            if suggested_models:
                issues.append(f"Consider referencing models: {', '.join(suggested_models)}")

            # Narrative quality checks (from craft-principles.md)
            body_lower = body.lower()

            # Check for mechanism explanation (not just events)
            mechanism_indicators = ["how", "works", "mechanism", "process", "system", "because", "evolved"]
            has_mechanism = sum(1 for w in mechanism_indicators if w in body_lower) >= 3
            if not has_mechanism:
                issues.append("narrative: No mechanism explanation detected — article may describe events without explaining HOW the system works")

            # Check for perspective internalization (inside-out language)
            perspective_indicators = ["you're", "your", "imagine", "from the", "at the desk", "in the office"]
            has_perspective = any(p in body_lower for p in perspective_indicators)
            if not has_perspective and word_count > 1000:
                issues.append("narrative: No perspective internalization — consider putting the reader inside the system")

            # Check for exhibit-list style (multiple documents listed without narrative)
            exhibit_pattern = re.findall(r"(?:document|exhibit|attachment|filing)\s+\d+", body_lower)
            if len(exhibit_pattern) > 5:
                issues.append("narrative: Possible exhibit-list evidence style — documents should be narrated as plot points, not listed")

            # Check for chronology-only structure
            chrono_markers = re.findall(r"(?:in \d{4}|on \w+ \d{1,2},? \d{4}|the following year|later that)", body_lower)
            if len(chrono_markers) > 8 and "mechanism" not in body_lower and "how" not in body_lower[:500]:
                issues.append("narrative: Appears to be pure chronology — consider thematic structure or dual-spine technique")

            # Check for sensationalist language
            sensational = [w for w in ["shocking", "explosive", "bombshell", "stunning", "horrifying"]
                          if w in body_lower]
            if sensational:
                issues.append(f"narrative: Sensationalist language detected: {', '.join(sensational)} — let the facts do the work")

            decision = "approve" if not issues else "revise"
            report = {
                "content_path": content_path,
                "review_type": "article",
                "decision": decision,
                "issues": issues,
                "missing_fields": missing,
                "referenced_models": referenced_models,
                "suggested_models": suggested_models,
                "word_count": word_count,
                "citation_count": len(citations),
                "citations": citations,
            }

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.json"
        report_path.write_text(json.dumps(report, indent=2))

        source_job_id = payload.get("source_job_id")
        if source_job_id:
            if decision == "approve":
                self.queue.set_status(source_job_id, "completed")
            elif decision == "reject":
                self.queue.set_status(source_job_id, "failed", error_message="editor_reject")
            else:
                self.queue.set_status(source_job_id, "awaiting_review")

        return {
            "report_path": str(report_path),
            "review": report,
        }


class DedupeReviewWorker(AgentWorker):
    JOB_TYPES = ["dedupe_review"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        action = payload.get("action", "scan")
        dry_run = bool(payload.get("dry_run", False))
        keep_id = payload.get("keep_id")
        delete_id = payload.get("delete_id")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        args: List[str] = []
        if action in {"scan", "stats", "seed", "apply"}:
            args = [action]
            if action in {"seed", "apply"} and dry_run:
                args.append("--dry-run")
        elif action == "merge":
            if keep_id is None or delete_id is None:
                raise ValueError("payload.keep_id and payload.delete_id are required for merge")
            args = ["merge", "--keep-id", str(keep_id), "--delete-id", str(delete_id)]
            if dry_run:
                args.append("--dry-run")
        else:
            raise ValueError(f"Unsupported dedupe action '{action}'")

        if not dry_run:
            tool_root = Path(__file__).resolve().parent.parent / "tools"
            tool_results["entity_dedup"] = _run_script(tool_root / "entity_dedup.py", args)

        report_lines = [
            f"# Dedupe Review: {action}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
        ]
        if dry_run:
            report_lines.append("## Dry Run: no dedupe actions executed")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "action": action,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class FindingVerificationWorker(AgentWorker):
    JOB_TYPES = ["verify_finding"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        finding_id = payload.get("finding_id")
        mark_verified = bool(payload.get("mark_verified", False))
        verified_by = payload.get("verified_by", f"agent:{self.persona}")
        dry_run = bool(payload.get("dry_run", False))

        if finding_id is None:
            raise ValueError("payload.finding_id is required")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        output_dir = workdir / "output"
        tool_results: Dict[str, Any] = {}

        tool_root = Path(__file__).resolve().parent.parent / "tools"
        if not dry_run:
            out = output_dir / f"finding-{finding_id}.json"
            tool_results["show"] = _run_tool(
                tool_root / "findings_tracker.py",
                ["show", str(finding_id)],
                out,
            )

            if mark_verified:
                tool_results["verify"] = _run_script(
                    tool_root / "findings_tracker.py",
                    ["verify", str(finding_id), "--by", verified_by],
                )

        report_lines = [
            f"# Finding Verification: {finding_id}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Tool Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no tool execution)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "finding_id": finding_id,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class ToolBuilderWorker(AgentWorker):
    JOB_TYPES = ["tool_build", "bug_fix"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        infra_id = payload.get("infra_id")
        note = payload.get("note")
        script = payload.get("script")
        script_args = payload.get("args", [])
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        if infra_id and not dry_run:
            tool_results["claim"] = _run_infra_action("claim", infra_id, [])
            if note:
                tool_results["note"] = _run_infra_action("note", infra_id, [note])

        if script:
            script_path = Path(script)
            if not script_path.is_absolute():
                script_path = Path(__file__).resolve().parent.parent / script_path
            if not dry_run:
                tool_results["script"] = _run_script(script_path, list(script_args))

        report_lines = [
            f"# Tool Build: {infra_id or 'no infra id'}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
        ]
        if dry_run:
            report_lines.append("## Dry Run: no infra actions executed")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "infra_id": infra_id,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class SourceIntegratorWorker(AgentWorker):
    JOB_TYPES = ["source_ingest"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        script = payload.get("script")
        script_args = payload.get("args", [])
        infra_id = payload.get("infra_id")
        dry_run = bool(payload.get("dry_run", False))

        if not script and not infra_id:
            raise ValueError("payload.script or payload.infra_id is required")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        if infra_id and not dry_run:
            tool_results["claim"] = _run_infra_action("claim", infra_id, [])

        if script:
            script_path = Path(script)
            if not script_path.is_absolute():
                script_path = Path(__file__).resolve().parent.parent / script_path
            if not dry_run:
                tool_results["script"] = _run_script(script_path, list(script_args))

        report_lines = [
            f"# Source Ingest: {infra_id or script}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
        ]
        if dry_run:
            report_lines.append("## Dry Run: no ingest actions executed")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "infra_id": infra_id,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class RegistryAdderWorker(AgentWorker):
    JOB_TYPES = ["registry_add"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        script = payload.get("script")
        script_args = payload.get("args", [])
        infra_id = payload.get("infra_id")
        dry_run = bool(payload.get("dry_run", False))

        if not script and not infra_id:
            raise ValueError("payload.script or payload.infra_id is required")

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        if infra_id and not dry_run:
            tool_results["claim"] = _run_infra_action("claim", infra_id, [])

        if script:
            script_path = Path(script)
            if not script_path.is_absolute():
                script_path = Path(__file__).resolve().parent.parent / script_path
            if not dry_run:
                tool_results["script"] = _run_script(script_path, list(script_args))

        report_lines = [
            f"# Registry Add: {infra_id or script}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
        ]
        if dry_run:
            report_lines.append("## Dry Run: no registry actions executed")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "infra_id": infra_id,
            "report_path": str(report_path),
            "tools": tool_results,
        }


class DeepInvestigationWorker(AgentWorker):
    JOB_TYPES = ["deep_investigate"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        target = payload.get("target_name") or payload.get("query")
        if not target:
            raise ValueError("payload.target_name is required")

        child_specs = payload.get("child_jobs")
        if not child_specs:
            child_specs = [
                {"job_type": "deep_person", "domain": "investigation"},
                {"job_type": "document_mine", "domain": "investigation"},
                {"job_type": "trace_entity", "domain": "investigation"},
                {"job_type": "pattern_trigger", "domain": "analysis"},
            ]

        thread_id = job.get("thread_id") or job["id"]
        parent_id = job["id"]
        child_jobs: List[str] = []
        for spec in child_specs:
            job_type = spec["job_type"]
            domain = spec.get("domain", "investigation")
            child_payload = dict(spec.get("payload", {}))

            if not child_payload:
                if job_type == "deep_person":
                    child_payload = {
                        "target_name": target,
                        "lead_id": payload.get("lead_id"),
                    }
                elif job_type == "document_mine":
                    child_payload = {
                        "query": target,
                        "dry_run": payload.get("dry_run", False),
                    }
                elif job_type == "trace_entity":
                    child_payload = {
                        "entity_name": target,
                        "dry_run": payload.get("dry_run", False),
                    }
                elif job_type == "pattern_trigger":
                    child_payload = {"dry_run": payload.get("dry_run", False)}

            for key in ("limit", "sources", "context", "jurisdictions"):
                if key in payload and key not in child_payload:
                    child_payload[key] = payload[key]

            child_id = self.queue.create_job(
                job_type=job_type,
                domain=domain,
                payload=child_payload,
                priority=payload.get("priority", 5),
                created_by=f"agent:{self.persona}",
                parent_job_id=parent_id,
                thread_id=thread_id,
                source_trigger="deep_investigate",
            )
            child_jobs.append(child_id)

        synthesis_id = None
        if payload.get("spawn_synthesis", True):
            synthesis_payload = {
                "query": target,
                "dry_run": payload.get("dry_run", False),
            }
            synthesis_id = self.queue.create_job(
                job_type="synthesis",
                domain="analysis",
                payload=synthesis_payload,
                priority=payload.get("priority", 5),
                created_by=f"agent:{self.persona}",
                parent_job_id=parent_id,
                thread_id=thread_id,
                depends_on=child_jobs,
                source_trigger="deep_investigate",
            )

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        child_path = workdir / "child_jobs.json"

        report_lines = [
            f"# Deep Investigation Orchestration: {target}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Child Jobs",
        ]
        for child_id in child_jobs:
            report_lines.append(f"- {child_id}")
        if synthesis_id:
            report_lines.append("")
            report_lines.append(f"## Synthesis Job: {synthesis_id}")

        report_path.write_text("\n".join(report_lines))
        child_path.write_text(
            json.dumps(
                {
                    "target": target,
                    "child_jobs": child_jobs,
                    "synthesis_job": synthesis_id,
                },
                indent=2,
            )
        )

        return {
            "target": target,
            "child_jobs": child_jobs,
            "synthesis_job": synthesis_id,
            "report_path": str(report_path),
            "child_jobs_path": str(child_path),
        }


class DossierWriterWorker(AgentWorker):
    JOB_TYPES = ["wiki_dossier_update"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        target = payload.get("target_name")
        min_findings = int(payload.get("min_findings", 5))
        update_backlinks = bool(payload.get("update_backlinks", False))
        spawn_review = bool(payload.get("spawn_review", False))
        curate = bool(payload.get("curate", False))
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        build_root = pipeline_root()
        review_job_id = None
        if not dry_run:
            args = []
            if target:
                args.extend(["--target", target])
            if min_findings:
                args.extend(["--min-findings", str(min_findings)])
            if payload.get("incremental"):
                args.append("--incremental")
            tool_results["export_dossiers"] = _run_script(
                build_root / "export_dossiers.py",
                args,
            )

            # Automated curation (key findings, viz data, identifiers)
            if curate or payload.get("curate"):
                curate_args = []
                if target:
                    curate_args.extend(["--target", target])
                else:
                    curate_args.append("--all")
                tool_results["curate_dossier"] = _run_script(
                    build_root / "curate_dossier.py",
                    curate_args,
                )

            if update_backlinks:
                tool_results["compute_backlinks"] = _run_script(
                    build_root / "compute_backlinks.py",
                    [],
                )

            if spawn_review and target:
                content_path = _content_root() / "dossiers" / f"{_slugify(target)}.json"
                review_job_id = self.queue.create_job(
                    job_type="editor_review",
                    domain="curation",
                    payload={
                        "content_path": str(content_path),
                        "modality": "wiki_dossier_update",
                        "source_job_id": job["id"],
                        "min_findings": min_findings,
                    },
                    priority=payload.get("review_priority", 4),
                    created_by=f"agent:{self.persona}",
                    source_trigger="wiki_dossier_update",
                )

        # Detect applicable analytical models for this target
        applicable_models = []
        if target and not dry_run:
            try:
                from tools.model_detector import detect_models
                detected = detect_models(target)
                applicable_models = [d["model_id"] for d in detected if d.get("confidence") in ("high", "medium")]
            except Exception:
                pass

        report_lines = [
            f"# Dossier Update: {target or 'all targets'}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Pipeline Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no pipeline scripts executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        if applicable_models:
            report_lines.append("")
            report_lines.append("## Applicable Models")
            for mid in applicable_models:
                report_lines.append(f"- {mid}")

        # Curation status
        if target:
            report_lines.append("")
            report_lines.append("## Curation")
            content_path = _content_root() / "dossiers" / f"{_slugify(target)}.json"
            if content_path.exists():
                try:
                    dossier_data = json.loads(content_path.read_text())
                    curation_data = dossier_data.get("curation") or {}
                    has_narrative = bool(curation_data.get("overview"))
                    key_count = len(curation_data.get("key_finding_ids", []))
                    has_viz = bool(dossier_data.get("viz_data", {}).get("ego_network"))
                    report_lines.append(f"- Key findings selected: {key_count}")
                    report_lines.append(f"- Viz data: {'yes' if has_viz else 'no'}")
                    report_lines.append(f"- Narrative overview: {'yes' if has_narrative else 'needs /curate-dossier'}")
                except (json.JSONDecodeError, KeyError):
                    report_lines.append("- Curation status: error reading dossier")
            else:
                report_lines.append("- Dossier file not found")

        report_path.write_text("\n".join(report_lines))

        return {
            "target": target,
            "report_path": str(report_path),
            "pipeline": tool_results,
            "applicable_models": applicable_models,
            "review_job_id": review_job_id,
            "job_status": "awaiting_review" if review_job_id else "completed",
        }


class DossierFreshnessWorker(AgentWorker):
    JOB_TYPES = ["dossier_freshness_audit"]

    def _has_pending_update(self, canonical_name: str) -> bool:
        pattern = f"%\"target_name\": \"{canonical_name}\"%"
        db = self.queue._connect()
        try:
            row = db.execute(
                """
                SELECT COUNT(*) as n
                FROM job_queue
                WHERE job_type='wiki_dossier_update'
                  AND status IN ('pending', 'claimed', 'in_progress', 'blocked')
                  AND payload LIKE ?
                """,
                (pattern,),
            ).fetchone()
            return row["n"] > 0
        finally:
            db.close()

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        min_findings = int(payload.get("min_findings", 5))
        max_updates = int(payload.get("max_updates", 25))
        dry_run = bool(payload.get("dry_run", False))
        update_backlinks = bool(payload.get("update_backlinks", False))
        spawn_review = bool(payload.get("spawn_review", False))
        review_priority = int(payload.get("review_priority", 4))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.json"

        content_root = _content_root()
        dossiers_dir = content_root / "dossiers"
        existing: Dict[str, Dict[str, Any]] = {}
        if dossiers_dir.exists():
            for path in dossiers_dir.glob("*.json"):
                if path.name.startswith("_"):
                    continue
                try:
                    dossier = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue
                name = dossier.get("name")
                if not name:
                    continue
                existing[name.lower()] = {
                    "name": name,
                    "slug": dossier.get("slug") or path.stem,
                    "last_updated": _parse_datetime(
                        dossier.get("last_updated") or dossier.get("generated_at")
                    ),
                    "aliases": dossier.get("aliases") or [],
                    "has_curation": bool(
                        dossier.get("curation", {}).get("key_finding_ids")
                    ),
                }

        db_path = Path(self.queue.db_path)
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        canonical_data: Dict[str, Dict[str, Any]] = {}
        try:
            raw_to_canonical, canonical_to_aliases = _load_alias_groups(db)
            rows = db.execute(
                """
                SELECT target_name, COUNT(*) as cnt, MAX(created_at) as max_created
                FROM findings
                WHERE verification_status != 'retracted'
                GROUP BY target_name
                """
            ).fetchall()
            for row in rows:
                raw_name = row["target_name"]
                canonical = raw_to_canonical.get(raw_name.lower(), raw_name)
                entry = canonical_data.setdefault(
                    canonical,
                    {"count": 0, "names": set(), "last_updated": None},
                )
                entry["count"] += row["cnt"]
                entry["names"].add(raw_name)
                entry["names"].add(canonical)
                if canonical in canonical_to_aliases:
                    entry["names"].update(canonical_to_aliases[canonical])
                entry["last_updated"] = _max_datetime(
                    entry["last_updated"],
                    _parse_datetime(row["max_created"]),
                )

            conn_rows = db.execute(
                """
                SELECT person_a as name, MAX(created_at) as max_created
                FROM connections
                WHERE verification_status != 'retracted'
                GROUP BY person_a
                UNION ALL
                SELECT person_b as name, MAX(created_at) as max_created
                FROM connections
                WHERE verification_status != 'retracted'
                GROUP BY person_b
                """
            ).fetchall()
            for row in conn_rows:
                raw_name = row["name"]
                if not raw_name:
                    continue
                canonical = raw_to_canonical.get(raw_name.lower(), raw_name)
                if canonical not in canonical_data:
                    continue
                entry = canonical_data[canonical]
                entry["last_updated"] = _max_datetime(
                    entry["last_updated"],
                    _parse_datetime(row["max_created"]),
                )
        finally:
            db.close()

        updates_needed: List[str] = []
        needs_curation: List[str] = []
        new_targets: List[str] = []
        for canonical, entry in sorted(
            canonical_data.items(),
            key=lambda item: -item[1]["count"],
        ):
            if entry["count"] < min_findings:
                continue
            existing_info = existing.get(canonical.lower())
            if not existing_info:
                new_targets.append(canonical)
                updates_needed.append(canonical)
                continue
            last_updated = existing_info.get("last_updated")
            if not last_updated or (
                entry["last_updated"] and entry["last_updated"] > last_updated
            ):
                updates_needed.append(canonical)
            # Check for missing curation
            elif not existing_info.get("has_curation"):
                needs_curation.append(canonical)
                updates_needed.append(canonical)

        curate_dossiers = bool(payload.get("curate", True))
        jobs_created: List[str] = []
        if not dry_run:
            for canonical in updates_needed:
                if len(jobs_created) >= max_updates:
                    break
                if self._has_pending_update(canonical):
                    continue
                job_id = self.queue.create_job(
                    job_type="wiki_dossier_update",
                    domain="curation",
                    payload={
                        "target_name": canonical,
                        "min_findings": min_findings,
                        "update_backlinks": update_backlinks,
                        "spawn_review": spawn_review,
                        "review_priority": review_priority,
                        "curate": curate_dossiers,
                    },
                    priority=payload.get("priority", 4),
                    created_by=f"agent:{self.persona}",
                    source_trigger="dossier_freshness_audit",
                )
                jobs_created.append(job_id)

        report = {
            "targets_checked": len(canonical_data),
            "targets_existing": len(existing),
            "new_targets": new_targets,
            "updates_needed": updates_needed,
            "needs_curation": needs_curation,
            "jobs_created": jobs_created,
            "dry_run": dry_run,
            "min_findings": min_findings,
        }
        report_path.write_text(json.dumps(report, indent=2))

        return {
            "report_path": str(report_path),
            "jobs_created": jobs_created,
            "updates_needed": updates_needed,
            "new_targets": new_targets,
        }


class VisualExportWorker(AgentWorker):
    JOB_TYPES = ["visual_export"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        export_type = payload.get("export_type", "network_graph")
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        build_root = pipeline_root()
        script_map = {
            "network_graph": "export_network.py",
            "financial_flows": "export_financials.py",
            "story_clusters": "story_clustering.py",
            "backlinks": "compute_backlinks.py",
        }
        script_name = script_map.get(export_type)
        if not script_name:
            raise ValueError(f"Unsupported export_type '{export_type}'")

        if not dry_run:
            tool_results[export_type] = _run_script(
                build_root / script_name,
                [],
            )

        report_lines = [
            f"# Visual Export: {export_type}",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Pipeline Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no pipeline scripts executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "export_type": export_type,
            "report_path": str(report_path),
            "pipeline": tool_results,
        }


class ContentBuildWorker(AgentWorker):
    JOB_TYPES = ["content_build"]

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload", {})
        dry_run = bool(payload.get("dry_run", False))

        workdir = _ensure_workdir(job["id"])
        report_path = workdir / "report.md"
        tool_results: Dict[str, Any] = {}

        build_root = pipeline_root()
        if not dry_run:
            tool_results["build_all"] = _run_script(
                build_root / "build_all.py",
                [],
            )

        report_lines = [
            "# Content Build",
            f"## Job ID: {job['id']}",
            f"## Agent: {self.persona}",
            f"## Executed: {_utcnow().isoformat()}",
            "",
            "## Pipeline Results",
        ]
        if dry_run:
            report_lines.append("- dry_run: true (no pipeline scripts executed)")
        for name, result in tool_results.items():
            status = "ok" if result.get("returncode") == 0 else "error"
            report_lines.append(f"- {name}: {status}")
            if result.get("stderr"):
                report_lines.append(f"  - stderr: {result['stderr'][:200]}")
        report_path.write_text("\n".join(report_lines))

        return {
            "report_path": str(report_path),
            "pipeline": tool_results,
        }


WORKER_REGISTRY = {
    "echo": EchoWorker,
    "lead_triage": LeadTriageWorker,
    "deep_person": DeepPersonWorker,
    "source_scan": SurveyorWorker,
    "surveyor": SurveyorWorker,
    "document_mine": DocumentMineWorker,
    "document_miner": DocumentMineWorker,
    "trace_entity": EntityTracerWorker,
    "entity_tracer": EntityTracerWorker,
    "pattern_trigger": PatternSpotterWorker,
    "pattern_spotter": PatternSpotterWorker,
    "synthesis": SynthesistWorker,
    "synthesist": SynthesistWorker,
    "network_analysis": NetworkAnalystWorker,
    "network_analyst": NetworkAnalystWorker,
    "timeline_correlation": TimelineAnalystWorker,
    "timeline_analyst": TimelineAnalystWorker,
    "systemic_analysis": SystemicAnalystWorker,
    "systemic_analyst": SystemicAnalystWorker,
    "mechanism_explainer": ExplainerWriterWorker,
    "explainer_writer": ExplainerWriterWorker,
    "analytical_article": ContextualAnalystWorker,
    "contextual_analyst": ContextualAnalystWorker,
    "editor_review": EditorReviewWorker,
    "editor": EditorReviewWorker,
    "fact_check": EditorReviewWorker,
    "dedupe_review": DedupeReviewWorker,
    "verify_finding": FindingVerificationWorker,
    "tool_build": ToolBuilderWorker,
    "bug_fix": ToolBuilderWorker,
    "source_ingest": SourceIntegratorWorker,
    "registry_add": RegistryAdderWorker,
    "deep_investigate": DeepInvestigationWorker,
    "investigation_orchestrator": DeepInvestigationWorker,
    "wiki_dossier_update": DossierWriterWorker,
    "dossier_writer": DossierWriterWorker,
    "dossier_freshness_audit": DossierFreshnessWorker,
    "visual_export": VisualExportWorker,
    "visual_exporter": VisualExportWorker,
    "content_build": ContentBuildWorker,
    "content_pipeline": ContentBuildWorker,
}
