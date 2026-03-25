#!/usr/bin/env python3
"""
FollowTheMoney (FtM) entity schema interop for OSINT investigations.

Export/import investigation.db entities and connections as FtM JSON stream,
enabling interop with Aleph, OpenSanctions, investigraph, and nomenklatura.

Part of investigation.db.

Usage:
    python tools/ftm_bridge.py export [--output FILE]
    python tools/ftm_bridge.py import --input FILE [--dry-run]
    python tools/ftm_bridge.py reconcile --input FILE [--threshold 85] [--limit 50]
"""

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "investigation.db"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db


# ── Entity Type Mapping ────────────────────────────────────

# investigation.db entity_type -> FtM schema
ENTITY_TYPE_MAP = {
    "person": "Person",
    "llc": "Company",
    "inc": "Company",
    "ltd": "Company",
    "corp": "Company",
    "trust": "LegalEntity",
    "foundation": "LegalEntity",
    "nonprofit": "LegalEntity",
    "partnership": "Company",
    "fund": "LegalEntity",
    "government": "PublicBody",
    "bank": "Company",
    "other": "LegalEntity",
}

# Connection relationship_type -> FtM schema (person↔person relationships)
RELATIONSHIP_MAP = {
    "financial": "UnknownLink",
    "social": "Associate",
    "legal": "UnknownLink",
    "intelligence": "UnknownLink",
    "employment": "UnknownLink",
    "familial": "Family",
    "corporate": "UnknownLink",
    "advisory": "Associate",
    "political": "Associate",
}

# entity_role role -> FtM schema
ROLE_MAP = {
    "director": "Directorship",
    "officer": "Directorship",
    "president": "Directorship",
    "secretary": "Directorship",
    "treasurer": "Directorship",
    "registered_agent": "Representation",
    "member": "Membership",
    "manager": "Directorship",
    "partner": "Ownership",
    "owner": "Ownership",
    "shareholder": "Ownership",
    "trustee": "Directorship",
    "agent": "Representation",
    "ceo": "Directorship",
    "cfo": "Directorship",
    "chairman": "Directorship",
}


def _make_id(prefix, *parts):
    """Generate a deterministic ID from parts."""
    key = "|".join(str(p) for p in parts)
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:12]}"


def export_entities_ftm(db):
    """Export investigation.db entities as FtM JSON entities.

    Returns list of FtM entity dicts (JSON-serializable).
    """
    try:
        from followthemoney import model
        use_ftm = True
    except ImportError:
        use_ftm = False

    entities = db.execute("""
        SELECT id, name, entity_type, jurisdiction, ein, address, status, notes, date_formed
        FROM entities
    """).fetchall()

    ftm_entities = []

    for e in entities:
        schema = ENTITY_TYPE_MAP.get(e["entity_type"], "LegalEntity")
        ftm_id = _make_id("inv", "entity", e["id"])

        if use_ftm:
            proxy = model.make_entity(schema)
            proxy.id = ftm_id
            proxy.add("name", e["name"])
            if e["jurisdiction"]:
                proxy.add("jurisdiction", e["jurisdiction"])
            if e["address"]:
                proxy.add("address", e["address"])
            if e["ein"]:
                proxy.add("registrationNumber", e["ein"])
            if e["notes"]:
                proxy.add("notes", e["notes"])
            ftm_entities.append(proxy.to_dict())
        else:
            ftm_entities.append({
                "id": ftm_id,
                "schema": schema,
                "properties": {
                    "name": [e["name"]],
                    **({"jurisdiction": [e["jurisdiction"]]} if e["jurisdiction"] else {}),
                    **({"address": [e["address"]]} if e["address"] else {}),
                    **({"registrationNumber": [e["ein"]]} if e["ein"] else {}),
                    **({"notes": [e["notes"]]} if e["notes"] else {}),
                },
            })

    # Export entity_roles as relationship entities
    roles = db.execute("""
        SELECT er.id, er.entity_id, er.person_name, er.role, er.date_start, er.date_end,
               e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
    """).fetchall()

    for r in roles:
        schema = ROLE_MAP.get(r["role"], "UnknownLink")
        ftm_id = _make_id("inv", "role", r["id"])
        person_id = _make_id("inv", "person", r["person_name"])
        entity_id = _make_id("inv", "entity", r["entity_id"])

        # Also emit the person entity
        if use_ftm:
            person = model.make_entity("Person")
            person.id = person_id
            person.add("name", r["person_name"])
            ftm_entities.append(person.to_dict())

            rel = model.make_entity(schema)
            rel.id = ftm_id
            # Map person → correct property per schema
            person_prop = {"Directorship": "director", "Ownership": "owner",
                           "Membership": "member", "Representation": "agent",
                           "UnknownLink": "subject"}.get(schema, "subject")
            org_prop = {"Directorship": "organization", "Membership": "organization",
                        "Ownership": "asset", "Representation": "client",
                        "UnknownLink": "object"}.get(schema, "object")
            rel.add(person_prop, person_id)
            rel.add(org_prop, entity_id)
            rel.add("role", r["role"])
            if r["date_start"]:
                rel.add("startDate", r["date_start"])
            if r["date_end"]:
                rel.add("endDate", r["date_end"])
            ftm_entities.append(rel.to_dict())
        else:
            ftm_entities.append({
                "id": person_id,
                "schema": "Person",
                "properties": {"name": [r["person_name"]]},
            })
            person_prop = {"Directorship": "director", "Ownership": "owner",
                           "Membership": "member", "Representation": "agent",
                           "UnknownLink": "subject"}.get(schema, "subject")
            org_prop = {"Directorship": "organization", "Membership": "organization",
                        "Ownership": "asset", "Representation": "client",
                        "UnknownLink": "object"}.get(schema, "object")
            ftm_entities.append({
                "id": ftm_id,
                "schema": schema,
                "properties": {
                    person_prop: [person_id],
                    org_prop: [entity_id],
                    "role": [r["role"]],
                    **({"startDate": [r["date_start"]]} if r["date_start"] else {}),
                    **({"endDate": [r["date_end"]]} if r["date_end"] else {}),
                },
            })

    return ftm_entities


