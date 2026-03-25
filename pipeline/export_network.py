#!/usr/bin/env python3
"""Export connections and entities from investigation.db as a network graph JSON.

Uses name_aliases table to merge duplicate nodes:
- person_variant: merge split person names ("Barak" + "Ehud Barak" -> one node)
- entity_variant: merge entity name variants ("Gratitude America" + "Gratitude America Ltd")
- entity_as_person: route organization names from connections to entity nodes
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"
OUTPUT_PATH = Path(__file__).parent.parent / "content" / "network.json"

# Add tools to path for name_resolver
sys.path.insert(0, str(Path(__file__).parent.parent))


def slugify(name: str) -> str:
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    return slug.strip('-')


def _load_aliases(conn: sqlite3.Connection) -> dict[str, tuple[str, str, int | None]]:
    """Load aliases from DB: {alias_lower: (canonical, type, entity_id)}."""
    aliases = {}
    try:
        rows = conn.execute("SELECT canonical_name, alias, alias_type, entity_id FROM name_aliases").fetchall()
        for row in rows:
            aliases[row["alias"].lower()] = (row["canonical_name"], row["alias_type"], row["entity_id"])
    except sqlite3.OperationalError:
        pass
    return aliases


def _resolve_node_id(name: str, aliases: dict, entity_id_map: dict[str, str]) -> str:
    """Resolve a person name to its node ID, routing entity_as_person to entity nodes."""
    entry = aliases.get(name.lower())
    if entry:
        canonical, alias_type, entity_id = entry
        if alias_type == "entity_as_person" and entity_id:
            return f"entity:{entity_id}"
        # For person/entity variants, check if canonical is also entity_as_person
        canonical_entry = aliases.get(canonical.lower())
        if canonical_entry and canonical_entry[1] == "entity_as_person" and canonical_entry[2]:
            return f"entity:{canonical_entry[2]}"
        return canonical
    return name


def export_network(db_path: str | Path = DB_PATH) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    aliases = _load_aliases(conn)

    nodes = {}  # id -> node dict
    edges = []

    # 2. Build entity nodes first (so entity_as_person routing has targets)
    entity_rows = conn.execute(
        """
        SELECT e.id, e.name, e.entity_type, e.jurisdiction, e.status
        FROM entities e
        ORDER BY e.name
        """
    ).fetchall()

    entity_id_map = {}  # entity name -> "entity:N"
    for row in entity_rows:
        eid = f"entity:{row['id']}"
        entity_id_map[row["name"].lower()] = eid
        nodes[eid] = {
            "id": eid,
            "name": row["name"],
            "slug": slugify(row["name"]),
            "type": "entity",
            "entity_type": row["entity_type"],
            "jurisdiction": row["jurisdiction"],
            "status": row["status"],
            "connections": 0,
        }

    # 1. Build person nodes from connections table (with alias resolution)
    conn_rows = conn.execute(
        """
        SELECT id, person_a, person_b, relationship_type, description,
               strength, date_range, verification_status, profile_id
        FROM connections
        WHERE verification_status != 'retracted'
        ORDER BY id
        """
    ).fetchall()

    for row in conn_rows:
        resolved_a = _resolve_node_id(row["person_a"], aliases, entity_id_map)
        resolved_b = _resolve_node_id(row["person_b"], aliases, entity_id_map)

        for node_id in [resolved_a, resolved_b]:
            if node_id not in nodes:
                # Determine display name: use canonical or original
                entry = aliases.get(row["person_a"].lower()) if node_id == resolved_a else aliases.get(row["person_b"].lower())
                display_name = entry[0] if entry else node_id
                nodes[node_id] = {
                    "id": node_id,
                    "slug": slugify(display_name),
                    "type": "person",
                    "connections": 0,
                }
            nodes[node_id]["connections"] += 1

        # Skip self-loops (can happen when aliases merge two names)
        if resolved_a == resolved_b:
            continue

        edges.append({
            "source": resolved_a,
            "target": resolved_b,
            "relationship_type": row["relationship_type"],
            "description": row["description"],
            "strength": row["strength"],
            "date_range": row["date_range"],
            "verified": row["verification_status"] == "verified",
            "profile_ids": [row["profile_id"]] if row["profile_id"] else [],
        })

    # 3. Add entity_roles edges (person -> entity) with alias resolution
    role_rows = conn.execute(
        """
        SELECT er.entity_id, er.person_name, er.role, er.date_start, er.date_end,
               e.name as entity_name
        FROM entity_roles er
        JOIN entities e ON er.entity_id = e.id
        """
    ).fetchall()

    for row in role_rows:
        entity_id = f"entity:{row['entity_id']}"
        person = _resolve_node_id(row["person_name"], aliases, entity_id_map)

        if person not in nodes:
            entry = aliases.get(row["person_name"].lower())
            display_name = entry[0] if entry else row["person_name"]
            nodes[person] = {
                "id": person,
                "slug": slugify(display_name),
                "type": "person",
                "connections": 0,
            }

        # Skip self-loops (person resolved to this entity)
        if person == entity_id:
            continue

        nodes[person]["connections"] += 1
        if entity_id in nodes:
            nodes[entity_id]["connections"] += 1

        edges.append({
            "source": person,
            "target": entity_id,
            "relationship_type": "corporate",
            "description": f"{row['role']} of {row['entity_name']}",
            "strength": "strong",
            "date_range": f"{row['date_start'] or '?'} - {row['date_end'] or 'present'}",
            "verified": True,
            "profile_ids": [],
        })

    # 4. Add entity_relations edges (entity -> entity)
    rel_rows = conn.execute(
        """
        SELECT er.entity_a_id, er.entity_b_id, er.relation_type, er.description,
               ea.name as entity_a_name, eb.name as entity_b_name
        FROM entity_relations er
        JOIN entities ea ON er.entity_a_id = ea.id
        JOIN entities eb ON er.entity_b_id = eb.id
        """
    ).fetchall()

    for row in rel_rows:
        source = f"entity:{row['entity_a_id']}"
        target = f"entity:{row['entity_b_id']}"

        if source in nodes:
            nodes[source]["connections"] += 1
        if target in nodes:
            nodes[target]["connections"] += 1

        edges.append({
            "source": source,
            "target": target,
            "relationship_type": row["relation_type"],
            "description": row["description"],
            "strength": "strong",
            "verified": True,
            "profile_ids": [],
        })

    # 5. Add finding counts (resolve target names through aliases)
    finding_counts = conn.execute(
        """
        SELECT target_name, COUNT(*) as cnt
        FROM findings
        WHERE verification_status != 'retracted'
        GROUP BY target_name
        """
    ).fetchall()

    for row in finding_counts:
        resolved = _resolve_node_id(row["target_name"], aliases, entity_id_map)
        if resolved in nodes:
            nodes[resolved]["finding_count"] = nodes[resolved].get("finding_count", 0) + row["cnt"]

    conn.close()

    # Deduplicate edges (same source+target pair)
    edge_map: dict[tuple, dict] = {}
    for edge in edges:
        key = (edge["source"], edge["target"])
        rev_key = (edge["target"], edge["source"])
        # Use canonical direction
        if rev_key in edge_map and key not in edge_map:
            key = rev_key
        if key in edge_map:
            existing = edge_map[key]
            # Merge: keep strongest strength, combine descriptions
            strength_order = ["strong", "medium", "weak", "circumstantial"]
            if strength_order.index(edge.get("strength", "medium")) < strength_order.index(existing.get("strength", "medium")):
                existing["strength"] = edge["strength"]
            existing["verified"] = existing.get("verified", False) or edge.get("verified", False)
            # Merge profile_ids
            merged = set(existing.get("profile_ids", []))
            merged.update(edge.get("profile_ids", []))
            existing["profile_ids"] = sorted(merged)
        else:
            edge_map[key] = edge

    deduped_edges = list(edge_map.values())

    # Build output
    node_list = sorted(nodes.values(), key=lambda n: n.get("connections", 0), reverse=True)
    for node in node_list:
        if node["type"] == "entity" and "name" not in node:
            node["name"] = node["id"]
        elif node["type"] == "person":
            node["name"] = node["id"]

    return {
        "nodes": node_list,
        "edges": deduped_edges,
        "stats": {
            "total_nodes": len(node_list),
            "person_nodes": sum(1 for n in node_list if n["type"] == "person"),
            "entity_nodes": sum(1 for n in node_list if n["type"] == "entity"),
            "total_edges": len(deduped_edges),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Export network graph from investigation.db")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()

    network = export_network()

    if args.stats_only:
        print(json.dumps(network["stats"], indent=2))
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(network, f, indent=2, default=str)

    print(f"Network: {network['stats']['total_nodes']} nodes, {network['stats']['total_edges']} edges")
    print(f"  Persons: {network['stats']['person_nodes']}")
    print(f"  Entities: {network['stats']['entity_nodes']}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
