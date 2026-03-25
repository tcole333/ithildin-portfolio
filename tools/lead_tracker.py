#!/usr/bin/env python3
"""
Lead tracking for OSINT investigations.

Part of investigation.db (shared with findings_tracker.py).

Usage:
    python tools/lead_tracker.py add --title "..." --category person --priority high
    python tools/lead_tracker.py list [--status open] [--priority high] [--category person]
    python tools/lead_tracker.py show 42
    python tools/lead_tracker.py claim 42
    python tools/lead_tracker.py note 42 "Found 50 ProtonMail docs"
    python tools/lead_tracker.py complete 42 --findings "..."
    python tools/lead_tracker.py reopen 42
    python tools/lead_tracker.py search "rod-larsen financial"
    python tools/lead_tracker.py stats
    python tools/lead_tracker.py next [--category person]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import investigation_db_path
try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

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


VALID_CATEGORIES = ["person", "entity", "financial", "document", "digital", "connection", "legal", "intelligence", "filing", "contract", "case"]
VALID_PRIORITIES = ["critical", "high", "medium", "low"]
VALID_STATUSES = ["open", "pending_triage", "in_progress", "completed", "blocked", "dead_end"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_schema_initialized = False


def get_db():
    """Get a database connection, creating schema if needed."""
    global _schema_initialized
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    if not _schema_initialized:
        db = _ensure_schema(db)
        _schema_initialized = True
    return db


def _ensure_schema(db):
    """Create all investigation tables if they don't exist."""
    db.executescript("""
        -- ══════════════════════════════════════════════════════════
        -- SESSIONS: Audit trail for agent/human activity
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            skill_invoked TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            summary TEXT
        );

        -- ══════════════════════════════════════════════════════════
        -- LEADS: Investigation work items
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            category TEXT CHECK(category IN ('person','entity','financial','document','digital','connection','legal','intelligence')),
            priority TEXT DEFAULT 'medium' CHECK(priority IN ('critical','high','medium','low')),
            status TEXT DEFAULT 'open' CHECK(status IN ('open','pending_triage','in_progress','completed','blocked','dead_end')),
            source TEXT,
            target_name TEXT,
            findings TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lead_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            note TEXT NOT NULL,
            session_id INTEGER REFERENCES sessions(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Junction: lead <-> evidence references
        CREATE TABLE IF NOT EXISTS lead_evidence (
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            evidence_type TEXT NOT NULL,  -- 'efta', 'file', 'url', 'doc_id'
            evidence_ref TEXT NOT NULL,
            PRIMARY KEY (lead_id, evidence_ref)
        );

        -- Junction: lead <-> lead relationships
        CREATE TABLE IF NOT EXISTS lead_relations (
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            related_lead_id INTEGER NOT NULL REFERENCES leads(id),
            relation_type TEXT DEFAULT 'related',  -- 'spawned_from', 'related', 'supersedes', 'duplicate'
            PRIMARY KEY (lead_id, related_lead_id)
        );

        -- ══════════════════════════════════════════════════════════
        -- FINDINGS: Confirmed intelligence
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_name TEXT NOT NULL,
            finding_type TEXT,
            summary TEXT NOT NULL,
            detail TEXT,
            source_datasets TEXT,
            confidence TEXT DEFAULT 'medium' CHECK(confidence IN (
                'confirmed','high','medium','low','unverified'
            )),
            date_of_event TEXT,
            lead_id INTEGER REFERENCES leads(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Junction: finding <-> evidence references
        CREATE TABLE IF NOT EXISTS finding_evidence (
            finding_id INTEGER NOT NULL REFERENCES findings(id),
            evidence_type TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            PRIMARY KEY (finding_id, evidence_ref)
        );

        -- ══════════════════════════════════════════════════════════
        -- CONNECTIONS: Relationships between persons/entities
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_a TEXT NOT NULL,
            person_b TEXT NOT NULL,
            relationship_type TEXT CHECK(relationship_type IN (
                'financial','social','legal','intelligence','employment',
                'familial','corporate','advisory','political',
                'owns','controls','funds','subsidiary_of','contracts_with',
                'successor_to','shares_officer','supplies'
            )),
            description TEXT,
            strength TEXT DEFAULT 'medium' CHECK(strength IN (
                'strong','medium','weak','circumstantial'
            )),
            date_range TEXT,
            finding_id INTEGER REFERENCES findings(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Junction: connection <-> evidence references
        CREATE TABLE IF NOT EXISTS connection_evidence (
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            evidence_type TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            PRIMARY KEY (connection_id, evidence_ref)
        );

        -- ══════════════════════════════════════════════════════════
        -- SEARCH LOG: Prevents redundant queries across sessions
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            source TEXT NOT NULL,
            result_count INTEGER,
            session_id INTEGER REFERENCES sessions(id),
            searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(query_text, source)
        );

        -- Immutable search history — preserves audit trail across re-runs
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            source TEXT NOT NULL,
            result_count INTEGER,
            session_id INTEGER REFERENCES sessions(id),
            searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ══════════════════════════════════════════════════════════
        -- ENTITIES: Corporate/financial entity registry
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT CHECK(entity_type IN (
                'llc','inc','ltd','trust','foundation','nonprofit',
                'partnership','fund','association','government','unknown'
            )),
            jurisdiction TEXT,              -- e.g., 'USVI', 'Delaware', 'BVI', 'New York'
            ein TEXT,                       -- IRS EIN if known
            address TEXT,                   -- Primary registered address
            status TEXT DEFAULT 'active',   -- active, dissolved, unknown
            source TEXT,                    -- Where we learned about this entity
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, jurisdiction)
        );

        -- Junction: entity <-> person roles (officers, directors, trustees, etc.)
        CREATE TABLE IF NOT EXISTS entity_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            person_name TEXT NOT NULL,
            role TEXT NOT NULL,             -- officer, director, trustee, founder, president, secretary, treasurer, manager, sole_member, beneficiary
            date_start TEXT,
            date_end TEXT,
            source TEXT,
            UNIQUE(entity_id, person_name, role)
        );

        -- Junction: entity <-> addresses (multiple addresses per entity over time)
        CREATE TABLE IF NOT EXISTS entity_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            address TEXT NOT NULL,
            address_type TEXT DEFAULT 'registered',  -- registered, mailing, physical, agent
            date_observed TEXT,
            source TEXT,
            UNIQUE(entity_id, address, address_type)
        );

        -- Junction: entity <-> entity relationships (ownership, control, shared officers)
        CREATE TABLE IF NOT EXISTS entity_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_a_id INTEGER NOT NULL REFERENCES entities(id),
            entity_b_id INTEGER NOT NULL REFERENCES entities(id),
            relation_type TEXT NOT NULL,    -- owns, controls, funds, shares_officer, subsidiary_of, successor_to
            description TEXT,
            source TEXT,
            UNIQUE(entity_a_id, entity_b_id, relation_type)
        );

        -- ══════════════════════════════════════════════════════════
        -- HUMAN ACTIONS: Things agents can't do, need human to handle
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS human_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            action_type TEXT CHECK(action_type IN (
                'foia_request','paid_lookup','manual_verification',
                'account_access','physical_records','legal_filing',
                'interview','purchase','configuration','other'
            )),
            priority TEXT DEFAULT 'medium' CHECK(priority IN ('critical','high','medium','low')),
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending','in_progress','completed','blocked','cancelled')),
            related_lead_id INTEGER REFERENCES leads(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            notes TEXT
        );

        -- ══════════════════════════════════════════════════════════
        -- SOURCE RELIABILITY: Track trustworthiness of information sources
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS source_reliability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL UNIQUE,    -- e.g., 'NYT', 'KPMG', 'DOJ EFTA', 'Wikipedia'
            source_type TEXT CHECK(source_type IN (
                'primary_government',       -- DOJ releases, FBI files, court filings
                'primary_forensic',         -- KPMG review, forensic accountant reports
                'primary_corporate',        -- SEC filings, corporate registries, 990s
                'primary_correspondence',   -- Actual emails, letters from corpus
                'secondary_quality',        -- Reputable investigative journalism (with caveats)
                'secondary_compromised',    -- Media with known subject connections
                'secondary_blog',           -- Independent researchers, blogs (verify everything)
                'tertiary_wiki',            -- Wikipedia, social media (use only as starting point)
                'unknown'
            )),
            reliability_notes TEXT,         -- Known biases, conflicts, limitations
            epstein_connection TEXT,        -- DEPRECATED: use subject_connection instead
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ══════════════════════════════════════════════════════════
        -- CORRECTIONS: Immutable audit trail for all data changes
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,            -- 'findings', 'connections', 'entities', etc.
            record_id INTEGER NOT NULL,          -- ID in the referenced table
            field_name TEXT NOT NULL,            -- which field was changed
            old_value TEXT,
            new_value TEXT,
            reason TEXT NOT NULL,                -- why it was corrected
            corrected_by TEXT,                   -- 'human', agent session ID, or username
            correction_type TEXT CHECK(correction_type IN (
                'factual_error',      -- claim was wrong
                'source_mismatch',    -- attributed to wrong source
                'hallucination',      -- LLM fabricated this
                'outdated',           -- was true, now superseded by new info
                'refinement',         -- improved accuracy/precision
                'merge',              -- deduplicated with another record
                'retraction'          -- finding withdrawn entirely
            )),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ══════════════════════════════════════════════════════════
        -- INDEXES
        -- ══════════════════════════════════════════════════════════
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE INDEX IF NOT EXISTS idx_leads_priority ON leads(priority);
        CREATE INDEX IF NOT EXISTS idx_leads_category ON leads(category);
        CREATE INDEX IF NOT EXISTS idx_leads_target ON leads(target_name);
        CREATE INDEX IF NOT EXISTS idx_lead_notes_lead_id ON lead_notes(lead_id);
        CREATE INDEX IF NOT EXISTS idx_lead_evidence_ref ON lead_evidence(evidence_ref);
        CREATE INDEX IF NOT EXISTS idx_findings_target ON findings(target_name);
        CREATE INDEX IF NOT EXISTS idx_findings_type ON findings(finding_type);
        CREATE INDEX IF NOT EXISTS idx_findings_confidence ON findings(confidence);
        CREATE INDEX IF NOT EXISTS idx_finding_evidence_ref ON finding_evidence(evidence_ref);
        CREATE INDEX IF NOT EXISTS idx_connections_a ON connections(person_a);
        CREATE INDEX IF NOT EXISTS idx_connections_b ON connections(person_b);
        CREATE INDEX IF NOT EXISTS idx_connection_evidence_ref ON connection_evidence(evidence_ref);
        CREATE INDEX IF NOT EXISTS idx_search_log_query ON search_log(query_text);
        CREATE INDEX IF NOT EXISTS idx_search_log_source ON search_log(source);

        -- Entity registry indexes
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_jurisdiction ON entities(jurisdiction);
        CREATE INDEX IF NOT EXISTS idx_entities_ein ON entities(ein);
        CREATE INDEX IF NOT EXISTS idx_entities_address ON entities(address);
        CREATE INDEX IF NOT EXISTS idx_entity_roles_entity ON entity_roles(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_roles_person ON entity_roles(person_name);
        CREATE INDEX IF NOT EXISTS idx_entity_roles_role ON entity_roles(role);
        CREATE INDEX IF NOT EXISTS idx_entity_addresses_entity ON entity_addresses(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_addresses_address ON entity_addresses(address);
        CREATE INDEX IF NOT EXISTS idx_entity_relations_a ON entity_relations(entity_a_id);
        CREATE INDEX IF NOT EXISTS idx_entity_relations_b ON entity_relations(entity_b_id);
        CREATE INDEX IF NOT EXISTS idx_human_actions_status ON human_actions(status);
        CREATE INDEX IF NOT EXISTS idx_human_actions_priority ON human_actions(priority);
        CREATE INDEX IF NOT EXISTS idx_human_actions_type ON human_actions(action_type);

        -- ══════════════════════════════════════════════════════════
        -- NAME ALIASES: Canonical name resolution for deduplication
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS name_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            alias TEXT NOT NULL UNIQUE,
            alias_type TEXT NOT NULL CHECK(alias_type IN (
                'person_variant',
                'entity_variant',
                'entity_as_person'
            )),
            entity_id INTEGER REFERENCES entities(id),
            created_by TEXT DEFAULT 'system',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Corrections indexes
        CREATE INDEX IF NOT EXISTS idx_corrections_table_record ON corrections(table_name, record_id);
        CREATE INDEX IF NOT EXISTS idx_corrections_type ON corrections(correction_type);
        CREATE INDEX IF NOT EXISTS idx_corrections_created ON corrections(created_at);

        -- Name alias indexes
        CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON name_aliases(canonical_name);
        CREATE INDEX IF NOT EXISTS idx_aliases_alias ON name_aliases(alias);
        CREATE INDEX IF NOT EXISTS idx_aliases_type ON name_aliases(alias_type);

        -- ══════════════════════════════════════════════════════════
        -- INFRA REQUESTS: Tool/source/registry build queue
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS infra_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            request_type TEXT NOT NULL CHECK(request_type IN (
                'new_source', 'new_registry', 'tool_improvement', 'tool_fix', 'new_feature'
            )),
            priority TEXT DEFAULT 'medium' CHECK(priority IN ('critical','high','medium','low')),
            status TEXT DEFAULT 'open' CHECK(status IN (
                'open', 'evaluating', 'in_progress', 'completed', 'blocked', 'rejected'
            )),
            -- Source metadata
            source_name TEXT,
            source_url TEXT,
            data_type TEXT,
            access_method TEXT CHECK(access_method IS NULL OR access_method IN (
                'rest_api', 'graphql', 'bulk_download', 'sftp', 'web_scrape',
                'soda_api', 'manual', 'sdk', 'other'
            )),
            auth_requirements TEXT CHECK(auth_requirements IS NULL OR auth_requirements IN (
                'none', 'api_key_free', 'api_key_paid', 'login_required', 'paid_subscription', 'other'
            )),
            estimated_coverage TEXT,
            -- Provenance
            discovered_by TEXT,
            discovered_during TEXT,
            related_lead_id INTEGER REFERENCES leads(id),
            -- Implementation
            tool_file TEXT,
            files_modified TEXT,           -- JSON array
            probe_results TEXT,
            evaluation_notes TEXT,
            completed_by TEXT,
            -- Timestamps
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            claimed_at TIMESTAMP,
            completed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS infra_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            infra_id INTEGER NOT NULL REFERENCES infra_requests(id),
            note TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Infra request indexes
        CREATE INDEX IF NOT EXISTS idx_infra_status ON infra_requests(status);
        CREATE INDEX IF NOT EXISTS idx_infra_priority ON infra_requests(priority);
        CREATE INDEX IF NOT EXISTS idx_infra_type ON infra_requests(request_type);
        CREATE INDEX IF NOT EXISTS idx_infra_source_name ON infra_requests(source_name);
        CREATE INDEX IF NOT EXISTS idx_infra_related_lead ON infra_requests(related_lead_id);
        CREATE INDEX IF NOT EXISTS idx_infra_notes_infra ON infra_notes(infra_id);

        -- ══════════════════════════════════════════════════════════
        -- METHODOLOGY OBSERVATIONS: Operational learning loop
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS methodology_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL CHECK(category IN (
                'friction', 'surprise', 'methodology', 'process_gap', 'source_quality'
            )),
            description TEXT NOT NULL,
            source_skill TEXT,
            source_lead_id INTEGER REFERENCES leads(id),
            source_agent TEXT,
            target_name TEXT,
            status TEXT DEFAULT 'open' CHECK(status IN (
                'open', 'acknowledged', 'addressed', 'dismissed', 'duplicate'
            )),
            resolution TEXT,
            related_infra_id INTEGER REFERENCES infra_requests(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_methobs_category ON methodology_observations(category);
        CREATE INDEX IF NOT EXISTS idx_methobs_status ON methodology_observations(status);
        CREATE INDEX IF NOT EXISTS idx_methobs_skill ON methodology_observations(source_skill);
        CREATE INDEX IF NOT EXISTS idx_methobs_lead ON methodology_observations(source_lead_id);

        -- ══════════════════════════════════════════════════════════
        -- IRS 990 XML: Parsed e-file data (Schedule I/R)
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS irs990_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            object_id TEXT UNIQUE NOT NULL,
            ein TEXT NOT NULL,
            taxpayer_name TEXT,
            return_type TEXT,
            tax_period TEXT,
            sub_date TEXT,
            xml_batch_id TEXT,
            parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            has_schedule_i INTEGER DEFAULT 0,
            has_schedule_r INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS irs990_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER REFERENCES irs990_filings(id),
            filer_ein TEXT NOT NULL,
            filer_name TEXT,
            tax_period TEXT,
            recipient_name TEXT,
            recipient_ein TEXT,
            recipient_address TEXT,
            cash_amount INTEGER DEFAULT 0,
            non_cash_amount INTEGER DEFAULT 0,
            purpose TEXT,
            recipient_type TEXT
        );

        CREATE TABLE IF NOT EXISTS irs990_related_orgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER REFERENCES irs990_filings(id),
            filer_ein TEXT NOT NULL,
            filer_name TEXT,
            tax_period TEXT,
            related_name TEXT,
            related_ein TEXT,
            related_address TEXT,
            relationship_type TEXT,
            primary_activities TEXT,
            legal_domicile TEXT,
            total_income INTEGER,
            end_of_year_assets INTEGER,
            direct_controlling_entity TEXT
        );

        -- IRS 990 XML indexes
        CREATE INDEX IF NOT EXISTS idx_irs990_filings_ein ON irs990_filings(ein);
        CREATE INDEX IF NOT EXISTS idx_irs990_filings_object ON irs990_filings(object_id);
        CREATE INDEX IF NOT EXISTS idx_irs990_grants_filer ON irs990_grants(filer_ein);
        CREATE INDEX IF NOT EXISTS idx_irs990_grants_recipient_ein ON irs990_grants(recipient_ein);
        CREATE INDEX IF NOT EXISTS idx_irs990_grants_recipient_name ON irs990_grants(recipient_name);
        CREATE INDEX IF NOT EXISTS idx_irs990_grants_filing ON irs990_grants(filing_id);
        CREATE INDEX IF NOT EXISTS idx_irs990_related_ein ON irs990_related_orgs(related_ein);
        CREATE INDEX IF NOT EXISTS idx_irs990_related_name ON irs990_related_orgs(related_name);
        CREATE INDEX IF NOT EXISTS idx_irs990_related_filer ON irs990_related_orgs(filer_ein);
        CREATE INDEX IF NOT EXISTS idx_irs990_related_filing ON irs990_related_orgs(filing_id);

        -- ══════════════════════════════════════════════════════════
        -- DISPATCH RUNS: Track headless Claude Code agent launches
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS dispatch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            target TEXT,
            pid INTEGER,
            status TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed','timeout')),
            session_id TEXT,
            prompt_hash TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            exit_code INTEGER,
            cost_usd REAL,
            findings_added INTEGER,
            leads_created INTEGER,
            output_file TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dispatch_status ON dispatch_runs(status);
        CREATE INDEX IF NOT EXISTS idx_dispatch_type ON dispatch_runs(run_type);
        CREATE INDEX IF NOT EXISTS idx_dispatch_started ON dispatch_runs(started_at);

        -- ══════════════════════════════════════════════════════════
        -- QUALITY RUNS / ISSUES / REVIEWS: Data quality gating
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS quality_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset TEXT NOT NULL,
            run_type TEXT NOT NULL,
            run_id TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT DEFAULT 'running' CHECK(status IN ('running','passed','failed')),
            tool_version TEXT,
            metrics_json TEXT
        );

        CREATE TABLE IF NOT EXISTS quality_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset TEXT NOT NULL,
            record_ref TEXT NOT NULL,
            issue_code TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','waived')),
            details_json TEXT,
            detected_in_run_id INTEGER REFERENCES quality_runs(id),
            resolved_by TEXT,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset TEXT NOT NULL,
            record_ref TEXT NOT NULL,
            tier TEXT NOT NULL CHECK(tier IN ('tier1','tier2')),
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_review','approved','rejected')),
            required_approvals INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES review_tasks(id),
            reviewer TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('approve','reject','needs_fix')),
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id, reviewer)
        );

        CREATE INDEX IF NOT EXISTS idx_quality_runs_dataset ON quality_runs(dataset, run_type);
        CREATE INDEX IF NOT EXISTS idx_quality_issues_open ON quality_issues(dataset, severity, status);
        CREATE INDEX IF NOT EXISTS idx_quality_issues_record ON quality_issues(record_ref, issue_code, status);
        CREATE INDEX IF NOT EXISTS idx_review_tasks_record ON review_tasks(dataset, record_ref, status);
        CREATE INDEX IF NOT EXISTS idx_review_decisions_task ON review_decisions(task_id);
    """)

    # Investigation threads — group related leads/findings by theme
    db.execute("""CREATE TABLE IF NOT EXISTS investigation_threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'active' CHECK(status IN ('active','paused','completed')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Investigation config — key/value store for profile settings
    db.execute("""CREATE TABLE IF NOT EXISTS investigation_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── Schema migrations: add columns to existing tables ──
    # SQLite ALTER TABLE ADD COLUMN is safe — errors if column exists, which we catch.
    _migrations = [
        # Provenance fields on findings
        ("findings", "claim_type TEXT DEFAULT 'inference'"),      # direct_quote, paraphrase, inference, synthesis, user_provided
        ("findings", "verification_status TEXT DEFAULT 'unverified'"),  # unverified, verified, disputed, retracted
        ("findings", "verified_by TEXT"),
        ("findings", "verified_at TIMESTAMP"),
        ("findings", "quality_state TEXT DEFAULT 'unchecked'"),
        ("findings", "confidence_requested TEXT"),
        # Provenance fields on finding_evidence
        ("finding_evidence", "source_quote TEXT"),               # exact text from source supporting claim
        ("finding_evidence", "source_page TEXT"),                # page/line/section within source
        ("finding_evidence", "assessment TEXT"),                 # how this evidence supports the finding
        # Lead ↔ infra_request linkage (lead blocked by missing tool/source)
        ("leads", "blocked_by_infra_id INTEGER REFERENCES infra_requests(id)"),
        # Triage tracking
        ("leads", "triaged_by TEXT"),
        ("leads", "triaged_at TIMESTAMP"),
        # Provenance fields on connections
        ("connections", "verification_status TEXT DEFAULT 'unverified'"),
        ("connections", "verified_by TEXT"),
        ("connections", "verified_at TIMESTAMP"),
        # Provenance fields on connection_evidence
        ("connection_evidence", "source_quote TEXT"),
        ("connection_evidence", "source_page TEXT"),
        # Investigation thread assignment
        ("leads", "thread_id INTEGER REFERENCES investigation_threads(id)"),
        ("findings", "thread_id INTEGER REFERENCES investigation_threads(id)"),
        # Email attribution on finding_evidence (from parse_email_chain.py)
        ("finding_evidence", "email_sender TEXT"),
        ("finding_evidence", "email_date TEXT"),
        ("finding_evidence", "chain_position INTEGER"),
        # Investigation profile scoping
        ("leads", "profile_id TEXT"),
        ("findings", "profile_id TEXT"),
        ("connections", "profile_id TEXT"),
        ("investigation_threads", "profile_id TEXT"),
        # Source reliability: rename epstein_connection -> subject_connection
        ("source_reliability", "subject_connection TEXT"),
        # Actual formation/filing date (distinct from DB ingestion created_at)
        ("entities", "date_formed TEXT"),
        # Lead lease tracking (stale-recovery for crashed agents)
        ("leads", "claimed_by TEXT"),
        ("leads", "claimed_at TIMESTAMP"),
        ("leads", "lease_until TIMESTAMP"),
        # Session performance metrics
        ("sessions", "findings_created INTEGER DEFAULT 0"),
        ("sessions", "connections_created INTEGER DEFAULT 0"),
        ("sessions", "leads_created INTEGER DEFAULT 0"),
        ("sessions", "retractions INTEGER DEFAULT 0"),
        # Bi-temporal: when the relationship existed in the real world
        ("connections", "valid_from DATE"),
        ("connections", "valid_until DATE"),
        # Triage scheduler fields (Phase 1 refactor)
        ("leads", "depth_tier TEXT"),
        ("leads", "recommended_skill TEXT"),
        ("leads", "triage_rationale TEXT"),
        ("leads", "stop_reason TEXT"),
    ]
    for table, column_def in _migrations:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate epstein_connection data to subject_connection (one-time)
    try:
        db.execute("""
            UPDATE source_reliability SET subject_connection = epstein_connection
            WHERE subject_connection IS NULL AND epstein_connection IS NOT NULL
        """)
    except sqlite3.OperationalError:
        pass  # epstein_connection column doesn't exist (fresh DB)

    # Relax finding_type CHECK constraint to allow negative_result, background
    # (Python-side VALID_FINDING_TYPES in findings_tracker.py handles validation)
    try:
        import re
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='findings'"
        ).fetchone()
        if schema and "CHECK(finding_type IN" in (schema[0] or ""):
            new_sql = re.sub(
                r"finding_type\s+TEXT\s+CHECK\(finding_type\s+IN\s*\([^)]+\)\)",
                "finding_type TEXT",
                schema[0]
            )
            if new_sql != schema[0]:
                db.execute("PRAGMA writable_schema=ON")
                db.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='findings'",
                    (new_sql,)
                )
                db.execute("PRAGMA writable_schema=OFF")
                db.commit()
                # Reconnect so SQLite reloads the compiled schema
                db.close()
                db = sqlite3.connect(str(DB_PATH))
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass  # Non-critical — Python validation still protects writes

    # Relax category CHECK constraint on leads (Python VALID_CATEGORIES handles validation)
    try:
        import re as _re2
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'"
        ).fetchone()
        if schema and "CHECK(category IN" in (schema[0] or ""):
            new_sql = _re2.sub(
                r"CHECK\(category\s+IN\s*\([^)]+\)\)",
                "",
                schema[0]
            )
            if new_sql != schema[0]:
                db.execute("PRAGMA writable_schema=ON")
                db.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='leads'",
                    (new_sql,)
                )
                db.execute("PRAGMA writable_schema=OFF")
                db.commit()
                db.close()
                db = sqlite3.connect(str(DB_PATH))
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    # Relax entity_type CHECK constraint on entities (Python VALID_ENTITY_TYPES handles validation)
    try:
        import re as _re3
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entities'"
        ).fetchone()
        if schema and "CHECK(entity_type IN" in (schema[0] or ""):
            new_sql = _re3.sub(
                r"CHECK\(entity_type\s+IN\s*\([^)]+\)\)",
                "",
                schema[0]
            )
            if new_sql != schema[0]:
                db.execute("PRAGMA writable_schema=ON")
                db.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='entities'",
                    (new_sql,)
                )
                db.execute("PRAGMA writable_schema=OFF")
                db.commit()
                db.close()
                db = sqlite3.connect(str(DB_PATH))
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    # Fix stale FK: lead_id REFERENCES leads_old_backup -> leads
    try:
        import re as _re
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='findings'"
        ).fetchone()
        if schema and "leads_old_backup" in (schema[0] or ""):
            new_sql = schema[0].replace('"leads_old_backup"', '"leads"')
            if new_sql != schema[0]:
                db.execute("PRAGMA writable_schema=ON")
                db.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='findings'",
                    (new_sql,)
                )
                db.execute("PRAGMA writable_schema=OFF")
                db.commit()
                db.close()
                db = sqlite3.connect(str(DB_PATH))
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    # Thread and profile indexes
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_leads_thread ON leads(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_findings_thread ON findings(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_leads_profile ON leads(profile_id)",
        "CREATE INDEX IF NOT EXISTS idx_findings_profile ON findings(profile_id)",
        "CREATE INDEX IF NOT EXISTS idx_connections_profile ON connections(profile_id)",
        "CREATE INDEX IF NOT EXISTS idx_threads_profile ON investigation_threads(profile_id)",
        "CREATE INDEX IF NOT EXISTS idx_leads_lease ON leads(status, lease_until)",
    ]:
        try:
            db.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Backfill profile_id for tech-right records (idempotent)
    # Thread IDs 9-14 belong to tech-right profile; correct the default 'epstein' value
    try:
        updated = db.execute("""
            UPDATE findings SET profile_id = 'tech-right'
            WHERE thread_id IN (9,10,11,12,13,14) AND COALESCE(profile_id, 'epstein') != 'tech-right'
        """).rowcount
        updated += db.execute("""
            UPDATE leads SET profile_id = 'tech-right'
            WHERE thread_id IN (9,10,11,12,13,14) AND COALESCE(profile_id, 'epstein') != 'tech-right'
        """).rowcount
        updated += db.execute("""
            UPDATE connections SET profile_id = 'tech-right'
            WHERE finding_id IN (SELECT id FROM findings WHERE thread_id IN (9,10,11,12,13,14))
              AND COALESCE(profile_id, 'epstein') != 'tech-right'
        """).rowcount
        if updated > 0:
            db.commit()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        try:
            db.rollback()
        except Exception:
            pass

    # Deduplicate connections and ensure UNIQUE constraint treats NULLs as equal.
    # Older schema used a plain unique index where NULL relationship_type/profile_id
    # values could still duplicate. Migrate to a COALESCE expression index.
    try:
        existing = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_connections_unique'"
        ).fetchone()
        existing_sql = (existing["sql"] or "").lower() if existing else ""
        needs_migration = (
            not existing
            or "coalesce(relationship_type" not in existing_sql
            or "coalesce(profile_id" not in existing_sql
        )
        if needs_migration:
            db.execute("DROP INDEX IF EXISTS idx_connections_unique")
            # Remove duplicate connections, keeping the oldest (smallest id) per group.
            # Group by coalesced values so NULLs are deduplicated correctly.
            # Temporarily disable FK checks for safe dedup migration.
            # PRAGMA foreign_keys must be set outside a transaction.
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")

            # Copy evidence from duplicate connections to canonical (MIN id).
            # INSERT OR IGNORE handles the case where canonical already has
            # the same evidence_ref — no data is lost.
            db.execute("""
                INSERT OR IGNORE INTO connection_evidence (connection_id, evidence_type, evidence_ref, source_quote, source_page)
                SELECT canon.id, ce.evidence_type, ce.evidence_ref, ce.source_quote, ce.source_page
                FROM connection_evidence ce
                JOIN connections c ON c.id = ce.connection_id
                JOIN (
                    SELECT MIN(id) as id, person_a, person_b,
                           COALESCE(relationship_type, '') as rt, COALESCE(profile_id, '') as pid
                    FROM connections
                    GROUP BY person_a, person_b, COALESCE(relationship_type, ''), COALESCE(profile_id, '')
                ) canon ON canon.person_a = c.person_a AND canon.person_b = c.person_b
                    AND canon.rt = COALESCE(c.relationship_type, '')
                    AND canon.pid = COALESCE(c.profile_id, '')
                WHERE ce.connection_id != canon.id
            """)
            # Remove evidence rows pointing at duplicates (already copied above)
            db.execute("""
                DELETE FROM connection_evidence WHERE connection_id NOT IN (
                    SELECT MIN(id) FROM connections
                    GROUP BY person_a, person_b, COALESCE(relationship_type, ''), COALESCE(profile_id, '')
                )
            """)
            # Now safe to delete duplicate connections
            db.execute("""
                DELETE FROM connections WHERE id NOT IN (
                    SELECT MIN(id) FROM connections
                    GROUP BY person_a, person_b, COALESCE(relationship_type, ''), COALESCE(profile_id, '')
                )
            """)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""
                CREATE UNIQUE INDEX idx_connections_unique
                ON connections(
                    person_a,
                    person_b,
                    COALESCE(relationship_type, ''),
                    COALESCE(profile_id, '')
                )
            """)
            db.commit()
    except sqlite3.OperationalError:
        pass

    # Widen connections.relationship_type CHECK constraint to include entity types.
    # SQLite can't ALTER CHECK constraints, so we rebuild the table if needed.
    try:
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='connections'"
        ).fetchone()
        if table_sql and "'owns'" not in (table_sql[0] or ""):
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("""CREATE TABLE connections_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_a TEXT NOT NULL,
                person_b TEXT NOT NULL,
                relationship_type TEXT CHECK(relationship_type IN (
                    'financial','social','legal','intelligence','employment',
                    'familial','corporate','advisory','political',
                    'owns','controls','funds','subsidiary_of','contracts_with',
                    'successor_to','shares_officer','supplies'
                )),
                description TEXT,
                strength TEXT DEFAULT 'medium' CHECK(strength IN (
                    'strong','medium','weak','circumstantial'
                )),
                date_range TEXT,
                finding_id INTEGER REFERENCES findings(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verification_status TEXT DEFAULT 'unverified',
                verified_by TEXT,
                verified_at TIMESTAMP,
                profile_id TEXT DEFAULT 'epstein',
                valid_from DATE,
                valid_until DATE
            )""")
            db.execute("""INSERT INTO connections_new
                SELECT id, person_a, person_b, relationship_type, description,
                       strength, date_range, finding_id, created_at,
                       verification_status, verified_by, verified_at,
                       profile_id, valid_from, valid_until
                FROM connections""")
            db.execute("DROP TABLE connections")
            db.execute("ALTER TABLE connections_new RENAME TO connections")
            db.execute("PRAGMA foreign_keys=ON")
            # Recreate indexes
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_connections_a ON connections(person_a)",
                "CREATE INDEX IF NOT EXISTS idx_connections_b ON connections(person_b)",
                "CREATE INDEX IF NOT EXISTS idx_connections_profile ON connections(profile_id)",
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_unique
                   ON connections(person_a, person_b, COALESCE(relationship_type, ''), COALESCE(profile_id, ''))""",
            ]:
                db.execute(idx_sql)
            db.commit()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        try:
            db.rollback()
        except Exception:
            pass

    # FTS for leads
    try:
        db.execute("""
            CREATE VIRTUAL TABLE leads_fts USING fts5(
                title, description, findings, target_name,
                content=leads, content_rowid=id
            )
        """)
        db.execute("""
            INSERT INTO leads_fts(rowid, title, description, findings, target_name)
            SELECT id, title, COALESCE(description,''), COALESCE(findings,''), COALESCE(target_name,'')
            FROM leads
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for findings
    try:
        db.execute("""
            CREATE VIRTUAL TABLE findings_fts USING fts5(
                target_name, summary, detail,
                content=findings, content_rowid=id
            )
        """)
        db.execute("""
            INSERT INTO findings_fts(rowid, target_name, summary, detail)
            SELECT id, target_name, summary, COALESCE(detail,'')
            FROM findings
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for entities
    try:
        db.execute("""
            CREATE VIRTUAL TABLE entities_fts USING fts5(
                name, jurisdiction, address, notes,
                content=entities, content_rowid=id
            )
        """)
        db.execute("""
            INSERT INTO entities_fts(rowid, name, jurisdiction, address, notes)
            SELECT id, name, COALESCE(jurisdiction,''), COALESCE(address,''), COALESCE(notes,'')
            FROM entities
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS sync triggers for entities
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
            INSERT INTO entities_fts(rowid, name, jurisdiction, address, notes)
            VALUES (new.id, new.name, COALESCE(new.jurisdiction,''), COALESCE(new.address,''), COALESCE(new.notes,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
            INSERT INTO entities_fts(entities_fts, rowid, name, jurisdiction, address, notes)
            VALUES ('delete', old.id, old.name, COALESCE(old.jurisdiction,''), COALESCE(old.address,''), COALESCE(old.notes,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
            INSERT INTO entities_fts(entities_fts, rowid, name, jurisdiction, address, notes)
            VALUES ('delete', old.id, old.name, COALESCE(old.jurisdiction,''), COALESCE(old.address,''), COALESCE(old.notes,''));
            INSERT INTO entities_fts(rowid, name, jurisdiction, address, notes)
            VALUES (new.id, new.name, COALESCE(new.jurisdiction,''), COALESCE(new.address,''), COALESCE(new.notes,''));
        END""",
    ]:
        try:
            db.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # FTS sync triggers for leads
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS leads_ai AFTER INSERT ON leads BEGIN
            INSERT INTO leads_fts(rowid, title, description, findings, target_name)
            VALUES (new.id, new.title, COALESCE(new.description,''), COALESCE(new.findings,''), COALESCE(new.target_name,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS leads_ad AFTER DELETE ON leads BEGIN
            INSERT INTO leads_fts(leads_fts, rowid, title, description, findings, target_name)
            VALUES ('delete', old.id, old.title, COALESCE(old.description,''), COALESCE(old.findings,''), COALESCE(old.target_name,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS leads_au AFTER UPDATE ON leads BEGIN
            INSERT INTO leads_fts(leads_fts, rowid, title, description, findings, target_name)
            VALUES ('delete', old.id, old.title, COALESCE(old.description,''), COALESCE(old.findings,''), COALESCE(old.target_name,''));
            INSERT INTO leads_fts(rowid, title, description, findings, target_name)
            VALUES (new.id, new.title, COALESCE(new.description,''), COALESCE(new.findings,''), COALESCE(new.target_name,''));
        END""",
    ]:
        try:
            db.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # FTS sync triggers for findings
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS findings_ai AFTER INSERT ON findings BEGIN
            INSERT INTO findings_fts(rowid, target_name, summary, detail)
            VALUES (new.id, new.target_name, new.summary, COALESCE(new.detail,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS findings_ad AFTER DELETE ON findings BEGIN
            INSERT INTO findings_fts(findings_fts, rowid, target_name, summary, detail)
            VALUES ('delete', old.id, old.target_name, old.summary, COALESCE(old.detail,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS findings_au AFTER UPDATE ON findings BEGIN
            INSERT INTO findings_fts(findings_fts, rowid, target_name, summary, detail)
            VALUES ('delete', old.id, old.target_name, old.summary, COALESCE(old.detail,''));
            INSERT INTO findings_fts(rowid, target_name, summary, detail)
            VALUES (new.id, new.target_name, new.summary, COALESCE(new.detail,''));
        END""",
    ]:
        try:
            db.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # FTS for infra_requests
    try:
        db.execute("""
            CREATE VIRTUAL TABLE infra_requests_fts USING fts5(
                title, description, source_name, data_type, probe_results, evaluation_notes,
                content=infra_requests, content_rowid=id
            )
        """)
        db.execute("""
            INSERT INTO infra_requests_fts(rowid, title, description, source_name, data_type, probe_results, evaluation_notes)
            SELECT id, title, description, COALESCE(source_name,''), COALESCE(data_type,''),
                   COALESCE(probe_results,''), COALESCE(evaluation_notes,'')
            FROM infra_requests
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS sync triggers for infra_requests
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS infra_ai AFTER INSERT ON infra_requests BEGIN
            INSERT INTO infra_requests_fts(rowid, title, description, source_name, data_type, probe_results, evaluation_notes)
            VALUES (new.id, new.title, new.description, COALESCE(new.source_name,''), COALESCE(new.data_type,''),
                    COALESCE(new.probe_results,''), COALESCE(new.evaluation_notes,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS infra_ad AFTER DELETE ON infra_requests BEGIN
            INSERT INTO infra_requests_fts(infra_requests_fts, rowid, title, description, source_name, data_type, probe_results, evaluation_notes)
            VALUES ('delete', old.id, old.title, old.description, COALESCE(old.source_name,''), COALESCE(old.data_type,''),
                    COALESCE(old.probe_results,''), COALESCE(old.evaluation_notes,''));
        END""",
        """CREATE TRIGGER IF NOT EXISTS infra_au AFTER UPDATE ON infra_requests BEGIN
            INSERT INTO infra_requests_fts(infra_requests_fts, rowid, title, description, source_name, data_type, probe_results, evaluation_notes)
            VALUES ('delete', old.id, old.title, old.description, COALESCE(old.source_name,''), COALESCE(old.data_type,''),
                    COALESCE(old.probe_results,''), COALESCE(old.evaluation_notes,''));
            INSERT INTO infra_requests_fts(rowid, title, description, source_name, data_type, probe_results, evaluation_notes)
            VALUES (new.id, new.title, new.description, COALESCE(new.source_name,''), COALESCE(new.data_type,''),
                    COALESCE(new.probe_results,''), COALESCE(new.evaluation_notes,''));
        END""",
    ]:
        try:
            db.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # Auto-update updated_at on infra_requests
    try:
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS infra_updated_at AFTER UPDATE ON infra_requests BEGIN
                UPDATE infra_requests SET updated_at = CURRENT_TIMESTAMP WHERE id = new.id;
            END
        """)
    except sqlite3.OperationalError:
        pass

    # ── Migrate leads CHECK constraint to include pending_triage ──
    # SQLite doesn't allow ALTER TABLE to modify constraints, so we check if the
    # constraint already includes 'pending_triage'. If not, we recreate the table.
    # This is safe because we're in WAL mode and the migration is idempotent.
    try:
        # Test if the new status is accepted
        db.execute("INSERT INTO leads (title, status) VALUES ('__migration_test__', 'pending_triage')")
        db.execute("DELETE FROM leads WHERE title = '__migration_test__'")
    except sqlite3.IntegrityError:
        # Old constraint — need to migrate. Recreate the table with new constraint.
        db.executescript("""
            CREATE TABLE leads_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT CHECK(category IN ('person','entity','financial','document','digital','connection','legal','intelligence')),
                priority TEXT DEFAULT 'medium' CHECK(priority IN ('critical','high','medium','low')),
                status TEXT DEFAULT 'open' CHECK(status IN ('open','pending_triage','in_progress','completed','blocked','dead_end')),
                source TEXT,
                target_name TEXT,
                findings TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
            INSERT INTO leads_new (id, title, description, category, priority, status, source, target_name, findings, created_at, updated_at, completed_at)
                SELECT id, title, description, category, priority, status, source, target_name, findings, created_at, updated_at, completed_at FROM leads;
            DROP TABLE leads;
            ALTER TABLE leads_new RENAME TO leads;
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_priority ON leads(priority);
            CREATE INDEX IF NOT EXISTS idx_leads_category ON leads(category);
            CREATE INDEX IF NOT EXISTS idx_leads_target ON leads(target_name);
        """)
        # Re-run the column migrations for leads (they were on the old table)
        for col_def in [
            "blocked_by_infra_id INTEGER REFERENCES infra_requests(id)",
            "triaged_by TEXT",
            "triaged_at TIMESTAMP",
        ]:
            try:
                db.execute(f"ALTER TABLE leads ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        # Recreate FTS and triggers (they reference the old table)
        try:
            db.execute("DROP TABLE IF EXISTS leads_fts")
        except sqlite3.OperationalError:
            pass

    # ── Fix lead_* foreign keys pointing to leads_old_backup ──
    def _fk_points_to_backup(table):
        try:
            rows = db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        except sqlite3.OperationalError:
            return False
        return any(r[2] == "leads_old_backup" for r in rows)

    if _fk_points_to_backup("lead_notes"):
        db.executescript("""
            CREATE TABLE lead_notes_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                note TEXT NOT NULL,
                session_id INTEGER REFERENCES sessions(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO lead_notes_new (id, lead_id, note, session_id, created_at)
                SELECT id, lead_id, note, session_id, created_at FROM lead_notes;
            DROP TABLE lead_notes;
            ALTER TABLE lead_notes_new RENAME TO lead_notes;
        """)

    if _fk_points_to_backup("lead_evidence"):
        db.executescript("""
            CREATE TABLE lead_evidence_new (
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                evidence_type TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                PRIMARY KEY (lead_id, evidence_ref)
            );
            INSERT INTO lead_evidence_new (lead_id, evidence_type, evidence_ref)
                SELECT lead_id, evidence_type, evidence_ref FROM lead_evidence;
            DROP TABLE lead_evidence;
            ALTER TABLE lead_evidence_new RENAME TO lead_evidence;
        """)

    if _fk_points_to_backup("lead_relations"):
        db.executescript("""
            CREATE TABLE lead_relations_new (
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                related_lead_id INTEGER NOT NULL REFERENCES leads(id),
                relation_type TEXT DEFAULT 'related',
                PRIMARY KEY (lead_id, related_lead_id)
            );
            INSERT INTO lead_relations_new (lead_id, related_lead_id, relation_type)
                SELECT lead_id, related_lead_id, relation_type FROM lead_relations;
            DROP TABLE lead_relations;
            ALTER TABLE lead_relations_new RENAME TO lead_relations;
        """)

    db.commit()
    return db


# ── Lead CRUD ────────────────────────────────────────────────


def add_lead(title, description=None, category=None, priority="medium",
             source=None, target_name=None, evidence=None, related_leads=None,
             thread_id=None, profile_id=None, depth_tier=None,
             recommended_skill=None):
    """Add a new lead. Returns the lead ID."""
    if profile_id is None:
        profile_id = _detect_active_profile()
    if target_name:
        try:
            from tools.name_resolver import resolve_canonical
            target_name = resolve_canonical(target_name)
        except Exception:
            pass

    db = get_db()
    cursor = db.execute("""
        INSERT INTO leads (title, description, category, priority, source, target_name, thread_id, profile_id, depth_tier, recommended_skill)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, description, category, priority, source, target_name, thread_id, profile_id, depth_tier, recommended_skill))
    lead_id = cursor.lastrowid

    if evidence:
        for ev in evidence:
            ev_type = "efta" if ev.startswith("EFTA") else "file" if "/" in ev else "url" if "://" in ev else "ref"
            db.execute(
                "INSERT OR IGNORE INTO lead_evidence (lead_id, evidence_type, evidence_ref) VALUES (?, ?, ?)",
                (lead_id, ev_type, ev)
            )

    if related_leads:
        for rel_id in related_leads:
            db.execute(
                "INSERT OR IGNORE INTO lead_relations (lead_id, related_lead_id, relation_type) VALUES (?, ?, 'related')",
                (lead_id, rel_id)
            )

    db.commit()
    db.close()
    return lead_id


def list_leads(status=None, priority=None, category=None, target=None, limit=50,
               thread_id=None):
    """List leads with optional filters."""
    db = get_db()
    conditions = []
    params = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if priority:
        conditions.append("priority = ?")
        params.append(priority)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if target:
        conditions.append("target_name LIKE ?")
        params.append(f"%{target}%")
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT * FROM leads {where}
        ORDER BY
            CASE priority
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
            END,
            created_at DESC
        LIMIT ?
    """
    params.append(limit)

    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_lead(lead_id):
    """Get a single lead with notes, evidence, and relations."""
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        db.close()
        return None

    result = dict(lead)
    result["notes"] = [dict(n) for n in db.execute(
        "SELECT * FROM lead_notes WHERE lead_id = ? ORDER BY created_at", (lead_id,)
    ).fetchall()]
    result["evidence"] = [dict(e) for e in db.execute(
        "SELECT * FROM lead_evidence WHERE lead_id = ?", (lead_id,)
    ).fetchall()]
    result["related_leads"] = [dict(r) for r in db.execute("""
        SELECT lr.*, l.title, l.status, l.priority
        FROM lead_relations lr
        JOIN leads l ON l.id = lr.related_lead_id
        WHERE lr.lead_id = ?
    """, (lead_id,)).fetchall()]
    # Also get any findings produced by this lead
    result["findings"] = [dict(f) for f in db.execute(
        "SELECT id, target_name, summary, confidence FROM findings WHERE lead_id = ?", (lead_id,)
    ).fetchall()]

    db.close()
    return result


def claim_lead(lead_id, session_id=None, claimed_by=None, lease_hours=2):
    """Mark a lead as in_progress with a lease."""
    db = get_db()
    now = _utcnow()
    now_iso = now.isoformat()
    lease_until = (now + timedelta(hours=lease_hours)).isoformat()
    cursor = db.execute(
        """UPDATE leads SET status = 'in_progress', updated_at = ?,
           claimed_by = ?, claimed_at = ?, lease_until = ?
           WHERE id = ? AND status = 'open'""",
        (now_iso, claimed_by, now_iso, lease_until, lead_id)
    )
    if cursor.rowcount != 1:
        db.close()
        return False
    if session_id:
        db.execute(
            "INSERT INTO lead_notes (lead_id, note, session_id) VALUES (?, ?, ?)",
            (lead_id, f"Claimed by session #{session_id}", session_id)
        )
    db.commit()
    db.close()
    return True


def claim_next_lead(category=None, thread_id=None, session_id=None,
                    claimed_by=None, lease_hours=2, profile_id=None,
                    depth_tier=None):
    """Atomically select and claim the next open lead.

    Uses BEGIN IMMEDIATE to acquire a write lock, preventing TOCTOU races
    where two agents could claim the same lead.

    Opportunistically recovers stale leads before selecting.
    Auto-filters by active investigation profile unless overridden.
    """
    if profile_id is None:
        profile_id = _detect_active_profile()
    db = get_db()
    db.execute("BEGIN IMMEDIATE")

    # Recover stale leads before picking next
    _mark_stale_leads_db(db)

    conditions = ["status = 'open'"]
    params = []
    if profile_id:
        conditions.append("profile_id = ?")
        params.append(profile_id)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)
    if depth_tier:
        conditions.append("depth_tier = ?")
        params.append(depth_tier)

    where = " AND ".join(conditions)
    row = db.execute(f"""
        SELECT * FROM leads WHERE {where}
        ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                               WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END,
                 created_at ASC LIMIT 1
    """, params).fetchone()

    if not row:
        db.commit()
        db.close()
        return None

    now = _utcnow()
    now_iso = now.isoformat()
    lease_until = (now + timedelta(hours=lease_hours)).isoformat()
    cursor = db.execute(
        """UPDATE leads SET status='in_progress', updated_at=?,
           claimed_by=?, claimed_at=?, lease_until=?
           WHERE id=? AND status='open'""",
        (now_iso, claimed_by, now_iso, lease_until, row["id"]))

    if cursor.rowcount != 1:
        db.commit()
        db.close()
        return None

    if session_id:
        db.execute("INSERT INTO lead_notes (lead_id, note, session_id) VALUES (?,?,?)",
                   (row["id"], f"Claimed by session #{session_id}", session_id))

    db.commit()
    result = dict(row)
    result["status"] = "in_progress"
    db.close()
    return result


def add_note(lead_id, note, session_id=None):
    """Add a note to a lead."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute(
        "INSERT INTO lead_notes (lead_id, note, session_id) VALUES (?, ?, ?)",
        (lead_id, note, session_id)
    )
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now, lead_id))
    db.commit()
    db.close()


