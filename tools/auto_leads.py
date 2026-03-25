#!/usr/bin/env python3
"""
Post-wave auto-lead generator.

Scans entity tables, findings, and connections for new entries not yet
cross-referenced and generates investigation leads automatically.

Run after each investigation wave:
    python tools/auto_leads.py
    python tools/auto_leads.py --dry-run    # Preview without creating leads
    python tools/auto_leads.py --stats      # Show what's been processed

Triggers (entity-based):
    - New entity address → search registries for other entities at that address
    - New entity role (person→entity) → search for person as officer elsewhere
    - New entity → search corporate registries + ACRIS + Aleph
    - New connection with < 5 findings → search corpus for person

Triggers (findings-based):
    - Findings coverage gap → target with ≤2 findings and no open lead
    - Contract patterns → shared solicitations, contracting officers, residential addresses
    - Connection network nodes → persons with 2+ connections but no entity record or lead
"""

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "investigation.db"

# Ensure project root is on path for tools.* imports when run as script
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_profile(profile_name=None):
    """Load the active (or specified) investigation profile."""
    try:
        if profile_name:
            from tools.investigation_context import load_profile
            return load_profile(profile_name)
        else:
            from tools.investigation_context import get_active_profile
            return get_active_profile()
    except Exception as e:
        print(f"  Warning: could not load profile: {e}", file=sys.stderr)
        return None


# Loaded lazily on first use
_profile_cache = None


def _get_profile():
    """Get cached profile. Returns (known_addresses, key_persons, primary_subject)."""
    global _profile_cache
    if _profile_cache is None:
        profile = _load_profile()
        if profile:
            _profile_cache = (
                profile.known_addresses or {},
                set(profile.key_persons or []),
                profile.primary_subject.lower() if profile.primary_subject else "",
            )
        else:
            _profile_cache = ({}, set(), "")
    return _profile_cache


def get_profile_thread_ids(db, profile_name, bridge_threads=None):
    """Get thread IDs belonging to this profile from the DB."""
    rows = db.execute(
        "SELECT id FROM investigation_threads WHERE profile_id = ?",
        (profile_name,)
    ).fetchall()
    thread_ids = {row["id"] for row in rows}
    if bridge_threads:
        thread_ids.update(bridge_threads)
    return thread_ids


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def is_processed(db, table_name, record_id, crossref_type):
    row = db.execute(
        "SELECT id FROM auto_crossref_log WHERE table_name=? AND record_id=? AND crossref_type=?",
        (table_name, record_id, crossref_type)
    ).fetchone()
    return row is not None


def log_processed(db, table_name, record_id, crossref_type, lead_id=None):
    db.execute(
        "INSERT OR IGNORE INTO auto_crossref_log (table_name, record_id, crossref_type, lead_id) VALUES (?,?,?,?)",
        (table_name, record_id, crossref_type, lead_id)
    )


def lead_exists(db, title_fragment):
    """Check if a similar lead already exists.

    Converts regex-style .* to SQL % wildcards for LIKE queries.
    """
    # Fix: .* has no meaning in SQL LIKE — convert to %
    pattern = title_fragment.replace(".*", "%")
    row = db.execute(
        "SELECT id FROM leads WHERE title LIKE ?", (f"%{pattern}%",)
    ).fetchone()
    return row["id"] if row else None


_leads_created_this_run = 0
_max_leads_per_run = 100


class LeadLimitReached(Exception):
    """Raised when the per-run lead creation limit is hit."""
    pass


def create_lead(db, title, category, priority, source, target=None, notes=None,
                profile_id=None, thread_id=None):
    """Create a lead and return its ID. Auto-leads go to pending_triage."""
    global _leads_created_this_run
    if _leads_created_this_run >= _max_leads_per_run:
        raise LeadLimitReached(f"Lead limit ({_max_leads_per_run}) reached for this run")
    db.execute(
        """INSERT INTO leads (title, category, priority, status, source, target_name,
           profile_id, thread_id, created_at)
           VALUES (?, ?, ?, 'pending_triage', ?, ?, ?, ?, datetime('now'))""",
        (title, category, priority, source, target, profile_id, thread_id)
    )
    lead_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    if notes:
        db.execute(
            "INSERT INTO lead_notes (lead_id, note, created_at) VALUES (?, ?, datetime('now'))",
            (lead_id, notes)
        )
    _leads_created_this_run += 1
    return lead_id


def address_priority(address):
    """Determine priority based on known addresses from active profile."""
    known_addresses, _, _ = _get_profile()
    addr_lower = address.lower()
    for pattern, desc in known_addresses.items():
        if pattern in addr_lower:
            return "high", desc
    return "medium", None


def person_priority(person_name):
    """Determine priority based on key persons from active profile."""
    _, key_persons, _ = _get_profile()
    name_lower = person_name.lower().strip()
    if name_lower in key_persons:
        return "high"
    # Check partial matches (last name)
    for kp in key_persons:
        parts = kp.split()
        if len(parts) >= 2 and parts[-1] == name_lower.split()[-1] if name_lower.split() else "":
            return "high"
    return "medium"


def normalize_address(addr):
    """Normalize address for dedup — extract street number + name."""
    import re
    addr = addr.strip().lower()
    # Remove suite/apt/fl info
    addr = re.sub(r'\b(suite|ste|apt|fl|floor|unit|#)\b.*', '', addr, flags=re.IGNORECASE)
    # Remove common suffixes
    addr = re.sub(r'\b(llc|inc|corp)\b', '', addr)
    # Extract just the street part (first line before comma)
    addr = addr.split(",")[0].strip()
    # Collapse whitespace
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr[:50]


def process_new_addresses(db, dry_run=False):
    """Generate leads for new entity addresses."""
    rows = db.execute("""
        SELECT ea.id, ea.entity_id, ea.address, ea.address_type, e.name as entity_name
        FROM entity_addresses ea
        JOIN entities e ON ea.entity_id = e.id
        WHERE ea.id NOT IN (
            SELECT record_id FROM auto_crossref_log WHERE table_name='entity_addresses'
        )
    """).fetchall()

    created = 0
    seen_addresses = {}  # normalized → lead_id

    for row in rows:
        addr = row["address"]
        if not addr or len(addr.strip()) < 10:
            log_processed(db, "entity_addresses", row["id"], "address_search")
            continue

        norm = normalize_address(addr)
        if len(norm) < 8:
            log_processed(db, "entity_addresses", row["id"], "address_search")
            continue

        # Skip vague addresses (just city/state, c/o addresses, PO boxes)
        skip_patterns = ["c/o ", "p.o. box", "po box", "unknown", "n/a"]
        if any(p in addr.lower() for p in skip_patterns):
            log_processed(db, "entity_addresses", row["id"], "address_search")
            continue
        # Skip addresses that are just a city/state (no street number)
        import re
        if not re.search(r'\d', norm):
            log_processed(db, "entity_addresses", row["id"], "address_search")
            continue

        # Dedup within this batch
        if norm in seen_addresses:
            log_processed(db, "entity_addresses", row["id"], "address_search", seen_addresses[norm])
            continue

        # Check existing leads
        existing = lead_exists(db, norm[:30])
        if existing:
            seen_addresses[norm] = existing
            log_processed(db, "entity_addresses", row["id"], "address_search", existing)
            continue

        priority, known_desc = address_priority(addr)
        addr_display = addr.split(",")[0].strip()[:40]
        title = f"Cross-ref address: {addr_display} — find other entities"
        notes = f"Entity '{row['entity_name']}' registered at {addr}."
        if known_desc:
            notes += f" Known investigation location: {known_desc}."
        notes += " Search: query_registry.py address, query_acris.py, ingest_newyork.py search-address"

        if dry_run:
            print(f"  [DRY] Address lead ({priority}): {title}")
            seen_addresses[norm] = -1
        else:
            lead_id = create_lead(db, title, "entity", priority, "agent:auto_leads", target=addr_display, notes=notes)
            seen_addresses[norm] = lead_id
            log_processed(db, "entity_addresses", row["id"], "address_search", lead_id)
            created += 1

    return created, len(rows)


