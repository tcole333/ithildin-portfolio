#!/usr/bin/env python3
"""Entity registry helper for investigation.db.

Provides a small CLI for entity/role/address/relation operations so skills
do not need inline SQL snippets.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DB_PATH = Path(__file__).parent.parent / "investigation.db"

VALID_ENTITY_TYPES = [
    "person",
    "llc",
    "inc",
    "ltd",
    "corporation",
    "pllc",
    "trust",
    "foundation",
    "nonprofit",
    "partnership",
    "fund",
    "association",
    "government",
    "pac",
    "agency",
    "joint_venture",
    "shell",
    "unknown",
]


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    # Reuse canonical schema creation from lead tracker.
    try:
        from tools.lead_tracker import _ensure_schema
    except ModuleNotFoundError:
        from lead_tracker import _ensure_schema

    db = _ensure_schema(db)
    return db


def cmd_lookup(args):
    db = get_db()
    rows = db.execute(
        """
        SELECT id, name, entity_type, jurisdiction, ein, status, source, created_at
        FROM entities
        WHERE name LIKE ?
        ORDER BY name
        LIMIT ?
        """,
        (f"%{args.name}%", args.limit),
    ).fetchall()
    results = [dict(r) for r in rows]
    db.close()

    if write_output(results, args, summary=f"entity lookup '{args.name}'"):
        return

    if not results:
        print(f"No entities found matching '{args.name}'.")
        return

    print(f"Found {len(results)} entities matching '{args.name}':")
    for r in results:
        print(
            f"  #{r['id']:>5} {r['name']} "
            f"[{r.get('entity_type') or 'unknown'} | {r.get('jurisdiction') or '?'} | {r.get('status') or '?'}]"
        )


def cmd_show(args):
    db = get_db()
    entity = db.execute("SELECT * FROM entities WHERE id = ?", (args.entity_id,)).fetchone()
    if not entity:
        db.close()
        print(f"Entity #{args.entity_id} not found.")
        sys.exit(1)

    roles = [
        dict(r)
        for r in db.execute(
            """
            SELECT id, person_name, role, date_start, date_end, source
            FROM entity_roles
            WHERE entity_id = ?
            ORDER BY person_name
            """,
            (args.entity_id,),
        ).fetchall()
    ]
    addresses = [
        dict(r)
        for r in db.execute(
            """
            SELECT id, address, address_type, date_observed, source
            FROM entity_addresses
            WHERE entity_id = ?
            ORDER BY id DESC
            """,
            (args.entity_id,),
        ).fetchall()
    ]
    rel_out = [
        dict(r)
        for r in db.execute(
            """
            SELECT er.id, er.entity_b_id AS related_id, e2.name AS related_name,
                   er.relation_type, er.description, er.source
            FROM entity_relations er
            JOIN entities e2 ON e2.id = er.entity_b_id
            WHERE er.entity_a_id = ?
            ORDER BY er.id DESC
            """,
            (args.entity_id,),
        ).fetchall()
    ]
    rel_in = [
        dict(r)
        for r in db.execute(
            """
            SELECT er.id, er.entity_a_id AS related_id, e1.name AS related_name,
                   er.relation_type, er.description, er.source
            FROM entity_relations er
            JOIN entities e1 ON e1.id = er.entity_a_id
            WHERE er.entity_b_id = ?
            ORDER BY er.id DESC
            """,
            (args.entity_id,),
        ).fetchall()
    ]
    db.close()

    payload = {
        "entity": dict(entity),
        "roles": roles,
        "addresses": addresses,
        "relations_outbound": rel_out,
        "relations_inbound": rel_in,
    }
    if write_output(payload, args, summary=f"entity #{args.entity_id} details"):
        return

    e = payload["entity"]
    print(
        f"Entity #{e['id']}: {e['name']} "
        f"[{e.get('entity_type') or 'unknown'} | {e.get('jurisdiction') or '?'} | {e.get('status') or '?'}]"
    )
    if e.get("source"):
        print(f"  Source: {e['source']}")
    if e.get("notes"):
        print(f"  Notes: {e['notes']}")

    print(f"\nRoles ({len(roles)}):")
    for r in roles:
        span = ""
        if r.get("date_start") or r.get("date_end"):
            span = f" ({r.get('date_start') or '?'} -> {r.get('date_end') or '?'})"
        print(f"  - {r['person_name']} :: {r['role']}{span}")

    print(f"\nAddresses ({len(addresses)}):")
    for a in addresses:
        print(f"  - [{a.get('address_type') or 'registered'}] {a['address']}")

    print(f"\nOutbound Relations ({len(rel_out)}):")
    for r in rel_out:
        print(f"  - {e['name']} --{r['relation_type']}--> {r['related_name']} (#{r['related_id']})")

    print(f"\nInbound Relations ({len(rel_in)}):")
    for r in rel_in:
        print(f"  - {r['related_name']} (#{r['related_id']}) --{r['relation_type']}--> {e['name']}")


def cmd_add_entity(args):
    db = get_db()
    try:
        cursor = db.execute(
            """
            INSERT INTO entities (name, entity_type, jurisdiction, ein, status, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.name.strip(),
                args.entity_type,
                args.jurisdiction,
                args.ein,
                args.status,
                args.source,
                args.notes,
            ),
        )
        entity_id = cursor.lastrowid
        created = True
    except sqlite3.IntegrityError:
        row = db.execute(
            """
            SELECT id FROM entities
            WHERE name = ? AND COALESCE(jurisdiction, '') = COALESCE(?, '')
            """,
            (args.name.strip(), args.jurisdiction),
        ).fetchone()
        if not row:
            db.close()
            raise
        entity_id = row["id"]
        created = False
    db.commit()
    db.close()

    if created:
        print(f"Created entity #{entity_id}: {args.name}")
    else:
        print(f"Entity already exists as #{entity_id}: {args.name}")


