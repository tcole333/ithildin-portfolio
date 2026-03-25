#!/usr/bin/env python3
"""
Network analysis primitives for OSINT investigations.

Builds an in-memory graph from connections table + name alias resolution,
then computes centrality, components, bridges, shortest paths, etc.
Pure stdlib (no networkx). Caches metrics in graph_metrics table.

Part of investigation.db.

Usage:
    python tools/graph_tools.py centrality [--metric degree|betweenness] [--top 30] [--output FILE]
    python tools/graph_tools.py components [--min-size 3] [--output FILE]
    python tools/graph_tools.py bridges [--output FILE]
    python tools/graph_tools.py paths "Leon Black" "Ehud Barak" [--max-hops 4]
    python tools/graph_tools.py neighbors "Leon Black" [--depth 2] [--output FILE]
    python tools/graph_tools.py holes [--min-degree 5] [--output FILE]
    python tools/graph_tools.py cliques [--min-size 4] [--output FILE]
    python tools/graph_tools.py triangles [--top 50] [--min-strength medium] [--rel-type financial] [--output FILE]
    python tools/graph_tools.py clustering [--min-degree 2] [--top 50] [--output FILE]
    python tools/graph_tools.py stats
"""

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

try:
    from tools.investigation_context import get_active_profile_id
except ImportError:
    try:
        from investigation_context import get_active_profile_id
    except ImportError:
        def get_active_profile_id():
            return ""


def _resolve_profile(profile_id=None, all_profiles=False):
    """Resolve profile_id: explicit > active profile > None."""
    if all_profiles:
        return None
    if profile_id is not None:
        return profile_id
    return get_active_profile_id() or None


VALID_METRICS = ["degree", "betweenness", "closeness"]


# ── Schema ────────────────────────────────────────────────────

def _ensure_graph_schema(db):
    """Create graph_metrics table if it doesn't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS graph_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_name TEXT NOT NULL,
            metric_type TEXT NOT NULL,
            metric_value REAL,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            analysis_run_id INTEGER,
            UNIQUE(node_name, metric_type, analysis_run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_graph_metrics_node ON graph_metrics(node_name);
        CREATE INDEX IF NOT EXISTS idx_graph_metrics_type ON graph_metrics(metric_type);
    """)


def get_graph_db():
    """Get DB connection with graph schema ensured."""
    db = get_db()
    _ensure_graph_schema(db)
    return db


# ── Graph Construction ────────────────────────────────────────

def _load_aliases(db):
    """Load name alias map: alias -> canonical_name."""
    alias_map = {}
    try:
        rows = db.execute("SELECT alias_name, canonical_name FROM name_aliases").fetchall()
        for r in rows:
            alias_map[r["alias_name"].lower()] = r["canonical_name"]
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet
    return alias_map


def _canonicalize(name, alias_map):
    """Resolve a name to its canonical form."""
    if not name:
        return name
    lower = name.strip().lower()
    return alias_map.get(lower, name.strip())


def build_graph(db=None, as_of=None, profile_id=None, all_profiles=False):
    """Build adjacency list from connections table with alias resolution.

    Args:
        db: Optional database connection.
        as_of: Optional date string (YYYY-MM-DD). If provided, only include
               connections where valid_from <= as_of and (valid_until IS NULL
               or valid_until >= as_of). Connections without temporal data
               are always included.
        profile_id: Scope to connections from this profile. Defaults to active profile.
        all_profiles: If True, include all profiles regardless of active profile.

    Returns:
        adj: dict[str, dict[str, list[dict]]] — adj[a][b] = list of connection records
        nodes: set of all node names
    """
    close_db = False
    if db is None:
        db = get_graph_db()
        close_db = True

    resolved = _resolve_profile(profile_id, all_profiles)
    alias_map = _load_aliases(db)

    conditions = []
    params = []
    if resolved:
        conditions.append("profile_id = ?")
        params.append(resolved)
    if as_of:
        conditions.append("(valid_from IS NULL OR valid_from <= ?)")
        conditions.append("(valid_until IS NULL OR valid_until >= ?)")
        params.extend([as_of, as_of])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cols = "id, person_a, person_b, relationship_type, description, strength, created_at"
    if as_of:
        cols += ", valid_from, valid_until"
    rows = db.execute(f"""
        SELECT {cols} FROM connections {where}
    """, params).fetchall()

    adj = defaultdict(lambda: defaultdict(list))
    nodes = set()

    for r in rows:
        a = _canonicalize(r["person_a"], alias_map)
        b = _canonicalize(r["person_b"], alias_map)
        if not a or not b or a == b:
            continue

        edge_data = {
            "id": r["id"],
            "type": r["relationship_type"],
            "description": r["description"],
            "strength": r["strength"],
        }
        adj[a][b].append(edge_data)
        adj[b][a].append(edge_data)
        nodes.add(a)
        nodes.add(b)

    # Also ingest entity_relations (entity-to-entity links stored separately)
    try:
        er_rows = db.execute("""
            SELECT er.id, ea.name AS entity_a, eb.name AS entity_b,
                   er.relation_type, er.description
            FROM entity_relations er
            JOIN entities ea ON ea.id = er.entity_a_id
            JOIN entities eb ON eb.id = er.entity_b_id
        """).fetchall()
        for r in er_rows:
            a = _canonicalize(r["entity_a"], alias_map)
            b = _canonicalize(r["entity_b"], alias_map)
            if not a or not b or a == b:
                continue
            edge_data = {
                "id": f"er:{r['id']}",
                "type": r["relation_type"],
                "description": r["description"],
                "strength": "medium",
            }
            adj[a][b].append(edge_data)
            adj[b][a].append(edge_data)
            nodes.add(a)
            nodes.add(b)
    except sqlite3.OperationalError:
        pass  # entity_relations table may not exist

    if close_db:
        db.close()

    return dict(adj), nodes