def process_new_roles(db, dry_run=False):
    """Generate leads for new person-entity roles (search for person as officer elsewhere)."""
    rows = db.execute("""
        SELECT er.id, er.entity_id, er.person_name, er.role, e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
        WHERE er.id NOT IN (
            SELECT record_id FROM auto_crossref_log WHERE table_name='entity_roles'
        )
    """).fetchall()

    # Group by person to avoid creating duplicate leads for same person at multiple entities
    persons_seen = {}
    created = 0
    for row in rows:
        person = row["person_name"]
        if not person or len(person.strip()) < 3:
            log_processed(db, "entity_roles", row["id"], "officer_search")
            continue

        person_normalized = person.strip().lower()
        if person_normalized in persons_seen:
            log_processed(db, "entity_roles", row["id"], "officer_search", persons_seen[person_normalized])
            continue

        # Skip the primary investigation subject — we know their entities
        _, _, primary_subject = _get_profile()
        if primary_subject and primary_subject in person_normalized:
            persons_seen[person_normalized] = None
            log_processed(db, "entity_roles", row["id"], "officer_search")
            continue

        # Check if lead already exists
        existing = lead_exists(db, f"officer: {person.strip()[:30]}")
        if existing:
            persons_seen[person_normalized] = existing
            log_processed(db, "entity_roles", row["id"], "officer_search", existing)
            continue

        # Check if person already has 5+ findings (well-investigated)
        finding_count = db.execute(
            "SELECT COUNT(*) FROM findings WHERE target_name LIKE ?",
            (f"%{person.strip()}%",)
        ).fetchone()[0]
        if finding_count >= 5:
            persons_seen[person_normalized] = None
            log_processed(db, "entity_roles", row["id"], "officer_search")
            continue

        priority = person_priority(person)
        title = f"Cross-ref officer: {person.strip()} — find other entity roles"
        notes = f"Found as {row['role']} at {row['entity_name']}."
        notes += f" {finding_count} existing findings."
        notes += " Search: query_registry.py officers, ingest_newyork.py search-officers, query_aleph.py"

        if dry_run:
            print(f"  [DRY] Officer lead ({priority}): {title}")
            persons_seen[person_normalized] = -1
        else:
            lead_id = create_lead(db, title, "person", priority, "agent:auto_leads", target=person.strip(), notes=notes)
            persons_seen[person_normalized] = lead_id
            log_processed(db, "entity_roles", row["id"], "officer_search", lead_id)
            created += 1

    return created, len(rows)


def process_new_entities(db, dry_run=False):
    """Generate leads for new entities (search registries)."""
    rows = db.execute("""
        SELECT e.id, e.name, e.entity_type, e.jurisdiction
        FROM entities e
        WHERE e.id NOT IN (
            SELECT record_id FROM auto_crossref_log WHERE table_name='entities'
        )
    """).fetchall()

    created = 0
    for row in rows:
        name = row["name"]
        if not name or len(name.strip()) < 3:
            log_processed(db, "entities", row["id"], "entity_search")
            continue

        # Skip entities that are just generic descriptions or well-known public entities
        skip_names = {"unknown", "n/a", "none", "self", "personal"}
        if name.strip().lower() in skip_names:
            log_processed(db, "entities", row["id"], "entity_search")
            continue

        # Skip well-known public institutions (not useful to registry-search)
        public_entities = {
            "goldman sachs", "jpmorgan", "jp morgan", "deutsche bank", "apollo global",
            "paul weiss", "kirkland & ellis", "kirkland and ellis",
            "harvard", "mit", "yale", "columbia", "nyu",
            "fbi", "doj", "cia", "nsa", "state department",
            "united nations", "world economic forum", "council of europe",
        }
        name_lower = name.strip().lower()
        if any(pe in name_lower for pe in public_entities):
            log_processed(db, "entities", row["id"], "entity_search")
            continue

        # Only create leads for entity types likely to have registry records
        searchable_types = {"llc", "inc", "ltd", "trust", "foundation", "nonprofit", "partnership", "fund"}
        if row["entity_type"] not in searchable_types:
            log_processed(db, "entities", row["id"], "entity_search")
            continue

        # Check if lead already exists
        name_short = name.strip()[:40]
        existing = lead_exists(db, f"registry: {name_short}")
        if existing:
            log_processed(db, "entities", row["id"], "entity_search", existing)
            continue

        # Determine priority — entity types commonly used for opacity get higher priority
        priority = "low"
        opacity_types = {"trust", "foundation", "nonprofit", "fund", "llc"}
        if row["entity_type"] in opacity_types:
            priority = "medium"

        # Check if entity has roles (more connected = higher priority)
        role_count = db.execute(
            "SELECT COUNT(*) FROM entity_roles WHERE entity_id=?", (row["id"],)
        ).fetchone()[0]
        if role_count >= 2:
            priority = "medium"
        if role_count >= 4:
            priority = "high"

        title = f"Cross-ref registry: {name_short} — search corporate registries"
        jur = row["jurisdiction"] or "unknown"
        notes = f"Type: {row['entity_type']}, Jurisdiction: {jur}, {role_count} known roles."
        notes += " Search: query_registry.py search, query_aleph.py search --schema Company, ingest_newyork.py search"

        if dry_run:
            print(f"  [DRY] Entity lead ({priority}): {title}")
        else:
            lead_id = create_lead(db, title, "entity", priority, "agent:auto_leads", target=name.strip(), notes=notes)
            log_processed(db, "entities", row["id"], "entity_search", lead_id)
            created += 1

    return created, len(rows)


def process_new_connections(db, dry_run=False):
    """Generate leads for new connections where person has < 5 findings."""
    rows = db.execute("""
        SELECT c.id, c.person_a, c.person_b, c.relationship_type
        FROM connections c
        WHERE c.id NOT IN (
            SELECT record_id FROM auto_crossref_log WHERE table_name='connections'
        )
    """).fetchall()

    # Only generate leads for persons with HIGH priority (key persons)
    # or persons with 0 findings AND a strong connection type
    STRONG_TYPES = {"financial", "legal", "intelligence", "employment", "corporate"}

    persons_checked = set()
    created = 0
    for row in rows:
        for person in [row["person_b"], row["person_a"]]:
            if not person or person.strip().lower() in persons_checked:
                continue
            persons_checked.add(person.strip().lower())

            # Skip the primary investigation subject
            _, _, primary_subject = _get_profile()
            if primary_subject and primary_subject in person.lower():
                continue

            # Skip very short or generic names
            if len(person.strip()) < 5:
                continue

            # Skip public figures who aren't direct network members
            skip_persons = {"mike pence", "stefan halper", "donald trump", "hillary clinton",
                           "barack obama", "vladimir putin", "mbs", "joe biden"}
            if person.strip().lower() in skip_persons:
                continue

            finding_count = db.execute(
                "SELECT COUNT(*) FROM findings WHERE target_name LIKE ?",
                (f"%{person.strip()}%",)
            ).fetchone()[0]
            if finding_count >= 3:
                continue

            existing = lead_exists(db, f"Deep-search: {person.strip()[:30]}")
            if existing:
                continue

            # Only create if: key person OR strong connection with 0 findings
            priority = person_priority(person)
            is_strong = row["relationship_type"] in STRONG_TYPES
            if priority != "high" and not (finding_count == 0 and is_strong):
                continue

            title = f"Deep-search: {person.strip()} — {finding_count} findings, expand coverage"
            notes = f"Connected via {row['relationship_type']} relationship."
            notes += f" Only {finding_count} findings. Search investigation corpus."

            if dry_run:
                print(f"  [DRY] Connection lead ({priority}): {title}")
            else:
                lead_id = create_lead(db, title, "person", priority, "agent:auto_leads", target=person.strip(), notes=notes)
                created += 1

        log_processed(db, "connections", row["id"], "connection_search")

    return created, len(rows)


