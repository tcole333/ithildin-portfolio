#!/usr/bin/env python3
"""
Institutional pillars and alumni dynamics tracker.

Models institutions as enabling infrastructure, tracking career arcs,
alumni dispersal, cohort overlaps, and cross-pillar orchestrator scores.

Part of investigation.db.

Usage:
    python tools/pillar_tracker.py register --name "Drexel Burnham Lambert" --type banking --sub-type investment_bank --status dissolved --dissolved 1990
    python tools/pillar_tracker.py list [--type banking] [--status dissolved]
    python tools/pillar_tracker.py show <id>
    python tools/pillar_tracker.py seed
    python tools/pillar_tracker.py arc --person "Leon Black" --pillar "Drexel Burnham Lambert" --role "Managing Director" --seniority senior --start 1977 --end 1990
    python tools/pillar_tracker.py career "Leon Black"
    python tools/pillar_tracker.py event --pillar "Drexel Burnham Lambert" --date 1990-02-13 --type collapse --description "..."
    python tools/pillar_tracker.py events <pillar-name-or-id>
    python tools/pillar_tracker.py bootstrap [--dry-run]
    python tools/pillar_tracker.py alumni "Drexel Burnham Lambert" [--active-during 1985-1990]
    python tools/pillar_tracker.py cohort "Drexel Burnham Lambert" --start 1985 --end 1990
    python tools/pillar_tracker.py dispersal "Drexel Burnham Lambert"
    python tools/pillar_tracker.py overlap --person-a "Leon Black" --person-b "Joshua Harris"
    python tools/pillar_tracker.py timeline "Leon Black"
    python tools/pillar_tracker.py score [--person "Leon Black"] [--top 30] [--type orchestrator]
    python tools/pillar_tracker.py gaps --person "Leon Black"
    python tools/pillar_tracker.py cross-pillar [--min-pillars 3]
    python tools/pillar_tracker.py pillar-network --type legal
    python tools/pillar_tracker.py stats
"""

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

VALID_PILLAR_TYPES = [
    "banking", "legal", "accounting", "government",
    "media", "operations", "intelligence", "philanthropy",
    "consulting", "academia",
]
VALID_STATUSES = ["active", "dissolved", "acquired", "transformed"]
VALID_SENIORITIES = ["junior", "mid", "senior", "leadership", "founder"]
VALID_EXIT_TYPES = [
    "voluntary", "fired", "collapse", "retirement",
    "government_appointment", "indictment", "unknown",
]
VALID_EVENT_TYPES = [
    "investigation", "indictment", "settlement", "collapse",
    "acquisition", "ipo", "scandal", "leadership_change",
    "regulatory_action", "mass_departure", "founding",
]
VALID_SCORE_TYPES = [
    "orchestrator", "pillar_breadth", "revolving_door",
    "dispersal_presence", "cohort_centrality", "institutional_depth",
]


# ── Schema ────────────────────────────────────────────────────

def _ensure_pillar_schema(db):
    """Create all pillar-related tables if they don't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL UNIQUE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(canonical_name);

        CREATE TABLE IF NOT EXISTS institutional_pillars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            pillar_type TEXT NOT NULL CHECK(pillar_type IN (
                'banking', 'legal', 'accounting', 'government',
                'media', 'operations', 'intelligence', 'philanthropy',
                'consulting', 'academia'
            )),
            sub_type TEXT,
            status TEXT DEFAULT 'active' CHECK(status IN (
                'active', 'dissolved', 'acquired', 'transformed'
            )),
            founded TEXT,
            dissolved TEXT,
            successor_id INTEGER REFERENCES institutional_pillars(id),
            entity_id INTEGER REFERENCES entities(id),
            jurisdiction TEXT,
            significance TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS career_arcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(id),
            person_name TEXT NOT NULL,
            pillar_id INTEGER NOT NULL REFERENCES institutional_pillars(id),
            role TEXT NOT NULL,
            department TEXT,
            seniority TEXT CHECK(seniority IN (
                'junior', 'mid', 'senior', 'leadership', 'founder'
            )),
            date_start TEXT,
            date_end TEXT,
            exit_type TEXT CHECK(exit_type IN (
                'voluntary', 'fired', 'collapse', 'retirement',
                'government_appointment', 'indictment', 'unknown'
            )),
            finding_id INTEGER REFERENCES findings(id),
            connection_id INTEGER REFERENCES connections(id),
            source TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(person_id, pillar_id, role, date_start)
        );
        CREATE INDEX IF NOT EXISTS idx_career_arcs_person ON career_arcs(person_id);
        CREATE INDEX IF NOT EXISTS idx_career_arcs_person_name ON career_arcs(person_name);
        CREATE INDEX IF NOT EXISTS idx_career_arcs_pillar ON career_arcs(pillar_id);
        CREATE INDEX IF NOT EXISTS idx_career_arcs_dates ON career_arcs(date_start, date_end);

        CREATE TABLE IF NOT EXISTS pillar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pillar_id INTEGER NOT NULL REFERENCES institutional_pillars(id),
            event_date TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'investigation', 'indictment', 'settlement', 'collapse',
                'acquisition', 'ipo', 'scandal', 'leadership_change',
                'regulatory_action', 'mass_departure', 'founding'
            )),
            description TEXT NOT NULL,
            timeline_event_id INTEGER REFERENCES event_timeline(id),
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pillar_id, event_date, event_type)
        );

        CREATE TABLE IF NOT EXISTS pillar_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(id),
            person_name TEXT NOT NULL,
            score_type TEXT NOT NULL CHECK(score_type IN (
                'orchestrator', 'pillar_breadth', 'revolving_door',
                'dispersal_presence', 'cohort_centrality', 'institutional_depth'
            )),
            score_value REAL NOT NULL,
            detail TEXT,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            analysis_run_id INTEGER,
            UNIQUE(person_id, score_type, analysis_run_id)
        );
    """)


def get_pillar_db():
    """Get DB connection with pillar schema ensured."""
    db = get_db()
    _ensure_pillar_schema(db)
    return db


# ── Person Resolution ────────────────────────────────────────

def _resolve_person(db, name):
    """Resolve a name to a persons.id, creating the person if needed.

    Uses name_aliases for canonical resolution, then looks up or creates
    the persons row.
    """
    # Try alias resolution first
    row = db.execute(
        "SELECT canonical_name FROM name_aliases WHERE LOWER(alias) = LOWER(?) OR LOWER(canonical_name) = LOWER(?)",
        (name, name)
    ).fetchone()
    canonical = row["canonical_name"] if row else name

    # Look up or create person
    row = db.execute("SELECT id FROM persons WHERE LOWER(canonical_name) = LOWER(?)", (canonical,)).fetchone()
    if row:
        return row["id"], canonical
    cursor = db.execute("INSERT INTO persons (canonical_name) VALUES (?)", (canonical,))
    return cursor.lastrowid, canonical