def export_connections_ftm(db):
    """Export connections as FtM relationship entities."""
    try:
        from followthemoney import model
        use_ftm = True
    except ImportError:
        use_ftm = False

    rows = db.execute("""
        SELECT id, person_a, person_b, relationship_type, description, strength
        FROM connections
    """).fetchall()

    ftm_entities = []
    for r in rows:
        schema = RELATIONSHIP_MAP.get(r["relationship_type"], "UnknownLink")
        ftm_id = _make_id("inv", "conn", r["id"])

        # Map properties per schema
        prop_map = {
            "Associate": ("person", "associate"),
            "Family": ("person", "relative"),
            "Employment": ("employee", "employer"),
            "Representation": ("agent", "client"),
        }
        prop_a, prop_b = prop_map.get(schema, ("subject", "object"))

        if use_ftm:
            rel = model.make_entity(schema)
            rel.id = ftm_id
            rel.add(prop_a, _make_id("inv", "person", r["person_a"]))
            rel.add(prop_b, _make_id("inv", "person", r["person_b"]))
            if r["description"]:
                rel.add("summary", r["description"])
            ftm_entities.append(rel.to_dict())
        else:
            props = {
                prop_a: [_make_id("inv", "person", r["person_a"])],
                prop_b: [_make_id("inv", "person", r["person_b"])],
            }
            if r["description"]:
                props["summary"] = [r["description"]]
            ftm_entities.append({
                "id": ftm_id,
                "schema": schema,
                "properties": props,
            })

    return ftm_entities


def import_ftm_entities(db, ftm_stream, dry_run=False):
    """Import FtM JSON entities into investigation.db.

    Args:
        ftm_stream: list of FtM entity dicts
        dry_run: if True, only count what would be imported

    Returns: dict with import counts
    """
    counts = {"entities": 0, "persons": 0, "relationships": 0, "skipped": 0}

    entity_schemas = {"Company", "LegalEntity", "Organization", "PublicBody"}
    person_schemas = {"Person"}
    rel_schemas = {"Directorship", "Ownership", "Membership", "Employment",
                   "Associate", "Family", "UnknownLink", "Representation"}

    # Reverse map for entity types
    ftm_to_type = {v: k for k, v in ENTITY_TYPE_MAP.items()}
    ftm_to_type["Company"] = "llc"
    ftm_to_type["PublicBody"] = "government"

    for entity in ftm_stream:
        schema = entity.get("schema", "")
        props = entity.get("properties", {})
        names = props.get("name", [])

        if not names:
            counts["skipped"] += 1
            continue

        name = names[0]

        if schema in entity_schemas:
            if dry_run:
                counts["entities"] += 1
                continue
            entity_type = ftm_to_type.get(schema, "other")
            jurisdiction = (props.get("jurisdiction", [None]) or [None])[0]
            address = (props.get("address", [None]) or [None])[0]
            ein = (props.get("registrationNumber", [None]) or [None])[0]

            try:
                db.execute("""
                    INSERT OR IGNORE INTO entities (name, entity_type, jurisdiction, ein, address,
                                                    source, created_at)
                    VALUES (?, ?, ?, ?, ?, 'ftm_import', datetime('now'))
                """, (name, entity_type, jurisdiction, ein, address))
                counts["entities"] += 1
            except sqlite3.IntegrityError:
                counts["skipped"] += 1

        elif schema in person_schemas:
            counts["persons"] += 1  # Tracked but not inserted (persons live in connections)

        elif schema in rel_schemas:
            counts["relationships"] += 1

        else:
            counts["skipped"] += 1

    if not dry_run:
        db.commit()

    return counts


