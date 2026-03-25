#!/usr/bin/env python3
"""Export corporate ownership structures as DAG JSON for CorporateStructure.tsx.

BFS traversal from configured root entities through entity_relations + entity_roles.
Outputs content/structures/*.json.
"""

import argparse
import json
import sqlite3
import sys
from collections import deque
from pathlib import Path

INVESTIGATION_DB = Path(__file__).parent.parent / "investigation.db"
OUTPUT_DIR = Path(__file__).parent.parent / "content" / "structures"

# Add tools to path for name_resolver
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.name_resolver import resolve_canonical

# Ownership/control relation types — these define parent→child in the DAG
STRUCTURAL_RELATIONS = {
    "owns", "controls", "subsidiary", "subsidiary_of", "funds",
    "manages", "invested_in", "transferred_to",
}

# Person roles that justify a person→entity edge
KEY_ROLES = {
    "owner", "founder", "trustee", "beneficiary", "director", "officer",
    "president", "chairman", "beneficial_owner", "ceo", "controller",
    "co-founder", "co-owner", "grantor", "sole_member", "principal",
    "founder/principal", "managing_director",
}

# Structure definitions: (id, title, subtitle, root_entity_ids)
STRUCTURES = [
    (
        "epstein-stc",
        "Epstein / Southern Trust Company",
        "Corporate ownership hierarchy centered on STC (USVI)",
        [4, 133],  # Both STC entity IDs (KPMG + DOJ)
    ),
    (
        "leon-black-apollo",
        "Leon Black / Apollo Financial Pipeline",
        "Black Family Partners and connected investment structures",
        [67],  # Black Family Partners LP
    ),
    (
        "offshore-chains",
        "Offshore Entity Chains",
        "ILEX, Khan Stiftung, Virgo Trust and connected offshore structures",
        [289, 235, 287],  # ILEX, Khan Stiftung, Virgo Trust
    ),
]


def get_db():
    if not INVESTIGATION_DB.exists():
        print(f"  Error: {INVESTIGATION_DB} not found")
        sys.exit(1)
    conn = sqlite3.connect(str(INVESTIGATION_DB))
    conn.row_factory = sqlite3.Row
    return conn