# ── Graph Algorithms ────────────────────────────────────────

def degree_centrality(adj, nodes):
    """Compute degree centrality (number of unique neighbors)."""
    result = {}
    for node in nodes:
        result[node] = len(adj.get(node, {}))
    return result


def betweenness_centrality(adj, nodes, sample_size=200):
    """Approximate betweenness centrality by sampling random pairs.

    For each sampled pair (s, t), find shortest path via BFS.
    Count how many times each intermediate node appears on shortest paths.
    """
    node_list = list(nodes)
    if len(node_list) < 3:
        return {n: 0.0 for n in nodes}

    counts = defaultdict(int)
    total_paths = 0

    rng = random.Random(42)  # deterministic
    pairs = set()
    attempts = 0
    max_attempts = sample_size * 10
    while len(pairs) < sample_size and attempts < max_attempts:
        s = rng.choice(node_list)
        t = rng.choice(node_list)
        if s != t and (s, t) not in pairs and (t, s) not in pairs:
            pairs.add((s, t))
        attempts += 1

    for s, t in pairs:
        path = _bfs_shortest_path(adj, s, t)
        if path and len(path) > 2:
            total_paths += 1
            for node in path[1:-1]:  # exclude endpoints
                counts[node] += 1

    # Normalize
    result = {}
    for node in nodes:
        result[node] = counts[node] / max(total_paths, 1)

    return result


def closeness_centrality(adj, nodes, sample_size=100):
    """Approximate closeness centrality using BFS from sampled nodes."""
    node_list = list(nodes)
    if len(node_list) < 2:
        return {n: 0.0 for n in nodes}

    total_dist = defaultdict(int)
    reachable_count = defaultdict(int)

    rng = random.Random(42)
    sources = rng.sample(node_list, min(sample_size, len(node_list)))

    for source in sources:
        distances = _bfs_distances(adj, source)
        for node, dist in distances.items():
            if dist > 0:
                total_dist[node] += dist
                reachable_count[node] += 1

    result = {}
    for node in nodes:
        if reachable_count[node] > 0:
            avg_dist = total_dist[node] / reachable_count[node]
            result[node] = 1.0 / avg_dist if avg_dist > 0 else 0.0
        else:
            result[node] = 0.0

    return result


def _bfs_shortest_path(adj, start, end, max_depth=10):
    """BFS shortest path between two nodes. Returns list of nodes or None."""
    if start == end:
        return [start]
    if start not in adj:
        return None

    visited = {start}
    queue = deque([(start, [start])])

    while queue:
        current, path = queue.popleft()
        if len(path) > max_depth:
            continue

        for neighbor in adj.get(current, {}):
            if neighbor == end:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return None


def _bfs_distances(adj, start, max_depth=20):
    """BFS distances from start to all reachable nodes."""
    distances = {start: 0}
    queue = deque([start])

    while queue:
        current = queue.popleft()
        d = distances[current]
        if d >= max_depth:
            continue
        for neighbor in adj.get(current, {}):
            if neighbor not in distances:
                distances[neighbor] = d + 1
                queue.append(neighbor)

    return distances


def find_components(adj, nodes):
    """Find connected components via BFS."""
    visited = set()
    components = []

    for node in nodes:
        if node in visited:
            continue
        # BFS to find component
        component = set()
        queue = deque([node])
        while queue:
            current = queue.popleft()
            if current in component:
                continue
            component.add(current)
            visited.add(current)
            for neighbor in adj.get(current, {}):
                if neighbor not in component:
                    queue.append(neighbor)
        components.append(sorted(component))

    components.sort(key=len, reverse=True)
    return components


def find_bridges(adj, nodes):
    """Find bridge nodes — removal disconnects the graph.

    A node is a bridge if removing it increases the number of components.
    We approximate by checking if any neighbor pair is connected without the node.
    """
    bridges = []
    for node in nodes:
        neighbors = list(adj.get(node, {}).keys())
        if len(neighbors) < 2:
            continue

        # Check if removing this node disconnects any pair of its neighbors
        # Quick check: BFS from first neighbor to all others, excluding node
        is_bridge = False
        start = neighbors[0]
        reachable = set()
        queue = deque([start])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for n in adj.get(current, {}):
                if n != node and n not in reachable:
                    queue.append(n)

        for n in neighbors[1:]:
            if n not in reachable:
                is_bridge = True
                break

        if is_bridge:
            bridges.append({
                "node": node,
                "degree": len(neighbors),
                "neighbors": neighbors,
            })

    bridges.sort(key=lambda x: x["degree"], reverse=True)
    return bridges