def add_evidence_to_lead(lead_id, evidence_ref, evidence_type=None):
    """Add an evidence reference to a lead."""
    if not evidence_type:
        evidence_type = "efta" if evidence_ref.startswith("EFTA") else "file" if "/" in evidence_ref else "url" if "://" in evidence_ref else "ref"
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO lead_evidence (lead_id, evidence_type, evidence_ref) VALUES (?, ?, ?)",
        (lead_id, evidence_type, evidence_ref)
    )
    db.commit()
    db.close()


def complete_lead(lead_id, findings_text):
    """Mark a lead as completed with findings."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute("""
        UPDATE leads SET status = 'completed', findings = ?, updated_at = ?, completed_at = ?
        WHERE id = ?
    """, (findings_text, now, now, lead_id))
    db.commit()
    db.close()


def block_lead(lead_id, reason):
    """Mark a lead as blocked."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute("UPDATE leads SET status = 'blocked', updated_at = ? WHERE id = ?", (now, lead_id))
    db.execute("INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)", (lead_id, f"BLOCKED: {reason}"))
    db.commit()
    db.close()


def dead_end_lead(lead_id, reason):
    """Mark a lead as a dead end."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute(
        """UPDATE leads SET status = 'dead_end', findings = ?, stop_reason = ?,
           triage_rationale = COALESCE(triage_rationale, ?),
           updated_at = ?, completed_at = ? WHERE id = ?""",
        (reason, reason, reason, now, now, lead_id)
    )
    db.commit()
    db.close()


def batch_dead_end_leads(lead_ids, reason):
    """Mark multiple leads as dead ends in a single transaction.

    Returns count of leads actually updated.
    """
    if not lead_ids:
        return 0
    db = get_db()
    now = _utcnow().isoformat()
    count = 0
    for lid in lead_ids:
        cur = db.execute(
            """UPDATE leads SET status = 'dead_end', findings = ?, stop_reason = ?,
               triage_rationale = COALESCE(triage_rationale, ?),
               updated_at = ?, completed_at = ?
            WHERE id = ? AND status IN ('open', 'pending_triage')""",
            (reason, reason, reason, now, now, lid)
        )
        count += cur.rowcount
    db.commit()
    db.close()
    return count


def reopen_lead(lead_id):
    """Reopen a closed lead."""
    db = get_db()
    now = _utcnow().isoformat()
    db.execute(
        "UPDATE leads SET status = 'open', updated_at = ?, completed_at = NULL WHERE id = ?",
        (now, lead_id)
    )
    db.commit()
    db.close()


def _mark_stale_leads_db(db, max_age_hours=2):
    """Reset stale in_progress leads to open (operates on existing db connection).

    A lead is stale if status='in_progress' and lease_until < NOW().
    Returns list of recovered lead IDs.
    """
    now = _utcnow().isoformat()
    rows = db.execute("""
        SELECT id, title FROM leads
        WHERE status = 'in_progress' AND lease_until IS NOT NULL AND lease_until < ?
    """, (now,)).fetchall()

    recovered = []
    for row in rows:
        db.execute("""
            UPDATE leads SET status = 'open', updated_at = ?,
                claimed_by = NULL, claimed_at = NULL, lease_until = NULL
            WHERE id = ?
        """, (now, row["id"]))
        db.execute(
            "INSERT INTO lead_notes (lead_id, note) VALUES (?, ?)",
            (row["id"], "Auto-reopened: lease expired (agent likely crashed)")
        )
        recovered.append({"id": row["id"], "title": row["title"]})
    return recovered


def mark_stale_leads(max_age_hours=2):
    """Find and reset stale in_progress leads. Returns list of recovered leads."""
    db = get_db()
    recovered = _mark_stale_leads_db(db, max_age_hours)
    db.commit()
    db.close()
    return recovered


def list_stale_leads():
    """List leads that have exceeded their lease without being completed."""
    db = get_db()
    now = _utcnow().isoformat()
    rows = db.execute("""
        SELECT id, title, priority, category, claimed_by, claimed_at, lease_until
        FROM leads
        WHERE status = 'in_progress' AND lease_until IS NOT NULL AND lease_until < ?
        ORDER BY lease_until ASC
    """, (now,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def search_leads(query):
    """Full-text search across lead content. Wraps terms in quotes for safety."""
    db = get_db()
    # Quote the query to handle special FTS5 characters (hyphens, etc.)
    safe_query = '"' + query.replace('"', '""') + '"'
    rows = db.execute("""
        SELECT leads.*, leads_fts.rank
        FROM leads_fts
        JOIN leads ON leads.id = leads_fts.rowid
        WHERE leads_fts MATCH ?
        ORDER BY leads_fts.rank
        LIMIT 30
    """, (safe_query,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_next_lead(category=None, thread_id=None, profile_id=None):
    """Get the highest priority open lead, filtered by active profile."""
    if profile_id is None:
        profile_id = _detect_active_profile()
    db = get_db()
    conditions = ["status = 'open'"]
    params = []
    if profile_id:
        conditions.append("profile_id = ?")
        params.append(profile_id)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)

    where = f"WHERE {' AND '.join(conditions)}"
    row = db.execute(f"""
        SELECT * FROM leads {where}
        ORDER BY
            CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END,
            created_at ASC
        LIMIT 1
    """, params).fetchone()
    db.close()
    return dict(row) if row else None


def find_by_evidence(evidence_ref):
    """Find all leads and findings referencing a specific evidence ID."""
    db = get_db()
    lead_rows = db.execute("""
        SELECT le.evidence_type, le.evidence_ref, l.*
        FROM lead_evidence le JOIN leads l ON l.id = le.lead_id
        WHERE le.evidence_ref = ?
    """, (evidence_ref,)).fetchall()
    finding_rows = db.execute("""
        SELECT fe.evidence_type, fe.evidence_ref, f.*
        FROM finding_evidence fe JOIN findings f ON f.id = fe.finding_id
        WHERE fe.evidence_ref = ?
    """, (evidence_ref,)).fetchall()
    db.close()
    return {
        "leads": [dict(r) for r in lead_rows],
        "findings": [dict(r) for r in finding_rows],
    }


def log_search(query_text, source, result_count, session_id=None):
    """Log a search to prevent redundant queries.

    Updates the dedup row (search_log) AND appends to immutable history
    (search_history) so audit trails are preserved even when the same
    query is re-run with different result counts.
    """
    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO search_log (query_text, source, result_count, session_id)
        VALUES (?, ?, ?, ?)
    """, (query_text, source, result_count, session_id))
    # Append to immutable history (table created by migration below)
    try:
        db.execute("""
            INSERT INTO search_history (query_text, source, result_count, session_id)
            VALUES (?, ?, ?, ?)
        """, (query_text, source, result_count, session_id))
    except Exception:
        pass  # table may not exist yet on first run
    db.commit()
    db.close()