def build_structure(conn, structure_id, title, subtitle, root_ids, max_depth=6):
    """BFS from root entities, collecting nodes and edges."""
    # Load all entities
    entities = {}
    for row in conn.execute("SELECT * FROM entities").fetchall():
        entities[row["id"]] = dict(row)

    # Load all entity relations
    relations = conn.execute("SELECT * FROM entity_relations").fetchall()
    # Build adjacency: entity_a -> entity_b and reverse
    outgoing = {}  # a_id -> [(b_id, relation_type, description)]
    incoming = {}  # b_id -> [(a_id, relation_type, description)]
    for rel in relations:
        rt = rel["relation_type"]
        if rt not in STRUCTURAL_RELATIONS:
            continue
        a, b = rel["entity_a_id"], rel["entity_b_id"]
        # For subsidiary_of, the direction is reversed (b owns a)
        if rt == "subsidiary_of":
            a, b = b, a
            rt = "subsidiary"
        outgoing.setdefault(a, []).append((b, rt, rel["description"] or ""))
        incoming.setdefault(b, []).append((a, rt, rel["description"] or ""))

    # Load person roles
    role_rows = conn.execute("SELECT * FROM entity_roles").fetchall()
    entity_persons = {}  # entity_id -> [(person_name, role)]
    for rr in role_rows:
        role = rr["role"].lower().replace(" ", "_") if rr["role"] else ""
        if role in KEY_ROLES:
            entity_persons.setdefault(rr["entity_id"], []).append(
                (rr["person_name"], role)
            )

    # BFS from roots
    visited_entities = set()
    queue = deque()
    for rid in root_ids:
        if rid in entities:
            queue.append((rid, 0))
            visited_entities.add(rid)

    while queue:
        eid, depth = queue.popleft()
        if depth >= max_depth:
            continue
        # Follow outgoing structural relations
        for target_id, rt, desc in outgoing.get(eid, []):
            if target_id not in visited_entities and target_id in entities:
                visited_entities.add(target_id)
                queue.append((target_id, depth + 1))

    # Collect person nodes for visited entities
    person_nodes = {}  # canonical_name -> set of (entity_id, role)
    for eid in visited_entities:
        for person_name, role in entity_persons.get(eid, []):
            canonical = resolve_canonical(person_name)
            person_nodes.setdefault(canonical, set()).add((eid, role))

    # Build output nodes and edges
    nodes = []
    edges = []
    node_ids = set()

    # Add person nodes
    for person_name, connections in person_nodes.items():
        pid = f"person:{person_name.replace(' ', '_').lower()}"
        if pid not in node_ids:
            nodes.append({
                "id": pid,
                "name": person_name,
                "nodeType": "person",
                "parentIds": [],
            })
            node_ids.add(pid)

        # Person -> entity edges
        for eid, role in connections:
            entity_node_id = f"entity:{eid}"
            edges.append({
                "source": pid,
                "target": entity_node_id,
                "relationType": role,
                "description": f"{person_name} — {role}",
            })

    # Add entity nodes
    for eid in visited_entities:
        ent = entities[eid]
        entity_node_id = f"entity:{eid}"
        # Determine parentIds from incoming structural relations
        parent_ids = []
        for source_id, rt, desc in incoming.get(eid, []):
            if source_id in visited_entities:
                parent_ids.append(f"entity:{source_id}")
        # Also add person parents
        for person_name, connections in person_nodes.items():
            for connected_eid, role in connections:
                if connected_eid == eid:
                    pid = f"person:{person_name.replace(' ', '_').lower()}"
                    parent_ids.append(pid)

        # Deduplicate
        parent_ids = list(dict.fromkeys(parent_ids))

        nodes.append({
            "id": entity_node_id,
            "name": ent["name"],
            "nodeType": "entity",
            "entityType": ent.get("entity_type") or "unknown",
            "jurisdiction": ent.get("jurisdiction") or "unknown",
            "status": ent.get("status") or "unknown",
            "parentIds": parent_ids,
        })
        node_ids.add(entity_node_id)

    # Add entity-to-entity edges
    for eid in visited_entities:
        for target_id, rt, desc in outgoing.get(eid, []):
            if target_id in visited_entities:
                edges.append({
                    "source": f"entity:{eid}",
                    "target": f"entity:{target_id}",
                    "relationType": rt,
                    "description": desc,
                })

    # Filter parentIds to only reference nodes that exist in our set
    for node in nodes:
        node["parentIds"] = [pid for pid in node["parentIds"] if pid in node_ids]

    # Cycle detection: find and break cycles by removing weakest edge
    # Simple DFS-based cycle detection
    broken = _break_cycles(nodes)
    if broken:
        print(f"    Broke {len(broken)} cycle(s)")

    return {
        "id": structure_id,
        "title": title,
        "subtitle": subtitle,
        "nodes": nodes,
        "edges": edges,
    }


def _break_cycles(nodes):
    """Remove parentIds that create cycles. Returns list of broken edges."""
    # Build adjacency: child -> parents
    node_map = {n["id"]: n for n in nodes}
    broken = []

    # Kahn's algorithm: compute in-degree, find nodes with 0 in-degree, remove
    in_degree = {n["id"]: len(n["parentIds"]) for n in nodes}
    children_of = {}  # parent -> [child]
    for n in nodes:
        for pid in n["parentIds"]:
            children_of.setdefault(pid, []).append(n["id"])

    queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
    processed = set()

    while queue:
        nid = queue.popleft()
        processed.add(nid)
        for child_id in children_of.get(nid, []):
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                queue.append(child_id)

    # Nodes not processed are in cycles
    cycle_nodes = set(in_degree.keys()) - processed
    if not cycle_nodes:
        return broken

    # Break cycles: for each cycle node, remove one parent edge
    for nid in cycle_nodes:
        node = node_map[nid]
        cycle_parents = [pid for pid in node["parentIds"] if pid in cycle_nodes]
        if cycle_parents:
            # Remove the first cycle-creating parent
            removed = cycle_parents[0]
            node["parentIds"].remove(removed)
            broken.append((removed, nid))

    return broken


def main():
    parser = argparse.ArgumentParser(description="Export corporate structures")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--structure", type=str, help="Export specific structure ID")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db()

    to_export = STRUCTURES
    if args.structure:
        to_export = [s for s in STRUCTURES if s[0] == args.structure]
        if not to_export:
            print(f"  Unknown structure: {args.structure}")
            sys.exit(1)

    for sid, title, subtitle, root_ids in to_export:
        print(f"  Building {sid}...")
        data = build_structure(conn, sid, title, subtitle, root_ids)
        out_path = args.output_dir / f"{sid}.json"
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"    {len(data['nodes'])} nodes, {len(data['edges'])} edges -> {out_path}")

    conn.close()


if __name__ == "__main__":
    main()