def reconcile_ftm(db, ftm_stream, threshold=85, limit=50):
    """Match investigation.db entities against an external FtM dataset.

    Returns list of match candidates.
    """
    from rapidfuzz import fuzz

    try:
        from tools.entity_resolution import normalize_entity_name
    except ImportError:
        from entity_resolution import normalize_entity_name

    # Load our entities
    our_entities = db.execute("SELECT id, name, entity_type, jurisdiction FROM entities").fetchall()
    our_normalized = [(r["id"], r["name"], normalize_entity_name(r["name"])) for r in our_entities]

    # Extract names from FtM stream
    external = []
    for entity in ftm_stream:
        schema = entity.get("schema", "")
        if schema in ("Company", "LegalEntity", "Organization", "PublicBody", "Person"):
            names = entity.get("properties", {}).get("name", [])
            for name in names:
                external.append({
                    "ftm_id": entity.get("id", ""),
                    "name": name,
                    "schema": schema,
                    "normalized": normalize_entity_name(name),
                })

    matches = []
    for ext in external:
        for our_id, our_name, our_norm in our_normalized:
            score = fuzz.token_sort_ratio(ext["normalized"], our_norm)
            if score >= threshold:
                matches.append({
                    "our_id": our_id,
                    "our_name": our_name,
                    "ftm_id": ext["ftm_id"],
                    "ftm_name": ext["name"],
                    "ftm_schema": ext["schema"],
                    "score": score,
                })
                if len(matches) >= limit:
                    return sorted(matches, key=lambda m: m["score"], reverse=True)

    return sorted(matches, key=lambda m: m["score"], reverse=True)[:limit]


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FollowTheMoney entity interop")
    sub = parser.add_subparsers(dest="command")

    p_export = sub.add_parser("export", help="Export entities as FtM JSON stream")
    add_output_args(p_export)

    p_import = sub.add_parser("import", help="Import FtM entities into investigation.db")
    p_import.add_argument("--input", required=True, help="Path to FtM JSON lines file")
    p_import.add_argument("--dry-run", action="store_true")

    p_reconcile = sub.add_parser("reconcile", help="Match entities against external FtM dataset")
    p_reconcile.add_argument("--input", required=True, help="Path to FtM JSON lines file")
    p_reconcile.add_argument("--threshold", type=int, default=85)
    p_reconcile.add_argument("--limit", type=int, default=50)
    add_output_args(p_reconcile)

    args = parser.parse_args()

    if args.command == "export":
        db = get_db()
        entities = export_entities_ftm(db)
        connections = export_connections_ftm(db)
        all_ftm = entities + connections
        db.close()

        if write_output(all_ftm, args, summary=f"FtM export ({len(all_ftm)} entities)"):
            return

        # Write as JSON lines to stdout
        print(f"# FtM export: {len(entities)} entities + {len(connections)} connections")
        for e in all_ftm:
            print(json.dumps(e))

    elif args.command == "import":
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"ERROR: File not found: {args.input}")
            sys.exit(1)

        ftm_stream = []
        with open(input_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ftm_stream.append(json.loads(line))

        db = get_db()
        counts = import_ftm_entities(db, ftm_stream, dry_run=args.dry_run)
        db.close()

        prefix = "[DRY RUN] Would import" if args.dry_run else "Imported"
        print(f"\n{prefix}:")
        print(f"  Entities:      {counts['entities']}")
        print(f"  Persons:       {counts['persons']} (tracked in connections)")
        print(f"  Relationships: {counts['relationships']}")
        print(f"  Skipped:       {counts['skipped']}")

    elif args.command == "reconcile":
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"ERROR: File not found: {args.input}")
            sys.exit(1)

        ftm_stream = []
        with open(input_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ftm_stream.append(json.loads(line))

        db = get_db()
        matches = reconcile_ftm(db, ftm_stream, threshold=args.threshold, limit=args.limit)
        db.close()

        if write_output(matches, args, summary=f"FtM reconciliation ({len(matches)} matches)"):
            return

        print(f"\nReconciliation Matches ({len(matches)}):")
        print(f"{'Score':>5}  {'Our Entity':<35}  {'FtM Entity':<35}  Schema")
        print("-" * 85)
        for m in matches:
            print(f"{m['score']:>5}  {m['our_name'][:35]:<35}  {m['ftm_name'][:35]:<35}  {m['ftm_schema']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