def find_structural_holes(adj, nodes, min_degree=5):
    """Find structural holes — nodes whose neighbors are poorly connected to each other.

    High structural holes score = node's neighbors don't know each other,
    meaning the node acts as a broker.
    """
    holes = []
    for node in nodes:
        neighbors = list(adj.get(node, {}).keys())
        if len(neighbors) < min_degree:
            continue

        # Count edges among neighbors
        possible_edges = len(neighbors) * (len(neighbors) - 1) / 2
        actual_edges = 0
        for i, n1 in enumerate(neighbors):
            for n2 in neighbors[i + 1:]:
                if n2 in adj.get(n1, {}):
                    actual_edges += 1

        density = actual_edges / possible_edges if possible_edges > 0 else 1.0
        constraint = density  # higher density = more constrained = fewer holes

        holes.append({
            "node": node,
            "degree": len(neighbors),
            "neighbor_density": round(density, 3),
            "brokerage_score": round(1.0 - density, 3),
            "neighbor_edges": actual_edges,
            "possible_edges": int(possible_edges),
        })

    holes.sort(key=lambda x: x["brokerage_score"], reverse=True)
    return holes


STRENGTH_WEIGHTS = {"strong": 4, "medium": 3, "weak": 2, "circumstantial": 1}


def find_open_triads(adj, nodes, db=None, top=50, min_strength=None, rel_type=None):
    """Find open triads — pairs (B, C) that share a mutual connection A but have no direct edge.

    Scores each gap by:
    - Strength factor (0.4): strong A-B + strong A-C = highly surprising gap
    - Type diversity factor (0.3): shared relationship types across A-B and A-C
    - Institutional overlap factor (0.3): B and C share career_arcs institutions or entity_roles

    Returns list of open triads sorted by closure score descending.
    """
    close_db = False
    if db is None:
        db = get_graph_db()
        close_db = True

    # Pre-load institutional data for overlap scoring
    person_institutions = defaultdict(set)
    try:
        for r in db.execute("SELECT person_name, pillar_id FROM career_arcs").fetchall():
            person_institutions[r["person_name"].lower()].add(("pillar", r["pillar_id"]))
    except sqlite3.OperationalError:
        pass  # pillar tables may not exist
    try:
        for r in db.execute("SELECT person_name, entity_id FROM entity_roles").fetchall():
            person_institutions[r["person_name"].lower()].add(("entity", r["entity_id"]))
    except sqlite3.OperationalError:
        pass

    if close_db:
        db.close()

    # Strength filter threshold
    strength_threshold = STRENGTH_WEIGHTS.get(min_strength, 0) if min_strength else 0

    # Track best gap per (B, C) pair
    best_gaps = {}  # tuple(sorted([b, c])) -> gap_dict
    pivot_counts = defaultdict(int)  # same key -> count of pivots

    for a in nodes:
        neighbors = adj.get(a, {})
        if len(neighbors) < 2:
            continue

        neighbor_list = list(neighbors.keys())
        for i, b in enumerate(neighbor_list):
            for c in neighbor_list[i + 1:]:
                # Check if B-C edge exists — if so, triad is closed
                if c in adj.get(b, {}):
                    continue

                ab_edges = neighbors[b]
                ac_edges = neighbors[c]

                # Apply rel_type filter
                if rel_type:
                    ab_edges = [e for e in ab_edges if e.get("type") == rel_type]
                    ac_edges = [e for e in ac_edges if e.get("type") == rel_type]
                    if not ab_edges or not ac_edges:
                        continue

                # Strength factor (0.4 weight)
                ab_max = max(STRENGTH_WEIGHTS.get(e.get("strength", "circumstantial"), 1) for e in ab_edges)
                ac_max = max(STRENGTH_WEIGHTS.get(e.get("strength", "circumstantial"), 1) for e in ac_edges)
                if ab_max < strength_threshold or ac_max < strength_threshold:
                    continue
                strength_score = (ab_max + ac_max) / 8.0  # normalize to 0-1

                # Type diversity factor (0.3 weight)
                ab_types = {e.get("type") for e in ab_edges if e.get("type")}
                ac_types = {e.get("type") for e in ac_edges if e.get("type")}
                shared_types = ab_types & ac_types
                all_types = ab_types | ac_types
                type_score = len(shared_types) / max(len(all_types), 1)

                # Institutional overlap factor (0.3 weight)
                b_insts = person_institutions.get(b.lower(), set())
                c_insts = person_institutions.get(c.lower(), set())
                if b_insts and c_insts:
                    overlap = len(b_insts & c_insts)
                    union = len(b_insts | c_insts)
                    inst_score = overlap / union
                else:
                    inst_score = 0.0

                closure_score = round(
                    0.4 * strength_score + 0.3 * type_score + 0.3 * inst_score, 4
                )

                key = tuple(sorted([b, c]))
                pivot_counts[key] += 1

                if key not in best_gaps or closure_score > best_gaps[key]["closure_score"]:
                    best_gaps[key] = {
                        "node_b": key[0],
                        "node_c": key[1],
                        "pivot": a,
                        "closure_score": closure_score,
                        "strength_score": round(strength_score, 4),
                        "type_score": round(type_score, 4),
                        "institutional_overlap": round(inst_score, 4),
                        "ab_types": sorted(ab_types),
                        "ac_types": sorted(ac_types),
                        "shared_institutions": len(b_insts & c_insts) if b_insts and c_insts else 0,
                    }

    # Attach pivot counts and sort
    results = []
    for key, gap in best_gaps.items():
        gap["pivot_count"] = pivot_counts[key]
        results.append(gap)

    results.sort(key=lambda x: x["closure_score"], reverse=True)
    return results[:top]