def process_alumni_clustering(db, dry_run=False):
    """Generate leads when 3+ alumni of a dissolved institution cluster at the same destination."""
    try:
        # Get all dissolved/acquired institutions
        dissolved = db.execute("""
            SELECT id, name FROM institutional_pillars
            WHERE status IN ('dissolved', 'acquired')
        """).fetchall()
    except sqlite3.OperationalError:
        return 0, 0  # pillar tables don't exist yet

    created = 0
    total = 0
    for inst in dissolved:
        # Find destination clustering
        destinations = db.execute("""
            SELECT ip_dest.name as dest_name, COUNT(DISTINCT ca_orig.person_id) as alumni_count,
                   GROUP_CONCAT(DISTINCT ca_orig.person_name) as alumni_names
            FROM career_arcs ca_orig
            JOIN career_arcs ca_dest ON ca_dest.person_id = ca_orig.person_id
                AND ca_dest.pillar_id != ca_orig.pillar_id
            JOIN institutional_pillars ip_dest ON ca_dest.pillar_id = ip_dest.id
            WHERE ca_orig.pillar_id = ?
            GROUP BY ip_dest.id
            HAVING COUNT(DISTINCT ca_orig.person_id) >= 3
        """, (inst["id"],)).fetchall()

        for dest in destinations:
            total += 1
            dedup_key = f"alumni_cluster:{inst['name']}:{dest['dest_name']}"
            if is_processed(db, "institutional_pillars", inst["id"], dedup_key):
                continue

            priority = "high" if dest["alumni_count"] >= 5 else "medium"
            title = f"Alumni clustering: {dest['alumni_count']} former {inst['name']} personnel now at {dest['dest_name']}"
            notes = f"Alumni: {dest['alumni_names']}. Investigate coordinated movement and shared playbook."

            if dry_run:
                print(f"  [DRY] Alumni clustering ({priority}): {title}")
            else:
                existing = lead_exists(db, f"Alumni clustering:.*{inst['name']}.*{dest['dest_name']}")
                if existing:
                    log_processed(db, "institutional_pillars", inst["id"], dedup_key, existing)
                    continue
                lead_id = create_lead(db, title, "connection", priority, "agent:auto_leads:alumni_cluster", notes=notes)
                log_processed(db, "institutional_pillars", inst["id"], dedup_key, lead_id)
                created += 1

    return created, total


def process_pillar_gaps(db, dry_run=False):
    """Generate leads for persons at 3+ institutions missing common pillar types."""
    try:
        # Persons with 3+ career arcs
        multi_arc = db.execute("""
            SELECT p.id, p.canonical_name, COUNT(DISTINCT ca.pillar_id) as inst_count,
                   GROUP_CONCAT(DISTINCT ip.pillar_type) as types_present
            FROM persons p
            JOIN career_arcs ca ON ca.person_id = p.id
            JOIN institutional_pillars ip ON ca.pillar_id = ip.id
            GROUP BY p.id
            HAVING COUNT(DISTINCT ca.pillar_id) >= 3
        """).fetchall()
    except sqlite3.OperationalError:
        return 0, 0  # pillar tables don't exist yet

    core_types = {"banking", "legal", "government"}
    created = 0
    total = 0

    for person in multi_arc:
        present = set(person["types_present"].split(","))
        missing = core_types - present
        if not missing:
            continue

        total += 1
        dedup_key = f"pillar_gap:{person['canonical_name']}"
        if is_processed(db, "persons", person["id"], dedup_key):
            continue

        for gap in missing:
            title = f"Pillar gap: {person['canonical_name']} has no {gap} connections"
            notes = (f"Has arcs at {person['inst_count']} institutions ({person['types_present']}) "
                     f"but no {gap} connections. Investigate hidden {gap} ties.")

            if dry_run:
                print(f"  [DRY] Pillar gap (medium): {title}")
            else:
                existing = lead_exists(db, f"Pillar gap: {person['canonical_name']}.*{gap}")
                if existing:
                    continue
                lead_id = create_lead(db, title, "connection", "medium", "agent:auto_leads:pillar_gap",
                                      target=person["canonical_name"], notes=notes)
                created += 1

        log_processed(db, "persons", person["id"], dedup_key)

    return created, total


# Known mass-market registered agent companies — these serve thousands of entities
# and should not trigger co-agent leads
MASS_MARKET_AGENTS = {
    "ct corporation", "c t corporation", "ct corp",
    "csc", "corporation service company",
    "national registered agents", "nrai",
    "registered agents inc", "rai",
    "legalzoom", "legal zoom",
    "the corporation trust", "corporation trust company",
    "united states corporation agents",
    "northwest registered agent",
    "incorp services", "incorp",
    "cogency global",
    "paracorp",
    "vcorp services",
    "wolters kluwer",
    "capitol services",
    "the company corporation",
    "incorporating services",
    "harvard business services",
    "agents and corporations",
    "spiegel & utrera",
    "blumberg excelsior",
}


def _is_mass_market_agent(agent_name, db=None):
    """Check if an agent is a mass-market registered agent company."""
    if not agent_name:
        return True
    name_lower = agent_name.strip().lower()
    # Static list check
    for mm in MASS_MARKET_AGENTS:
        if mm in name_lower:
            return True
    # Dynamic threshold: if this agent serves 50+ entities in registry.db, flag it
    if db:
        try:
            registry_db = sqlite3.connect(str(PROJECT_ROOT / "registry.db"))
            count = registry_db.execute(
                "SELECT COUNT(*) FROM entities WHERE registered_agent LIKE ?",
                (f"%{agent_name.strip()[:30]}%",)
            ).fetchone()[0]
            registry_db.close()
            if count >= 50:
                return True
        except (sqlite3.OperationalError, FileNotFoundError):
            pass
    return False


def process_officer_escalation(db, dry_run=False):
    """Escalate priority for officers found at 3+ entities (serial director pattern)."""
    rows = db.execute("""
        SELECT person_name, COUNT(DISTINCT entity_id) as entity_count
        FROM entity_roles
        GROUP BY LOWER(TRIM(person_name))
        HAVING COUNT(DISTINCT entity_id) >= 3
    """).fetchall()

    escalated = 0
    total = len(rows)
    for row in rows:
        person = row["person_name"]
        entity_count = row["entity_count"]

        dedup_key = f"officer_escalation:{person.strip().lower()}"
        if is_processed(db, "entity_roles", 0, dedup_key):
            continue

        # Check if there's an existing lead for this person
        existing = lead_exists(db, f"officer: {person.strip()[:30]}")
        if existing:
            # Escalate priority if it's currently medium/low
            if not dry_run:
                db.execute(
                    "UPDATE leads SET priority = 'high' WHERE id = ? AND priority IN ('medium', 'low')",
                    (existing,)
                )
                db.execute(
                    "INSERT INTO lead_notes (lead_id, note, created_at) VALUES (?, ?, datetime('now'))",
                    (existing, f"Auto-escalated: officer at {entity_count} entities (serial director pattern)")
                )
            escalated += 1
        else:
            # Create a new high-priority lead
            title = f"Serial director: {person.strip()} — officer at {entity_count} entities"
            notes = f"Found as officer at {entity_count} different entities. Pattern suggests nominee director or key network operative."
            if not dry_run:
                create_lead(db, title, "person", "high", "agent:auto_leads:officer_escalation", target=person.strip(), notes=notes)
            else:
                print(f"  [DRY] Serial director (high): {title}")
            escalated += 1

        log_processed(db, "entity_roles", 0, dedup_key)

    return escalated, total


