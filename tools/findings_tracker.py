#!/usr/bin/env python3
"""
Findings and connections tracker for OSINT investigations.

Part of investigation.db (shared with lead_tracker.py).

Usage:
    python tools/findings_tracker.py add --target "Rod-Larsen" --type financial --summary "..." --detail "..."
    python tools/findings_tracker.py list [--target "Rod-Larsen"] [--type financial]
    python tools/findings_tracker.py show 42
    python tools/findings_tracker.py connect --person-a "Epstein" --person-b "Rod-Larsen" --type financial
    python tools/findings_tracker.py connections "Epstein" [--depth 2]
    python tools/findings_tracker.py search "gates foundation"
    python tools/findings_tracker.py timeline [--target "Rod-Larsen"] [--start 2016-01-01] [--end 2019-12-31]
    python tools/findings_tracker.py stats
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import investigation_db_path
try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# Shared database with lead_tracker.py
DB_PATH = investigation_db_path()


def _detect_active_profile():
    """Detect active profile with fallback to direct DB read."""
    try:
        from tools.investigation_context import get_active_profile_id
        pid = get_active_profile_id()
        if pid:
            return pid
    except Exception:
        pass
    # Fallback: read directly from DB (works even if import fails in subagents)
    try:
        _db = sqlite3.connect(str(DB_PATH))
        row = _db.execute(
            "SELECT value FROM investigation_config WHERE key='active_profile'"
        ).fetchone()
        _db.close()
        if row:
            return row[0] or None
    except Exception:
        pass
    return None

VALID_FINDING_TYPES = [
    "communication", "financial", "relationship", "identity",
    "location", "document", "legal", "intelligence",
    "negative_result", "background",
]
VALID_CONFIDENCE = ["confirmed", "high", "medium", "low", "unverified"]
VALID_RELATIONSHIP_TYPES = [
    "financial", "social", "legal", "intelligence", "employment",
    "familial", "corporate", "advisory", "political",
    # Entity-to-entity relationship types
    "owns", "controls", "funds", "subsidiary_of", "contracts_with",
    "successor_to", "shares_officer", "supplies",
]
VALID_STRENGTHS = ["strong", "medium", "weak", "circumstantial"]


_schema_initialized = False


def get_db():
    """Get database connection. Schema is created by lead_tracker._ensure_schema()."""
    global _schema_initialized
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    if not _schema_initialized:
        from tools.lead_tracker import _ensure_schema
        _ensure_schema(db)
        _schema_initialized = True

    return db


def _get_db_standalone():
    """Standalone DB init (when run directly, not as import)."""
    global _schema_initialized
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    if not _schema_initialized:
        import importlib.util
        spec = importlib.util.spec_from_file_location("lead_tracker", Path(__file__).parent / "lead_tracker.py")
        lt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lt)
        lt._ensure_schema(db)
        _schema_initialized = True

    return db


# ── Findings CRUD ────────────────────────────────────────────


VALID_SOURCES = [
    "web_search", "doj_vol11", "duggan", "lmsband", "unified_db",
    "fec", "edgar", "courtlistener", "990", "registry",
    "usaspending", "sam_gov", "lobbying", "fara", "littlesis",
    "gdelt", "aleph", "icij", "acris", "gleif", "opensanctions",
    "shodan", "crtsh", "wayback", "urlscan", "medicaid",
    "analysis_run", "offshorealert", "uk_companies_house",
    "ca_sos", "tx_comptroller", "mi_lara", "nj_rev", "ma_corps",
    "ny_dos", "nv_sos", "fl_sunbiz", "nm_sos", "dc_dlcp",
    "usvi", "ds10_financial", "ucc", "faa", "sam_bulk",
    "highergov", "documentcloud", "muckrock", "fincen",
    "opencorporates", "zefix", "hudoc", "france_sirene",
    "panama_rp", "investigations_db", "fdic",
    "propublica_disclosures", "propublica_congress", "ppp",
    "govinfo", "congress_gov", "sec_enforcement", "bisbase",
]
VALID_CLAIM_TYPES = ["direct_quote", "paraphrase", "inference", "synthesis", "user_provided"]
VALID_VERIFICATION = ["unverified", "verified", "disputed", "retracted"]

# Confidence caps by claim type — enforced at write time
CONFIDENCE_CAPS = {
    "direct_quote": "confirmed",    # verbatim from primary source
    "paraphrase": "high",           # agent summary of source
    "inference": "medium",          # agent conclusion from evidence
    "synthesis": "medium",          # combined multiple sources
    "user_provided": "confirmed",   # human-supplied
}
_CONFIDENCE_ORDER = ["unverified", "low", "medium", "high", "confirmed"]


def _enforce_confidence_cap(claim_type, confidence):
    """Clamp confidence to the maximum allowed for this claim type.

    Returns (clamped_confidence, was_clamped).
    """
    cap = CONFIDENCE_CAPS.get(claim_type)
    if not cap:
        return confidence, False
    cap_idx = _CONFIDENCE_ORDER.index(cap)
    conf_idx = _CONFIDENCE_ORDER.index(confidence) if confidence in _CONFIDENCE_ORDER else 2
    if conf_idx > cap_idx:
        return cap, True
    return confidence, False
VALID_CORRECTION_TYPES = [
    "factual_error", "source_mismatch", "hallucination",
    "outdated", "refinement", "merge", "retraction",
]

# Fields that can be corrected via update_finding() — whitelist to prevent SQL injection
ALLOWED_CORRECT_FIELDS = {
    "summary", "detail", "target_name", "date_of_event",
    "confidence", "finding_type", "claim_type", "thread_id",
    "source_datasets", "profile_id",
}


def add_finding(target_name, summary, finding_type=None, detail=None,
                evidence_ids=None, source_datasets=None, confidence="medium",
                date_of_event=None, lead_id=None, claim_type="inference",
                source_quotes=None, thread_id=None, email_sender=None,
                profile_id=None):
    """Add a new finding with evidence references and provenance.

    Args:
        source_quotes: dict mapping evidence_ref -> {quote, page, assessment}
                       e.g. {"EFTA02336502": {"quote": "craft purchase 18M", "page": "p3"}}
        thread_id: Investigation thread ID to assign this finding to.
        email_sender: Email sender name to store on EFTA evidence rows.
        profile_id: Investigation profile. Auto-detected from active profile if None.
    Returns: finding ID.
    """
    if not source_datasets:
        raise ValueError(
            "source_datasets is required. Provide the data source(s) that produced this finding "
            "(e.g., ['web_search'], ['fec'], ['edgar', 'registry'])."
        )

    # Enforce confidence caps by claim type
    confidence, was_clamped = _enforce_confidence_cap(claim_type, confidence)
    if was_clamped:
        print(f"WARNING: Confidence clamped to '{confidence}' (max for claim_type='{claim_type}'). "
              f"See CONFIDENCE_CAPS in findings_tracker.py.", file=sys.stderr)

    # Warn on unknown source names (don't block — allows new sources without code changes)
    if source_datasets:
        for src in source_datasets:
            if src not in VALID_SOURCES:
                print(f"WARNING: Unknown source '{src}'. Known sources: {', '.join(sorted(VALID_SOURCES)[:10])}... "
                      f"(If this is a new source, consider adding it to VALID_SOURCES in findings_tracker.py)",
                      file=sys.stderr)

    # Auto-detect profile_id from active investigation if not provided
    if profile_id is None:
        profile_id = _detect_active_profile()

    # Resolve aliases to prevent future duplicates
    try:
        from tools.name_resolver import resolve_canonical
        target_name = resolve_canonical(target_name)
    except Exception:
        pass

    db = _get_db_standalone()
    sources_json = json.dumps(source_datasets) if source_datasets else None

    cursor = db.execute("""
        INSERT INTO findings (target_name, finding_type, summary, detail,
                             source_datasets, confidence, date_of_event, lead_id,
                             claim_type, verification_status, thread_id,
                             quality_state, confidence_requested, profile_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified', ?, 'unchecked', ?, ?)
    """, (target_name, finding_type, summary, detail,
          sources_json, confidence, date_of_event, lead_id, claim_type, thread_id, confidence,
          profile_id))
    finding_id = cursor.lastrowid

    if evidence_ids:
        for ev in evidence_ids:
            ev_type = "efta" if ev.startswith("EFTA") else "file" if "/" in ev else "url" if "://" in ev else "ref"
            sq = source_quotes.get(ev, {}) if source_quotes else {}
            db.execute("""
                INSERT OR IGNORE INTO finding_evidence
                    (finding_id, evidence_type, evidence_ref, source_quote,
                     source_page, assessment, email_sender)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (finding_id, ev_type, ev,
                  sq.get("quote"), sq.get("page"), sq.get("assessment"),
                  email_sender if ev_type == "efta" else None))

    db.commit()
    db.close()
    return finding_id