def cmd_add_role(args):
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO entity_roles
            (entity_id, person_name, role, date_start, date_end, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            args.entity_id,
            args.person_name.strip(),
            args.role.strip(),
            args.date_start,
            args.date_end,
            args.source,
        ),
    )
    db.commit()
    db.close()
    print(f"Recorded role: entity #{args.entity_id} :: {args.person_name} -> {args.role}")


def cmd_add_address(args):
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO entity_addresses
            (entity_id, address, address_type, date_observed, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            args.entity_id,
            args.address.strip(),
            args.address_type,
            args.date_observed,
            args.source,
        ),
    )
    db.commit()
    db.close()
    print(f"Recorded address for entity #{args.entity_id}: {args.address}")


def cmd_add_relation(args):
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO entity_relations
            (entity_a_id, entity_b_id, relation_type, description, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            args.entity_a_id,
            args.entity_b_id,
            args.relation_type.strip(),
            args.description,
            args.source,
        ),
    )
    db.commit()
    db.close()
    print(
        f"Recorded relation: entity #{args.entity_a_id} --{args.relation_type}--> entity #{args.entity_b_id}"
    )


def main():
    parser = argparse.ArgumentParser(description="Entity registry helper for investigation.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lookup", help="Lookup entities by name")
    p.add_argument("--name", required=True)
    p.add_argument("--limit", type=int, default=30)
    add_output_args(p)

    p = sub.add_parser("show", help="Show entity details with roles/addresses/relations")
    p.add_argument("entity_id", type=int)
    add_output_args(p)

    p = sub.add_parser("add-entity", help="Insert an entity row")
    p.add_argument("--name", required=True)
    p.add_argument("--entity-type", choices=VALID_ENTITY_TYPES, default="unknown")
    p.add_argument("--jurisdiction")
    p.add_argument("--ein")
    p.add_argument("--status", default="active")
    p.add_argument("--source")
    p.add_argument("--notes")

    p = sub.add_parser("add-role", help="Insert a person role for an entity")
    p.add_argument("--entity-id", type=int, required=True)
    p.add_argument("--person-name", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--date-start")
    p.add_argument("--date-end")
    p.add_argument("--source")

    p = sub.add_parser("add-address", help="Insert an address for an entity")
    p.add_argument("--entity-id", type=int, required=True)
    p.add_argument("--address", required=True)
    p.add_argument("--address-type", default="registered")
    p.add_argument("--date-observed")
    p.add_argument("--source")

    p = sub.add_parser("add-relation", help="Insert an entity-to-entity relationship")
    p.add_argument("--entity-a-id", type=int, required=True)
    p.add_argument("--entity-b-id", type=int, required=True)
    p.add_argument("--relation-type", required=True)
    p.add_argument("--description")
    p.add_argument("--source")

    args = parser.parse_args()
    if args.command == "lookup":
        cmd_lookup(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "add-entity":
        cmd_add_entity(args)
    elif args.command == "add-role":
        cmd_add_role(args)
    elif args.command == "add-address":
        cmd_add_address(args)
    elif args.command == "add-relation":
        cmd_add_relation(args)


if __name__ == "__main__":
    main()