def process_filing_clusters(db, dry_run=False):
    """Find entities filed within 7 days of each other by the same officer/agent."""
    try:
        # Get entities with known formation dates and officers
        # Prefer date_formed (actual filing date) over created_at (DB ingestion time)
        entities = db.execute("""
            SELECT e.id, e.name, e.jurisdiction,
                   MIN(er.person_name) as first_officer,
                   COALESCE(e.date_formed, e.created_at) as formation_date
            FROM entities e
            JOIN entity_roles er ON er.entity_id = e.id
            WHERE COALESCE(e.date_formed, e.created_at) IS NOT NULL
            GROUP BY e.id
        """).fetchall()
    except sqlite3.OperationalError:
        return 0, 0

    if len(entities) < 2:
        return 0, 0

    # Group by officer and find clusters within 7-day windows
    from collections import defaultdict
    import datetime

    officer_entities = defaultdict(list)
    for e in entities:
        officer = e["first_officer"].strip().lower() if e["first_officer"] else None
        if not officer:
            continue
        try:
            date = datetime.datetime.strptime(e["formation_date"][:10], "%Y-%m-%d")
            officer_entities[officer].append((date, e["name"], e["id"], e["jurisdiction"]))
        except (ValueError, TypeError):
            continue

    created = 0
    total = 0
    for officer, ents in officer_entities.items():
        if len(ents) < 2:
            continue
        ents.sort(key=lambda x: x[0])

        # Sliding window: find groups within 7 days
        i = 0
        while i < len(ents):
            cluster = [ents[i]]
            j = i + 1
            while j < len(ents) and (ents[j][0] - ents[i][0]).days <= 7:
                cluster.append(ents[j])
                j += 1
            if len(cluster) >= 2:
                total += 1
                names = ", ".join(c[1] for c in cluster[:5])
                dedup_key = f"filing_cluster:{officer}:{cluster[0][0].strftime('%Y-%m')}"
                if not is_processed(db, "entities", 0, dedup_key):
                    title = f"Filing cluster: {len(cluster)} entities by {officer.title()} within 7 days"
                    notes = (f"Entities: {names}. Filed within {(cluster[-1][0] - cluster[0][0]).days} days "
                             f"starting {cluster[0][0].strftime('%Y-%m-%d')}. Investigate coordinated formation.")
                    if dry_run:
                        print(f"  [DRY] Filing cluster (high): {title}")
                    else:
                        create_lead(db, title, "entity", "high", "agent:auto_leads:filing_cluster",
                                    target=officer.title(), notes=notes)
                    created += 1
                    log_processed(db, "entities", 0, dedup_key)
            i = j if j > i + 1 else i + 1

    return created, total


def process_jurisdiction_clusters(db, dry_run=False):
    """Find persons with 3+ entities in unusual jurisdictions."""
    # Jurisdictions that are commonly used for opacity
    UNUSUAL_JURISDICTIONS = {
        "wyoming", "wy", "nevada", "nv", "delaware", "de",
        "usvi", "us virgin islands", "bvi", "british virgin islands",
        "cayman", "cayman islands", "panama", "bermuda", "jersey",
        "guernsey", "isle of man", "bahamas", "seychelles",
        "marshall islands", "nevis", "st. kitts",
    }

    try:
        rows = db.execute("""
            SELECT er.person_name, e.jurisdiction, COUNT(DISTINCT e.id) as entity_count,
                   GROUP_CONCAT(DISTINCT e.name) as entity_names
            FROM entity_roles er
            JOIN entities e ON er.entity_id = e.id
            WHERE e.jurisdiction IS NOT NULL
            GROUP BY LOWER(TRIM(er.person_name)), LOWER(TRIM(e.jurisdiction))
            HAVING COUNT(DISTINCT e.id) >= 3
        """).fetchall()
    except sqlite3.OperationalError:
        return 0, 0

    created = 0
    total = len(rows)
    for row in rows:
        jur = (row["jurisdiction"] or "").strip().lower()
        if jur not in UNUSUAL_JURISDICTIONS:
            continue

        person = row["person_name"]
        dedup_key = f"jurisdiction_cluster:{person.strip().lower()}:{jur}"
        if is_processed(db, "entity_roles", 0, dedup_key):
            continue

        entity_names = row["entity_names"][:200] if row["entity_names"] else "unknown"
        title = f"Jurisdiction cluster: {person.strip()} has {row['entity_count']} entities in {row['jurisdiction']}"
        notes = (f"Entities: {entity_names}. Concentration in {row['jurisdiction']} "
                 f"may indicate opacity-seeking behavior.")

        if dry_run:
            print(f"  [DRY] Jurisdiction cluster (medium): {title}")
        else:
            create_lead(db, title, "entity", "medium", "agent:auto_leads:jurisdiction_cluster",
                        target=person.strip(), notes=notes)
        created += 1
        log_processed(db, "entity_roles", 0, dedup_key)

    return created, total


def _is_address_target(target):
    """Check if a target looks like an address rather than a person/entity name."""
    import re
    t = target.strip()
    # Starts with digits and has street keywords
    if re.match(r'^\d+\s', t) and re.search(
            r'\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|pl|place|ct|court)\b',
            t, re.IGNORECASE):
        return True
    # Common address patterns: "123 E 45th" or "PO Box"
    if re.match(r'^\d+\s+[NESW]\s+\d', t):
        return True
    return False


def process_findings_coverage_gaps(db, thread_ids, profile_id, dry_run=False):
    """Generate leads for finding targets with few findings and no open leads.

    Scoped to the active profile's threads. Only generates leads for targets
    that have network context (at least 1 connection).
    """
    if not thread_ids:
        return 0, 0

    placeholders = ",".join("?" * len(thread_ids))
    rows = db.execute(f"""
        SELECT target_name, COUNT(*) as finding_count,
               GROUP_CONCAT(DISTINCT thread_id) as threads,
               MAX(f.id) as latest_finding_id,
               GROUP_CONCAT(DISTINCT f.finding_type) as finding_types
        FROM findings f
        WHERE target_name IS NOT NULL AND LENGTH(TRIM(target_name)) > 3
          AND f.thread_id IN ({placeholders})
        GROUP BY LOWER(TRIM(target_name))
        HAVING COUNT(*) <= 2
    """, list(thread_ids)).fetchall()

    created = 0
    total = 0
    for row in rows:
        target = row["target_name"].strip()
        target_lower = target.lower()
        total += 1

        dedup_key = f"findings_coverage:{target_lower}"
        if is_processed(db, "findings", row["latest_finding_id"], dedup_key):
            continue

        # Skip addresses masquerading as targets
        if _is_address_target(target):
            log_processed(db, "findings", row["latest_finding_id"], dedup_key)
            continue

        # Skip very generic targets
        skip_targets = {
            "unknown", "n/a", "none", "self", "personal",
            "department of defense", "united states", "fbi", "doj", "cia",
            "congress", "white house", "pentagon", "ice", "dhs",
        }
        if target_lower in skip_targets:
            log_processed(db, "findings", row["latest_finding_id"], dedup_key)
            continue

        # Skip targets that are descriptions, not proper names
        desc_patterns = ["program", "policy", "analysis", "overview", "summary",
                         "landscape", "ecosystem", "complex", "pipeline", "nexus",
                         "network", "search results", "red flags", "conflicts",
                         "pattern", "architecture", "strategy", "framework"]
        if any(p in target_lower for p in desc_patterns):
            log_processed(db, "findings", row["latest_finding_id"], dedup_key)
            continue

        # Skip targets with " / " compound separators (usually topic descriptions)
        if " / " in target and not target.endswith("LLC") and not target.endswith("Inc."):
            log_processed(db, "findings", row["latest_finding_id"], dedup_key)
            continue

        # Require at least 1 connection (network significance gate)
        conn_count = db.execute(
            "SELECT COUNT(*) FROM connections WHERE person_a LIKE ? OR person_b LIKE ?",
            (f"%{target[:30]}%", f"%{target[:30]}%")
        ).fetchone()[0]
        if conn_count == 0:
            log_processed(db, "findings", row["latest_finding_id"], dedup_key)
            continue

        # Check if lead already exists for this target
        existing_lead = db.execute(
            """SELECT id FROM leads WHERE
               (target_name LIKE ? OR title LIKE ?)
               AND status IN ('open', 'in_progress', 'pending_triage')""",
            (f"%{target[:30]}%", f"%{target[:30]}%")
        ).fetchone()
        if existing_lead:
            log_processed(db, "findings", row["latest_finding_id"], dedup_key, existing_lead["id"])
            continue

        priority = person_priority(target)
        if conn_count >= 2 and priority == "medium":
            priority = "high"

        threads = row["threads"] or "none"
        title = f"Coverage gap: {target} — only {row['finding_count']} finding(s), expand"
        notes = (f"Target has {row['finding_count']} finding(s) and {conn_count} connection(s) "
                 f"in threads [{threads}]. Types: {row['finding_types']}. "
                 f"Investigate further: corporate registries, SAM.gov, HigherGov, "
                 f"EDGAR, FEC, CourtListener, media search.")

        if dry_run:
            print(f"  [DRY] Coverage gap ({priority}): {title}")
        else:
            lead_id = create_lead(db, title, "person", priority,
                                  "agent:auto_leads:coverage_gap", target=target, notes=notes,
                                  profile_id=profile_id)
            log_processed(db, "findings", row["latest_finding_id"], dedup_key, lead_id)
            created += 1

    return created, total


