#!/usr/bin/env python3
"""
Unified corporate registry query tool.

Searches across all ingested state/country corporate registries with a
standardized schema. Data is stored in registry.db.

Usage:
    python tools/query_registry.py search "Jeffrey Epstein"
    python tools/query_registry.py search "LSJE" --jurisdiction fl
    python tools/query_registry.py search "Financial Trust" --jurisdiction vi
    python tools/query_registry.py entity <entity_id>
    python tools/query_registry.py officers "Darren Indyke"
    python tools/query_registry.py address "457 Madison"
    python tools/query_registry.py agent "CT Corporation"
    python tools/query_registry.py filings <entity_id>
    python tools/query_registry.py stats
    python tools/query_registry.py jurisdictions
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import registry_db_path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DB_PATH = registry_db_path()


def get_db():
    """Get a database connection, creating schema if needed."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(db)
    return db


def _ensure_schema(db):
    """Create the unified corporate registry schema."""
    db.executescript("""
        -- ══════════════════════════════════════════════════════════
        -- REGISTRY ENTITIES: One row per corporate entity
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_jurisdiction TEXT NOT NULL,      -- fl, ny, vi, de, vg, etc.
            source_id TEXT NOT NULL,                -- Original ID from source (corp number, DOS ID, etc.)
            entity_name TEXT NOT NULL,
            entity_type TEXT,                       -- corp, llc, lp, llp, nonprofit, trust, foreign_corp, etc.
            status TEXT,                            -- active, inactive, dissolved, cancelled, void
            formation_date TEXT,                    -- ISO date
            dissolution_date TEXT,                  -- ISO date (if dissolved)
            last_filing_date TEXT,                  -- ISO date
            ein TEXT,                               -- Federal EIN if available
            state_of_formation TEXT,                -- Where originally formed (may differ from filing state)
            purpose TEXT,                           -- Business purpose (if available)
            -- Principal address
            principal_address TEXT,
            principal_city TEXT,
            principal_state TEXT,
            principal_zip TEXT,
            principal_country TEXT,
            -- Mailing address
            mailing_address TEXT,
            mailing_city TEXT,
            mailing_state TEXT,
            mailing_zip TEXT,
            mailing_country TEXT,
            -- Metadata
            source_url TEXT,                        -- URL to source registry page
            raw_data TEXT,                          -- JSON blob of all original fields
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_jurisdiction, source_id)
        );

        -- ══════════════════════════════════════════════════════════
        -- REGISTRY OFFICERS: Officers, directors, managers, etc.
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_officers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES registry_entities(id),
            officer_name TEXT NOT NULL,
            title TEXT,                             -- president, treasurer, director, secretary, VP, manager, member, etc.
            officer_type TEXT,                      -- person, corporation
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT,
            -- For tracking changes over time
            effective_date TEXT,                    -- When this officer was recorded (from filing)
            end_date TEXT,                          -- When they were removed (if known)
            source_filing_id INTEGER REFERENCES registry_filings(id),
            UNIQUE(entity_id, officer_name, title, effective_date)
        );

        -- ══════════════════════════════════════════════════════════
        -- REGISTRY AGENTS: Registered agents
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES registry_entities(id),
            agent_name TEXT NOT NULL,
            agent_type TEXT,                        -- person, corporation
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT,
            effective_date TEXT,
            end_date TEXT,
            UNIQUE(entity_id, agent_name, effective_date)
        );

        -- ══════════════════════════════════════════════════════════
        -- REGISTRY FILINGS: Filing/event history
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES registry_entities(id),
            filing_type TEXT,                       -- annual_report, amendment, name_change, dissolution, etc.
            filing_date TEXT,
            effective_date TEXT,
            description TEXT,
            entity_name_at_time TEXT,               -- Name at time of filing (for tracking name changes)
            raw_data TEXT,                          -- JSON of original fields
            UNIQUE(entity_id, filing_type, filing_date)
        );

        -- ══════════════════════════════════════════════════════════
        -- REGISTRY NAME HISTORY: Track name changes
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_name_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES registry_entities(id),
            previous_name TEXT NOT NULL,
            change_date TEXT,
            filing_id INTEGER REFERENCES registry_filings(id)
        );

        -- ══════════════════════════════════════════════════════════
        -- INGEST LOG: Track what's been ingested and when
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS registry_ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jurisdiction TEXT NOT NULL,
            source_type TEXT,                       -- sftp_bulk, api, scrape
            file_name TEXT,
            record_count INTEGER,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        );

        -- ══════════════════════════════════════════════════════════
        -- INDEXES
        -- ══════════════════════════════════════════════════════════
        CREATE INDEX IF NOT EXISTS idx_re_jurisdiction ON registry_entities(source_jurisdiction);
        CREATE INDEX IF NOT EXISTS idx_re_name ON registry_entities(entity_name);
        CREATE INDEX IF NOT EXISTS idx_re_status ON registry_entities(status);
        CREATE INDEX IF NOT EXISTS idx_re_ein ON registry_entities(ein);
        CREATE INDEX IF NOT EXISTS idx_re_source_id ON registry_entities(source_id);
        CREATE INDEX IF NOT EXISTS idx_re_formation ON registry_entities(formation_date);

        CREATE INDEX IF NOT EXISTS idx_ro_entity ON registry_officers(entity_id);
        CREATE INDEX IF NOT EXISTS idx_ro_name ON registry_officers(officer_name);
        CREATE INDEX IF NOT EXISTS idx_ro_title ON registry_officers(title);

        CREATE INDEX IF NOT EXISTS idx_ra_entity ON registry_agents(entity_id);
        CREATE INDEX IF NOT EXISTS idx_ra_name ON registry_agents(agent_name);

        CREATE INDEX IF NOT EXISTS idx_rf_entity ON registry_filings(entity_id);
        CREATE INDEX IF NOT EXISTS idx_rf_date ON registry_filings(filing_date);
        CREATE INDEX IF NOT EXISTS idx_rf_type ON registry_filings(filing_type);

        CREATE INDEX IF NOT EXISTS idx_rnh_entity ON registry_name_history(entity_id);
        CREATE INDEX IF NOT EXISTS idx_rnh_name ON registry_name_history(previous_name);

        -- ══════════════════════════════════════════════════════════
        -- UCC FILINGS: Uniform Commercial Code secured transactions
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS ucc_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_jurisdiction TEXT NOT NULL,      -- fl, nm, ny, de, etc.
            filing_number TEXT NOT NULL,
            filing_type TEXT,                       -- initial, amendment, continuation, termination, assignment
            filing_date TEXT,                       -- ISO date
            lapse_date TEXT,                        -- ISO date (when filing expires)
            status TEXT,                            -- active, lapsed, terminated
            file_number TEXT,                       -- Original/initial filing number (for amendments)
            raw_data TEXT,                          -- JSON blob of all original fields
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_jurisdiction, filing_number)
        );

        -- ══════════════════════════════════════════════════════════
        -- UCC DEBTORS: Parties who pledged collateral
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS ucc_debtors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER NOT NULL REFERENCES ucc_filings(id),
            debtor_name TEXT NOT NULL,
            debtor_type TEXT,                       -- individual, organization
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT,
            registry_entity_id INTEGER REFERENCES registry_entities(id)  -- Cross-link to corporate registry
        );

        -- ══════════════════════════════════════════════════════════
        -- UCC SECURED PARTIES: Creditors / lienholders
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS ucc_secured_parties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER NOT NULL REFERENCES ucc_filings(id),
            party_name TEXT NOT NULL,
            party_type TEXT,                        -- individual, organization
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT
        );

        -- ══════════════════════════════════════════════════════════
        -- UCC COLLATERAL: What was pledged
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS ucc_collateral (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER NOT NULL REFERENCES ucc_filings(id),
            description TEXT NOT NULL               -- Full-text collateral description
        );

        -- ══════════════════════════════════════════════════════════
        -- UCC FILING HISTORY: Amendments, continuations, terminations
        -- ══════════════════════════════════════════════════════════
        CREATE TABLE IF NOT EXISTS ucc_filing_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id INTEGER NOT NULL REFERENCES ucc_filings(id),
            action_type TEXT,                       -- amendment, continuation, termination, assignment
            action_date TEXT,                       -- ISO date
            action_filing_number TEXT,              -- The filing number of the amendment itself
            description TEXT,
            raw_data TEXT
        );

        -- UCC INDEXES
        CREATE INDEX IF NOT EXISTS idx_uf_jurisdiction ON ucc_filings(source_jurisdiction);
        CREATE INDEX IF NOT EXISTS idx_uf_number ON ucc_filings(filing_number);
        CREATE INDEX IF NOT EXISTS idx_uf_date ON ucc_filings(filing_date);
        CREATE INDEX IF NOT EXISTS idx_uf_status ON ucc_filings(status);
        CREATE INDEX IF NOT EXISTS idx_uf_file ON ucc_filings(file_number);

        CREATE INDEX IF NOT EXISTS idx_ud_filing ON ucc_debtors(filing_id);
        CREATE INDEX IF NOT EXISTS idx_ud_name ON ucc_debtors(debtor_name);
        CREATE INDEX IF NOT EXISTS idx_ud_registry ON ucc_debtors(registry_entity_id);

        CREATE INDEX IF NOT EXISTS idx_usp_filing ON ucc_secured_parties(filing_id);
        CREATE INDEX IF NOT EXISTS idx_usp_name ON ucc_secured_parties(party_name);

        CREATE INDEX IF NOT EXISTS idx_uc_filing ON ucc_collateral(filing_id);

        CREATE INDEX IF NOT EXISTS idx_ufh_filing ON ucc_filing_history(filing_id);
        CREATE INDEX IF NOT EXISTS idx_ufh_date ON ucc_filing_history(action_date);
    """)

    # FTS for entity name search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE registry_entities_fts USING fts5(
                entity_name, principal_address, purpose,
                content=registry_entities, content_rowid=id
            )
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for officer name search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE registry_officers_fts USING fts5(
                officer_name, address,
                content=registry_officers, content_rowid=id
            )
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for UCC debtor search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE ucc_debtors_fts USING fts5(
                debtor_name, address,
                content=ucc_debtors, content_rowid=id
            )
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for UCC secured party search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE ucc_secured_parties_fts USING fts5(
                party_name, address,
                content=ucc_secured_parties, content_rowid=id
            )
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass

    # FTS for UCC collateral search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE ucc_collateral_fts USING fts5(
                description,
                content=ucc_collateral, content_rowid=id
            )
        """)
        db.commit()
    except sqlite3.OperationalError:
        pass


def _rebuild_fts(db):
    """Rebuild FTS indexes after bulk insert."""
    db.execute("INSERT INTO registry_entities_fts(registry_entities_fts) VALUES('rebuild')")
    db.execute("INSERT INTO registry_officers_fts(registry_officers_fts) VALUES('rebuild')")
    # Rebuild UCC FTS indexes if tables exist
    try:
        db.execute("INSERT INTO ucc_debtors_fts(ucc_debtors_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("INSERT INTO ucc_secured_parties_fts(ucc_secured_parties_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("INSERT INTO ucc_collateral_fts(ucc_collateral_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    db.commit()


def format_entity(row, verbose=False):
    """Format an entity row for display."""
    lines = []
    juris = row["source_jurisdiction"].upper()
    status = row["status"] or "?"
    etype = row["entity_type"] or "?"
    lines.append(f"  [{juris}] {row['entity_name']} ({etype}, {status})")
    lines.append(f"    ID: {row['id']} | Source: {row['source_id']}")

    if row["formation_date"]:
        lines.append(f"    Formed: {row['formation_date']}")
    if row["dissolution_date"]:
        lines.append(f"    Dissolved: {row['dissolution_date']}")
    if row["ein"]:
        lines.append(f"    EIN: {row['ein']}")
    if row["state_of_formation"] and row["state_of_formation"] != row["source_jurisdiction"]:
        lines.append(f"    State of formation: {row['state_of_formation']}")

    if row["principal_address"]:
        addr = row["principal_address"]
        if row["principal_city"]:
            addr += f", {row['principal_city']}"
        if row["principal_state"]:
            addr += f", {row['principal_state']}"
        if row["principal_zip"]:
            addr += f" {row['principal_zip']}"
        lines.append(f"    Address: {addr}")

    if row["last_filing_date"]:
        lines.append(f"    Last filing: {row['last_filing_date']}")

    if row["source_url"]:
        lines.append(f"    URL: {row['source_url']}")

    return "\n".join(lines)


def cmd_search(args):
    """Search entities by name."""
    db = get_db()

    if args.jurisdiction:
        juris_filter = "AND re.source_jurisdiction = ?"
        params_extra = [args.jurisdiction.lower()]
    else:
        juris_filter = ""
        params_extra = []

    if args.exact:
        query = f"""
            SELECT re.* FROM registry_entities re
            WHERE re.entity_name LIKE ?
            {juris_filter}
            ORDER BY re.entity_name
            LIMIT ?
        """
        params = [f"%{args.query}%"] + params_extra + [args.limit]
    else:
        query = f"""
            SELECT re.* FROM registry_entities_fts fts
            JOIN registry_entities re ON fts.rowid = re.id
            WHERE registry_entities_fts MATCH ?
            {juris_filter}
            ORDER BY rank
            LIMIT ?
        """
        params = [args.query] + params_extra + [args.limit]

    try:
        rows = db.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        # FTS may not be populated yet, fall back to LIKE
        query = f"""
            SELECT re.* FROM registry_entities re
            WHERE re.entity_name LIKE ?
            {juris_filter}
            ORDER BY re.entity_name
            LIMIT ?
        """
        params = [f"%{args.query}%"] + params_extra + [args.limit]
        rows = db.execute(query, params).fetchall()

    if not write_output([dict(r) for r in rows], args, summary=f"registry search '{args.query}'"):
        if getattr(args, "json_out", False):
            print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        else:
            print(f"Found {len(rows)} entities matching '{args.query}'")
            print()
            for row in rows:
                print(format_entity(row))
                # Show officers inline
                officers = db.execute(
                    "SELECT officer_name, title FROM registry_officers WHERE entity_id = ? LIMIT 6",
                    [row["id"]]
                ).fetchall()
                if officers:
                    for o in officers:
                        title = o["title"] or "?"
                        print(f"    Officer: {o['officer_name']} ({title})")
                # Show registered agent
                agent = db.execute(
                    "SELECT agent_name, address, city, state FROM registry_agents WHERE entity_id = ? ORDER BY effective_date DESC LIMIT 1",
                    [row["id"]]
                ).fetchone()
                if agent:
                    agent_addr = agent["address"] or ""
                    if agent["city"]:
                        agent_addr += f", {agent['city']}"
                    if agent["state"]:
                        agent_addr += f", {agent['state']}"
                    print(f"    Agent: {agent['agent_name']}" + (f" ({agent_addr})" if agent_addr else ""))
                print()


def cmd_entity(args):
    """Get full entity details by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM registry_entities WHERE id = ?", [args.entity_id]).fetchone()
    if not row:
        print(f"Entity {args.entity_id} not found")
        return

    # Gather related data
    officers = db.execute(
        "SELECT * FROM registry_officers WHERE entity_id = ? ORDER BY title",
        [args.entity_id]
    ).fetchall()
    agents = db.execute(
        "SELECT * FROM registry_agents WHERE entity_id = ? ORDER BY effective_date DESC",
        [args.entity_id]
    ).fetchall()
    filings = db.execute(
        "SELECT * FROM registry_filings WHERE entity_id = ? ORDER BY filing_date DESC LIMIT 20",
        [args.entity_id]
    ).fetchall()
    names = db.execute(
        "SELECT * FROM registry_name_history WHERE entity_id = ? ORDER BY change_date DESC",
        [args.entity_id]
    ).fetchall()

    data = {
        "entity": dict(row),
        "officers": [dict(o) for o in officers],
        "agents": [dict(a) for a in agents],
        "filings": [dict(f) for f in filings],
        "name_history": [dict(n) for n in names],
    }

    if write_output(data, args, summary=f"registry entity {args.entity_id}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    print(format_entity(row, verbose=True))
    print()
    if officers:
        print("  Officers:")
        for o in officers:
            title = o["title"] or "?"
            otype = f" [{o['officer_type']}]" if o["officer_type"] else ""
            addr = o["address"] or ""
            if o["city"]:
                addr += f", {o['city']}"
            if o["state"]:
                addr += f", {o['state']}"
            if o["zip"]:
                addr += f" {o['zip']}"
            print(f"    {o['officer_name']} ({title}){otype}")
            if addr:
                print(f"      Address: {addr}")
            if o["effective_date"]:
                period = f"From: {o['effective_date']}"
                if o["end_date"]:
                    period += f" To: {o['end_date']}"
                print(f"      {period}")
    if agents:
        print("\n  Registered Agents:")
        for a in agents:
            atype = f" [{a['agent_type']}]" if a["agent_type"] else ""
            addr = a["address"] or ""
            if a["city"]:
                addr += f", {a['city']}"
            if a["state"]:
                addr += f", {a['state']}"
            print(f"    {a['agent_name']}{atype}")
            if addr:
                print(f"      Address: {addr}")
    if filings:
        print(f"\n  Filing History ({len(filings)} most recent):")
        for f in filings:
            desc = f["description"] or f["filing_type"] or "?"
            date = f["filing_date"] or "?"
            name_note = ""
            if f["entity_name_at_time"] and f["entity_name_at_time"] != row["entity_name"]:
                name_note = f" (as: {f['entity_name_at_time']})"
            print(f"    {date}: {desc}{name_note}")
    if names:
        print("\n  Name History:")
        for n in names:
            print(f"    {n['change_date'] or '?'}: {n['previous_name']}")


def cmd_officers(args):
    """Search officers by name."""
    db = get_db()

    try:
        rows = db.execute("""
            SELECT o.*, re.entity_name, re.source_jurisdiction, re.source_id, re.status
            FROM registry_officers_fts fts
            JOIN registry_officers o ON fts.rowid = o.id
            JOIN registry_entities re ON o.entity_id = re.id
            WHERE registry_officers_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, [args.name, args.limit]).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute("""
            SELECT o.*, re.entity_name, re.source_jurisdiction, re.source_id, re.status
            FROM registry_officers o
            JOIN registry_entities re ON o.entity_id = re.id
            WHERE o.officer_name LIKE ?
            ORDER BY o.officer_name
            LIMIT ?
        """, [f"%{args.name}%", args.limit]).fetchall()

    if write_output([dict(r) for r in rows], args, summary=f"officers search '{args.name}'"):
        return

    print(f"Found {len(rows)} officer records matching '{args.name}'")
    print()
    for r in rows:
        juris = r["source_jurisdiction"].upper()
        title = r["title"] or "?"
        status = r["status"] or "?"
        print(f"  {r['officer_name']} ({title})")
        print(f"    Entity: [{juris}] {r['entity_name']} ({status})")
        addr = r["address"] or ""
        if r["city"]:
            addr += f", {r['city']}"
        if r["state"]:
            addr += f", {r['state']}"
        if addr:
            print(f"    Address: {addr}")
        print()


def cmd_address(args):
    """Search entities and officers by address."""
    db = get_db()

    pattern = f"%{args.query}%"

    # Search entity addresses
    entity_rows = db.execute("""
        SELECT * FROM registry_entities
        WHERE principal_address LIKE ?
           OR mailing_address LIKE ?
        ORDER BY entity_name
        LIMIT ?
    """, [pattern, pattern, args.limit]).fetchall()

    # Search officer addresses
    officer_rows = db.execute("""
        SELECT o.*, re.entity_name, re.source_jurisdiction
        FROM registry_officers o
        JOIN registry_entities re ON o.entity_id = re.id
        WHERE o.address LIKE ?
        ORDER BY o.officer_name
        LIMIT ?
    """, [pattern, args.limit]).fetchall()

    # Search agent addresses
    agent_rows = db.execute("""
        SELECT a.*, re.entity_name, re.source_jurisdiction
        FROM registry_agents a
        JOIN registry_entities re ON a.entity_id = re.id
        WHERE a.address LIKE ?
        ORDER BY a.agent_name
        LIMIT ?
    """, [pattern, args.limit]).fetchall()

    data = {
        "entities": [dict(r) for r in entity_rows],
        "officers": [dict(r) for r in officer_rows],
        "agents": [dict(r) for r in agent_rows],
    }
    if write_output(data, args, summary=f"address search '{args.query}'"):
        return

    print(f"Address search for '{args.query}':")
    if entity_rows:
        print(f"\n  Entities ({len(entity_rows)}):")
        for row in entity_rows:
            print(format_entity(row))
            print()

    if officer_rows:
        print(f"\n  Officers ({len(officer_rows)}):")
        for r in officer_rows:
            juris = r["source_jurisdiction"].upper()
            print(f"    {r['officer_name']} ({r['title'] or '?'})")
            print(f"      Entity: [{juris}] {r['entity_name']}")
            print(f"      Address: {r['address']}")
            print()

    if agent_rows:
        print(f"\n  Registered Agents ({len(agent_rows)}):")
        for r in agent_rows:
            juris = r["source_jurisdiction"].upper()
            print(f"    {r['agent_name']}")
            print(f"      Entity: [{juris}] {r['entity_name']}")
            print(f"      Address: {r['address']}")
            print()

    total = len(entity_rows) + len(officer_rows) + len(agent_rows)
    if total == 0:
        print("  No results found.")


def cmd_agent(args):
    """Search by registered agent name."""
    db = get_db()

    rows = db.execute("""
        SELECT a.*, re.entity_name, re.source_jurisdiction, re.source_id, re.status, re.entity_type
        FROM registry_agents a
        JOIN registry_entities re ON a.entity_id = re.id
        WHERE a.agent_name LIKE ?
        ORDER BY re.entity_name
        LIMIT ?
    """, [f"%{args.name}%", args.limit]).fetchall()

    if write_output([dict(r) for r in rows], args, summary=f"agent search '{args.name}'"):
        return

    print(f"Found {len(rows)} entities with agent matching '{args.name}'")
    print()
    for r in rows:
        juris = r["source_jurisdiction"].upper()
        status = r["status"] or "?"
        etype = r["entity_type"] or "?"
        print(f"  [{juris}] {r['entity_name']} ({etype}, {status})")
        print(f"    Agent: {r['agent_name']}")
        addr = r["address"] or ""
        if r["city"]:
            addr += f", {r['city']}"
        if r["state"]:
            addr += f", {r['state']}"
        if addr:
            print(f"    Agent address: {addr}")
        print()


def cmd_filings(args):
    """Get all filings for an entity."""
    db = get_db()

    entity = db.execute("SELECT * FROM registry_entities WHERE id = ?", [args.entity_id]).fetchone()
    if not entity:
        print(f"Entity {args.entity_id} not found")
        return

    filings = db.execute("""
        SELECT * FROM registry_filings
        WHERE entity_id = ?
        ORDER BY filing_date DESC
        LIMIT ?
    """, [args.entity_id, args.limit]).fetchall()

    if write_output([dict(f) for f in filings], args, summary=f"filings for entity {args.entity_id}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps([dict(f) for f in filings], indent=2, default=str))
        return

    print(f"Filings for {entity['entity_name']} ({len(filings)} records):")
    print()
    for f in filings:
        desc = f["description"] or f["filing_type"] or "?"
        date = f["filing_date"] or "?"
        eff = f""
        if f["effective_date"] and f["effective_date"] != f["filing_date"]:
            eff = f" (effective: {f['effective_date']})"
        name_note = ""
        if f["entity_name_at_time"] and f["entity_name_at_time"] != entity["entity_name"]:
            name_note = f"\n    Name at time: {f['entity_name_at_time']}"
        print(f"  {date}: {desc}{eff}{name_note}")


def cmd_stats(args):
    """Show registry statistics."""
    db = get_db()

    total = db.execute("SELECT COUNT(*) FROM registry_entities").fetchone()[0]
    print(f"Total entities: {total:,}")
    print()

    # By jurisdiction
    rows = db.execute("""
        SELECT source_jurisdiction, COUNT(*) as cnt,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active
        FROM registry_entities
        GROUP BY source_jurisdiction
        ORDER BY cnt DESC
    """).fetchall()
    if rows:
        print("By jurisdiction:")
        for r in rows:
            print(f"  {r['source_jurisdiction'].upper()}: {r['cnt']:,} ({r['active']:,} active)")

    # Officers
    officer_count = db.execute("SELECT COUNT(*) FROM registry_officers").fetchone()[0]
    print(f"\nTotal officers: {officer_count:,}")

    # Filings
    filing_count = db.execute("SELECT COUNT(*) FROM registry_filings").fetchone()[0]
    print(f"Total filings: {filing_count:,}")

    # Ingest log
    ingests = db.execute(
        "SELECT jurisdiction, source_type, record_count, ingested_at FROM registry_ingest_log ORDER BY ingested_at DESC LIMIT 10"
    ).fetchall()
    if ingests:
        print("\nRecent ingests:")
        for i in ingests:
            print(f"  {i['jurisdiction'].upper()} ({i['source_type']}): {i['record_count']:,} records at {i['ingested_at']}")


def cmd_jurisdictions(args):
    """List available jurisdictions."""
    db = get_db()
    rows = db.execute("""
        SELECT source_jurisdiction, COUNT(*) as cnt,
               MIN(formation_date) as earliest,
               MAX(last_filing_date) as latest
        FROM registry_entities
        GROUP BY source_jurisdiction
        ORDER BY cnt DESC
    """).fetchall()

    print("Available jurisdictions:")
    for r in rows:
        print(f"  {r['source_jurisdiction'].upper()}: {r['cnt']:,} entities (earliest: {r['earliest'] or '?'}, latest filing: {r['latest'] or '?'})")


# ══════════════════════════════════════════════════════════
# UCC QUERY COMMANDS
# ══════════════════════════════════════════════════════════

def _format_ucc_filing(row, db, verbose=False):
    """Format a UCC filing row for display."""
    lines = []
    juris = row["source_jurisdiction"].upper()
    status = row["status"] or "?"
    ftype = row["filing_type"] or "?"
    lines.append(f"  [{juris}] Filing #{row['filing_number']} ({ftype}, {status})")
    lines.append(f"    ID: {row['id']} | Filed: {row['filing_date'] or '?'}")
    if row["lapse_date"]:
        lines.append(f"    Lapse date: {row['lapse_date']}")
    if row["file_number"] and row["file_number"] != row["filing_number"]:
        lines.append(f"    Original filing: {row['file_number']}")

    if verbose:
        # Show debtors
        debtors = db.execute(
            "SELECT * FROM ucc_debtors WHERE filing_id = ?", [row["id"]]
        ).fetchall()
        if debtors:
            lines.append("    Debtors:")
            for d in debtors:
                dtype = f" [{d['debtor_type']}]" if d["debtor_type"] else ""
                lines.append(f"      {d['debtor_name']}{dtype}")
                addr = d["address"] or ""
                if d["city"]:
                    addr += f", {d['city']}"
                if d["state"]:
                    addr += f", {d['state']}"
                if d["zip"]:
                    addr += f" {d['zip']}"
                if addr:
                    lines.append(f"        Address: {addr}")
                if d["registry_entity_id"]:
                    lines.append(f"        Linked to registry entity: {d['registry_entity_id']}")

        # Show secured parties
        parties = db.execute(
            "SELECT * FROM ucc_secured_parties WHERE filing_id = ?", [row["id"]]
        ).fetchall()
        if parties:
            lines.append("    Secured parties:")
            for p in parties:
                ptype = f" [{p['party_type']}]" if p["party_type"] else ""
                lines.append(f"      {p['party_name']}{ptype}")
                addr = p["address"] or ""
                if p["city"]:
                    addr += f", {p['city']}"
                if p["state"]:
                    addr += f", {p['state']}"
                if addr:
                    lines.append(f"        Address: {addr}")

        # Show collateral
        collateral = db.execute(
            "SELECT * FROM ucc_collateral WHERE filing_id = ?", [row["id"]]
        ).fetchall()
        if collateral:
            lines.append("    Collateral:")
            for c in collateral:
                desc = c["description"]
                if len(desc) > 200:
                    desc = desc[:197] + "..."
                lines.append(f"      {desc}")

        # Show history
        history = db.execute(
            "SELECT * FROM ucc_filing_history WHERE filing_id = ? ORDER BY action_date",
            [row["id"]]
        ).fetchall()
        if history:
            lines.append("    History:")
            for h in history:
                lines.append(f"      {h['action_date'] or '?'}: {h['action_type']} ({h['action_filing_number'] or '?'})")
                if h["description"]:
                    lines.append(f"        {h['description']}")

    return "\n".join(lines)


def cmd_ucc_search(args):
    """Search UCC filings by debtor or secured party name."""
    db = get_db()

    juris_filter = ""
    juris_params = []
    if args.jurisdiction:
        juris_filter = "AND f.source_jurisdiction = ?"
        juris_params = [args.jurisdiction.lower()]

    role_filter = args.role  # "debtor", "secured", or None (both)

    results = []

    # Search debtors
    if role_filter in (None, "debtor"):
        try:
            rows = db.execute(f"""
                SELECT d.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction, f.lapse_date, f.file_number, f.id as filing_pk
                FROM ucc_debtors_fts fts
                JOIN ucc_debtors d ON fts.rowid = d.id
                JOIN ucc_filings f ON d.filing_id = f.id
                WHERE ucc_debtors_fts MATCH ?
                {juris_filter}
                ORDER BY rank
                LIMIT ?
            """, [args.query] + juris_params + [args.limit]).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute(f"""
                SELECT d.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction, f.lapse_date, f.file_number, f.id as filing_pk
                FROM ucc_debtors d
                JOIN ucc_filings f ON d.filing_id = f.id
                WHERE d.debtor_name LIKE ?
                {juris_filter}
                ORDER BY d.debtor_name
                LIMIT ?
            """, [f"%{args.query}%"] + juris_params + [args.limit]).fetchall()

        for r in rows:
            results.append(("debtor", r))

    # Search secured parties
    if role_filter in (None, "secured"):
        try:
            rows = db.execute(f"""
                SELECT sp.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction, f.lapse_date, f.file_number, f.id as filing_pk
                FROM ucc_secured_parties_fts fts
                JOIN ucc_secured_parties sp ON fts.rowid = sp.id
                JOIN ucc_filings f ON sp.filing_id = f.id
                WHERE ucc_secured_parties_fts MATCH ?
                {juris_filter}
                ORDER BY rank
                LIMIT ?
            """, [args.query] + juris_params + [args.limit]).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute(f"""
                SELECT sp.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction, f.lapse_date, f.file_number, f.id as filing_pk
                FROM ucc_secured_parties sp
                JOIN ucc_filings f ON sp.filing_id = f.id
                WHERE sp.party_name LIKE ?
                {juris_filter}
                ORDER BY sp.party_name
                LIMIT ?
            """, [f"%{args.query}%"] + juris_params + [args.limit]).fetchall()

        for r in rows:
            results.append(("secured", r))

    if write_output([(role, dict(r)) for role, r in results], args, summary=f"ucc search '{args.query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps([(role, dict(r)) for role, r in results], indent=2, default=str))
        return

    print(f"Found {len(results)} UCC records matching '{args.query}'")
    print()
    for role, r in results:
        juris = r["source_jurisdiction"].upper()
        name = r["debtor_name"] if role == "debtor" else r["party_name"]
        status = r["status"] or "?"
        print(f"  [{juris}] {name} ({role})")
        print(f"    Filing #{r['filing_number']} ({r['filing_type'] or '?'}, {status})")
        print(f"    Filed: {r['filing_date'] or '?'} | Lapse: {r['lapse_date'] or '?'}")
        addr = r["address"] or ""
        if r["city"]:
            addr += f", {r['city']}"
        if r["state"]:
            addr += f", {r['state']}"
        if addr:
            print(f"    Address: {addr}")
        print()


def cmd_ucc_filing(args):
    """Get full detail for a specific UCC filing."""
    db = get_db()
    row = db.execute("SELECT * FROM ucc_filings WHERE id = ?", [args.filing_id]).fetchone()
    if not row:
        print(f"UCC filing {args.filing_id} not found")
        return

    if write_output(dict(row), args, summary=f"ucc filing {args.filing_id}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(dict(row), indent=2, default=str))
        return
    print(_format_ucc_filing(row, db, verbose=True))


def cmd_ucc_collateral(args):
    """Search UCC collateral descriptions."""
    db = get_db()

    try:
        rows = db.execute("""
            SELECT c.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                   f.source_jurisdiction, f.id as filing_pk
            FROM ucc_collateral_fts fts
            JOIN ucc_collateral c ON fts.rowid = c.id
            JOIN ucc_filings f ON c.filing_id = f.id
            WHERE ucc_collateral_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, [args.query, args.limit]).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute("""
            SELECT c.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                   f.source_jurisdiction, f.id as filing_pk
            FROM ucc_collateral c
            JOIN ucc_filings f ON c.filing_id = f.id
            WHERE c.description LIKE ?
            ORDER BY f.filing_date DESC
            LIMIT ?
        """, [f"%{args.query}%", args.limit]).fetchall()

    print(f"Found {len(rows)} UCC collateral records matching '{args.query}'")
    print()
    for r in rows:
        juris = r["source_jurisdiction"].upper()
        desc = r["description"]
        if len(desc) > 200:
            desc = desc[:197] + "..."
        print(f"  [{juris}] Filing #{r['filing_number']} ({r['filing_date'] or '?'})")
        print(f"    {desc}")

        # Show associated debtors
        debtors = db.execute(
            "SELECT debtor_name FROM ucc_debtors WHERE filing_id = ?", [r["filing_pk"]]
        ).fetchall()
        if debtors:
            names = ", ".join(d["debtor_name"] for d in debtors)
            print(f"    Debtors: {names}")
        print()


def cmd_ucc_party(args):
    """Search UCC parties by name with role filter."""
    db = get_db()

    if args.role == "secured":
        try:
            rows = db.execute("""
                SELECT sp.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction
                FROM ucc_secured_parties_fts fts
                JOIN ucc_secured_parties sp ON fts.rowid = sp.id
                JOIN ucc_filings f ON sp.filing_id = f.id
                WHERE ucc_secured_parties_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, [args.name, args.limit]).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute("""
                SELECT sp.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction
                FROM ucc_secured_parties sp
                JOIN ucc_filings f ON sp.filing_id = f.id
                WHERE sp.party_name LIKE ?
                ORDER BY sp.party_name
                LIMIT ?
            """, [f"%{args.name}%", args.limit]).fetchall()

        print(f"Found {len(rows)} secured party records matching '{args.name}'")
        print()
        for r in rows:
            juris = r["source_jurisdiction"].upper()
            print(f"  [{juris}] {r['party_name']} (secured party)")
            print(f"    Filing #{r['filing_number']} ({r['filing_type'] or '?'}, {r['status'] or '?'})")
            print(f"    Filed: {r['filing_date'] or '?'}")
            addr = r["address"] or ""
            if r["city"]:
                addr += f", {r['city']}"
            if r["state"]:
                addr += f", {r['state']}"
            if addr:
                print(f"    Address: {addr}")
            print()
    else:
        try:
            rows = db.execute("""
                SELECT d.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction
                FROM ucc_debtors_fts fts
                JOIN ucc_debtors d ON fts.rowid = d.id
                JOIN ucc_filings f ON d.filing_id = f.id
                WHERE ucc_debtors_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, [args.name, args.limit]).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute("""
                SELECT d.*, f.filing_number, f.filing_type, f.filing_date, f.status,
                       f.source_jurisdiction
                FROM ucc_debtors d
                JOIN ucc_filings f ON d.filing_id = f.id
                WHERE d.debtor_name LIKE ?
                ORDER BY d.debtor_name
                LIMIT ?
            """, [f"%{args.name}%", args.limit]).fetchall()

        print(f"Found {len(rows)} debtor records matching '{args.name}'")
        print()
        for r in rows:
            juris = r["source_jurisdiction"].upper()
            print(f"  [{juris}] {r['debtor_name']} (debtor)")
            print(f"    Filing #{r['filing_number']} ({r['filing_type'] or '?'}, {r['status'] or '?'})")
            print(f"    Filed: {r['filing_date'] or '?'}")
            addr = r["address"] or ""
            if r["city"]:
                addr += f", {r['city']}"
            if r["state"]:
                addr += f", {r['state']}"
            if addr:
                print(f"    Address: {addr}")
            print()


def cmd_ucc_stats(args):
    """Show UCC filing statistics."""
    db = get_db()

    total_filings = db.execute("SELECT COUNT(*) FROM ucc_filings").fetchone()[0]
    total_debtors = db.execute("SELECT COUNT(*) FROM ucc_debtors").fetchone()[0]
    total_parties = db.execute("SELECT COUNT(*) FROM ucc_secured_parties").fetchone()[0]
    total_collateral = db.execute("SELECT COUNT(*) FROM ucc_collateral").fetchone()[0]

    print(f"UCC Filing Statistics")
    print(f"  Total filings: {total_filings:,}")
    print(f"  Total debtors: {total_debtors:,}")
    print(f"  Total secured parties: {total_parties:,}")
    print(f"  Total collateral records: {total_collateral:,}")
    print()

    # By jurisdiction
    rows = db.execute("""
        SELECT source_jurisdiction, COUNT(*) as cnt,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
               MIN(filing_date) as earliest,
               MAX(filing_date) as latest
        FROM ucc_filings
        GROUP BY source_jurisdiction
        ORDER BY cnt DESC
    """).fetchall()
    if rows:
        print("By jurisdiction:")
        for r in rows:
            print(f"  {r['source_jurisdiction'].upper()}: {r['cnt']:,} filings ({r['active']:,} active)")
            print(f"    Date range: {r['earliest'] or '?'} to {r['latest'] or '?'}")

    # By filing type
    types = db.execute("""
        SELECT filing_type, COUNT(*) as cnt
        FROM ucc_filings
        GROUP BY filing_type
        ORDER BY cnt DESC
    """).fetchall()
    if types:
        print("\nBy filing type:")
        for t in types:
            print(f"  {t['filing_type'] or '?'}: {t['cnt']:,}")

    # Top secured parties
    top_parties = db.execute("""
        SELECT party_name, COUNT(*) as cnt
        FROM ucc_secured_parties
        GROUP BY party_name
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    if top_parties:
        print("\nTop secured parties:")
        for p in top_parties:
            print(f"  {p['party_name']}: {p['cnt']:,} filings")


def main():
    parser = argparse.ArgumentParser(description="Unified corporate registry query tool")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search entities by name")
    p.add_argument("query")
    p.add_argument("--jurisdiction", "-j", help="Filter by jurisdiction code (fl, ny, vi, etc.)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--exact", action="store_true", help="Use LIKE instead of FTS")
    add_output_args(p)

    # entity
    p = sub.add_parser("entity", help="Get full entity details")
    p.add_argument("entity_id", type=int)
    add_output_args(p)

    # officers
    p = sub.add_parser("officers", help="Search officers by name")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # address
    p = sub.add_parser("address", help="Search by address")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # agent
    p = sub.add_parser("agent", help="Search by registered agent")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # filings
    p = sub.add_parser("filings", help="Get filing history for an entity")
    p.add_argument("entity_id", type=int)
    p.add_argument("--limit", type=int, default=50)
    add_output_args(p)

    # stats
    p = sub.add_parser("stats", help="Show registry statistics")
    add_output_args(p)

    # jurisdictions
    p = sub.add_parser("jurisdictions", help="List available jurisdictions")
    add_output_args(p)

    # ── UCC subcommands ──

    # ucc-search
    p = sub.add_parser("ucc-search", help="Search UCC filings by debtor/secured party name")
    p.add_argument("query")
    p.add_argument("--jurisdiction", "-j", help="Filter by jurisdiction code (fl, nm, etc.)")
    p.add_argument("--role", choices=["debtor", "secured"], help="Filter by party role")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # ucc-filing
    p = sub.add_parser("ucc-filing", help="Get full UCC filing detail")
    p.add_argument("filing_id", type=int)
    add_output_args(p)

    # ucc-collateral
    p = sub.add_parser("ucc-collateral", help="Search UCC collateral descriptions")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # ucc-party
    p = sub.add_parser("ucc-party", help="Search UCC parties by name")
    p.add_argument("name")
    p.add_argument("--role", choices=["debtor", "secured"], default="debtor",
                   help="Party role (default: debtor)")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # ucc-stats
    p = sub.add_parser("ucc-stats", help="Show UCC filing statistics")
    add_output_args(p)

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "search": cmd_search,
        "entity": cmd_entity,
        "officers": cmd_officers,
        "address": cmd_address,
        "agent": cmd_agent,
        "filings": cmd_filings,
        "stats": cmd_stats,
        "jurisdictions": cmd_jurisdictions,
        "ucc-search": cmd_ucc_search,
        "ucc-filing": cmd_ucc_filing,
        "ucc-collateral": cmd_ucc_collateral,
        "ucc-party": cmd_ucc_party,
        "ucc-stats": cmd_ucc_stats,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