def clustering_coefficient(adj, nodes, min_degree=2):
    """Compute local clustering coefficient for each node.

    C(v) = 2 * |edges among neighbors| / (degree * (degree - 1))
    Returns dict of node -> coefficient, filtered by min_degree.
    """
    results = {}
    for node in nodes:
        neighbors = list(adj.get(node, {}).keys())
        degree = len(neighbors)
        if degree < min_degree:
            continue

        # Count edges among neighbors
        actual_edges = 0
        for i, n1 in enumerate(neighbors):
            for n2 in neighbors[i + 1:]:
                if n2 in adj.get(n1, {}):
                    actual_edges += 1

        possible = degree * (degree - 1) / 2
        coeff = (actual_edges / possible) if possible > 0 else 0.0
        results[node] = round(coeff, 4)

    return results


def find_cliques(adj, nodes, min_size=4):
    """Find dense subgraphs (approximate cliques) via greedy expansion.

    Not exact clique detection (NP-hard), but finds dense neighborhoods.
    """
    cliques = []
    seen = set()

    # Start from high-degree nodes
    degree_sorted = sorted(nodes, key=lambda n: len(adj.get(n, {})), reverse=True)

    for seed in degree_sorted[:50]:  # limit seed nodes
        neighbors = set(adj.get(seed, {}).keys())
        if len(neighbors) < min_size - 1:
            continue

        # Greedily build dense subgraph
        clique = {seed}
        candidates = sorted(neighbors, key=lambda n: len(adj.get(n, {})), reverse=True)

        for candidate in candidates:
            # Check if candidate is connected to all current clique members
            connected_to_all = all(
                candidate in adj.get(member, {}) for member in clique
            )
            if connected_to_all:
                clique.add(candidate)

        if len(clique) >= min_size:
            key = tuple(sorted(clique))
            if key not in seen:
                seen.add(key)
                cliques.append({
                    "members": sorted(clique),
                    "size": len(clique),
                    "seed": seed,
                })

    cliques.sort(key=lambda x: x["size"], reverse=True)
    return cliques


def detect_communities(adj, nodes, resolution=1.0):
    """Detect communities using Louvain algorithm via networkx.

    Falls back to connected components if networkx is not available.

    Args:
        adj: adjacency list
        nodes: set of node names
        resolution: Louvain resolution parameter (higher = more communities)

    Returns:
        list of communities, each a dict with: id, members, size, internal_edges, bridge_nodes
    """
    try:
        import networkx as nx

        G = nx.Graph()
        G.add_nodes_from(nodes)
        seen_edges = set()
        for a in adj:
            for b in adj[a]:
                key = tuple(sorted([a, b]))
                if key not in seen_edges:
                    seen_edges.add(key)
                    # Weight by number of connection records
                    weight = len(adj[a][b])
                    G.add_edge(a, b, weight=weight)

        communities_gen = nx.community.louvain_communities(G, resolution=resolution, seed=42)
        raw_communities = [sorted(c) for c in communities_gen]
    except ImportError:
        print("  Warning: networkx not installed. Falling back to connected components.")
        raw_communities = find_components(adj, nodes)

    raw_communities.sort(key=len, reverse=True)

    results = []
    for i, members in enumerate(raw_communities):
        member_set = set(members)
        # Count internal edges
        internal_edges = 0
        for m in members:
            for neighbor in adj.get(m, {}):
                if neighbor in member_set:
                    internal_edges += 1
        internal_edges //= 2  # each edge counted twice

        # Find bridge nodes (members connected to other communities)
        bridge_nodes = []
        for m in members:
            for neighbor in adj.get(m, {}):
                if neighbor not in member_set:
                    bridge_nodes.append(m)
                    break

        results.append({
            "id": i,
            "members": members,
            "size": len(members),
            "internal_edges": internal_edges,
            "bridge_nodes": sorted(bridge_nodes),
        })

    return results


def community_summary(adj, nodes, db=None, resolution=1.0, profile_id=None,
                      all_profiles=False):
    """Generate LLM-friendly community summaries with top findings per member.

    Returns list of community dicts with member lists + top findings.
    """
    close_db = False
    if db is None:
        db = get_graph_db()
        close_db = True

    resolved = _resolve_profile(profile_id, all_profiles)
    communities = detect_communities(adj, nodes, resolution=resolution)

    # Load findings for summary
    all_findings = {}
    if resolved:
        rows = db.execute("""
            SELECT id, target_name, summary, finding_type, confidence
            FROM findings WHERE profile_id = ? ORDER BY id DESC
        """, (resolved,)).fetchall()
    else:
        rows = db.execute("""
            SELECT id, target_name, summary, finding_type, confidence
            FROM findings ORDER BY id DESC
        """).fetchall()
    for r in rows:
        name = r["target_name"]
        if name not in all_findings:
            all_findings[name] = []
        all_findings[name].append(dict(r))

    if close_db:
        db.close()

    for comm in communities:
        # Gather top findings for community members
        top_findings = []
        for member in comm["members"]:
            member_findings = all_findings.get(member, [])
            for f in member_findings[:3]:  # top 3 per member
                top_findings.append(f)

        # Sort by finding ID desc (most recent first), limit to 10
        top_findings.sort(key=lambda f: f["id"], reverse=True)
        comm["top_findings"] = top_findings[:10]
        # Don't include full member list in summary to keep concise
        comm["member_preview"] = comm["members"][:10]
        if len(comm["members"]) > 10:
            comm["member_preview"].append(f"... +{len(comm['members']) - 10} more")

    return communities