def process_contract_patterns(db, thread_ids, profile_id, dry_run=False):
    """Detect patterns in federal contract findings — shared solicitations,
    contracting officers, batch awards, and residential addresses.

    Scoped to the active profile's threads.
    """
    import re

    if not thread_ids:
        return 0, 0

    placeholders = ",".join("?" * len(thread_ids))
    rows = db.execute(f"""
        SELECT id, target_name, summary, detail, thread_id
        FROM findings
        WHERE detail IS NOT NULL AND LENGTH(detail) > 50
          AND thread_id IN ({placeholders})
          AND id NOT IN (
              SELECT record_id FROM auto_crossref_log
              WHERE table_name='findings' AND crossref_type='contract_pattern'
          )
    """, list(thread_ids)).fetchall()

    solicitation_map = {}
    officer_map = {}
    residential_flags = []

    for row in rows:
        detail = (row["detail"] or "") + " " + (row["summary"] or "")
        target = row["target_name"] or "unknown"
        fid = row["id"]

        # Extract solicitation numbers (e.g., 26-SOL-DCR-01, 70CDCR26D00000006)
        for sol in re.findall(r'\b(\d{2}-SOL-[A-Z]+-\d+|\d{2}[A-Z]{4}\d{2}[A-Z]\d{7,})\b', detail):
            solicitation_map.setdefault(sol, []).append((fid, target))

        # Extract contracting officer codes (e.g., JABYAD7012, SWRAY7012)
        for officer in re.findall(r'\b([A-Z]{3,6}\d{4})\b', detail):
            if not re.match(r'^\d', officer) and len(officer) >= 8:
                officer_map.setdefault(officer, []).append((fid, target))

        # Flag residential address indicators
        residential_keywords = ["residential", "sleeping dragon", "home address",
                                "single-family", "apartment", "condo"]
        if any(kw in detail.lower() for kw in residential_keywords):
            residential_flags.append((fid, target, row["thread_id"]))

    created = 0
    total = 0

    for sol_id, entries in solicitation_map.items():
        unique_targets = set(t for _, t in entries)
        if len(unique_targets) >= 3:
            total += 1
            dedup_key = f"solicitation_cluster:{sol_id}"
            if is_processed(db, "findings", 0, dedup_key):
                continue
            existing = lead_exists(db, sol_id)
            if existing:
                log_processed(db, "findings", 0, dedup_key, existing)
                continue

            title = f"Solicitation cluster: {sol_id} — {len(unique_targets)} awardees to investigate"
            targets_str = ", ".join(list(unique_targets)[:10])
            notes = (f"Solicitation {sol_id} awarded to {len(unique_targets)} entities: {targets_str}. "
                     f"Investigate all awardees for shell companies, shared addresses, batch formation patterns.")

            if dry_run:
                print(f"  [DRY] Solicitation cluster (high): {title}")
            else:
                lead_id = create_lead(db, title, "entity", "high",
                                      "agent:auto_leads:contract_pattern", notes=notes,
                                      profile_id=profile_id)
                log_processed(db, "findings", 0, dedup_key, lead_id)
                created += 1

    for officer_code, entries in officer_map.items():
        unique_targets = set(t for _, t in entries)
        if len(unique_targets) >= 3:
            total += 1
            dedup_key = f"officer_cluster:{officer_code}"
            if is_processed(db, "findings", 0, dedup_key):
                continue
            existing = lead_exists(db, officer_code)
            if existing:
                log_processed(db, "findings", 0, dedup_key, existing)
                continue

            title = f"Contracting officer: {officer_code} — approved {len(unique_targets)} entities"
            targets_str = ", ".join(list(unique_targets)[:10])
            notes = (f"Officer code {officer_code} appears in findings for {len(unique_targets)} "
                     f"entities: {targets_str}. Check for internal control violations "
                     f"(same person as preparer/approver) and conflict of interest.")

            if dry_run:
                print(f"  [DRY] Officer cluster (high): {title}")
            else:
                lead_id = create_lead(db, title, "person", "high",
                                      "agent:auto_leads:contract_pattern", target=officer_code,
                                      notes=notes, profile_id=profile_id)
                log_processed(db, "findings", 0, dedup_key, lead_id)
                created += 1

    seen_residential = set()
    for fid, target, thread_id in residential_flags:
        if target.lower() in seen_residential:
            continue
        seen_residential.add(target.lower())
        total += 1
        dedup_key = f"residential_flag:{target.lower()}"
        if is_processed(db, "findings", fid, dedup_key):
            continue
        existing = lead_exists(db, f"residential.*{target[:20]}")
        if existing:
            log_processed(db, "findings", fid, dedup_key, existing)
            continue

        title = f"Residential address flag: {target} — operating from home"
        notes = (f"Finding #{fid} indicates {target} operates from a residential address. "
                 f"For government contractors, this may indicate shell company or sole proprietor "
                 f"handling contracts above typical scale. Verify address, check property records.")

        if dry_run:
            print(f"  [DRY] Residential flag (medium): {title}")
        else:
            lead_id = create_lead(db, title, "entity", "medium",
                                  "agent:auto_leads:contract_pattern", target=target,
                                  notes=notes, profile_id=profile_id)
            log_processed(db, "findings", fid, dedup_key, lead_id)
            created += 1

    for row in rows:
        log_processed(db, "findings", row["id"], "contract_pattern")

    return created, total