def _resolve_profile(profile_id=None, all_profiles=False):
    """Resolve profile_id: explicit > active profile > None. Returns None if all_profiles."""
    if all_profiles:
        return None
    if profile_id is not None:
        return profile_id
    try:
        from tools.investigation_context import get_active_profile_id
        return get_active_profile_id() or None
    except ImportError:
        try:
            from investigation_context import get_active_profile_id
            return get_active_profile_id() or None
        except ImportError:
            return None


def list_findings(target=None, finding_type=None, confidence=None, limit=50,
                  thread_id=None, profile_id=None, all_profiles=False):
    """List findings with optional filters."""
    db = _get_db_standalone()
    conditions = []
    params = []

    resolved_profile = _resolve_profile(profile_id, all_profiles)
    if resolved_profile:
        conditions.append("profile_id = ?")
        params.append(resolved_profile)

    if target:
        conditions.append("target_name LIKE ?")
        params.append(f"%{target}%")
    if finding_type:
        conditions.append("finding_type = ?")
        params.append(finding_type)
    if confidence:
        conditions.append("confidence = ?")
        params.append(confidence)
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT * FROM findings {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_finding(finding_id):
    """Get a single finding with evidence and connections."""
    db = _get_db_standalone()
    finding = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if not finding:
        db.close()
        return None

    result = dict(finding)
    result["evidence"] = [dict(e) for e in db.execute(
        "SELECT * FROM finding_evidence WHERE finding_id = ?", (finding_id,)
    ).fetchall()]
    result["connections"] = [dict(c) for c in db.execute(
        "SELECT * FROM connections WHERE finding_id = ?", (finding_id,)
    ).fetchall()]
    db.close()
    return result


def update_finding(finding_id, field, new_value, reason, correction_type="refinement",
                   corrected_by=None):
    """Update a finding field with correction audit trail. Returns True on success."""
    if field not in ALLOWED_CORRECT_FIELDS:
        raise ValueError(
            f"Cannot correct field '{field}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_CORRECT_FIELDS))}"
        )

    db = _get_db_standalone()
    finding = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if not finding:
        db.close()
        return False

    old_value = finding[field] if field in finding.keys() else None

    # Record the correction
    db.execute("""
        INSERT INTO corrections (table_name, record_id, field_name, old_value, new_value,
                                reason, corrected_by, correction_type)
        VALUES ('findings', ?, ?, ?, ?, ?, ?, ?)
    """, (finding_id, field, str(old_value) if old_value is not None else None,
          str(new_value), reason, corrected_by, correction_type))

    # Apply the update
    db.execute(f"UPDATE findings SET {field} = ? WHERE id = ?", (new_value, finding_id))
    db.commit()
    db.close()
    return True


def verify_finding(finding_id, verified_by="human"):
    """Mark a finding as verified by a human or agent."""
    db = _get_db_standalone()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        UPDATE findings SET verification_status = 'verified', verified_by = ?, verified_at = ?
        WHERE id = ?
    """, (verified_by, now, finding_id))
    db.commit()
    db.close()


def dispute_finding(finding_id, reason, corrected_by=None):
    """Mark a finding as disputed with reason recorded in corrections."""
    db = _get_db_standalone()
    now = datetime.now(timezone.utc).isoformat()

    # Get current status for audit trail
    finding = db.execute("SELECT verification_status FROM findings WHERE id = ?", (finding_id,)).fetchone()
    old_status = finding["verification_status"] if finding else "unknown"

    db.execute("""
        INSERT INTO corrections (table_name, record_id, field_name, old_value, new_value,
                                reason, corrected_by, correction_type)
        VALUES ('findings', ?, 'verification_status', ?, 'disputed', ?, ?, 'factual_error')
    """, (finding_id, old_status, reason, corrected_by))

    db.execute("""
        UPDATE findings SET verification_status = 'disputed', verified_by = ?, verified_at = ?
        WHERE id = ?
    """, (corrected_by, now, finding_id))
    db.commit()
    db.close()


def retract_finding(finding_id, reason, corrected_by=None):
    """Retract a finding entirely. Flags downstream leads for review."""
    db = _get_db_standalone()
    now = datetime.now(timezone.utc).isoformat()

    finding = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if not finding:
        db.close()
        return False

    # Record the retraction
    db.execute("""
        INSERT INTO corrections (table_name, record_id, field_name, old_value, new_value,
                                reason, corrected_by, correction_type)
        VALUES ('findings', ?, 'verification_status', ?, 'retracted', ?, ?, 'retraction')
    """, (finding_id, finding["verification_status"], reason, corrected_by))

    db.execute("""
        UPDATE findings SET verification_status = 'retracted', verified_by = ?, verified_at = ?
        WHERE id = ?
    """, (corrected_by, now, finding_id))

    # Flag the originating lead if it exists
    if finding["lead_id"]:
        db.execute("""
            INSERT INTO lead_notes (lead_id, note)
            VALUES (?, ?)
        """, (finding["lead_id"],
              f"WARNING: Finding #{finding_id} was retracted. Reason: {reason}"))

    # Flag any connections that cite this finding
    connections = db.execute(
        "SELECT id FROM connections WHERE finding_id = ?", (finding_id,)
    ).fetchall()
    for conn in connections:
        db.execute("""
            INSERT INTO corrections (table_name, record_id, field_name, old_value, new_value,
                                    reason, corrected_by, correction_type)
            VALUES ('connections', ?, 'verification_status', 'unverified', 'disputed',
                    ?, ?, 'retraction')
        """, (conn["id"],
              f"Source finding #{finding_id} was retracted: {reason}",
              corrected_by))
        db.execute(
            "UPDATE connections SET verification_status = 'disputed' WHERE id = ?",
            (conn["id"],)
        )

    db.commit()
    db.close()
    return True


def get_corrections(table_name=None, record_id=None, correction_type=None, limit=50):
    """Get correction audit trail with optional filters."""
    db = _get_db_standalone()
    conditions = []
    params = []

    if table_name:
        conditions.append("table_name = ?")
        params.append(table_name)
    if record_id is not None:
        conditions.append("record_id = ?")
        params.append(record_id)
    if correction_type:
        conditions.append("correction_type = ?")
        params.append(correction_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT * FROM corrections {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_unverified(limit=50):
    """Get findings that haven't been human-verified yet."""
    db = _get_db_standalone()
    rows = db.execute("""
        SELECT f.*, GROUP_CONCAT(fe.evidence_ref, ', ') as evidence_refs
        FROM findings f
        LEFT JOIN finding_evidence fe ON fe.finding_id = f.id
        WHERE f.verification_status = 'unverified'
        GROUP BY f.id
        ORDER BY
            CASE f.confidence
                WHEN 'confirmed' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 WHEN 'low' THEN 3
                WHEN 'unverified' THEN 4
            END,
            f.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_provenance(finding_id):
    """Get full provenance chain for a finding: evidence with source quotes + corrections."""
    db = _get_db_standalone()
    finding = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if not finding:
        db.close()
        return None

    result = dict(finding)
    result["evidence"] = [dict(e) for e in db.execute(
        "SELECT * FROM finding_evidence WHERE finding_id = ?", (finding_id,)
    ).fetchall()]
    result["corrections"] = [dict(c) for c in db.execute(
        "SELECT * FROM corrections WHERE table_name = 'findings' AND record_id = ? ORDER BY created_at",
        (finding_id,)
    ).fetchall()]
    result["connections"] = [dict(c) for c in db.execute(
        "SELECT * FROM connections WHERE finding_id = ?", (finding_id,)
    ).fetchall()]

    # Check source reliability for each evidence source
    for ev in result["evidence"]:
        # Try to match evidence to a known source
        rel = db.execute(
            "SELECT * FROM source_reliability WHERE ? LIKE '%' || source_name || '%'",
            (ev.get("evidence_ref", ""),)
        ).fetchone()
        ev["source_reliability"] = dict(rel) if rel else None

    db.close()
    return result


def search_findings(query, thread_id=None, profile_id=None, all_profiles=False):
    """Full-text search across findings. Wraps terms in quotes for safety."""
    db = _get_db_standalone()
    safe_query = '"' + query.replace('"', '""') + '"'
    resolved_profile = _resolve_profile(profile_id, all_profiles)

    conditions = ["findings_fts MATCH ?"]
    params = [safe_query]
    if thread_id:
        conditions.append("findings.thread_id = ?")
        params.append(thread_id)
    if resolved_profile:
        conditions.append("findings.profile_id = ?")
        params.append(resolved_profile)

    where = " AND ".join(conditions)
    rows = db.execute(f"""
        SELECT findings.*, findings_fts.rank
        FROM findings_fts
        JOIN findings ON findings.id = findings_fts.rowid
        WHERE {where}
        ORDER BY findings_fts.rank
        LIMIT 30
    """, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── Connections CRUD ─────────────────────────────────────────


def add_connection(person_a, person_b, relationship_type=None, description=None,
                   evidence_ids=None, strength="medium", date_range=None, finding_id=None,
                   profile_id=None):
    """Add a connection between two persons/entities."""
    # Auto-detect profile_id from active investigation if not provided
    if profile_id is None:
        profile_id = _detect_active_profile()

    # Resolve aliases to prevent future duplicates
    try:
        from tools.name_resolver import resolve_canonical
        person_a = resolve_canonical(person_a)
        person_b = resolve_canonical(person_b)
    except Exception:
        pass

    db = _get_db_standalone()

    # Normalize: store alphabetically for dedup
    if person_a > person_b:
        person_a, person_b = person_b, person_a

    cursor = db.execute("""
        INSERT OR IGNORE INTO connections (person_a, person_b, relationship_type, description,
                                strength, date_range, finding_id, profile_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (person_a, person_b, relationship_type, description, strength, date_range, finding_id,
          profile_id))
    conn_id = cursor.lastrowid
    if conn_id == 0:
        # Duplicate — find the existing connection id
        existing = db.execute("""
            SELECT id FROM connections
            WHERE person_a = ? AND person_b = ?
              AND COALESCE(relationship_type, '') = COALESCE(?, '')
              AND COALESCE(profile_id, '') = COALESCE(?, '')
        """, (person_a, person_b, relationship_type, profile_id)).fetchone()
        if existing:
            conn_id = existing["id"]

    if evidence_ids:
        for ev in evidence_ids:
            ev_type = "efta" if ev.startswith("EFTA") else "file" if "/" in ev else "url" if "://" in ev else "ref"
            db.execute(
                "INSERT OR IGNORE INTO connection_evidence (connection_id, evidence_type, evidence_ref) VALUES (?, ?, ?)",
                (conn_id, ev_type, ev)
            )

    db.commit()
    db.close()
    return conn_id


def get_connections(person, depth=1, relationship_type=None, profile_id=None,
                    all_profiles=False):
    """Get all connections for a person, optionally multi-hop."""
    db = _get_db_standalone()
    resolved_profile = _resolve_profile(profile_id, all_profiles)

    visited = set()
    current_layer = {person.lower()}
    all_connections = []

    for _ in range(depth):
        if not current_layer:
            break

        placeholders = ",".join("?" for _ in current_layer)
        conditions = [f"(LOWER(person_a) IN ({placeholders}) OR LOWER(person_b) IN ({placeholders}))"]
        params = list(current_layer) + list(current_layer)

        if relationship_type:
            conditions.append("relationship_type = ?")
            params.append(relationship_type)
        if resolved_profile:
            conditions.append("profile_id = ?")
            params.append(resolved_profile)

        where = " AND ".join(conditions)
        rows = db.execute(f"SELECT * FROM connections WHERE {where}", params).fetchall()

        next_layer = set()
        for row in rows:
            conn = dict(row)
            conn_key = (conn["person_a"], conn["person_b"], conn.get("relationship_type"))
            if conn_key not in visited:
                visited.add(conn_key)
                all_connections.append(conn)
                next_layer.add(conn["person_a"].lower())
                next_layer.add(conn["person_b"].lower())

        current_layer = next_layer - {n for n in current_layer}

    db.close()
    return all_connections


def get_timeline(target=None, start_date=None, end_date=None, limit=100,
                 profile_id=None, all_profiles=False):
    """Get findings ordered by event date."""
    db = _get_db_standalone()
    conditions = ["date_of_event IS NOT NULL AND date_of_event != ''"]
    params = []

    resolved_profile = _resolve_profile(profile_id, all_profiles)
    if resolved_profile:
        conditions.append("profile_id = ?")
        params.append(resolved_profile)

    if target:
        conditions.append("target_name LIKE ?")
        params.append(f"%{target}%")
    if start_date:
        conditions.append("date_of_event >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date_of_event <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(conditions)}"
    rows = db.execute(
        f"SELECT * FROM findings {where} ORDER BY date_of_event LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_stats(profile_id=None, all_profiles=False):
    """Findings and connections statistics."""
    db = _get_db_standalone()
    resolved_profile = _resolve_profile(profile_id, all_profiles)
    stats = {}

    f_where = "WHERE profile_id = ?" if resolved_profile else ""
    c_where = "WHERE profile_id = ?" if resolved_profile else ""
    f_params = [resolved_profile] if resolved_profile else []
    c_params = [resolved_profile] if resolved_profile else []

    stats["total_findings"] = db.execute(f"SELECT COUNT(*) FROM findings {f_where}", f_params).fetchone()[0]
    stats["total_connections"] = db.execute(f"SELECT COUNT(*) FROM connections {c_where}", c_params).fetchone()[0]
    if resolved_profile:
        stats["profile_id"] = resolved_profile

    rows = db.execute(
        f"SELECT finding_type, COUNT(*) as cnt FROM findings {f_where} GROUP BY finding_type", f_params
    ).fetchall()
    stats["by_type"] = {r["finding_type"]: r["cnt"] for r in rows}

    rows = db.execute(
        f"SELECT confidence, COUNT(*) as cnt FROM findings {f_where} GROUP BY confidence", f_params
    ).fetchall()
    stats["by_confidence"] = {r["confidence"]: r["cnt"] for r in rows}

    rows = db.execute(
        f"SELECT target_name, COUNT(*) as cnt FROM findings {f_where} GROUP BY target_name ORDER BY cnt DESC LIMIT 20",
        f_params
    ).fetchall()
    stats["top_targets"] = {r["target_name"]: r["cnt"] for r in rows}

    rows = db.execute(
        f"SELECT relationship_type, COUNT(*) as cnt FROM connections {c_where} GROUP BY relationship_type",
        c_params
    ).fetchall()
    stats["connection_types"] = {r["relationship_type"]: r["cnt"] for r in rows}

    db.close()
    return stats


# ── CLI ──────────────────────────────────────────────────────


def format_finding(finding, verbose=False):
    conf_markers = {"confirmed": "[+++]", "high": "[++ ]", "medium": "[+  ]", "low": "[   ]", "unverified": "[?  ]"}
    verif_markers = {"verified": "V", "unverified": "?", "disputed": "D", "retracted": "X"}
    conf = conf_markers.get(finding["confidence"], "[?  ]")
    verif = verif_markers.get(finding.get("verification_status", "unverified"), "?")
    ftype = finding.get("finding_type") or "?"
    date = finding.get("date_of_event", "")
    date_str = f" ({date})" if date else ""
    claim = finding.get("claim_type", "?")

    line = f"{conf}{verif} #{finding['id']:>4} [{ftype:>13}] {finding['target_name']}: {finding['summary']}{date_str}"

    if verbose:
        if finding.get("claim_type"):
            line += f"\n       Claim type: {finding['claim_type']}"
        if finding.get("verification_status"):
            line += f"\n       Verification: {finding['verification_status']}"
            if finding.get("verified_by"):
                line += f" (by {finding['verified_by']} at {finding.get('verified_at', '?')})"
        if finding.get("detail"):
            line += f"\n       Detail: {finding['detail'][:300]}"
        if finding.get("evidence"):
            for ev in finding["evidence"]:
                line += f"\n       Evidence [{ev['evidence_type']}]: {ev['evidence_ref']}"
                if ev.get("source_quote"):
                    line += f"\n         Quote: \"{ev['source_quote'][:200]}\""
                if ev.get("source_page"):
                    line += f" (at {ev['source_page']})"
                if ev.get("assessment"):
                    line += f"\n         Assessment: {ev['assessment']}"
        if finding.get("source_datasets"):
            line += f"\n       Sources: {finding['source_datasets']}"
        if finding.get("lead_id"):
            line += f"\n       From lead: #{finding['lead_id']}"
        if finding.get("corrections"):
            line += f"\n       Corrections: {len(finding['corrections'])} recorded"
            for c in finding["corrections"]:
                line += f"\n         [{c['created_at']}] {c['correction_type']}: {c['field_name']} — {c['reason']}"

    return line


def format_connection(conn):
    strength_markers = {"strong": "===", "medium": "---", "weak": "- -", "circumstantial": "..."}
    marker = strength_markers.get(conn["strength"], "---")
    rtype = conn.get("relationship_type", "?")
    return f"  {conn['person_a']} {marker}[{rtype}]{marker} {conn['person_b']}"


def main():
    parser = argparse.ArgumentParser(description="OSINT investigation findings tracker")
    subparsers = parser.add_subparsers(dest="command")

    # add
    add_p = subparsers.add_parser("add", help="Add a finding")
    add_p.add_argument("--target", required=True)
    add_p.add_argument("--summary", "-s", required=True)
    add_p.add_argument("--type", "-t", choices=VALID_FINDING_TYPES, dest="finding_type")
    add_p.add_argument("--detail", "-d")
    add_p.add_argument("--evidence", "-e", nargs="+")
    add_p.add_argument("--sources", nargs="+")
    add_p.add_argument("--confidence", "-c", choices=VALID_CONFIDENCE, default="medium")
    add_p.add_argument("--date")
    add_p.add_argument("--lead-id", type=int)
    add_p.add_argument("--claim-type", choices=VALID_CLAIM_TYPES, default="inference")
    add_p.add_argument("--source-quote", nargs="+",
                       help="ref:quote pairs, e.g. 'EFTA02336502:craft purchase 18M'")
    add_p.add_argument("--thread-id", type=int, help="Investigation thread ID")
    add_p.add_argument("--email-sender", help="Email sender for EFTA evidence (e.g. 'Jeffrey Epstein')")
    add_p.add_argument("--profile", help="Investigation profile ID (auto-detected if omitted)")

    # list
    list_p = subparsers.add_parser("list", help="List findings")
    list_p.add_argument("--target")
    list_p.add_argument("--type", choices=VALID_FINDING_TYPES, dest="finding_type")
    list_p.add_argument("--confidence", choices=VALID_CONFIDENCE)
    list_p.add_argument("--thread-id", type=int, help="Filter by investigation thread")
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("-v", "--verbose", action="store_true")
    list_p.add_argument("--profile", help="Investigation profile (default: active)")
    list_p.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    add_output_args(list_p)

    # show
    show_p = subparsers.add_parser("show", help="Show finding details")
    show_p.add_argument("id", type=int)
    add_output_args(show_p)

    # connect
    conn_p = subparsers.add_parser("connect", help="Add a connection between any two nodes (persons, orgs, programs)")
    conn_p.add_argument("--person-a", "--node-a", "-a", required=True)
    conn_p.add_argument("--person-b", "--node-b", "-b", required=True)
    conn_p.add_argument("--type", choices=VALID_RELATIONSHIP_TYPES, dest="rel_type")
    conn_p.add_argument("--description", "-d")
    conn_p.add_argument("--evidence", "-e", nargs="+")
    conn_p.add_argument("--strength", choices=VALID_STRENGTHS, default="medium")
    conn_p.add_argument("--date-range")
    conn_p.add_argument("--finding-id", type=int)
    conn_p.add_argument("--profile", help="Investigation profile ID (auto-detected if omitted)")

    # connections
    conns_p = subparsers.add_parser("connections", help="Get connections for a node (person, org, or program)")
    conns_p.add_argument("person", metavar="NODE")
    conns_p.add_argument("--depth", type=int, default=1)
    conns_p.add_argument("--type", choices=VALID_RELATIONSHIP_TYPES, dest="rel_type")
    conns_p.add_argument("--profile", help="Investigation profile (default: active)")
    conns_p.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    add_output_args(conns_p)

    # search
    search_p = subparsers.add_parser("search", help="Full-text search")
    search_p.add_argument("query")
    search_p.add_argument("--thread-id", type=int, help="Filter by investigation thread")
    search_p.add_argument("--profile", help="Investigation profile (default: active)")
    search_p.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    add_output_args(search_p)

    # timeline
    tl_p = subparsers.add_parser("timeline", help="Timeline of findings")
    tl_p.add_argument("--target")
    tl_p.add_argument("--start")
    tl_p.add_argument("--end")
    tl_p.add_argument("--limit", type=int, default=100)
    tl_p.add_argument("--profile", help="Investigation profile (default: active)")
    tl_p.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    add_output_args(tl_p)

    # verify
    verify_p = subparsers.add_parser("verify", help="Mark finding as verified")
    verify_p.add_argument("id", type=int)
    verify_p.add_argument("--by", default="human", help="Who verified (default: human)")

    # dispute
    dispute_p = subparsers.add_parser("dispute", help="Mark finding as disputed")
    dispute_p.add_argument("id", type=int)
    dispute_p.add_argument("--reason", "-r", required=True)
    dispute_p.add_argument("--by", default="human")

    # retract
    retract_p = subparsers.add_parser("retract", help="Retract a finding (cascades to connections)")
    retract_p.add_argument("id", type=int)
    retract_p.add_argument("--reason", "-r", required=True)
    retract_p.add_argument("--by", default="human")

    # correct
    correct_p = subparsers.add_parser("correct", help="Correct a field with audit trail")
    correct_p.add_argument("id", type=int)
    correct_p.add_argument("--field", "-f", required=True,
                           help="Field to correct (summary, detail, target_name, date_of_event, etc.)")
    correct_p.add_argument("--value", "-v", required=True, help="New value")
    correct_p.add_argument("--reason", "-r", required=True, help="Why the correction was needed")
    correct_p.add_argument("--correction-type", choices=VALID_CORRECTION_TYPES, default="refinement")
    correct_p.add_argument("--by", default="human")

    # audit
    audit_p = subparsers.add_parser("audit", help="Show correction history")
    audit_p.add_argument("id", type=int, nargs="?", help="Finding ID (omit for all)")
    audit_p.add_argument("--table", default="findings")
    audit_p.add_argument("--correction-type", choices=VALID_CORRECTION_TYPES)
    audit_p.add_argument("--limit", type=int, default=50)

    # provenance
    prov_p = subparsers.add_parser("provenance", help="Show full provenance chain for a finding")
    prov_p.add_argument("id", type=int)
    add_output_args(prov_p)

    # unverified
    unverified_p = subparsers.add_parser("unverified", help="List unverified findings")
    unverified_p.add_argument("--limit", type=int, default=50)
    add_output_args(unverified_p)

    # stats
    stats_p = subparsers.add_parser("stats", help="Show statistics")
    stats_p.add_argument("--profile", help="Investigation profile (default: active)")
    stats_p.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    add_output_args(stats_p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "add":
        if not args.sources:
            print("ERROR: --sources is required. Specify the data source(s) that produced this finding "
                  "(e.g., --sources web_search, --sources fec edgar).", file=sys.stderr)
            sys.exit(1)

        # Parse source quotes from CLI (format: "ref:quote text")
        source_quotes = None
        if getattr(args, "source_quote", None):
            source_quotes = {}
            for sq in args.source_quote:
                if ":" in sq:
                    ref, quote = sq.split(":", 1)
                    source_quotes[ref] = {"quote": quote}

        fid = add_finding(
            target_name=args.target, summary=args.summary, finding_type=args.finding_type,
            detail=args.detail, evidence_ids=args.evidence, source_datasets=args.sources,
            confidence=args.confidence, date_of_event=args.date, lead_id=args.lead_id,
            claim_type=getattr(args, "claim_type", "inference"),
            source_quotes=source_quotes,
            thread_id=getattr(args, "thread_id", None),
            email_sender=getattr(args, "email_sender", None),
            profile_id=getattr(args, "profile", None),
        )
        print(f"Created finding #{fid}: {args.target} - {args.summary}")

    elif args.command == "list":
        findings = list_findings(
            target=args.target, finding_type=args.finding_type,
            confidence=args.confidence, limit=args.limit,
            thread_id=getattr(args, "thread_id", None),
            profile_id=getattr(args, "profile", None),
            all_profiles=getattr(args, "all_profiles", False),
        )
        if not write_output(findings, args, summary=f"findings list: {len(findings)} results"):
            if not findings:
                print("No findings match filters.")
            else:
                for f in findings:
                    print(format_finding(f, verbose=args.verbose))

    elif args.command == "show":
        finding = get_finding(args.id)
        if not finding:
            print(f"Finding #{args.id} not found.")
            sys.exit(1)
        if not write_output(finding, args, summary=f"finding #{args.id}"):
            print(format_finding(finding, verbose=True))
            if finding.get("connections"):
                print("\n  Connections:")
                for c in finding["connections"]:
                    print(format_connection(c))

    elif args.command == "connect":
        cid = add_connection(
            person_a=args.person_a, person_b=args.person_b, relationship_type=args.rel_type,
            description=args.description, evidence_ids=args.evidence,
            strength=args.strength, date_range=args.date_range, finding_id=args.finding_id,
            profile_id=getattr(args, "profile", None),
        )
        print(f"Created connection #{cid}: {args.person_a} <-> {args.person_b}")

    elif args.command == "connections":
        conns = get_connections(args.person, depth=args.depth, relationship_type=args.rel_type,
                               profile_id=getattr(args, "profile", None),
                               all_profiles=getattr(args, "all_profiles", False))
        if not write_output(conns, args, summary=f"connections for '{args.person}': {len(conns)} results"):
            if not conns:
                print(f"No connections found for '{args.person}'")
            else:
                print(f"Connections for '{args.person}' (depth={args.depth}):")
                for c in conns:
                    print(format_connection(c))

    elif args.command == "search":
        results = search_findings(args.query, thread_id=getattr(args, "thread_id", None),
                                  profile_id=getattr(args, "profile", None),
                                  all_profiles=getattr(args, "all_profiles", False))
        if not write_output(results, args, summary=f"findings search '{args.query}': {len(results)} results"):
            if not results:
                print(f"No findings matching '{args.query}'")
            else:
                print(f"Found {len(results)} findings matching '{args.query}':")
                for f in results:
                    print(format_finding(f))

    elif args.command == "timeline":
        events = get_timeline(target=args.target, start_date=args.start, end_date=args.end, limit=args.limit,
                              profile_id=getattr(args, "profile", None),
                              all_profiles=getattr(args, "all_profiles", False))
        if not write_output(events, args, summary=f"timeline: {len(events)} events"):
            if not events:
                print("No dated findings found.")
            else:
                print(f"Timeline ({len(events)} events):")
                for f in events:
                    print(f"  {f['date_of_event']}  {f['target_name']}: {f['summary']}")

    elif args.command == "verify":
        verify_finding(args.id, verified_by=args.by)
        print(f"Verified finding #{args.id}")

    elif args.command == "dispute":
        dispute_finding(args.id, reason=args.reason, corrected_by=args.by)
        print(f"Disputed finding #{args.id}: {args.reason}")

    elif args.command == "retract":
        if retract_finding(args.id, reason=args.reason, corrected_by=args.by):
            print(f"Retracted finding #{args.id}: {args.reason}")
            print("  (downstream connections flagged as disputed)")
        else:
            print(f"Finding #{args.id} not found.")

    elif args.command == "correct":
        if args.field not in ALLOWED_CORRECT_FIELDS:
            print(f"ERROR: Cannot correct field '{args.field}'. "
                  f"Allowed: {', '.join(sorted(ALLOWED_CORRECT_FIELDS))}", file=sys.stderr)
            sys.exit(1)
        if update_finding(args.id, args.field, args.value, args.reason,
                         correction_type=args.correction_type, corrected_by=args.by):
            print(f"Corrected finding #{args.id}.{args.field}")
            print(f"  Reason: {args.reason}")
        else:
            print(f"Finding #{args.id} not found.")

    elif args.command == "audit":
        corrections = get_corrections(
            table_name=args.table,
            record_id=args.id,
            correction_type=getattr(args, "correction_type", None),
            limit=args.limit,
        )
        if not corrections:
            print("No corrections found.")
        else:
            print(f"Correction history ({len(corrections)} entries):")
            for c in corrections:
                print(f"  [{c['created_at']}] {c['table_name']}#{c['record_id']}.{c['field_name']}")
                print(f"    Type: {c['correction_type']}  By: {c.get('corrected_by', '?')}")
                print(f"    Old: {c['old_value']}")
                print(f"    New: {c['new_value']}")
                print(f"    Reason: {c['reason']}")
                print()

    elif args.command == "provenance":
        prov = get_provenance(args.id)
        if not prov:
            print(f"Finding #{args.id} not found.")
            sys.exit(1)
        if not write_output(prov, args, summary=f"provenance for finding #{args.id}"):
            print(f"=== Provenance for Finding #{args.id} ===")
            print(f"Target: {prov['target_name']}")
            print(f"Summary: {prov['summary']}")
            print(f"Claim type: {prov.get('claim_type', '?')}")
            print(f"Verification: {prov.get('verification_status', '?')}")
            print(f"Confidence: {prov['confidence']}")
            print()
            if prov["evidence"]:
                print(f"--- Evidence ({len(prov['evidence'])}) ---")
                for ev in prov["evidence"]:
                    print(f"  [{ev['evidence_type']}] {ev['evidence_ref']}")
                    if ev.get("source_quote"):
                        print(f"    Quote: \"{ev['source_quote']}\"")
                    if ev.get("email_sender"):
                        sender = ev["email_sender"]
                        recip = ""
                        date_str = f" ({ev['email_date']})" if ev.get("email_date") else ""
                        pos = ev.get("chain_position")
                        pos_str = f", chain position {pos}" if pos is not None else ""
                        print(f"    Email sender: {sender}{date_str}{pos_str}")
                    if ev.get("source_page"):
                        print(f"    Page/Loc: {ev['source_page']}")
                    if ev.get("assessment"):
                        print(f"    Assessment: {ev['assessment']}")
                    if ev.get("source_reliability"):
                        rel = ev["source_reliability"]
                        print(f"    Source reliability: {rel['source_type']} — {rel.get('reliability_notes', '')}")
            else:
                print("  WARNING: No evidence references attached!")
            if prov["corrections"]:
                print(f"\n--- Corrections ({len(prov['corrections'])}) ---")
                for c in prov["corrections"]:
                    print(f"  [{c['created_at']}] {c['correction_type']}: {c['field_name']}")
                    print(f"    {c['old_value']} -> {c['new_value']}")
                    print(f"    Reason: {c['reason']}")
            if prov["connections"]:
                print(f"\n--- Connections ({len(prov['connections'])}) ---")
                for conn in prov["connections"]:
                    vstat = conn.get("verification_status", "?")
                    print(f"  {conn['person_a']} <-> {conn['person_b']} [{conn.get('relationship_type', '?')}] (verif: {vstat})")

    elif args.command == "unverified":
        findings = get_unverified(limit=args.limit)
        if not write_output(findings, args, summary=f"unverified findings: {len(findings)}"):
            if not findings:
                print("All findings verified!")
            else:
                print(f"Unverified findings ({len(findings)}):")
                for f in findings:
                    refs = f.get("evidence_refs", "none")
                    print(f"  #{f['id']:>4} [{f.get('claim_type', '?'):>12}] {f['target_name']}: {f['summary']}")
                    print(f"         Evidence: {refs}")

    elif args.command == "stats":
        p_id = getattr(args, "profile", None)
        p_all = getattr(args, "all_profiles", False)
        stats = get_stats(profile_id=p_id, all_profiles=p_all)
        # Augment with audit stats
        resolved_profile = _resolve_profile(p_id, p_all)
        db = _get_db_standalone()
        profile_cond = " AND profile_id = ?" if resolved_profile else ""
        profile_params = [resolved_profile] if resolved_profile else []
        total_corrections = db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]
        hallucinations = db.execute(
            "SELECT COUNT(*) FROM corrections WHERE correction_type = 'hallucination'"
        ).fetchone()[0]
        retracted = db.execute(
            f"SELECT COUNT(*) FROM findings WHERE verification_status = 'retracted'{profile_cond}",
            profile_params
        ).fetchone()[0]
        verified = db.execute(
            f"SELECT COUNT(*) FROM findings WHERE verification_status = 'verified'{profile_cond}",
            profile_params
        ).fetchone()[0]
        unverified_ct = db.execute(
            f"SELECT COUNT(*) FROM findings WHERE verification_status = 'unverified'{profile_cond}",
            profile_params
        ).fetchone()[0]
        disputed = db.execute(
            f"SELECT COUNT(*) FROM findings WHERE verification_status = 'disputed'{profile_cond}",
            profile_params
        ).fetchone()[0]
        db.close()
        stats["audit"] = {
            "verified": verified, "unverified": unverified_ct,
            "disputed": disputed, "retracted": retracted,
            "total_corrections": total_corrections, "hallucinations": hallucinations,
        }
        if not write_output(stats, args, summary=f"findings stats: {stats['total_findings']} findings, {stats['total_connections']} connections"):
            print(f"Total findings: {stats['total_findings']}")
            print(f"Total connections: {stats['total_connections']}")
            if stats.get("by_type"):
                print(f"\nBy type:")
                for t, c in sorted(stats["by_type"].items(), key=lambda x: (x[0] is None, x[0] or '')):
                    print(f"  {t or '(none)'}: {c}")
            if stats.get("by_confidence"):
                print(f"\nBy confidence:")
                for conf, c in sorted(stats["by_confidence"].items(), key=lambda x: (x[0] is None, x[0] or '')):
                    print(f"  {conf or '(none)'}: {c}")
            if stats.get("top_targets"):
                print(f"\nTop targets:")
                for name, c in stats["top_targets"].items():
                    print(f"  {name}: {c}")
            print(f"\nAudit status:")
            print(f"  Verified: {verified}")
            print(f"  Unverified: {unverified_ct}")
            print(f"  Disputed: {disputed}")
            print(f"  Retracted: {retracted}")
            print(f"  Total corrections: {total_corrections}")
            if hallucinations:
                print(f"  Hallucinations caught: {hallucinations}")


if __name__ == "__main__":
    main()