def ego_network(adj, center, depth=2):
    """Get ego network: all nodes within N hops of center."""
    distances = _bfs_distances(adj, center, max_depth=depth)
    result_nodes = set(distances.keys())

    edges = []
    seen_edges = set()
    for node in result_nodes:
        for neighbor in adj.get(node, {}):
            if neighbor in result_nodes:
                edge_key = tuple(sorted([node, neighbor]))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edge_types = [e["type"] for e in adj[node][neighbor]]
                    edges.append({
                        "source": node,
                        "target": neighbor,
                        "types": edge_types,
                    })

    node_details = []
    for node in sorted(result_nodes):
        node_details.append({
            "name": node,
            "distance": distances[node],
            "degree": len(adj.get(node, {})),
        })

    return {
        "center": center,
        "depth": depth,
        "nodes": node_details,
        "edges": edges,
        "node_count": len(result_nodes),
        "edge_count": len(edges),
    }


def graph_stats(adj, nodes):
    """Compute summary statistics for the graph."""
    edge_count = sum(len(neighbors) for neighbors in adj.values()) // 2
    degrees = [len(adj.get(n, {})) for n in nodes]

    if degrees:
        avg_degree = sum(degrees) / len(degrees)
        max_degree = max(degrees)
        median_degree = sorted(degrees)[len(degrees) // 2]
    else:
        avg_degree = max_degree = median_degree = 0

    components = find_components(adj, nodes)
    largest_component = len(components[0]) if components else 0

    isolates = sum(1 for d in degrees if d == 0)

    return {
        "nodes": len(nodes),
        "edges": edge_count,
        "avg_degree": round(avg_degree, 2),
        "median_degree": median_degree,
        "max_degree": max_degree,
        "components": len(components),
        "largest_component": largest_component,
        "isolates": isolates,
        "density": round(2 * edge_count / (len(nodes) * (len(nodes) - 1)), 6)
        if len(nodes) > 1 else 0,
    }


# ── Cache Metrics ────────────────────────────────────────

def cache_metrics(metrics, metric_type, run_id=None):
    """Store computed metrics in graph_metrics table."""
    db = get_graph_db()
    for node, value in metrics.items():
        try:
            db.execute("""
                INSERT OR REPLACE INTO graph_metrics (node_name, metric_type, metric_value, analysis_run_id)
                VALUES (?, ?, ?, ?)
            """, (node, metric_type, value, run_id))
        except sqlite3.IntegrityError:
            pass
    db.commit()
    db.close()


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Network analysis tools for investigation graph")
    parser.add_argument("--profile", default=None, help="Investigation profile (default: active)")
    parser.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    sub = parser.add_subparsers(dest="command")

    # centrality
    p_cent = sub.add_parser("centrality", help="Compute centrality metrics")
    p_cent.add_argument("--metric", choices=VALID_METRICS, default="degree")
    p_cent.add_argument("--top", type=int, default=30)
    p_cent.add_argument("--cache", action="store_true", help="Cache results in graph_metrics table")
    p_cent.add_argument("--as-of", help="Temporal snapshot date (YYYY-MM-DD)")
    add_output_args(p_cent)

    # components
    p_comp = sub.add_parser("components", help="Find connected components")
    p_comp.add_argument("--min-size", type=int, default=3)
    add_output_args(p_comp)

    # bridges
    p_bridge = sub.add_parser("bridges", help="Find bridge nodes")
    add_output_args(p_bridge)

    # paths
    p_paths = sub.add_parser("paths", help="Shortest path between two nodes")
    p_paths.add_argument("source")
    p_paths.add_argument("target")
    p_paths.add_argument("--max-hops", type=int, default=6)

    # neighbors
    p_neigh = sub.add_parser("neighbors", help="Ego network around a node")
    p_neigh.add_argument("center")
    p_neigh.add_argument("--depth", type=int, default=2)
    add_output_args(p_neigh)

    # holes
    p_holes = sub.add_parser("holes", help="Find structural holes (brokerage positions)")
    p_holes.add_argument("--min-degree", type=int, default=5)
    add_output_args(p_holes)

    # cliques
    p_cliq = sub.add_parser("cliques", help="Find dense subgraphs")
    p_cliq.add_argument("--min-size", type=int, default=4)
    add_output_args(p_cliq)

    # pillar-subgraph
    p_psub = sub.add_parser("pillar-subgraph", help="Subgraph filtered to people with arcs at pillar type")
    p_psub.add_argument("--pillar-type", required=True,
                        choices=["banking", "legal", "accounting", "government",
                                 "media", "operations", "intelligence", "philanthropy",
                                 "consulting", "academia"])
    p_psub.add_argument("--metric", choices=VALID_METRICS, default="degree")
    p_psub.add_argument("--top", type=int, default=30)
    add_output_args(p_psub)

    # institutional-graph
    p_igraph = sub.add_parser("institutional-graph", help="Institution-to-institution graph weighted by shared alumni")
    p_igraph.add_argument("--min-shared", type=int, default=1)
    add_output_args(p_igraph)

    # triangles (open triads)
    p_tri = sub.add_parser("triangles", help="Find open triads (missing edges between mutual connections)")
    p_tri.add_argument("--top", type=int, default=50)
    p_tri.add_argument("--min-strength", choices=["strong", "medium", "weak", "circumstantial"])
    p_tri.add_argument("--rel-type", help="Filter by relationship type (e.g., financial)")
    add_output_args(p_tri)

    # clustering coefficient
    p_clust = sub.add_parser("clustering", help="Compute local clustering coefficients")
    p_clust.add_argument("--min-degree", type=int, default=2)
    p_clust.add_argument("--top", type=int, default=50)
    add_output_args(p_clust)

    # communities
    p_comm = sub.add_parser("communities", help="Detect communities in the connection graph")
    p_comm.add_argument("--resolution", type=float, default=1.0,
                        help="Louvain resolution (higher = more communities)")
    add_output_args(p_comm)

    # community (single)
    p_comm1 = sub.add_parser("community", help="Show details for a specific community")
    p_comm1.add_argument("id", type=int, help="Community ID from 'communities' output")
    p_comm1.add_argument("--resolution", type=float, default=1.0)
    add_output_args(p_comm1)

    # community-summary
    p_csum = sub.add_parser("community-summary", help="LLM-friendly community summaries with findings")
    p_csum.add_argument("--resolution", type=float, default=1.0)
    add_output_args(p_csum)

    # stats
    p_stats = sub.add_parser("stats", help="Graph summary statistics")
    p_stats.add_argument("--as-of", help="Temporal snapshot date (YYYY-MM-DD)")

    args = parser.parse_args()
    p_id = getattr(args, "profile", None)
    p_all = getattr(args, "all_profiles", False)

    if args.command == "centrality":
        adj, nodes = build_graph(as_of=getattr(args, 'as_of', None),
                                 profile_id=p_id, all_profiles=p_all)
        if args.metric == "degree":
            metrics = degree_centrality(adj, nodes)
        elif args.metric == "betweenness":
            print("Computing betweenness centrality (sampling 200 pairs)...")
            metrics = betweenness_centrality(adj, nodes, sample_size=200)
        elif args.metric == "closeness":
            print("Computing closeness centrality (sampling 100 sources)...")
            metrics = closeness_centrality(adj, nodes, sample_size=100)

        # Sort by value descending
        ranked = sorted(metrics.items(), key=lambda x: x[1], reverse=True)[:args.top]
        results = [{"rank": i + 1, "node": n, args.metric: round(v, 4)} for i, (n, v) in enumerate(ranked)]

        if args.cache:
            cache_metrics(metrics, args.metric)
            print(f"Cached {len(metrics)} {args.metric} values in graph_metrics")

        if write_output(results, args, summary=f"{args.metric} centrality top {args.top}"):
            return
        print(f"\n{args.metric.title()} Centrality (top {args.top}):")
        print(f"{'Rank':>4}  {'Node':<40} {args.metric.title():>12}")
        print("-" * 60)
        for r in results:
            print(f"{r['rank']:>4}  {r['node']:<40} {r[args.metric]:>12.4f}")

    elif args.command == "components":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        components = find_components(adj, nodes)
        filtered = [c for c in components if len(c) >= args.min_size]
        results = [{"component_id": i + 1, "size": len(c), "members": c}
                   for i, c in enumerate(filtered)]

        if write_output(results, args, summary=f"components (min size {args.min_size})"):
            return
        print(f"\nConnected Components (min size {args.min_size}): {len(filtered)} found")
        for r in results:
            members_preview = ", ".join(r["members"][:8])
            if len(r["members"]) > 8:
                members_preview += f" ... (+{len(r['members']) - 8} more)"
            print(f"  Component #{r['component_id']} ({r['size']} nodes): {members_preview}")

    elif args.command == "bridges":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        bridges = find_bridges(adj, nodes)
        if write_output(bridges, args, summary=f"bridge nodes ({len(bridges)})"):
            return
        print(f"\nBridge Nodes ({len(bridges)}):")
        for b in bridges[:30]:
            neighbor_preview = ", ".join(b["neighbors"][:5])
            if len(b["neighbors"]) > 5:
                neighbor_preview += f" ... (+{len(b['neighbors']) - 5})"
            print(f"  {b['node']:<40} degree={b['degree']:<3}  neighbors: {neighbor_preview}")

    elif args.command == "paths":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        path = _bfs_shortest_path(adj, args.source, args.target, max_depth=args.max_hops)
        if path:
            print(f"\nShortest path ({len(path) - 1} hops):")
            for i, node in enumerate(path):
                prefix = "  " if i == 0 else "  → "
                if i < len(path) - 1:
                    next_node = path[i + 1]
                    edge_types = [e["type"] for e in adj.get(node, {}).get(next_node, [])]
                    type_str = f"  [{', '.join(edge_types)}]" if edge_types else ""
                    print(f"{prefix}{node}{type_str}")
                else:
                    print(f"{prefix}{node}")
        else:
            print(f"\nNo path found between '{args.source}' and '{args.target}' within {args.max_hops} hops")
            # Suggest close matches
            close_source = [n for n in nodes if args.source.lower() in n.lower()]
            close_target = [n for n in nodes if args.target.lower() in n.lower()]
            if close_source:
                print(f"  Similar to '{args.source}': {', '.join(close_source[:5])}")
            if close_target:
                print(f"  Similar to '{args.target}': {', '.join(close_target[:5])}")

    elif args.command == "neighbors":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        if args.center not in nodes:
            # Try fuzzy match
            matches = [n for n in nodes if args.center.lower() in n.lower()]
            if matches:
                print(f"'{args.center}' not found. Did you mean: {', '.join(matches[:5])}")
            else:
                print(f"'{args.center}' not found in graph")
            sys.exit(1)

        result = ego_network(adj, args.center, depth=args.depth)
        if write_output(result, args, summary=f"ego network of {args.center}"):
            return
        print(f"\nEgo Network: {args.center} (depth {args.depth})")
        print(f"  {result['node_count']} nodes, {result['edge_count']} edges\n")
        for n in sorted(result["nodes"], key=lambda x: (x["distance"], -x["degree"])):
            dist_marker = "★" if n["distance"] == 0 else f"d={n['distance']}"
            print(f"  [{dist_marker}] {n['name']:<40} degree={n['degree']}")

    elif args.command == "holes":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        holes = find_structural_holes(adj, nodes, min_degree=args.min_degree)
        if write_output(holes, args, summary=f"structural holes (min degree {args.min_degree})"):
            return
        print(f"\nStructural Holes (min degree {args.min_degree}): {len(holes)} found")
        print(f"{'Node':<40} {'Degree':>6} {'Brokerage':>9} {'Nbr Density':>11}")
        print("-" * 70)
        for h in holes[:30]:
            print(f"{h['node']:<40} {h['degree']:>6} {h['brokerage_score']:>9.3f} "
                  f"{h['neighbor_density']:>11.3f}")

    elif args.command == "cliques":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        cliques = find_cliques(adj, nodes, min_size=args.min_size)
        if write_output(cliques, args, summary=f"cliques (min size {args.min_size})"):
            return
        print(f"\nDense Subgraphs (min size {args.min_size}): {len(cliques)} found")
        for c in cliques[:20]:
            print(f"  Size {c['size']}: {', '.join(c['members'])}")

    elif args.command == "pillar-subgraph":
        # Build subgraph from people who have career arcs at institutions of given type
        db = get_graph_db()
        try:
            arc_people = db.execute("""
                SELECT DISTINCT ca.person_name
                FROM career_arcs ca
                JOIN institutional_pillars ip ON ca.pillar_id = ip.id
                WHERE ip.pillar_type = ?
            """, (args.pillar_type,)).fetchall()
            pillar_people = {r["person_name"].lower() for r in arc_people}
        except sqlite3.OperationalError:
            print("ERROR: pillar tables not found. Run 'pillar_tracker.py seed' first.")
            sys.exit(1)
        db.close()

        if not pillar_people:
            print(f"No people with career arcs at {args.pillar_type} institutions")
            sys.exit(0)

        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        # Filter to only nodes that are in pillar_people
        sub_nodes = {n for n in nodes if n.lower() in pillar_people}
        sub_adj = {}
        for n in sub_nodes:
            if n in adj:
                sub_adj[n] = {nb: edges for nb, edges in adj[n].items() if nb in sub_nodes}

        if args.metric == "degree":
            metrics = degree_centrality(sub_adj, sub_nodes)
        elif args.metric == "betweenness":
            metrics = betweenness_centrality(sub_adj, sub_nodes, sample_size=100)
        elif args.metric == "closeness":
            metrics = closeness_centrality(sub_adj, sub_nodes, sample_size=50)

        ranked = sorted(metrics.items(), key=lambda x: x[1], reverse=True)[:args.top]
        results = [{"rank": i + 1, "node": n, args.metric: round(v, 4)} for i, (n, v) in enumerate(ranked)]

        if write_output(results, args, summary=f"pillar-subgraph {args.pillar_type} {args.metric}"):
            return
        print(f"\nPillar Subgraph: {args.pillar_type} — {args.metric} centrality ({len(sub_nodes)} nodes)")
        print(f"{'Rank':>4}  {'Node':<40} {args.metric.title():>12}")
        print("-" * 60)
        for r in results:
            print(f"{r['rank']:>4}  {r['node']:<40} {r[args.metric]:>12.4f}")

    elif args.command == "institutional-graph":
        # Nodes = institutions, edges = shared alumni (weighted by count)
        db = get_graph_db()
        try:
            arcs = db.execute("""
                SELECT ca.person_id, ip.name as pillar_name
                FROM career_arcs ca
                JOIN institutional_pillars ip ON ca.pillar_id = ip.id
            """).fetchall()
        except sqlite3.OperationalError:
            print("ERROR: pillar tables not found. Run 'pillar_tracker.py seed' first.")
            sys.exit(1)
        db.close()

        # Group people by institution
        person_institutions = defaultdict(set)
        for a in arcs:
            person_institutions[a["person_id"]].add(a["pillar_name"])

        # Count shared alumni between institution pairs
        from collections import Counter as _Counter
        pair_counts = _Counter()
        for pid, insts in person_institutions.items():
            insts_list = sorted(insts)
            for i, a in enumerate(insts_list):
                for b in insts_list[i + 1:]:
                    pair_counts[(a, b)] += 1

        # Filter by min-shared
        edges = []
        for (a, b), count in pair_counts.most_common():
            if count >= args.min_shared:
                edges.append({"source": a, "target": b, "shared_alumni": count})

        inst_nodes = set()
        for e in edges:
            inst_nodes.add(e["source"])
            inst_nodes.add(e["target"])

        results = {
            "nodes": sorted(inst_nodes),
            "edges": edges,
            "node_count": len(inst_nodes),
            "edge_count": len(edges),
        }

        if write_output(results, args, summary=f"institutional graph ({len(edges)} edges)"):
            return
        print(f"\nInstitutional Graph ({len(inst_nodes)} institutions, {len(edges)} edges, min shared={args.min_shared})")
        print(f"{'─' * 70}")
        for e in edges:
            print(f"  {e['source']:<35} ↔ {e['target']:<35} ({e['shared_alumni']} shared)")

    elif args.command == "triangles":
        db = get_graph_db()
        adj, nodes = build_graph(db, profile_id=p_id, all_profiles=p_all)
        triads = find_open_triads(
            adj, nodes, db=db, top=args.top,
            min_strength=args.min_strength, rel_type=args.rel_type,
        )
        if write_output(triads, args, summary=f"open triads (top {args.top})"):
            return
        print(f"\nOpen Triads ({len(triads)} found):")
        print(f"{'B':<25} {'C':<25} {'Pivot':<25} {'Score':>6} {'Pivots':>6}")
        print("-" * 95)
        for t in triads:
            print(f"{t['node_b']:<25} {t['node_c']:<25} {t['pivot']:<25} "
                  f"{t['closure_score']:>6.3f} {t['pivot_count']:>6}")

    elif args.command == "clustering":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        coefficients = clustering_coefficient(adj, nodes, min_degree=args.min_degree)
        ranked = sorted(coefficients.items(), key=lambda x: x[1], reverse=True)[:args.top]
        results = [{"rank": i + 1, "node": n, "clustering_coefficient": v, "degree": len(adj.get(n, {}))}
                   for i, (n, v) in enumerate(ranked)]

        if write_output(results, args, summary=f"clustering coefficients (min degree {args.min_degree})"):
            return
        print(f"\nClustering Coefficients (min degree {args.min_degree}, top {args.top}):")
        print(f"{'Rank':>4}  {'Node':<40} {'Coefficient':>11} {'Degree':>6}")
        print("-" * 65)
        for r in results:
            print(f"{r['rank']:>4}  {r['node']:<40} {r['clustering_coefficient']:>11.4f} {r['degree']:>6}")

    elif args.command == "communities":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        communities = detect_communities(adj, nodes, resolution=args.resolution)
        if write_output(communities, args, summary=f"communities (resolution={args.resolution})"):
            return
        print(f"\nCommunities Detected: {len(communities)} (resolution={args.resolution})")
        print(f"{'ID':>3}  {'Size':>5}  {'Internal':>8}  {'Bridges':>7}  Members")
        print("-" * 90)
        for c in communities:
            preview = ", ".join(c["members"][:5])
            if len(c["members"]) > 5:
                preview += f" ... (+{len(c['members']) - 5})"
            print(f"{c['id']:>3}  {c['size']:>5}  {c['internal_edges']:>8}  "
                  f"{len(c['bridge_nodes']):>7}  {preview}")

    elif args.command == "community":
        adj, nodes = build_graph(profile_id=p_id, all_profiles=p_all)
        communities = detect_communities(adj, nodes, resolution=args.resolution)
        target_id = args.id
        if target_id >= len(communities):
            print(f"ERROR: Community #{target_id} not found. Max ID: {len(communities) - 1}")
            sys.exit(1)
        c = communities[target_id]
        if write_output(c, args, summary=f"community #{target_id}"):
            return
        print(f"\nCommunity #{target_id} ({c['size']} members, {c['internal_edges']} internal edges)")
        print(f"\nMembers:")
        for m in c["members"]:
            degree = len(adj.get(m, {}))
            is_bridge = "B" if m in c["bridge_nodes"] else " "
            print(f"  [{is_bridge}] {m:<40} degree={degree}")
        if c["bridge_nodes"]:
            print(f"\nBridge nodes (connected to other communities):")
            for b in c["bridge_nodes"]:
                external = [n for n in adj.get(b, {}) if n not in set(c["members"])]
                print(f"  {b} → {', '.join(external[:5])}")

    elif args.command == "community-summary":
        db = get_graph_db()
        adj, nodes = build_graph(db, profile_id=p_id, all_profiles=p_all)
        summaries = community_summary(adj, nodes, db=db, resolution=args.resolution,
                                      profile_id=p_id, all_profiles=p_all)
        if write_output(summaries, args, summary=f"community summaries"):
            return
        for c in summaries:
            if c["size"] < 2:
                continue
            print(f"\n{'='*60}")
            print(f"Community #{c['id']} — {c['size']} members, "
                  f"{c['internal_edges']} internal edges, {len(c['bridge_nodes'])} bridges")
            print(f"Members: {', '.join(c['member_preview'])}")
            if c["top_findings"]:
                print(f"Key findings:")
                for f in c["top_findings"][:5]:
                    conf = f.get("confidence", "?")
                    print(f"  F#{f['id']} [{conf}] {f['target_name']}: {f['summary'][:70]}")

    elif args.command == "stats":
        adj, nodes = build_graph(as_of=getattr(args, 'as_of', None),
                                 profile_id=p_id, all_profiles=p_all)
        s = graph_stats(adj, nodes)
        print("Graph Statistics")
        print("=" * 40)
        print(f"  Nodes:              {s['nodes']}")
        print(f"  Edges:              {s['edges']}")
        print(f"  Avg degree:         {s['avg_degree']}")
        print(f"  Median degree:      {s['median_degree']}")
        print(f"  Max degree:         {s['max_degree']}")
        print(f"  Components:         {s['components']}")
        print(f"  Largest component:  {s['largest_component']}")
        print(f"  Isolates:           {s['isolates']}")
        print(f"  Density:            {s['density']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