def process_connection_persons(db, thread_ids, profile_id, dry_run=False):
    """Generate leads for persons in connections who lack entities or leads.

    Scoped to the active profile via finding_id → findings.thread_id join.
    """
    if not thread_ids:
        return 0, 0

    placeholders = ",".join("?" * len(thread_ids))
    rows = db.execute(f"""
        SELECT person_name, COUNT(*) as conn_count,
               GROUP_CONCAT(DISTINCT relationship_type) as rel_types
        FROM (
            SELECT c.person_a as person_name, c.relationship_type
            FROM connections c
            LEFT JOIN findings f ON c.finding_id = f.id
            WHERE f.thread_id IN ({placeholders})
               OR (c.finding_id IS NULL AND c.profile_id = ?)
            UNION ALL
            SELECT c.person_b as person_name, c.relationship_type
            FROM connections c
            LEFT JOIN findings f ON c.finding_id = f.id
            WHERE f.thread_id IN ({placeholders})
               OR (c.finding_id IS NULL AND c.profile_id = ?)
        )
        WHERE person_name IS NOT NULL AND LENGTH(TRIM(person_name)) > 3
        GROUP BY LOWER(TRIM(person_name))
        HAVING COUNT(*) >= 2
    """, list(thread_ids) + [profile_id] + list(thread_ids) + [profile_id]).fetchall()

    created = 0
    total = 0
    for row in rows:
        person = row["person_name"].strip()
        person_lower = person.lower()
        total += 1

        dedup_key = f"connection_person:{person_lower}"
        if is_processed(db, "connections", 0, dedup_key):
            continue

        # Skip if already an entity
        entity_exists = db.execute(
            "SELECT id FROM entities WHERE LOWER(name) = ?", (person_lower,)
        ).fetchone()
        if entity_exists:
            log_processed(db, "connections", 0, dedup_key)
            continue

        # Skip if already has an open lead
        existing_lead = db.execute(
            """SELECT id FROM leads WHERE
               (target_name LIKE ? OR title LIKE ?)
               AND status IN ('open', 'in_progress', 'pending_triage')""",
            (f"%{person[:30]}%", f"%{person[:30]}%")
        ).fetchone()
        if existing_lead:
            log_processed(db, "connections", 0, dedup_key, existing_lead["id"])
            continue

        skip_persons = {"donald trump", "joe biden", "barack obama", "hillary clinton",
                        "vladimir putin", "mike pence", "mbs", "kamala harris"}
        if person_lower in skip_persons:
            log_processed(db, "connections", 0, dedup_key)
            continue

        finding_count = db.execute(
            "SELECT COUNT(*) FROM findings WHERE LOWER(target_name) LIKE ?",
            (f"%{person_lower[:30]}%",)
        ).fetchone()[0]

        if finding_count >= 5:
            log_processed(db, "connections", 0, dedup_key)
            continue

        priority = person_priority(person)
        if row["conn_count"] >= 4:
            priority = "high"

        rel_types = row["rel_types"] or "unknown"
        title = f"Network node: {person} — {row['conn_count']} connections, {finding_count} findings"
        notes = (f"Person has {row['conn_count']} connections ({rel_types}) but only "
                 f"{finding_count} findings and no entity record. "
                 f"Investigate: corporate registries, EDGAR, FEC, CourtListener, "
                 f"SAM.gov, HigherGov, media search.")

        if dry_run:
            print(f"  [DRY] Network node ({priority}): {title}")
        else:
            lead_id = create_lead(db, title, "person", priority,
                                  "agent:auto_leads:connection_person", target=person,
                                  notes=notes, profile_id=profile_id)
            log_processed(db, "connections", 0, dedup_key, lead_id)
            created += 1

    return created, total


def process_entity_crossref(db, dry_run=False):
    """Fuzzy-match newly ingested entities against existing entities/connections/findings.

    Inspired by Aleph/OpenAleph cross-reference on ingest.
    """
    from rapidfuzz import fuzz

    try:
        from tools.entity_resolution import normalize_entity_name
    except ImportError:
        from entity_resolution import normalize_entity_name

    THRESHOLD = 85

    # Get entities not yet cross-referenced
    rows = db.execute("""
        SELECT id, name, entity_type, jurisdiction
        FROM entities
        WHERE id NOT IN (
            SELECT record_id FROM auto_crossref_log
            WHERE table_name='entities' AND crossref_type='entity_crossref'
        )
    """).fetchall()

    # Load all entity names for matching
    all_entities = db.execute("SELECT id, name FROM entities").fetchall()
    entity_names = [(r["id"], r["name"], normalize_entity_name(r["name"])) for r in all_entities]

    # Load connection person names for matching
    conn_persons = set()
    for r in db.execute("SELECT DISTINCT person_a FROM connections").fetchall():
        conn_persons.add(r["person_a"])
    for r in db.execute("SELECT DISTINCT person_b FROM connections").fetchall():
        conn_persons.add(r["person_b"])

    # Load finding target names
    finding_targets = set()
    for r in db.execute("SELECT DISTINCT target_name FROM findings WHERE target_name IS NOT NULL").fetchall():
        finding_targets.add(r["target_name"])

    created = 0
    for row in rows:
        name = row["name"]
        if not name or len(name.strip()) < 3:
            log_processed(db, "entities", row["id"], "entity_crossref")
            continue

        norm = normalize_entity_name(name)
        if len(norm) < 3:
            log_processed(db, "entities", row["id"], "entity_crossref")
            continue

        matches = []

        # Match against other entities
        for eid, ename, enorm in entity_names:
            if eid == row["id"]:
                continue
            score = fuzz.token_sort_ratio(norm, enorm)
            if score >= THRESHOLD:
                matches.append(("entity", eid, ename, score))

        # Match against connection persons
        for person in conn_persons:
            score = fuzz.token_sort_ratio(norm, person.lower())
            if score >= THRESHOLD:
                matches.append(("connection_person", None, person, score))

        # Match against finding targets
        for target in finding_targets:
            score = fuzz.token_sort_ratio(norm, target.lower())
            if score >= THRESHOLD:
                matches.append(("finding_target", None, target, score))

        if matches:
            # Take best match
            best = max(matches, key=lambda m: m[3])
            match_type, match_id, match_name, score = best

            title = f"Entity crossref: '{name.strip()[:30]}' ↔ '{match_name[:30]}' (score={score})"
            existing = lead_exists(db, f"Entity crossref: '{name.strip()[:30]}'")
            if existing:
                log_processed(db, "entities", row["id"], "entity_crossref", existing)
                continue

            notes = f"Fuzzy match ({score}%) between entity #{row['id']} '{name}' and {match_type} '{match_name}'."
            if match_id:
                notes += f" Match entity ID: {match_id}."
            notes += f" {len(matches)} total matches above {THRESHOLD}% threshold."

            priority = "high" if score >= 95 else "medium"

            if dry_run:
                print(f"  [DRY] Entity crossref ({priority}): {title}")
            else:
                lead_id = create_lead(db, title, "entity", priority,
                                      "agent:auto_leads:entity_crossref",
                                      target=name.strip(), notes=notes)
                log_processed(db, "entities", row["id"], "entity_crossref", lead_id)
                created += 1
        else:
            log_processed(db, "entities", row["id"], "entity_crossref")

    return created, len(rows)