def check_searched(query_text, source):
    """Check if a query has already been run against a source."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM search_log WHERE query_text = ? AND source = ?",
        (query_text, source)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def update_session_metrics(session_id):
    """Recompute session performance metrics from actual DB counts."""
    db = get_db()
    findings = db.execute(
        "SELECT COUNT(*) FROM findings WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    connections = db.execute(
        "SELECT COUNT(*) FROM connections WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    leads = db.execute(
        "SELECT COUNT(*) FROM lead_notes WHERE session_id = ? AND note LIKE 'Claimed by session%'", (session_id,)
    ).fetchone()[0]
    retractions = db.execute(
        "SELECT COUNT(*) FROM findings WHERE session_id = ? AND verification_status = 'retracted'", (session_id,)
    ).fetchone()[0]
    db.execute("""
        UPDATE sessions SET findings_created = ?, connections_created = ?,
            leads_created = ?, retractions = ?
        WHERE id = ?
    """, (findings, connections, leads, retractions, session_id))
    db.commit()
    result = {"session_id": session_id, "findings": findings, "connections": connections,
              "leads": leads, "retractions": retractions}
    db.close()
    return result


def get_session_stats(limit=20):
    """Get performance metrics across recent sessions."""
    db = get_db()
    rows = db.execute("""
        SELECT id, agent_id, skill_invoked, started_at, ended_at,
               findings_created, connections_created, leads_created, retractions
        FROM sessions
        ORDER BY started_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_stats():
    """Get summary statistics across the investigation."""
    db = get_db()
    stats = {}

    # Leads
    rows = db.execute("SELECT status, COUNT(*) as cnt FROM leads GROUP BY status").fetchall()
    stats["leads_by_status"] = {r["status"]: r["cnt"] for r in rows}
    rows = db.execute(
        "SELECT priority, COUNT(*) as cnt FROM leads WHERE status IN ('open','in_progress') GROUP BY priority"
    ).fetchall()
    stats["open_by_priority"] = {r["priority"]: r["cnt"] for r in rows}
    rows = db.execute("SELECT category, COUNT(*) as cnt FROM leads GROUP BY category").fetchall()
    stats["leads_by_category"] = {r["category"]: r["cnt"] for r in rows}

    # Findings
    stats["total_findings"] = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    stats["total_connections"] = db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
    stats["total_leads"] = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    # Recent activity
    stats["recently_completed"] = db.execute(
        "SELECT COUNT(*) FROM leads WHERE completed_at > datetime('now', '-7 days')"
    ).fetchone()[0]
    stats["recent_findings"] = db.execute(
        "SELECT COUNT(*) FROM findings WHERE created_at > datetime('now', '-7 days')"
    ).fetchone()[0]

    # Searches performed
    stats["total_searches"] = db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
    rows = db.execute("SELECT source, COUNT(*) as cnt FROM search_log GROUP BY source").fetchall()
    stats["searches_by_source"] = {r["source"]: r["cnt"] for r in rows}

    # Sessions
    stats["total_sessions"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    # Leads in triage queue
    stats["pending_triage"] = db.execute(
        "SELECT COUNT(*) FROM leads WHERE status = 'pending_triage'"
    ).fetchone()[0]

    # Leads blocked by infra
    stats["blocked_by_infra"] = db.execute(
        "SELECT COUNT(*) FROM leads WHERE blocked_by_infra_id IS NOT NULL AND status = 'blocked'"
    ).fetchone()[0]

    # Infra requests
    stats["infra_open"] = db.execute(
        "SELECT COUNT(*) FROM infra_requests WHERE status = 'open'"
    ).fetchone()[0]
    stats["infra_active"] = db.execute(
        "SELECT COUNT(*) FROM infra_requests WHERE status IN ('evaluating', 'in_progress')"
    ).fetchone()[0]

    # Stale leads (lease expired)
    now = _utcnow().isoformat()
    stats["stale_leads"] = db.execute(
        "SELECT COUNT(*) FROM leads WHERE status = 'in_progress' AND lease_until IS NOT NULL AND lease_until < ?",
        (now,)
    ).fetchone()[0]

    db.close()
    return stats


# ── CLI ──────────────────────────────────────────────────────


def format_lead(lead, verbose=False):
    """Format a lead for display."""
    status_icons = {
        "open": "[ ]", "pending_triage": "[T]", "in_progress": "[~]",
        "completed": "[x]", "blocked": "[!]", "dead_end": "[-]",
    }
    icon = status_icons.get(lead["status"], "[?]")
    prio = {"critical": "!!!!", "high": "!!!", "medium": "!!", "low": "!"}.get(lead["priority"], "")

    line = f"{icon} #{lead['id']:>4} {prio:<4} [{(lead.get('category') or '?'):>10}] {lead['title']}"
    if lead.get("target_name"):
        line += f"  (target: {lead['target_name']})"

    if verbose:
        if lead.get("description"):
            line += f"\n       Description: {lead['description']}"
        if lead.get("source"):
            line += f"\n       Source: {lead['source']}"
        if lead.get("findings") and isinstance(lead["findings"], str):
            line += f"\n       Findings: {lead['findings'][:200]}"
        if lead.get("evidence"):
            for ev in lead["evidence"]:
                line += f"\n       Evidence [{ev['evidence_type']}]: {ev['evidence_ref']}"
        if lead.get("notes"):
            for n in lead["notes"]:
                line += f"\n       Note ({n['created_at']}): {n['note']}"
        if lead.get("related_leads"):
            for r in lead["related_leads"]:
                line += f"\n       Related: #{r['related_lead_id']} ({r['relation_type']}) - {r['title']}"

    return line


def main():
    parser = argparse.ArgumentParser(description="OSINT investigation lead tracker")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # add
    add_p = subparsers.add_parser("add", help="Add a new lead")
    add_p.add_argument("--title", "-t", required=True)
    add_p.add_argument("--description", "-d")
    add_p.add_argument("--category", "-c", choices=VALID_CATEGORIES)
    add_p.add_argument("--priority", "-p", choices=VALID_PRIORITIES, default="medium")
    add_p.add_argument("--source", "-s")
    add_p.add_argument("--target")
    add_p.add_argument("--evidence", "-e", nargs="+")
    add_p.add_argument("--related", "-r", nargs="+", type=int)
    add_p.add_argument("--thread-id", type=int, help="Investigation thread ID")
    add_p.add_argument("--depth-tier", choices=["scan", "standard", "deep_dive"], help="Investigation depth tier")
    add_p.add_argument("--recommended-skill", help="Recommended skill for this lead")

    # list
    list_p = subparsers.add_parser("list", help="List leads")
    list_p.add_argument("--status", choices=VALID_STATUSES)
    list_p.add_argument("--priority", choices=VALID_PRIORITIES)
    list_p.add_argument("--category", choices=VALID_CATEGORIES)
    list_p.add_argument("--target")
    list_p.add_argument("--thread-id", type=int, help="Filter by investigation thread")
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("-v", "--verbose", action="store_true")
    add_output_args(list_p)

    # show
    show_p = subparsers.add_parser("show", help="Show lead details")
    show_p.add_argument("id", type=int)
    add_output_args(show_p)

    # claim
    claim_p = subparsers.add_parser("claim", help="Claim a lead")
    claim_p.add_argument("id", type=int)

    # claim-next (atomic select + claim)
    claim_next_p = subparsers.add_parser("claim-next", help="Atomically select and claim the next open lead")
    claim_next_p.add_argument("--category", choices=VALID_CATEGORIES)
    claim_next_p.add_argument("--thread-id", type=int, help="Filter by investigation thread")
    claim_next_p.add_argument("--depth-tier", choices=["scan", "standard", "deep_dive"], help="Filter by depth tier")
    add_output_args(claim_next_p)

    # note
    note_p = subparsers.add_parser("note", help="Add a note")
    note_p.add_argument("id", type=int)
    note_p.add_argument("text")

    # complete
    comp_p = subparsers.add_parser("complete", help="Complete a lead")
    comp_p.add_argument("id", type=int)
    comp_p.add_argument("--findings", "-f", required=True)

    # block
    block_p = subparsers.add_parser("block", help="Block a lead")
    block_p.add_argument("id", type=int)
    block_p.add_argument("reason")

    # dead-end
    dead_p = subparsers.add_parser("dead-end", help="Mark as dead end")
    dead_p.add_argument("id", type=int)
    dead_p.add_argument("reason")

    # batch-dead-end
    batch_dead_p = subparsers.add_parser("batch-dead-end", help="Batch mark leads as dead end")
    batch_dead_p.add_argument("--ids-file", required=True, help="File with one lead ID per line")
    batch_dead_p.add_argument("--reason", required=True, help="Stop reason for all leads")

    # reopen
    reopen_p = subparsers.add_parser("reopen", help="Reopen a lead")
    reopen_p.add_argument("id", type=int)

    # search
    search_p = subparsers.add_parser("search", help="Full-text search")
    search_p.add_argument("query")
    add_output_args(search_p)

    # next
    next_p = subparsers.add_parser("next", help="Get next lead to investigate")
    next_p.add_argument("--category", choices=VALID_CATEGORIES)
    next_p.add_argument("--thread-id", type=int, help="Filter by investigation thread")
    add_output_args(next_p)

    # evidence-search
    ev_p = subparsers.add_parser("evidence", help="Find all items referencing an evidence ID")
    ev_p.add_argument("ref", help="Evidence reference (EFTA ID, file path, URL)")
    add_output_args(ev_p)

    # stats
    stats_p = subparsers.add_parser("stats", help="Show statistics")
    stats_p.add_argument("--session-stats", action="store_true", help="Include per-session performance metrics")
    add_output_args(stats_p)

    # stale — list stale leads
    stale_p = subparsers.add_parser("stale", help="List leads with expired leases")
    add_output_args(stale_p)

    # recover-stale — reset stale leads to open
    recover_p = subparsers.add_parser("recover-stale", help="Reset stale leads to open")

    # triage-log — review triage decisions
    tlog_p = subparsers.add_parser("triage-log", help="Review triage decisions (dead-ends and promotions)")
    tlog_p.add_argument("--status", choices=["dead_end", "open", "all"], default="all",
                        help="Filter by post-triage status")
    tlog_p.add_argument("--limit", type=int, default=30)
    tlog_p.add_argument("--missing-rationale", action="store_true",
                        help="Show only dead-ended leads with no rationale")
    add_output_args(tlog_p)

    # tier — tag a lead with investigation depth
    tier_p = subparsers.add_parser("tier", help="Tag a lead with investigation depth tier")
    tier_p.add_argument("id", type=int, help="Lead ID")
    tier_p.add_argument("depth", choices=["scan", "standard", "deep_dive"], help="Investigation depth tier")

    # tier-list — list leads by tier
    tier_list_p = subparsers.add_parser("tier-list", help="List leads by investigation depth tier")
    tier_list_p.add_argument("--tier", choices=["scan", "standard", "deep_dive"], help="Filter by tier")
    tier_list_p.add_argument("--limit", type=int, default=50)
    add_output_args(tier_list_p)

    # thread
    thread_p = subparsers.add_parser("thread", help="Manage investigation threads")
    thread_sub = thread_p.add_subparsers(dest="thread_command")
    thread_list_p = thread_sub.add_parser("list", help="List all threads")
    add_output_args(thread_list_p)
    thread_add_p = thread_sub.add_parser("add", help="Create a thread")
    thread_add_p.add_argument("--title", required=True)
    thread_add_p.add_argument("--description")
    thread_seed_p = thread_sub.add_parser("seed", help="Seed initial investigation threads")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "add":
        lead_id = add_lead(
            title=args.title, description=args.description, category=args.category,
            priority=args.priority, source=args.source, target_name=args.target,
            evidence=args.evidence, related_leads=args.related,
            thread_id=getattr(args, "thread_id", None),
            depth_tier=getattr(args, "depth_tier", None),
            recommended_skill=getattr(args, "recommended_skill", None),
        )
        print(f"Created lead #{lead_id}: {args.title}")

    elif args.command == "list":
        leads = list_leads(
            status=args.status, priority=args.priority,
            category=args.category, target=args.target, limit=args.limit,
            thread_id=getattr(args, "thread_id", None),
        )
        if not write_output(leads, args, summary=f"leads list: {len(leads)} results"):
            if not leads:
                print("No leads found matching filters.")
            else:
                for lead in leads:
                    print(format_lead(lead, verbose=args.verbose))

    elif args.command == "show":
        lead = get_lead(args.id)
        if not lead:
            print(f"Lead #{args.id} not found.")
            sys.exit(1)
        if not write_output(lead, args, summary=f"lead #{args.id}"):
            print(format_lead(lead, verbose=True))

    elif args.command == "claim":
        if claim_lead(args.id):
            print(f"Claimed lead #{args.id}")
        else:
            print(f"Could not claim lead #{args.id} (may not be open)")

    elif args.command == "claim-next":
        lead = claim_next_lead(
            category=getattr(args, "category", None),
            thread_id=getattr(args, "thread_id", None),
            depth_tier=getattr(args, "depth_tier", None),
        )
        if lead:
            lead_full = get_lead(lead["id"])
            if not write_output(lead_full, args, summary=f"claimed lead #{lead['id']}"):
                print(f"Claimed lead #{lead['id']}:")
                print(format_lead(lead_full, verbose=True))
        else:
            cat_msg = f" in category '{args.category}'" if getattr(args, "category", None) else ""
            print(f"No open leads{cat_msg} to claim.")

    elif args.command == "note":
        add_note(args.id, args.text)
        print(f"Added note to lead #{args.id}")

    elif args.command == "complete":
        complete_lead(args.id, args.findings)
        print(f"Completed lead #{args.id}")

    elif args.command == "block":
        block_lead(args.id, args.reason)
        print(f"Blocked lead #{args.id}")

    elif args.command == "dead-end":
        dead_end_lead(args.id, args.reason)
        print(f"Marked lead #{args.id} as dead end")

    elif args.command == "batch-dead-end":
        ids_path = Path(args.ids_file)
        if not ids_path.exists():
            print(f"File not found: {args.ids_file}")
            sys.exit(1)
        lead_ids = [int(line.strip()) for line in ids_path.read_text().splitlines() if line.strip().isdigit()]
        if not lead_ids:
            print("No valid lead IDs found in file.")
            sys.exit(1)
        count = batch_dead_end_leads(lead_ids, args.reason)
        print(f"Dead-ended {count}/{len(lead_ids)} leads with reason: {args.reason}")

    elif args.command == "reopen":
        reopen_lead(args.id)
        print(f"Reopened lead #{args.id}")

    elif args.command == "search":
        results = search_leads(args.query)
        if not write_output(results, args, summary=f"lead search '{args.query}': {len(results)} results"):
            if not results:
                print(f"No leads matching '{args.query}'")
            else:
                print(f"Found {len(results)} leads matching '{args.query}':")
                for r in results:
                    print(format_lead(r))

    elif args.command == "next":
        lead = get_next_lead(args.category, thread_id=getattr(args, "thread_id", None))
        if lead:
            lead_full = get_lead(lead["id"])
            if not write_output(lead_full, args, summary=f"next lead: #{lead['id']}"):
                print("Next lead to investigate:")
                print(format_lead(lead_full, verbose=True))
        else:
            cat_msg = f" in category '{args.category}'" if args.category else ""
            print(f"No open leads{cat_msg}.")

    elif args.command == "evidence":
        results = find_by_evidence(args.ref)
        if not write_output(results, args, summary=f"evidence '{args.ref}': {len(results.get('leads',[]))} leads, {len(results.get('findings',[]))} findings"):
            if results["leads"]:
                print(f"Leads referencing '{args.ref}':")
                for r in results["leads"]:
                    print(f"  Lead #{r['id']}: {r['title']} [{r['status']}]")
            if results["findings"]:
                print(f"Findings referencing '{args.ref}':")
                for r in results["findings"]:
                    print(f"  Finding #{r['id']}: {r['target_name']} - {r['summary']}")
            if not results["leads"] and not results["findings"]:
                print(f"No leads or findings reference '{args.ref}'")

    elif args.command == "stats":
        stats = get_stats()
        if not write_output(stats, args, summary=f"stats: {stats['total_leads']} leads, {stats['total_findings']} findings"):
            print(f"Total leads: {stats['total_leads']}")
            print(f"Total findings: {stats['total_findings']}")
            print(f"Total connections: {stats['total_connections']}")

            if stats.get("leads_by_status"):
                print(f"\nLeads by status:")
                for s, c in sorted(stats["leads_by_status"].items(), key=lambda x: (x[0] is None, x[0] or '')):
                    print(f"  {s or '(none)'}: {c}")
            if stats.get("open_by_priority"):
                print(f"\nOpen/in-progress by priority:")
                for p, c in sorted(stats["open_by_priority"].items(), key=lambda x: (x[0] is None, x[0] or '')):
                    print(f"  {p or '(none)'}: {c}")
            if stats.get("leads_by_category"):
                print(f"\nLeads by category:")
                for cat, c in sorted(stats["leads_by_category"].items(), key=lambda x: (x[0] is None, x[0])):
                    print(f"  {cat or '(none)'}: {c}")

            print(f"\nCompleted in last 7 days: {stats['recently_completed']}")
            print(f"Findings in last 7 days: {stats['recent_findings']}")
            print(f"Total searches logged: {stats['total_searches']}")
            print(f"Total sessions: {stats['total_sessions']}")

            # Queue status
            queues = []
            if stats.get('pending_triage', 0) > 0:
                queues.append(f"{stats['pending_triage']} leads pending triage → /triage-leads")
            if stats.get('infra_open', 0) > 0:
                queues.append(f"{stats['infra_open']} infra requests open → /build-infra")
            if stats.get('infra_active', 0) > 0:
                queues.append(f"{stats['infra_active']} infra requests in progress")
            if stats.get('blocked_by_infra', 0) > 0:
                queues.append(f"{stats['blocked_by_infra']} leads blocked on infra")
            if stats.get('stale_leads', 0) > 0:
                queues.append(f"{stats['stale_leads']} stale leads (lease expired) → recover-stale")
            if queues:
                print(f"\nQueues:")
                for q in queues:
                    print(f"  ** {q} **")

            if args.session_stats:
                sessions = get_session_stats(limit=10)
                if sessions:
                    print(f"\nRecent session performance:")
                    print(f"  {'ID':>5} {'Agent':<20} {'Skill':<20} {'F':>3} {'C':>3} {'L':>3} {'R':>3}")
                    for s in sessions:
                        print(f"  {s['id']:>5} {(s['agent_id'] or '?')[:20]:<20} {(s['skill_invoked'] or '?')[:20]:<20} "
                              f"{s.get('findings_created') or 0:>3} {s.get('connections_created') or 0:>3} "
                              f"{s.get('leads_created') or 0:>3} {s.get('retractions') or 0:>3}")

    elif args.command == "stale":
        stale = list_stale_leads()
        if not write_output(stale, args, summary=f"{len(stale)} stale leads"):
            if not stale:
                print("No stale leads (all leases current).")
            else:
                print(f"{len(stale)} stale leads (lease expired):")
                for s in stale:
                    print(f"  #{s['id']:>4} [{s['priority']:<8}] {s['title']}")
                    print(f"         claimed_by={s.get('claimed_by') or '?'}  lease expired: {s['lease_until']}")

    elif args.command == "recover-stale":
        recovered = mark_stale_leads()
        if not recovered:
            print("No stale leads to recover.")
        else:
            print(f"Recovered {len(recovered)} stale leads:")
            for r in recovered:
                print(f"  #{r['id']:>4} → open: {r['title']}")

    elif args.command == "triage-log":
        db = get_db()
        if args.missing_rationale:
            rows = db.execute("""
                SELECT id, title, target_name, status, stop_reason, triage_rationale,
                       triaged_by, triaged_at, completed_at
                FROM leads WHERE status='dead_end'
                    AND (triage_rationale IS NULL OR triage_rationale = '')
                ORDER BY completed_at DESC NULLS LAST
                LIMIT ?
            """, (args.limit,)).fetchall()
        else:
            status_clause = ""
            if args.status == "dead_end":
                status_clause = "AND status='dead_end'"
            elif args.status == "open":
                status_clause = "AND status='open'"
            rows = db.execute(f"""
                SELECT id, title, target_name, status, stop_reason, triage_rationale,
                       triaged_by, triaged_at, depth_tier, recommended_skill
                FROM leads WHERE triaged_at IS NOT NULL {status_clause}
                ORDER BY triaged_at DESC
                LIMIT ?
            """, (args.limit,)).fetchall()
        results = [dict(r) for r in rows]
        db.close()
        if not write_output(results, args, summary=f"triage log: {len(results)} entries"):
            if not results:
                if args.missing_rationale:
                    print("All dead-ended leads have rationales.")
                else:
                    print("No triage log entries found.")
            else:
                for r in results:
                    status_tag = f"[{r['status']}]"
                    tier = r.get('depth_tier') or ''
                    skill = r.get('recommended_skill') or ''
                    print(f"#{r['id']:>5} {status_tag:<12} {r['title']}")
                    if r.get('target_name'):
                        print(f"        target: {r['target_name']}")
                    if tier or skill:
                        print(f"        tier={tier}  skill={skill}")
                    if r.get('stop_reason'):
                        print(f"        stop_reason: {r['stop_reason']}")
                    if r.get('triage_rationale'):
                        print(f"        rationale: {r['triage_rationale']}")
                    else:
                        print(f"        rationale: (MISSING)")
                    print(f"        triaged_by={r.get('triaged_by') or '?'}  at={r.get('triaged_at') or '?'}")
                    print()

    elif args.command == "tier":
        try:
            from tools.tag_manager import add_tag, remove_tag
        except ImportError:
            from tag_manager import add_tag, remove_tag
        for old_tier in ["scan", "standard", "deep_dive"]:
            remove_tag("leads", args.id, "operational", f"tier:{old_tier}")
        if add_tag("leads", args.id, "operational", f"tier:{args.depth}", created_by="lead_tracker"):
            print(f"Tagged lead #{args.id} with tier={args.depth}")
        else:
            print(f"Lead #{args.id} already tagged with tier={args.depth}")

    elif args.command == "tier-list":
        try:
            from tools.tag_manager import find_tags
        except ImportError:
            from tag_manager import find_tags
        tier_filter = f"tier:{args.tier}" if args.tier else "tier:*"
        tags = find_tags(tag_type="operational", tag_value=tier_filter, table_name="leads", limit=args.limit)
        if not tags:
            tier_msg = f" at tier={args.tier}" if args.tier else ""
            print(f"No leads tagged with investigation tiers{tier_msg}.")
        else:
            # Group by tier
            by_tier = {}
            lead_ids = []
            for t in tags:
                tier_val = t["tag_value"].replace("tier:", "")
                by_tier.setdefault(tier_val, []).append(t["record_id"])
                lead_ids.append(t["record_id"])

            # Fetch lead details
            db = get_db()
            leads_by_id = {}
            for lid in lead_ids:
                row = db.execute("SELECT id, title, status, priority, category, target_name FROM leads WHERE id = ?", (lid,)).fetchone()
                if row:
                    leads_by_id[lid] = dict(row)
            db.close()

            tier_data = []
            for tier_name in ["scan", "standard", "deep_dive"]:
                if tier_name in by_tier:
                    print(f"\n=== Tier: {tier_name} ({len(by_tier[tier_name])} leads) ===")
                    for lid in by_tier[tier_name]:
                        lead = leads_by_id.get(lid)
                        if lead:
                            tier_data.append({**lead, "tier": tier_name})
                            status_icon = {"open": "[ ]", "in_progress": "[~]", "completed": "[x]"}.get(lead["status"], "[?]")
                            print(f"  {status_icon} #{lead['id']:>4} [{lead.get('priority', '?'):>8}] {lead['title']}")

            if hasattr(args, 'output'):
                write_output(tier_data, args, summary=f"tier-list: {len(tier_data)} leads")

    elif args.command == "thread":
        db = get_db()
        if args.thread_command == "list":
            rows = db.execute("""
                SELECT t.*,
                    (SELECT COUNT(*) FROM leads WHERE thread_id = t.id) as lead_count,
                    (SELECT COUNT(*) FROM findings WHERE thread_id = t.id) as finding_count
                FROM investigation_threads t ORDER BY t.id
            """).fetchall()
            threads = [dict(r) for r in rows]
            db.close()
            if hasattr(args, 'output') and not write_output(threads, args, summary=f"threads: {len(threads)}"):
                if not threads:
                    print("No investigation threads.")
                else:
                    for t in threads:
                        print(f"  Thread #{t['id']}: {t['title']} [{t['status']}] — {t['lead_count']} leads, {t['finding_count']} findings")
                        if t.get("description"):
                            print(f"    {t['description']}")
            elif not threads:
                print("No investigation threads.")
            else:
                for t in threads:
                    print(f"  Thread #{t['id']}: {t['title']} [{t['status']}] — {t['lead_count']} leads, {t['finding_count']} findings")
                    if t.get("description"):
                        print(f"    {t['description']}")

        elif args.thread_command == "add":
            cursor = db.execute(
                "INSERT INTO investigation_threads (title, description) VALUES (?, ?)",
                (args.title, args.description),
            )
            db.commit()
            db.close()
            print(f"Created thread #{cursor.lastrowid}: {args.title}")

        elif args.thread_command == "seed":
            from tools.investigation_context import get_active_profile
            profile = get_active_profile()
            if not profile.threads:
                print("No threads defined in active investigation profile.")
                db.close()
                return
            created = 0
            for thread_def in profile.threads:
                title = thread_def.get("name", "")
                desc = thread_def.get("description", "")
                if not title:
                    continue
                existing = db.execute("SELECT id FROM investigation_threads WHERE title = ?", (title,)).fetchone()
                if not existing:
                    profile_id = profile.name
                    db.execute(
                        "INSERT INTO investigation_threads (title, description, profile_id) VALUES (?, ?, ?)",
                        (title, desc, profile_id)
                    )
                    created += 1
            db.commit()
            db.close()
            print(f"Seeded {created} threads ({len(profile.threads) - created} already existed)")

        else:
            thread_p.print_help()


if __name__ == "__main__":
    main()