def _resolve_pillar(db, name_or_id):
    """Resolve a pillar by name or ID. Returns row or None."""
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
        return db.execute("SELECT * FROM institutional_pillars WHERE id = ?", (int(name_or_id),)).fetchone()
    return db.execute(
        "SELECT * FROM institutional_pillars WHERE LOWER(name) = LOWER(?)", (name_or_id,)
    ).fetchone()


# ── Institution Management ────────────────────────────────────

def register_pillar(name, pillar_type, sub_type=None, status="active",
                    founded=None, dissolved=None, successor_id=None,
                    entity_id=None, jurisdiction=None, significance=None, source=None):
    """Register an institutional pillar."""
    if pillar_type not in VALID_PILLAR_TYPES:
        print(f"ERROR: Invalid pillar_type '{pillar_type}'. Valid: {VALID_PILLAR_TYPES}")
        return None
    if status not in VALID_STATUSES:
        print(f"ERROR: Invalid status '{status}'. Valid: {VALID_STATUSES}")
        return None

    db = get_pillar_db()
    try:
        cursor = db.execute("""
            INSERT INTO institutional_pillars
                (name, pillar_type, sub_type, status, founded, dissolved,
                 successor_id, entity_id, jurisdiction, significance, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, pillar_type, sub_type, status, founded, dissolved,
              successor_id, entity_id, jurisdiction, significance, source))
        pid = cursor.lastrowid
        db.commit()
        print(f"Registered pillar #{pid}: {name} ({pillar_type}/{sub_type or '-'})")
        return pid
    except sqlite3.IntegrityError:
        row = db.execute("SELECT id FROM institutional_pillars WHERE name = ?", (name,)).fetchone()
        print(f"Already exists: pillar #{row['id']}: {name}")
        return row["id"]
    finally:
        db.close()


def list_pillars(pillar_type=None, status=None):
    """List registered institutional pillars."""
    db = get_pillar_db()
    query = "SELECT * FROM institutional_pillars WHERE 1=1"
    params = []
    if pillar_type:
        query += " AND pillar_type = ?"
        params.append(pillar_type)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY pillar_type, name"
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def show_pillar(pillar_id):
    """Show detailed info for a pillar including arc counts."""
    db = get_pillar_db()
    pillar = db.execute("SELECT * FROM institutional_pillars WHERE id = ?", (pillar_id,)).fetchone()
    if not pillar:
        db.close()
        return None
    result = dict(pillar)
    result["arc_count"] = db.execute(
        "SELECT COUNT(*) FROM career_arcs WHERE pillar_id = ?", (pillar_id,)
    ).fetchone()[0]
    result["events"] = [dict(r) for r in db.execute(
        "SELECT * FROM pillar_events WHERE pillar_id = ? ORDER BY event_date", (pillar_id,)
    ).fetchall()]
    db.close()
    return result


# ── Career Arc Management ────────────────────────────────────

def add_arc(person_name, pillar_name, role, department=None, seniority=None,
            date_start=None, date_end=None, exit_type=None,
            finding_id=None, connection_id=None, source=None, notes=None):
    """Add a career arc for a person at an institution."""
    if seniority and seniority not in VALID_SENIORITIES:
        print(f"ERROR: Invalid seniority '{seniority}'. Valid: {VALID_SENIORITIES}")
        return None
    if exit_type and exit_type not in VALID_EXIT_TYPES:
        print(f"ERROR: Invalid exit_type '{exit_type}'. Valid: {VALID_EXIT_TYPES}")
        return None

    db = get_pillar_db()
    pillar = _resolve_pillar(db, pillar_name)
    if not pillar:
        print(f"ERROR: Pillar '{pillar_name}' not found. Register it first.")
        db.close()
        return None

    person_id, canonical = _resolve_person(db, person_name)

    try:
        cursor = db.execute("""
            INSERT INTO career_arcs
                (person_id, person_name, pillar_id, role, department, seniority,
                 date_start, date_end, exit_type, finding_id, connection_id, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (person_id, canonical, pillar["id"], role, department, seniority,
              date_start, date_end, exit_type, finding_id, connection_id, source, notes))
        arc_id = cursor.lastrowid
        db.commit()
        date_str = f" ({date_start or '?'} - {date_end or 'present'})"
        print(f"Arc #{arc_id}: {canonical} → {pillar['name']} as {role}{date_str}")
        return arc_id
    except sqlite3.IntegrityError:
        print(f"Duplicate arc: {canonical} at {pillar['name']} as {role} from {date_start}")
        return None
    finally:
        db.close()


def get_career(person_name):
    """Get chronological career timeline for a person."""
    db = get_pillar_db()
    person_id, canonical = _resolve_person(db, person_name)

    rows = db.execute("""
        SELECT ca.*, ip.name as pillar_name, ip.pillar_type, ip.status as pillar_status
        FROM career_arcs ca
        JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ?
        ORDER BY COALESCE(ca.date_start, '9999') ASC
    """, (person_id,)).fetchall()
    db.close()
    return canonical, [dict(r) for r in rows]


# ── Events ────────────────────────────────────────────────────

def add_event(pillar_name, event_date, event_type, description,
              timeline_event_id=None, source=None):
    """Add an event to an institution's timeline."""
    if event_type not in VALID_EVENT_TYPES:
        print(f"ERROR: Invalid event_type '{event_type}'. Valid: {VALID_EVENT_TYPES}")
        return None

    db = get_pillar_db()
    pillar = _resolve_pillar(db, pillar_name)
    if not pillar:
        print(f"ERROR: Pillar '{pillar_name}' not found.")
        db.close()
        return None

    try:
        cursor = db.execute("""
            INSERT INTO pillar_events (pillar_id, event_date, event_type, description, timeline_event_id, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (pillar["id"], event_date, event_type, description, timeline_event_id, source))
        eid = cursor.lastrowid
        db.commit()
        print(f"Event #{eid}: {pillar['name']} — {event_type} on {event_date}")
        return eid
    except sqlite3.IntegrityError:
        print(f"Duplicate event: {pillar['name']} {event_type} on {event_date}")
        return None
    finally:
        db.close()


def get_events(pillar_name_or_id):
    """Get timeline of events for an institution."""
    db = get_pillar_db()
    pillar = _resolve_pillar(db, pillar_name_or_id)
    if not pillar:
        db.close()
        return None, []
    rows = db.execute("""
        SELECT * FROM pillar_events WHERE pillar_id = ? ORDER BY event_date
    """, (pillar["id"],)).fetchall()
    db.close()
    return dict(pillar), [dict(r) for r in rows]


# ── Alumni / Cohort / Dispersal Analysis ──────────────────────

def get_alumni(pillar_name_or_id, active_start=None, active_end=None):
    """Get all known alumni of an institution, optionally filtered to a time window."""
    db = get_pillar_db()
    pillar = _resolve_pillar(db, pillar_name_or_id)
    if not pillar:
        db.close()
        return None, []

    query = """
        SELECT ca.*, ip.name as pillar_name
        FROM career_arcs ca
        JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.pillar_id = ?
    """
    params = [pillar["id"]]

    if active_start and active_end:
        # Person was there during at least part of the window
        query += " AND (ca.date_start IS NULL OR ca.date_start <= ?) AND (ca.date_end IS NULL OR ca.date_end >= ?)"
        params.extend([active_end, active_start])

    query += " ORDER BY COALESCE(ca.date_start, '0000') ASC"
    rows = db.execute(query, params).fetchall()
    db.close()
    return dict(pillar), [dict(r) for r in rows]


def get_cohort(pillar_name_or_id, start, end):
    """Get people who overlapped at an institution during a specific period."""
    return get_alumni(pillar_name_or_id, active_start=start, active_end=end)


def get_dispersal(pillar_name_or_id):
    """Where alumni went after leaving this institution."""
    db = get_pillar_db()
    pillar = _resolve_pillar(db, pillar_name_or_id)
    if not pillar:
        db.close()
        return None, {}

    # Get all people who were at this institution
    alumni = db.execute(
        "SELECT DISTINCT person_id, person_name, date_end FROM career_arcs WHERE pillar_id = ?",
        (pillar["id"],)
    ).fetchall()

    destinations = defaultdict(list)
    for alum in alumni:
        # Find their next career arcs at other institutions
        next_arcs = db.execute("""
            SELECT ca.*, ip.name as dest_name, ip.pillar_type as dest_type
            FROM career_arcs ca
            JOIN institutional_pillars ip ON ca.pillar_id = ip.id
            WHERE ca.person_id = ? AND ca.pillar_id != ?
            ORDER BY COALESCE(ca.date_start, '9999') ASC
        """, (alum["person_id"], pillar["id"])).fetchall()
        for arc in next_arcs:
            destinations[arc["dest_name"]].append({
                "person": alum["person_name"],
                "departed": alum["date_end"],
                "arrived": arc["date_start"],
                "role": arc["role"],
                "dest_type": arc["dest_type"],
            })

    db.close()
    return dict(pillar), dict(destinations)


def get_overlap(person_a, person_b):
    """Find shared institutional tenures between two people."""
    db = get_pillar_db()
    pid_a, name_a = _resolve_person(db, person_a)
    pid_b, name_b = _resolve_person(db, person_b)

    arcs_a = db.execute("""
        SELECT ca.*, ip.name as pillar_name, ip.pillar_type
        FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ?
    """, (pid_a,)).fetchall()

    arcs_b = db.execute("""
        SELECT ca.*, ip.name as pillar_name, ip.pillar_type
        FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ?
    """, (pid_b,)).fetchall()

    db.close()

    overlaps = []
    for a in arcs_a:
        for b in arcs_b:
            if a["pillar_id"] != b["pillar_id"]:
                continue
            # Check temporal overlap (if both have dates)
            a_start = a["date_start"] or "0000"
            a_end = a["date_end"] or "9999"
            b_start = b["date_start"] or "0000"
            b_end = b["date_end"] or "9999"
            overlap_start = max(a_start, b_start)
            overlap_end = min(a_end, b_end)
            if overlap_start <= overlap_end:
                overlaps.append({
                    "institution": a["pillar_name"],
                    "pillar_type": a["pillar_type"],
                    "person_a_role": a["role"],
                    "person_b_role": b["role"],
                    "overlap_start": overlap_start if overlap_start != "0000" else None,
                    "overlap_end": overlap_end if overlap_end != "9999" else None,
                })

    return name_a, name_b, overlaps


def get_person_timeline(person_name):
    """Career arcs interleaved with pillar events and external events."""
    db = get_pillar_db()
    person_id, canonical = _resolve_person(db, person_name)

    arcs = db.execute("""
        SELECT ca.date_start as event_date, 'arc_start' as event_kind,
               ca.role || ' at ' || ip.name as description, ip.pillar_type, ca.id as ref_id
        FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ? AND ca.date_start IS NOT NULL
        UNION ALL
        SELECT ca.date_end as event_date, 'arc_end' as event_kind,
               'Left ' || ip.name || ' (' || COALESCE(ca.exit_type, 'unknown') || ')' as description,
               ip.pillar_type, ca.id as ref_id
        FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ? AND ca.date_end IS NOT NULL
    """, (person_id, person_id)).fetchall()

    # Get pillar events for institutions this person was at
    pillar_events = db.execute("""
        SELECT pe.event_date, 'pillar_event' as event_kind,
               ip.name || ': ' || pe.description as description,
               ip.pillar_type, pe.id as ref_id
        FROM pillar_events pe
        JOIN institutional_pillars ip ON pe.pillar_id = ip.id
        WHERE pe.pillar_id IN (SELECT DISTINCT pillar_id FROM career_arcs WHERE person_id = ?)
    """, (person_id,)).fetchall()

    # Get external events from event_timeline (if table exists)
    external = []
    try:
        external = db.execute("""
            SELECT event_date, 'external' as event_kind,
                   event_name as description, category as pillar_type, id as ref_id
            FROM event_timeline
            WHERE event_date BETWEEN
                (SELECT MIN(COALESCE(date_start, '1900')) FROM career_arcs WHERE person_id = ?)
                AND
                (SELECT MAX(COALESCE(date_end, '2030')) FROM career_arcs WHERE person_id = ?)
        """, (person_id, person_id)).fetchall()
    except sqlite3.OperationalError:
        pass

    db.close()

    all_events = [dict(r) for r in arcs] + [dict(r) for r in pillar_events] + [dict(r) for r in external]
    all_events.sort(key=lambda e: e.get("event_date") or "0000")
    return canonical, all_events


# ── Cross-Pillar / Orchestrator Analysis ──────────────────────

def compute_scores(person_name=None, top=30, score_type="orchestrator", run_id=None):
    """Compute orchestrator scores. If person_name given, score just that person."""
    db = get_pillar_db()

    if person_name:
        pid, canonical = _resolve_person(db, person_name)
        person_ids = [(pid, canonical)]
    else:
        person_ids = db.execute("""
            SELECT DISTINCT p.id, p.canonical_name
            FROM persons p
            JOIN career_arcs ca ON ca.person_id = p.id
            GROUP BY p.id HAVING COUNT(ca.id) >= 1
        """).fetchall()
        person_ids = [(r["id"], r["canonical_name"]) for r in person_ids]

    all_person_arcs = {}
    for pid, _ in person_ids:
        arcs = db.execute("""
            SELECT ca.*, ip.pillar_type, ip.status as pillar_status, ip.name as pillar_name
            FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
            WHERE ca.person_id = ?
        """, (pid,)).fetchall()
        all_person_arcs[pid] = [dict(a) for a in arcs]

    results = []
    for pid, canonical in person_ids:
        arcs = all_person_arcs.get(pid, [])
        if not arcs:
            continue

        # Component: pillar_breadth — distinct pillar types, weighted by seniority
        seniority_weights = {"founder": 3, "leadership": 2.5, "senior": 2, "mid": 1.5, "junior": 1}
        pillar_types = set()
        breadth_score = 0
        for a in arcs:
            pillar_types.add(a["pillar_type"])
            w = seniority_weights.get(a.get("seniority"), 1)
            breadth_score += w
        breadth_score = len(pillar_types) * (breadth_score / max(len(arcs), 1))

        # Component: revolving_door — government <-> private transitions
        gov_arcs = [a for a in arcs if a["pillar_type"] == "government"]
        private_arcs = [a for a in arcs if a["pillar_type"] != "government"]
        revolving_count = 0
        if gov_arcs and private_arcs:
            revolving_count = min(len(gov_arcs), len(private_arcs))

        # Component: dispersal_presence — was at collapsed institution, then moved
        dispersal_count = 0
        for a in arcs:
            if a.get("pillar_status") in ("dissolved", "acquired") or a.get("exit_type") == "collapse":
                # Check if person has subsequent arc
                later = [x for x in arcs if x["pillar_id"] != a["pillar_id"]
                         and (x.get("date_start") or "9999") >= (a.get("date_end") or "0000")]
                if later:
                    dispersal_count += 1

        # Component: cohort_centrality — number of institutional overlaps with other tracked persons
        cohort_count = 0
        for a in arcs:
            others = db.execute("""
                SELECT COUNT(DISTINCT person_id) FROM career_arcs
                WHERE pillar_id = ? AND person_id != ?
            """, (a["pillar_id"], pid)).fetchone()[0]
            cohort_count += others

        # Component: institutional_depth — total career years
        total_years = 0
        for a in arcs:
            start = a.get("date_start")
            end = a.get("date_end")
            if start and end:
                try:
                    s = int(start[:4])
                    e = int(end[:4])
                    total_years += max(e - s, 0)
                except (ValueError, IndexError):
                    pass

        # Composite: breadth * 3 + revolving_door * 4 + dispersal * 2 + sqrt(cohort) + log(years + 1)
        composite = (
            breadth_score * 3
            + revolving_count * 4
            + dispersal_count * 2
            + math.sqrt(cohort_count)
            + math.log(total_years + 1)
        )

        detail = {
            "pillar_breadth": round(breadth_score, 2),
            "pillar_types": sorted(pillar_types),
            "revolving_door": revolving_count,
            "dispersal_presence": dispersal_count,
            "cohort_centrality": cohort_count,
            "institutional_depth_years": total_years,
            "arc_count": len(arcs),
        }

        results.append({
            "person_id": pid,
            "person_name": canonical,
            "orchestrator_score": round(composite, 2),
            "detail": detail,
        })

        # Cache in DB
        if run_id is not None:
            for stype, sval in [
                ("orchestrator", composite),
                ("pillar_breadth", breadth_score),
                ("revolving_door", float(revolving_count)),
                ("dispersal_presence", float(dispersal_count)),
                ("cohort_centrality", float(cohort_count)),
                ("institutional_depth", float(total_years)),
            ]:
                db.execute("""
                    INSERT OR REPLACE INTO pillar_scores
                        (person_id, person_name, score_type, score_value, detail, analysis_run_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, canonical, stype, round(sval, 2), json.dumps(detail), run_id))

    if run_id is not None:
        db.commit()
    db.close()

    results.sort(key=lambda r: r["orchestrator_score"], reverse=True)
    return results[:top] if not person_name else results


def get_pillar_gaps(person_name):
    """Find pillar types missing from a person's career arcs."""
    db = get_pillar_db()
    pid, canonical = _resolve_person(db, person_name)
    present = db.execute("""
        SELECT DISTINCT ip.pillar_type
        FROM career_arcs ca JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ca.person_id = ?
    """, (pid,)).fetchall()
    db.close()
    present_types = {r["pillar_type"] for r in present}
    missing = [t for t in VALID_PILLAR_TYPES if t not in present_types]
    return canonical, sorted(present_types), missing


def get_cross_pillar(min_pillars=3):
    """People who appear across N+ pillar types."""
    db = get_pillar_db()
    rows = db.execute("""
        SELECT p.id, p.canonical_name,
               COUNT(DISTINCT ip.pillar_type) as pillar_count,
               GROUP_CONCAT(DISTINCT ip.pillar_type) as pillar_types,
               COUNT(ca.id) as arc_count
        FROM persons p
        JOIN career_arcs ca ON ca.person_id = p.id
        JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        GROUP BY p.id
        HAVING COUNT(DISTINCT ip.pillar_type) >= ?
        ORDER BY pillar_count DESC, arc_count DESC
    """, (min_pillars,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── Bootstrap ────────────────────────────────────────────────

# Mapping from common institution names to pillar names for bootstrap
INSTITUTION_ALIASES = {
    "goldman sachs": "Goldman Sachs",
    "goldman": "Goldman Sachs",
    "gs": "Goldman Sachs",
    "deutsche bank": "Deutsche Bank",
    "db": "Deutsche Bank",
    "jpmorgan": "JPMorgan Chase",
    "jp morgan": "JPMorgan Chase",
    "jpmorgan chase": "JPMorgan Chase",
    "bear stearns": "Bear Stearns",
    "bear": "Bear Stearns",
    "drexel": "Drexel Burnham Lambert",
    "drexel burnham": "Drexel Burnham Lambert",
    "kirkland": "Kirkland & Ellis",
    "kirkland & ellis": "Kirkland & Ellis",
    "k&e": "Kirkland & Ellis",
    "paul weiss": "Paul Weiss",
    "sullivan & cromwell": "Sullivan & Cromwell",
    "dechert": "Dechert LLP",
    "latham": "Latham & Watkins",
    "latham & watkins": "Latham & Watkins",
    "boies schiller": "Boies Schiller Flexner",
    "arthur andersen": "Arthur Andersen",
    "kpmg": "KPMG",
    "ernst & young": "Ernst & Young",
    "ey": "Ernst & Young",
    "pwc": "PricewaterhouseCoopers",
    "pricewaterhousecoopers": "PricewaterhouseCoopers",
    "deloitte": "Deloitte",
    "doj": "DOJ (Criminal Division)",
    "sec": "SEC",
    "sdny": "SDNY (US Attorney)",
    "fbi": "FBI",
    "cia": "CIA",
    "apollo": "Apollo Global Management",
    "harvard": "Harvard",
    "mit": "MIT",
    "clinton foundation": "Clinton Foundation",
    "southern trust": "Southern Trust Company",
    "stc": "Southern Trust Company",
    "citigroup": "Citigroup",
    "citi": "Citigroup",
    "steptoe": "Steptoe & Johnson",
}


def _match_institution(name, db):
    """Try to match a name to a registered pillar."""
    # Exact match
    row = db.execute(
        "SELECT id, name FROM institutional_pillars WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row:
        return row["id"], row["name"]

    # Alias match
    normalized = name.lower().strip()
    if normalized in INSTITUTION_ALIASES:
        canonical = INSTITUTION_ALIASES[normalized]
        row = db.execute(
            "SELECT id, name FROM institutional_pillars WHERE LOWER(name) = LOWER(?)", (canonical,)
        ).fetchone()
        if row:
            return row["id"], row["name"]

    # Fuzzy: check if any pillar name is contained in the input or vice versa
    all_pillars = db.execute("SELECT id, name FROM institutional_pillars").fetchall()
    for p in all_pillars:
        pname_lower = p["name"].lower()
        if pname_lower in normalized or normalized in pname_lower:
            return p["id"], p["name"]

    return None, None


def _resolve_person_for_bootstrap(db, name):
    """Resolve a person name using aliases, return (person_id, canonical_name).

    Creates the person entry if needed. Unlike _resolve_person, operates
    on a shared db connection (no open/close).
    """
    row = db.execute(
        "SELECT canonical_name FROM name_aliases WHERE LOWER(alias) = LOWER(?) OR LOWER(canonical_name) = LOWER(?)",
        (name, name)
    ).fetchone()
    canonical = row["canonical_name"] if row else name

    person_row = db.execute("SELECT id FROM persons WHERE LOWER(canonical_name) = LOWER(?)", (canonical,)).fetchone()
    if person_row:
        return person_row["id"], canonical

    try:
        cursor = db.execute("INSERT INTO persons (canonical_name) VALUES (?)", (canonical,))
        return cursor.lastrowid, canonical
    except sqlite3.IntegrityError:
        person_row = db.execute("SELECT id FROM persons WHERE LOWER(canonical_name) = LOWER(?)", (canonical,)).fetchone()
        return person_row["id"], canonical


def bootstrap(dry_run=False):
    """Mine existing data to populate persons and career_arcs."""
    db = get_pillar_db()
    stats = {"persons_created": 0, "arcs_created": 0, "arcs_skipped": 0, "unmatched": []}

    # Step 0: Populate persons from name_aliases (canonical names only)
    try:
        alias_names = db.execute(
            "SELECT DISTINCT canonical_name FROM name_aliases WHERE alias_type = 'person_variant'"
        ).fetchall()
        for row in alias_names:
            try:
                db.execute("INSERT OR IGNORE INTO persons (canonical_name) VALUES (?)",
                           (row["canonical_name"],))
                if db.execute("SELECT changes()").fetchone()[0]:
                    stats["persons_created"] += 1
            except sqlite3.IntegrityError:
                pass
    except sqlite3.OperationalError:
        pass

    # Step 1: Employment connections → career arcs
    emp_rows = db.execute("""
        SELECT id, person_a, person_b, description, date_range
        FROM connections WHERE relationship_type = 'employment'
    """).fetchall()

    for row in emp_rows:
        pa, pb = row["person_a"], row["person_b"]

        pillar_id_a, pillar_name_a = _match_institution(pa, db)
        pillar_id_b, pillar_name_b = _match_institution(pb, db)

        if pillar_id_a and not pillar_id_b:
            person_name = pb
            pillar_id, pillar_name = pillar_id_a, pillar_name_a
        elif pillar_id_b and not pillar_id_a:
            person_name = pa
            pillar_id, pillar_name = pillar_id_b, pillar_name_b
        else:
            stats["unmatched"].append(f"conn#{row['id']}: {pa} / {pb}")
            stats["arcs_skipped"] += 1
            continue

        person_id, canonical = _resolve_person_for_bootstrap(db, person_name)
        if canonical != person_name:
            stats["persons_created"] += 0  # already counted or existed

        # Parse date_range
        date_start, date_end = None, None
        if row["date_range"]:
            parts = re.split(r'\s*(?:to|-)\s*', row["date_range"], maxsplit=1)
            if parts:
                date_start = parts[0].strip()
            if len(parts) > 1:
                date_end = parts[1].strip()

        # Extract role from description
        role = "employee"
        desc = row["description"] or ""
        role_patterns = [
            r'\b(Partner|Managing Director|MD|General Counsel|CEO|CFO|COO|CTO|Chairman|President|VP|Vice President)\b',
            r'\b(Head of|Global Head|Director of)\s+\w+',
            r'\b(analyst|associate|counsel|attorney|manager|advisor|consultant)\b',
        ]
        for pattern in role_patterns:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                role = m.group(0).strip()
                break

        if dry_run:
            print(f"  [DRY] Arc: {canonical} → {pillar_name} as {role} ({date_start} - {date_end})")
            stats["arcs_created"] += 1
            continue

        try:
            db.execute("""
                INSERT OR IGNORE INTO career_arcs
                    (person_id, person_name, pillar_id, role, date_start, date_end,
                     connection_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (person_id, canonical, pillar_id, role, date_start, date_end,
                  row["id"], "bootstrap:employment_connections"))
            if db.execute("SELECT changes()").fetchone()[0]:
                stats["arcs_created"] += 1
            else:
                stats["arcs_skipped"] += 1
        except sqlite3.IntegrityError:
            stats["arcs_skipped"] += 1

    # Step 2: Entity roles → career arcs (exclude primary subject from bootstrap)
    from tools.investigation_context import get_active_profile
    _profile = get_active_profile()
    _exclude_name = _profile.primary_subject.lower() if _profile.primary_subject else ""
    entity_roles = db.execute("""
        SELECT er.id, er.entity_id, er.person_name, er.role, er.date_start, er.date_end, er.source,
               e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
        WHERE LOWER(er.person_name) != ?
    """, (_exclude_name,)).fetchall()

    for er in entity_roles:
        pillar_id, pillar_name = _match_institution(er["entity_name"], db)
        if not pillar_id:
            continue

        person_id, canonical = _resolve_person_for_bootstrap(db, er["person_name"])

        if dry_run:
            print(f"  [DRY] Arc (entity_role): {canonical} → {pillar_name} as {er['role']}")
            stats["arcs_created"] += 1
            continue

        try:
            db.execute("""
                INSERT OR IGNORE INTO career_arcs
                    (person_id, person_name, pillar_id, role, date_start, date_end, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (person_id, canonical, pillar_id, er["role"],
                  er["date_start"], er["date_end"], "bootstrap:entity_roles"))
            if db.execute("SELECT changes()").fetchone()[0]:
                stats["arcs_created"] += 1
            else:
                stats["arcs_skipped"] += 1
        except sqlite3.IntegrityError:
            stats["arcs_skipped"] += 1

    if not dry_run:
        db.commit()
    db.close()
    return stats


def rebootstrap():
    """Clear all persons and career_arcs, then re-run bootstrap with alias-aware resolution."""
    db = get_pillar_db()
    db.execute("DELETE FROM pillar_scores")
    db.execute("DELETE FROM career_arcs")
    db.execute("DELETE FROM persons")
    db.commit()
    db.close()
    print("Cleared persons, career_arcs, pillar_scores")
    return bootstrap(dry_run=False)


# ── Seed Data ────────────────────────────────────────────────

def _load_seed_pillars():
    """Load seed pillars from the active investigation profile."""
    try:
        from tools.investigation_context import get_active_profile
        profile = get_active_profile()
        return profile.seed_pillars or []
    except Exception:
        return []


def seed():
    """Populate institutional pillars from the active investigation profile."""
    pillars = _load_seed_pillars()
    if not pillars:
        print("No seed pillars defined in active investigation profile.")
        return 0

    db = get_pillar_db()
    created = 0
    skipped = 0

    for entry in pillars:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            ptype = entry.get("pillar_type", "")
            sub_type = entry.get("sub_type", "")
            status = entry.get("status", "active")
            founded = entry.get("founded")
            dissolved = entry.get("dissolved")
            jurisdiction = entry.get("jurisdiction")
            significance = entry.get("significance", "")
        else:
            # Legacy tuple format
            name, ptype, sub_type, status, founded, dissolved, jurisdiction, significance = entry

        try:
            db.execute("""
                INSERT INTO institutional_pillars
                    (name, pillar_type, sub_type, status, founded, dissolved, jurisdiction, significance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, ptype, sub_type, status, founded, dissolved, jurisdiction, significance))
            created += 1
        except sqlite3.IntegrityError:
            skipped += 1

    db.commit()
    db.close()
    print(f"Seed complete: {created} created, {skipped} already existed")
    return created


# ── Stats ────────────────────────────────────────────────────

def get_stats():
    """Summary counts for the pillar system."""
    db = get_pillar_db()
    stats = {}
    stats["pillars"] = db.execute("SELECT COUNT(*) FROM institutional_pillars").fetchone()[0]
    stats["persons"] = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    stats["career_arcs"] = db.execute("SELECT COUNT(*) FROM career_arcs").fetchone()[0]
    stats["pillar_events"] = db.execute("SELECT COUNT(*) FROM pillar_events").fetchone()[0]
    stats["pillar_scores"] = db.execute("SELECT COUNT(*) FROM pillar_scores").fetchone()[0]

    # Breakdown by pillar type
    type_rows = db.execute("""
        SELECT pillar_type, COUNT(*) as cnt
        FROM institutional_pillars GROUP BY pillar_type ORDER BY cnt DESC
    """).fetchall()
    stats["by_type"] = {r["pillar_type"]: r["cnt"] for r in type_rows}

    # Top institutions by arc count
    top_rows = db.execute("""
        SELECT ip.name, COUNT(ca.id) as arc_count
        FROM institutional_pillars ip
        LEFT JOIN career_arcs ca ON ca.pillar_id = ip.id
        GROUP BY ip.id
        HAVING arc_count > 0
        ORDER BY arc_count DESC
        LIMIT 10
    """).fetchall()
    stats["top_institutions"] = [{"name": r["name"], "arcs": r["arc_count"]} for r in top_rows]

    db.close()
    return stats


# ── Pillar Network ────────────────────────────────────────────

def get_pillar_network(pillar_type):
    """Get all people connected through institutions of a given type."""
    db = get_pillar_db()
    rows = db.execute("""
        SELECT ca.person_name, ip.name as pillar_name, ca.role, ca.seniority,
               ca.date_start, ca.date_end
        FROM career_arcs ca
        JOIN institutional_pillars ip ON ca.pillar_id = ip.id
        WHERE ip.pillar_type = ?
        ORDER BY ip.name, ca.person_name
    """, (pillar_type,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Institutional pillars and alumni dynamics tracker")
    sub = parser.add_subparsers(dest="command")

    # register
    p_reg = sub.add_parser("register", help="Register a new institutional pillar")
    p_reg.add_argument("--name", required=True)
    p_reg.add_argument("--type", required=True, dest="pillar_type", choices=VALID_PILLAR_TYPES)
    p_reg.add_argument("--sub-type")
    p_reg.add_argument("--status", default="active", choices=VALID_STATUSES)
    p_reg.add_argument("--founded")
    p_reg.add_argument("--dissolved")
    p_reg.add_argument("--successor-id", type=int)
    p_reg.add_argument("--entity-id", type=int)
    p_reg.add_argument("--jurisdiction")
    p_reg.add_argument("--significance")
    p_reg.add_argument("--source")

    # list
    p_list = sub.add_parser("list", help="List registered institutional pillars")
    p_list.add_argument("--type", dest="pillar_type", choices=VALID_PILLAR_TYPES)
    p_list.add_argument("--status", choices=VALID_STATUSES)
    add_output_args(p_list)

    # show
    p_show = sub.add_parser("show", help="Show pillar details")
    p_show.add_argument("pillar_id", type=int)
    add_output_args(p_show)

    # seed
    sub.add_parser("seed", help="Populate initial ~35 institutional pillars")

    # arc
    p_arc = sub.add_parser("arc", help="Add a career arc")
    p_arc.add_argument("--person", required=True)
    p_arc.add_argument("--pillar", required=True)
    p_arc.add_argument("--role", required=True)
    p_arc.add_argument("--department")
    p_arc.add_argument("--seniority", choices=VALID_SENIORITIES)
    p_arc.add_argument("--start")
    p_arc.add_argument("--end")
    p_arc.add_argument("--exit-type", choices=VALID_EXIT_TYPES)
    p_arc.add_argument("--finding-id", type=int)
    p_arc.add_argument("--connection-id", type=int)
    p_arc.add_argument("--source")
    p_arc.add_argument("--notes")

    # career
    p_career = sub.add_parser("career", help="Show career timeline for a person")
    p_career.add_argument("person")
    add_output_args(p_career)

    # event
    p_event = sub.add_parser("event", help="Add an institutional event")
    p_event.add_argument("--pillar", required=True)
    p_event.add_argument("--date", required=True)
    p_event.add_argument("--type", required=True, dest="event_type", choices=VALID_EVENT_TYPES)
    p_event.add_argument("--description", required=True)
    p_event.add_argument("--timeline-event-id", type=int)
    p_event.add_argument("--source")

    # events
    p_events = sub.add_parser("events", help="Show events for an institution")
    p_events.add_argument("pillar")
    add_output_args(p_events)

    # bootstrap
    p_boot = sub.add_parser("bootstrap", help="Mine existing data to populate career arcs")
    p_boot.add_argument("--dry-run", action="store_true")

    # rebootstrap
    sub.add_parser("rebootstrap", help="Clear and re-run bootstrap with alias-aware resolution")

    # alumni
    p_alumni = sub.add_parser("alumni", help="List alumni of an institution")
    p_alumni.add_argument("pillar")
    p_alumni.add_argument("--active-during", help="Time window as START-END (e.g. 1985-1990)")
    add_output_args(p_alumni)

    # cohort
    p_cohort = sub.add_parser("cohort", help="People who overlapped at an institution")
    p_cohort.add_argument("pillar")
    p_cohort.add_argument("--start", required=True)
    p_cohort.add_argument("--end", required=True)
    add_output_args(p_cohort)

    # dispersal
    p_disp = sub.add_parser("dispersal", help="Where alumni went after leaving")
    p_disp.add_argument("pillar")
    add_output_args(p_disp)

    # overlap
    p_over = sub.add_parser("overlap", help="Shared institutional tenures between two people")
    p_over.add_argument("--person-a", required=True)
    p_over.add_argument("--person-b", required=True)
    add_output_args(p_over)

    # timeline
    p_tl = sub.add_parser("timeline", help="Person timeline with career + institutional + external events")
    p_tl.add_argument("person")
    add_output_args(p_tl)

    # score
    p_score = sub.add_parser("score", help="Compute orchestrator scores")
    p_score.add_argument("--person")
    p_score.add_argument("--top", type=int, default=30)
    p_score.add_argument("--type", default="orchestrator", dest="score_type", choices=VALID_SCORE_TYPES)
    p_score.add_argument("--cache", action="store_true", help="Cache results in pillar_scores table")
    add_output_args(p_score)

    # gaps
    p_gaps = sub.add_parser("gaps", help="Pillar types missing from person's career")
    p_gaps.add_argument("--person", required=True)

    # cross-pillar
    p_xp = sub.add_parser("cross-pillar", help="People appearing across multiple pillar types")
    p_xp.add_argument("--min-pillars", type=int, default=3)
    add_output_args(p_xp)

    # pillar-network
    p_pn = sub.add_parser("pillar-network", help="People connected through institutions of a type")
    p_pn.add_argument("--type", required=True, dest="pillar_type", choices=VALID_PILLAR_TYPES)
    add_output_args(p_pn)

    # stats
    sub.add_parser("stats", help="Summary counts")

    args = parser.parse_args()

    # ── Dispatch ────────────────────────────────────────────

    if args.command == "register":
        register_pillar(
            args.name, args.pillar_type, sub_type=args.sub_type,
            status=args.status, founded=args.founded, dissolved=args.dissolved,
            successor_id=args.successor_id, entity_id=args.entity_id,
            jurisdiction=args.jurisdiction, significance=args.significance,
            source=args.source,
        )

    elif args.command == "list":
        pillars = list_pillars(pillar_type=args.pillar_type, status=args.status)
        if write_output(pillars, args, summary=f"pillars ({len(pillars)})"):
            return
        current_type = None
        for p in pillars:
            if p["pillar_type"] != current_type:
                current_type = p["pillar_type"]
                print(f"\n  {current_type.upper()}")
                print(f"  {'─' * 50}")
            status_tag = f" [{p['status']}]" if p["status"] != "active" else ""
            print(f"  #{p['id']:<3} {p['name']:<40} {p['sub_type'] or '':<20}{status_tag}")

    elif args.command == "show":
        result = show_pillar(args.pillar_id)
        if not result:
            print(f"Pillar #{args.pillar_id} not found")
            sys.exit(1)
        if write_output(result, args, summary=f"pillar #{args.pillar_id}"):
            return
        print(f"\n{'═' * 60}")
        print(f"  {result['name']}")
        print(f"  Type: {result['pillar_type']}/{result.get('sub_type', '-')}")
        print(f"  Status: {result['status']}")
        if result.get("founded"):
            print(f"  Founded: {result['founded']}")
        if result.get("dissolved"):
            print(f"  Dissolved: {result['dissolved']}")
        if result.get("jurisdiction"):
            print(f"  Jurisdiction: {result['jurisdiction']}")
        if result.get("significance"):
            print(f"  Significance: {result['significance']}")
        print(f"  Career arcs: {result['arc_count']}")
        if result["events"]:
            print(f"\n  Events ({len(result['events'])}):")
            for e in result["events"]:
                print(f"    {e['event_date']}  {e['event_type']:<20}  {e['description']}")
        print(f"{'═' * 60}")

    elif args.command == "seed":
        seed()

    elif args.command == "arc":
        add_arc(
            args.person, args.pillar, args.role,
            department=args.department, seniority=args.seniority,
            date_start=args.start, date_end=args.end,
            exit_type=args.exit_type, finding_id=args.finding_id,
            connection_id=args.connection_id, source=args.source,
            notes=args.notes,
        )

    elif args.command == "career":
        canonical, arcs = get_career(args.person)
        if write_output({"person": canonical, "arcs": arcs}, args, summary=f"career of {canonical}"):
            return
        print(f"\nCareer Timeline: {canonical}")
        print(f"{'═' * 70}")
        if not arcs:
            print("  No career arcs recorded")
        for a in arcs:
            dates = f"{a.get('date_start') or '?'} – {a.get('date_end') or 'present'}"
            exit_str = f" ({a['exit_type']})" if a.get("exit_type") else ""
            print(f"  {dates:<20}  {a['pillar_name']:<30}  {a['role']}{exit_str}")

    elif args.command == "event":
        add_event(args.pillar, args.date, args.event_type, args.description,
                  timeline_event_id=args.timeline_event_id, source=args.source)

    elif args.command == "events":
        pillar, events = get_events(args.pillar)
        if not pillar:
            print(f"Pillar '{args.pillar}' not found")
            sys.exit(1)
        if write_output({"pillar": pillar, "events": events}, args, summary=f"events for {pillar['name']}"):
            return
        print(f"\nEvents: {pillar['name']}")
        print(f"{'─' * 60}")
        if not events:
            print("  No events recorded")
        for e in events:
            print(f"  {e['event_date']}  {e['event_type']:<20}  {e['description']}")

    elif args.command == "bootstrap":
        print("Bootstrap: Mining existing data for career arcs...")
        if args.dry_run:
            print("[DRY RUN — no changes will be saved]\n")
        stats = bootstrap(dry_run=args.dry_run)
        print(f"\nBootstrap results:")
        print(f"  Persons created: {stats['persons_created']}")
        print(f"  Career arcs created: {stats['arcs_created']}")
        print(f"  Arcs skipped (duplicate/unmatched): {stats['arcs_skipped']}")
        if stats["unmatched"][:10]:
            print(f"\n  Unmatched connections (first 10):")
            for u in stats["unmatched"][:10]:
                print(f"    {u}")
        if args.dry_run:
            print("\n(Dry run — nothing was saved)")

    elif args.command == "rebootstrap":
        print("Rebootstrap: Clearing and re-mining with alias-aware resolution...")
        stats = rebootstrap()
        print(f"\nRebootstrap results:")
        print(f"  Persons created: {stats['persons_created']}")
        print(f"  Career arcs created: {stats['arcs_created']}")
        print(f"  Arcs skipped: {stats['arcs_skipped']}")

    elif args.command == "alumni":
        active_start, active_end = None, None
        if args.active_during:
            parts = args.active_during.split("-", 1)
            if len(parts) == 2:
                active_start, active_end = parts[0], parts[1]

        pillar, alumni = get_alumni(args.pillar, active_start, active_end)
        if not pillar:
            print(f"Pillar '{args.pillar}' not found")
            sys.exit(1)
        if write_output({"pillar": pillar, "alumni": alumni}, args, summary=f"alumni of {pillar['name']}"):
            return
        window = f" (active {args.active_during})" if args.active_during else ""
        print(f"\nAlumni: {pillar['name']}{window} — {len(alumni)} records")
        print(f"{'─' * 70}")
        for a in alumni:
            dates = f"{a.get('date_start') or '?'} – {a.get('date_end') or 'present'}"
            print(f"  {a['person_name']:<30}  {a['role']:<25}  {dates}")

    elif args.command == "cohort":
        pillar, members = get_cohort(args.pillar, args.start, args.end)
        if not pillar:
            print(f"Pillar '{args.pillar}' not found")
            sys.exit(1)
        if write_output({"pillar": pillar, "cohort": members}, args, summary=f"cohort at {pillar['name']}"):
            return
        print(f"\nCohort: {pillar['name']} ({args.start} – {args.end}) — {len(members)} members")
        print(f"{'─' * 70}")
        for m in members:
            dates = f"{m.get('date_start') or '?'} – {m.get('date_end') or 'present'}"
            print(f"  {m['person_name']:<30}  {m['role']:<25}  {dates}")

    elif args.command == "dispersal":
        pillar, destinations = get_dispersal(args.pillar)
        if not pillar:
            print(f"Pillar '{args.pillar}' not found")
            sys.exit(1)
        if write_output({"pillar": pillar, "destinations": destinations}, args,
                        summary=f"dispersal from {pillar['name']}"):
            return
        print(f"\nDispersal: {pillar['name']} → where alumni went")
        print(f"{'═' * 70}")
        if not destinations:
            print("  No dispersal data (need more career arcs)")
        for dest, people in sorted(destinations.items(), key=lambda x: -len(x[1])):
            print(f"\n  → {dest} ({len(people)} alumni)")
            for p in people:
                print(f"    {p['person']:<30}  as {p['role']}")

    elif args.command == "overlap":
        name_a, name_b, overlaps = get_overlap(args.person_a, args.person_b)
        if write_output({"person_a": name_a, "person_b": name_b, "overlaps": overlaps}, args,
                        summary=f"overlap {name_a} / {name_b}"):
            return
        print(f"\nInstitutional Overlap: {name_a} × {name_b}")
        print(f"{'═' * 70}")
        if not overlaps:
            print("  No shared institutional tenures found")
        for o in overlaps:
            period = ""
            if o.get("overlap_start") or o.get("overlap_end"):
                period = f" ({o.get('overlap_start', '?')} – {o.get('overlap_end', '?')})"
            print(f"  {o['institution']:<35}  [{o['pillar_type']}]")
            print(f"    {name_a}: {o['person_a_role']}")
            print(f"    {name_b}: {o['person_b_role']}{period}")

    elif args.command == "timeline":
        canonical, events = get_person_timeline(args.person)
        if write_output({"person": canonical, "events": events}, args, summary=f"timeline of {canonical}"):
            return
        print(f"\nTimeline: {canonical}")
        print(f"{'═' * 70}")
        if not events:
            print("  No timeline events")
        for e in events:
            kind_marker = {
                "arc_start": "→",
                "arc_end": "←",
                "pillar_event": "!",
                "external": "⊕",
            }.get(e["event_kind"], "·")
            print(f"  {e.get('event_date', '?'):<12}  {kind_marker}  {e['description']}")

    elif args.command == "score":
        run_id = None
        if args.cache:
            db = get_pillar_db()
            cursor = db.execute("INSERT INTO pillar_scores (person_id, person_name, score_type, score_value) VALUES (0, '_run_marker', 'orchestrator', 0)")
            run_id = cursor.lastrowid
            db.execute("DELETE FROM pillar_scores WHERE id = ?", (run_id,))
            db.commit()
            db.close()

        results = compute_scores(person_name=args.person, top=args.top, run_id=run_id)
        if write_output(results, args, summary=f"orchestrator scores ({len(results)})"):
            return
        print(f"\nOrchestrator Scores (top {args.top})")
        print(f"{'Rank':>4}  {'Person':<35}  {'Score':>7}  {'Pillars':>7}  {'RevDoor':>7}  {'Arcs':>4}")
        print(f"{'─' * 80}")
        for i, r in enumerate(results):
            d = r["detail"]
            print(f"{i+1:>4}  {r['person_name']:<35}  {r['orchestrator_score']:>7.1f}"
                  f"  {len(d['pillar_types']):>7}  {d['revolving_door']:>7}  {d['arc_count']:>4}")

    elif args.command == "gaps":
        canonical, present, missing = get_pillar_gaps(args.person)
        print(f"\nPillar Gaps: {canonical}")
        print(f"  Present: {', '.join(present) if present else 'none'}")
        print(f"  Missing: {', '.join(missing) if missing else 'none (full coverage!)'}")

    elif args.command == "cross-pillar":
        results = get_cross_pillar(min_pillars=args.min_pillars)
        if write_output(results, args, summary=f"cross-pillar ({len(results)})"):
            return
        print(f"\nCross-Pillar Actors (min {args.min_pillars} types)")
        print(f"{'─' * 70}")
        if not results:
            print(f"  No persons span {args.min_pillars}+ pillar types")
        for r in results:
            print(f"  {r['canonical_name']:<35}  {r['pillar_count']} types  ({r['pillar_types']})")

    elif args.command == "pillar-network":
        rows = get_pillar_network(args.pillar_type)
        if write_output(rows, args, summary=f"pillar network ({args.pillar_type})"):
            return
        print(f"\nPillar Network: {args.pillar_type}")
        print(f"{'─' * 70}")
        current_pillar = None
        for r in rows:
            if r["pillar_name"] != current_pillar:
                current_pillar = r["pillar_name"]
                print(f"\n  {current_pillar}")
            dates = f" ({r.get('date_start') or '?'} – {r.get('date_end') or 'present'})"
            print(f"    {r['person_name']:<30}  {r['role']}{dates}")

    elif args.command == "stats":
        s = get_stats()
        print("Pillar System Statistics")
        print("=" * 40)
        print(f"  Institutions:   {s['pillars']}")
        print(f"  Persons:        {s['persons']}")
        print(f"  Career arcs:    {s['career_arcs']}")
        print(f"  Pillar events:  {s['pillar_events']}")
        print(f"  Pillar scores:  {s['pillar_scores']}")
        print(f"\n  By type:")
        for t, c in s["by_type"].items():
            print(f"    {t:<20} {c}")
        if s["top_institutions"]:
            print(f"\n  Top institutions by arcs:")
            for inst in s["top_institutions"]:
                print(f"    {inst['name']:<40} {inst['arcs']} arcs")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