def process_person_crossref(db, dry_run=False):
    """Fuzzy-match entity_roles person names against connections/findings."""
    from rapidfuzz import fuzz

    try:
        from tools.entity_resolution import normalize_person_name
    except ImportError:
        from entity_resolution import normalize_person_name

    THRESHOLD = 85

    # Get person names from entity_roles not yet cross-referenced
    rows = db.execute("""
        SELECT er.id, er.person_name, er.entity_id, e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
        WHERE er.id NOT IN (
            SELECT record_id FROM auto_crossref_log
            WHERE table_name='entity_roles' AND crossref_type='person_crossref'
        )
    """).fetchall()

    # Load connection person names
    conn_persons = set()
    for r in db.execute("SELECT DISTINCT person_a FROM connections").fetchall():
        conn_persons.add(r["person_a"])
    for r in db.execute("SELECT DISTINCT person_b FROM connections").fetchall():
        conn_persons.add(r["person_b"])

    # Load finding target names
    finding_targets = set()
    for r in db.execute("SELECT DISTINCT target_name FROM findings WHERE target_name IS NOT NULL").fetchall():
        finding_targets.add(r["target_name"])

    created = 0
    for row in rows:
        person = row["person_name"]
        if not person or len(person.strip()) < 3:
            log_processed(db, "entity_roles", row["id"], "person_crossref")
            continue

        norm = normalize_person_name(person)
        if len(norm) < 3:
            log_processed(db, "entity_roles", row["id"], "person_crossref")
            continue

        matches = []

        for cp in conn_persons:
            score = fuzz.token_sort_ratio(norm, normalize_person_name(cp))
            if score >= THRESHOLD:
                matches.append(("connection", cp, score))

        for ft in finding_targets:
            score = fuzz.token_sort_ratio(norm, ft.lower())
            if score >= THRESHOLD:
                matches.append(("finding", ft, score))

        if matches:
            best = max(matches, key=lambda m: m[2])
            match_type, match_name, score = best

            title = f"Person crossref: '{person.strip()[:25]}' (officer of {row['entity_name'][:20]}) ↔ '{match_name[:25]}' ({score}%)"
            existing = lead_exists(db, f"Person crossref: '{person.strip()[:25]}'")
            if existing:
                log_processed(db, "entity_roles", row["id"], "person_crossref", existing)
                continue

            notes = (f"Officer '{person}' of entity '{row['entity_name']}' fuzzy-matches "
                     f"{match_type} '{match_name}' at {score}%. "
                     f"{len(matches)} total matches above {THRESHOLD}%.")

            priority = "high" if score >= 95 else "medium"

            if dry_run:
                print(f"  [DRY] Person crossref ({priority}): {title}")
            else:
                lead_id = create_lead(db, title, "person", priority,
                                      "agent:auto_leads:person_crossref",
                                      target=person.strip(), notes=notes)
                log_processed(db, "entity_roles", row["id"], "person_crossref", lead_id)
                created += 1
        else:
            log_processed(db, "entity_roles", row["id"], "person_crossref")

    return created, len(rows)


def process_enforcement_check(db, dry_run=False):
    """Check new entities and connections against SEC enforcement defendants.

    Looks up newly added entities and connection persons in the SEC enforcement
    database (datasets/sec_enforcement.db). Creates leads for matches.
    """
    ENFORCEMENT_DB = PROJECT_ROOT / "datasets" / "sec_enforcement.db"
    if not ENFORCEMENT_DB.exists():
        return 0, 0

    try:
        from tools.entity_resolution import normalize_entity_name, normalize_person_name
    except ImportError:
        try:
            from entity_resolution import normalize_entity_name, normalize_person_name
        except ImportError:
            return 0, 0

    enf_db = sqlite3.connect(str(ENFORCEMENT_DB))
    enf_db.row_factory = sqlite3.Row

    # Build a set of all normalized enforcement defendant names for fast lookup
    enf_names = set()
    enf_rows = enf_db.execute(
        "SELECT DISTINCT name_normalized FROM enforcement_defendants"
    ).fetchall()
    for r in enf_rows:
        enf_names.add(r["name_normalized"])

    # --- Check new entities ---
    entity_rows = db.execute("""
        SELECT e.id, e.name, e.entity_type
        FROM entities e
        WHERE e.id NOT IN (
            SELECT record_id FROM auto_crossref_log
            WHERE table_name='entities' AND crossref_type='enforcement_check'
        )
    """).fetchall()

    # --- Check new connections (unique person names) ---
    conn_rows = db.execute("""
        SELECT c.id, c.person_a, c.person_b
        FROM connections c
        WHERE c.id NOT IN (
            SELECT record_id FROM auto_crossref_log
            WHERE table_name='connections' AND crossref_type='enforcement_check'
        )
    """).fetchall()

    created = 0
    total_checked = 0
    seen_names = set()

    # Check entities
    for row in entity_rows:
        name = row["name"]
        if not name or len(name.strip()) < 3:
            log_processed(db, "entities", row["id"], "enforcement_check")
            continue

        total_checked += 1
        norm_e = normalize_entity_name(name)
        norm_p = normalize_person_name(name)

        matched_norm = None
        if norm_e in enf_names:
            matched_norm = norm_e
        elif norm_p in enf_names:
            matched_norm = norm_p

        if matched_norm and matched_norm not in seen_names:
            seen_names.add(matched_norm)

            # Get enforcement details
            enf_details = enf_db.execute("""
                SELECT ea.release_number, ea.source_type, ea.date_published, ea.respondent_text,
                       ed.name_raw, ed.defendant_type
                FROM enforcement_defendants ed
                JOIN enforcement_actions ea ON ed.action_id = ea.id
                WHERE ed.name_normalized = ?
                ORDER BY ea.date_published DESC LIMIT 5
            """, (matched_norm,)).fetchall()

            action_count = enf_db.execute(
                "SELECT COUNT(DISTINCT action_id) FROM enforcement_defendants WHERE name_normalized = ?",
                (matched_norm,)
            ).fetchone()[0]

            title = f"SEC enforcement history: {name.strip()[:40]} ({action_count} actions)"
            existing = lead_exists(db, f"SEC enforcement history: {name.strip()[:30]}")
            if existing:
                log_processed(db, "entities", row["id"], "enforcement_check", existing)
                continue

            details_text = "\n".join(
                f"  {d['release_number']} ({d['source_type']}, {d['date_published']}): {d['respondent_text'][:120]}"
                for d in enf_details
            )
            notes = (
                f"Entity '{name}' matches SEC enforcement defendant '{enf_details[0]['name_raw']}' "
                f"({enf_details[0]['defendant_type']})\n"
                f"{action_count} enforcement action(s):\n{details_text}"
            )

            priority = "high" if action_count >= 3 else "medium"

            if dry_run:
                print(f"  [DRY] Enforcement match ({priority}): {title}")
            else:
                lead_id = create_lead(db, title, "enforcement", priority,
                                      "agent:auto_leads:enforcement_check",
                                      target=name.strip(), notes=notes)
                log_processed(db, "entities", row["id"], "enforcement_check", lead_id)
                created += 1
        else:
            log_processed(db, "entities", row["id"], "enforcement_check")

    # Check connections (both person_a and person_b)
    for row in conn_rows:
        for person in [row["person_a"], row["person_b"]]:
            if not person or len(person.strip()) < 4:
                continue

            total_checked += 1
            norm_p = normalize_person_name(person)

            if norm_p in enf_names and norm_p not in seen_names:
                seen_names.add(norm_p)

                enf_details = enf_db.execute("""
                    SELECT ea.release_number, ea.source_type, ea.date_published, ea.respondent_text,
                           ed.name_raw, ed.defendant_type
                    FROM enforcement_defendants ed
                    JOIN enforcement_actions ea ON ed.action_id = ea.id
                    WHERE ed.name_normalized = ?
                    ORDER BY ea.date_published DESC LIMIT 5
                """, (norm_p,)).fetchall()

                action_count = enf_db.execute(
                    "SELECT COUNT(DISTINCT action_id) FROM enforcement_defendants WHERE name_normalized = ?",
                    (norm_p,)
                ).fetchone()[0]

                title = f"SEC enforcement history: {person.strip()[:40]} ({action_count} actions)"
                existing = lead_exists(db, f"SEC enforcement history: {person.strip()[:30]}")
                if existing:
                    continue

                details_text = "\n".join(
                    f"  {d['release_number']} ({d['source_type']}, {d['date_published']}): {d['respondent_text'][:120]}"
                    for d in enf_details
                )
                notes = (
                    f"Connection person '{person}' matches SEC enforcement defendant "
                    f"'{enf_details[0]['name_raw']}' ({enf_details[0]['defendant_type']})\n"
                    f"{action_count} enforcement action(s):\n{details_text}"
                )

                priority = "high" if action_count >= 3 else "medium"

                if dry_run:
                    print(f"  [DRY] Enforcement match ({priority}): {title}")
                else:
                    lead_id = create_lead(db, title, "enforcement", priority,
                                          "agent:auto_leads:enforcement_check",
                                          target=person.strip(), notes=notes)
                    created += 1

        log_processed(db, "connections", row["id"], "enforcement_check")

    enf_db.close()
    return created, total_checked


def cmd_run(args):
    global _max_leads_per_run, _leads_created_this_run
    _max_leads_per_run = getattr(args, 'max_leads', 100)
    _leads_created_this_run = 0

    db = get_db()

    # Load profile for scoping
    profile = _load_profile(getattr(args, 'profile', None))
    if profile:
        profile_name = profile.name
        bridge = getattr(profile, 'bridge_threads', None) or []
        thread_ids = get_profile_thread_ids(db, profile_name, bridge)
    else:
        profile_name = None
        thread_ids = set()

    print("=" * 60)
    print("AUTO-LEAD GENERATOR — Post-Wave Cross-Reference")
    print("=" * 60)
    if profile_name:
        print(f"Profile: {profile_name} (threads: {sorted(thread_ids)})")
    if args.dry_run:
        print("[DRY RUN — no leads will be created]")

    results = {}
    stopped_early_reason = None
    try:
        # --- Entity-based generators (unscoped — registry data is investigation-agnostic) ---

        print("\n--- New Addresses ---")
        c, t = process_new_addresses(db, args.dry_run)
        results["addresses"] = (c, t)
        print(f"  {t} new addresses scanned, {c} leads created")

        print("\n--- New Officer Roles ---")
        c, t = process_new_roles(db, args.dry_run)
        results["roles"] = (c, t)
        print(f"  {t} new roles scanned, {c} leads created")

        print("\n--- New Entities ---")
        c, t = process_new_entities(db, args.dry_run)
        results["entities"] = (c, t)
        print(f"  {t} new entities scanned, {c} leads created")

        print("\n--- New Connections ---")
        c, t = process_new_connections(db, args.dry_run)
        results["connections"] = (c, t)
        print(f"  {t} new connections scanned, {c} leads created")

        print("\n--- Alumni Clustering ---")
        c, t = process_alumni_clustering(db, args.dry_run)
        results["alumni_clustering"] = (c, t)
        print(f"  {t} institution pairs checked, {c} leads created")

        print("\n--- Pillar Gap Analysis ---")
        c, t = process_pillar_gaps(db, args.dry_run)
        results["pillar_gaps"] = (c, t)
        print(f"  {t} multi-institution persons checked, {c} leads created")

        print("\n--- Serial Director Detection ---")
        c, t = process_officer_escalation(db, args.dry_run)
        results["officer_escalation"] = (c, t)
        print(f"  {t} multi-entity officers checked, {c} escalated/created")

        print("\n--- Filing Date Clusters ---")
        c, t = process_filing_clusters(db, args.dry_run)
        results["filing_clusters"] = (c, t)
        print(f"  {t} officer filing groups checked, {c} leads created")

        print("\n--- Jurisdiction Clusters ---")
        c, t = process_jurisdiction_clusters(db, args.dry_run)
        results["jurisdiction_clusters"] = (c, t)
        print(f"  {t} person-jurisdiction pairs checked, {c} leads created")

        print("\n--- Entity Cross-Reference ---")
        c, t = process_entity_crossref(db, args.dry_run)
        results["entity_crossref"] = (c, t)
        print(f"  {t} entities cross-referenced, {c} leads created")

        print("\n--- Person Cross-Reference ---")
        c, t = process_person_crossref(db, args.dry_run)
        results["person_crossref"] = (c, t)
        print(f"  {t} person names cross-referenced, {c} leads created")

        print("\n--- SEC Enforcement Check ---")
        c, t = process_enforcement_check(db, args.dry_run)
        results["enforcement_check"] = (c, t)
        print(f"  {t} names checked against SEC enforcement DB, {c} leads created")

        # --- Findings-based generators (scoped to active profile's threads) ---

        print("\n--- Findings Coverage Gaps ---")
        c, t = process_findings_coverage_gaps(db, thread_ids, profile_name, args.dry_run)
        results["findings_coverage"] = (c, t)
        print(f"  {t} under-investigated targets checked, {c} leads created")

        print("\n--- Contract Patterns ---")
        c, t = process_contract_patterns(db, thread_ids, profile_name, args.dry_run)
        results["contract_patterns"] = (c, t)
        print(f"  {t} contract patterns checked, {c} leads created")

        print("\n--- Connection Network Nodes ---")
        c, t = process_connection_persons(db, thread_ids, profile_name, args.dry_run)
        results["connection_persons"] = (c, t)
        print(f"  {t} connected persons checked, {c} leads created")

    except LeadLimitReached as exc:
        stopped_early_reason = str(exc)
        print(f"\nLead creation limit reached: {stopped_early_reason}. Stopping further generators.")
    except Exception:
        if not args.dry_run:
            db.rollback()
        db.close()
        raise

    if not args.dry_run:
        db.commit()

    total_created = sum(v[0] for v in results.values())
    total_scanned = sum(v[1] for v in results.values())

    print(f"\n{'=' * 60}")
    print(f"Total: {total_scanned} items scanned, {total_created} leads created")
    if stopped_early_reason:
        print(f"Stopped early: {stopped_early_reason}")
    if args.dry_run:
        print("(Dry run — nothing was saved)")
    print(f"{'=' * 60}")
    db.close()


def cmd_stats(args):
    db = get_db()
    print("Auto Cross-Reference Stats")
    print("-" * 40)
    for table in ["entities", "entity_addresses", "entity_roles", "connections", "findings"]:
        total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        processed = db.execute(
            "SELECT COUNT(DISTINCT record_id) FROM auto_crossref_log WHERE table_name=?",
            (table,)
        ).fetchone()[0]
        leads = db.execute(
            "SELECT COUNT(*) FROM auto_crossref_log WHERE table_name=? AND lead_id IS NOT NULL",
            (table,)
        ).fetchone()[0]
        print(f"  {table}: {processed}/{total} processed, {leads} leads created")

    print()
    sources = db.execute(
        """SELECT source, COUNT(*) FROM leads
           WHERE source LIKE 'agent:auto_leads%'
           GROUP BY source ORDER BY COUNT(*) DESC"""
    ).fetchall()
    total_leads = sum(r[1] for r in sources)
    print(f"Total auto-generated leads: {total_leads}")
    for source, count in sources:
        label = source.replace("agent:auto_leads:", "  ").replace("agent:auto_leads", "  (base)")
        print(f"  {label}: {count}")

    # Per-profile breakdown
    print()
    profiles = db.execute(
        """SELECT COALESCE(profile_id, '(unset)') as pid, COUNT(*) FROM leads
           WHERE source LIKE 'agent:auto_leads%'
           GROUP BY pid ORDER BY COUNT(*) DESC"""
    ).fetchall()
    if profiles:
        print("By profile:")
        for pid, count in profiles:
            print(f"  {pid}: {count}")


def main():
    parser = argparse.ArgumentParser(description="Post-wave auto-lead generator")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="Generate leads from new entity data")
    p.add_argument("--dry-run", action="store_true", help="Preview without creating")
    p.add_argument("--profile", type=str, default=None,
                   help="Override active profile (default: use active profile)")
    p.add_argument("--max-leads", type=int, default=100,
                   help="Maximum number of leads to create per run (default: 100)")

    sub.add_parser("stats", help="Show processing stats")

    args = parser.parse_args()
    if not args.command:
        args.command = "run"
        args.dry_run = False
        args.profile = None
        args.max_leads = 100

    if args.command == "run":
        cmd_run(args)
    elif args.command == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
