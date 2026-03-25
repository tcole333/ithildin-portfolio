#!/usr/bin/env python3
"""Compute backlinks between dossiers, articles, and entities.

Produces backlinks.json consumed by Astro pages at build time:
- dossier→dossier: from connections + shared entities
- article→dossier: person/entity mentions in article content
- dossier→article: reverse of above (which articles mention this target)
- article→article: shared targets between cluster definitions
"""

import json
import re
import sqlite3
from pathlib import Path

CONTENT_DIR = Path(__file__).parent.parent / "content"
DOSSIERS_DIR = CONTENT_DIR / "dossiers"
ARTICLES_DIR = CONTENT_DIR / "articles"
DB_PATH = Path(__file__).parent.parent / "investigation.db"


def load_dossier_index() -> dict[str, dict]:
    """Load all dossier slugs and names."""
    index_path = DOSSIERS_DIR / "_index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text())
    return {d["slug"]: d for d in index}


def load_dossier(slug: str) -> dict | None:
    path = DOSSIERS_DIR / f"{slug}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    return slug.strip('-')


def load_clusters() -> list[dict]:
    """Load story clusters for article cross-references."""
    clusters_path = CONTENT_DIR / "clusters.json"
    if not clusters_path.exists():
        return []
    return json.loads(clusters_path.read_text())


def load_articles() -> list[dict]:
    """Load article MDX files and parse frontmatter + content."""
    articles = []
    if not ARTICLES_DIR.exists():
        return articles

    for mdx_path in sorted(ARTICLES_DIR.glob("*.mdx")):
        raw = mdx_path.read_text()
        meta: dict[str, str] = {}
        content = raw

        fm_match = re.match(r'^---\n([\s\S]*?)\n---', raw)
        if fm_match:
            for line in fm_match.group(1).split('\n'):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    meta[parts[0].strip()] = parts[1].strip().strip('"\'')
            content = raw[fm_match.end():]

        articles.append({
            "slug": mdx_path.stem,
            "title": meta.get("title", mdx_path.stem),
            "cluster": meta.get("cluster", ""),
            "targets": meta.get("targets", ""),
            "content": content,
            "meta": meta,
        })

    return articles


def compute_dossier_to_dossier(dossier_index: dict[str, dict]) -> dict[str, list[dict]]:
    """From connections in each dossier, find which other dossiers are linked."""
    links: dict[str, list[dict]] = {slug: [] for slug in dossier_index}

    for slug in dossier_index:
        dossier = load_dossier(slug)
        if not dossier:
            continue

        seen = set()
        # Connections already have other_person_slug
        for conn in dossier.get("connections", []):
            other_slug = conn.get("other_person_slug", "")
            if other_slug and other_slug in dossier_index and other_slug != slug and other_slug not in seen:
                seen.add(other_slug)
                links[slug].append({
                    "slug": other_slug,
                    "name": dossier_index[other_slug]["name"],
                    "via": "connection",
                    "relationship_type": conn.get("relationship_type", ""),
                    "strength": conn.get("strength", ""),
                })

        # Entity co-membership (shared entities)
        for entity in dossier.get("entities", []):
            entity_name = entity.get("entity_name", "")
            entity_slug = slugify(entity_name)
            if entity_slug in dossier_index and entity_slug != slug and entity_slug not in seen:
                seen.add(entity_slug)
                links[slug].append({
                    "slug": entity_slug,
                    "name": dossier_index[entity_slug]["name"],
                    "via": "shared_entity",
                    "entity": entity_name,
                })

    return links


def _load_alias_to_slug(dossier_index: dict[str, dict]) -> dict[str, str]:
    """Build name→slug lookup including all aliases from name_aliases table."""
    name_to_slug: dict[str, str] = {}

    # Start with dossier names
    for slug, info in dossier_index.items():
        name = info["name"].lower()
        name_to_slug[name] = slug
        parts = name.split()
        if len(parts) >= 2:
            name_to_slug[parts[-1]] = slug

    # Add aliases from DB
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT canonical_name, alias FROM name_aliases").fetchall()
        conn.close()

        for row in rows:
            canonical_slug = slugify(row["canonical_name"])
            if canonical_slug in dossier_index:
                name_to_slug[row["alias"].lower()] = canonical_slug
    except (sqlite3.OperationalError, FileNotFoundError):
        pass

    return name_to_slug


def compute_article_to_dossier(articles: list[dict], dossier_index: dict[str, dict]) -> dict[str, list[dict]]:
    """For each article, find which dossier targets are mentioned."""
    name_to_slug = _load_alias_to_slug(dossier_index)

    links: dict[str, list[dict]] = {}

    for article in articles:
        article_links = []
        seen_slugs = set()
        content_lower = article["content"].lower()

        # Check each dossier name against article content
        # Sort by name length descending to match longest first
        for name, slug in sorted(name_to_slug.items(), key=lambda x: -len(x[0])):
            if len(name) < 4:  # Skip very short names to avoid false matches
                continue
            if slug in seen_slugs:
                continue
            if name in content_lower:
                seen_slugs.add(slug)
                article_links.append({
                    "slug": slug,
                    "name": dossier_index[slug]["name"],
                    "matched_text": name,
                })

        # Also check targets from frontmatter
        if article["targets"]:
            for target in article["targets"].split(","):
                target = target.strip()
                target_slug = slugify(target)
                if target_slug in dossier_index and target_slug not in seen_slugs:
                    seen_slugs.add(target_slug)
                    article_links.append({
                        "slug": target_slug,
                        "name": dossier_index[target_slug]["name"],
                        "matched_text": target.lower(),
                    })

        links[article["slug"]] = article_links

    return links


def compute_dossier_to_article(articles: list[dict], article_to_dossier: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Reverse of article_to_dossier: for each dossier, which articles mention it."""
    links: dict[str, list[dict]] = {}

    article_map = {a["slug"]: a for a in articles}

    for article_slug, dossier_links in article_to_dossier.items():
        article = article_map.get(article_slug)
        if not article:
            continue
        for dl in dossier_links:
            dossier_slug = dl["slug"]
            if dossier_slug not in links:
                links[dossier_slug] = []
            links[dossier_slug].append({
                "slug": article_slug,
                "title": article["title"],
            })

    return links


def compute_article_to_article(clusters: list[dict]) -> dict[str, list[dict]]:
    """Cross-link articles that share targets."""
    # Map target → cluster IDs
    target_to_clusters: dict[str, list[str]] = {}
    for cluster in clusters:
        for target in cluster.get("targets", []):
            name = target.lower()
            if name not in target_to_clusters:
                target_to_clusters[name] = []
            target_to_clusters[name].append(cluster["id"])

    # Find clusters that share targets
    links: dict[str, list[dict]] = {}
    cluster_map = {c["id"]: c for c in clusters}

    for cluster in clusters:
        related = set()
        for target in cluster.get("targets", []):
            for other_id in target_to_clusters.get(target.lower(), []):
                if other_id != cluster["id"]:
                    related.add(other_id)

        links[cluster["id"]] = [
            {"slug": cid, "title": cluster_map[cid]["title"], "shared_targets": [
                t for t in cluster["targets"]
                if cid in target_to_clusters.get(t.lower(), [])
            ]}
            for cid in sorted(related)
            if cid in cluster_map
        ]

    return links


def compute_cluster_dossier_links(clusters: list[dict], dossier_index: dict[str, dict]) -> dict[str, list[dict]]:
    """For unpublished clusters, compute which dossiers their targets map to.
    This lets us show 'Related Dossiers' even before articles are written."""
    name_to_slug = _load_alias_to_slug(dossier_index)
    links: dict[str, list[dict]] = {}

    for cluster in clusters:
        cluster_links = []
        seen = set()
        for target in cluster.get("targets", []):
            # Try alias lookup first, then direct slug
            target_slug = name_to_slug.get(target.lower(), slugify(target))
            if target_slug in dossier_index and target_slug not in seen:
                seen.add(target_slug)
                cluster_links.append({
                    "slug": target_slug,
                    "name": dossier_index[target_slug]["name"],
                })
        links[cluster["id"]] = cluster_links

    return links


MODELS_DIR = CONTENT_DIR / "models"


def load_model_index() -> list[dict]:
    """Load model index for cross-referencing."""
    index_path = MODELS_DIR / "_index.json"
    if not index_path.exists():
        return []
    return json.loads(index_path.read_text())


def compute_model_backlinks(
    dossier_index: dict[str, dict],
    model_index: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Compute model↔dossier backlinks.

    Sources:
    1. Tags table: findings tagged with tag_type='model' and value=model-slug
       → map finding's target_name to dossier slug
    2. Canonical instances: model JSON references specific targets
       → match target labels to dossier slugs

    Returns (model_to_dossier, dossier_to_model).
    """
    model_ids = {m["id"] for m in model_index}
    model_map = {m["id"]: m for m in model_index}

    m2d: dict[str, list[dict]] = {mid: [] for mid in model_ids}
    d2m: dict[str, list[dict]] = {}

    name_to_slug: dict[str, str] = {}
    for slug, info in dossier_index.items():
        name_to_slug[info["name"].lower()] = slug

    # Source 1: Tags with type='model'
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT t.tag_value, f.target_name
            FROM tags t
            JOIN findings f ON t.record_id = f.id AND t.table_name = 'findings'
            WHERE t.tag_type = 'model'
        """).fetchall()
        conn.close()

        for row in rows:
            model_slug = row["tag_value"]
            target_name = row["target_name"]
            if model_slug not in model_ids:
                continue

            dossier_slug = name_to_slug.get(target_name.lower()) if target_name else None
            if not dossier_slug or dossier_slug not in dossier_index:
                dossier_slug = slugify(target_name) if target_name else None
            if not dossier_slug or dossier_slug not in dossier_index:
                continue

            # Add to m2d if not already present
            existing_slugs = {d["slug"] for d in m2d[model_slug]}
            if dossier_slug not in existing_slugs:
                m2d[model_slug].append({
                    "slug": dossier_slug,
                    "name": dossier_index[dossier_slug]["name"],
                    "via": "tag",
                })
    except (sqlite3.OperationalError, FileNotFoundError):
        pass

    # Source 2: Canonical instances from model JSONs
    for model_info in model_index:
        model_path = MODELS_DIR / f"{model_info['id']}.json"
        if not model_path.exists():
            continue
        model_data = json.loads(model_path.read_text())
        for inst in model_data.get("canonical_instances", []):
            label = inst.get("label", "").lower()
            # Try to match label to a dossier
            for name, slug in name_to_slug.items():
                if name in label or label in name:
                    existing_slugs = {d["slug"] for d in m2d[model_info["id"]]}
                    if slug not in existing_slugs:
                        m2d[model_info["id"]].append({
                            "slug": slug,
                            "name": dossier_index[slug]["name"],
                            "via": "canonical_instance",
                        })
                    break

    # Build reverse: dossier_to_model
    for model_slug, dossier_links in m2d.items():
        for dl in dossier_links:
            dossier_slug = dl["slug"]
            if dossier_slug not in d2m:
                d2m[dossier_slug] = []
            existing_model_ids = {m["id"] for m in d2m[dossier_slug]}
            if model_slug not in existing_model_ids:
                d2m[dossier_slug].append({
                    "id": model_slug,
                    "title": model_map[model_slug]["title"],
                })

    return m2d, d2m


def main():
    print("Computing backlinks...")

    dossier_index = load_dossier_index()
    print(f"  Loaded {len(dossier_index)} dossiers")

    articles = load_articles()
    print(f"  Loaded {len(articles)} articles")

    clusters = load_clusters()
    print(f"  Loaded {len(clusters)} clusters")

    # Compute all link types
    d2d = compute_dossier_to_dossier(dossier_index)
    d2d_count = sum(len(v) for v in d2d.values())
    print(f"  Dossier→Dossier: {d2d_count} links")

    a2d = compute_article_to_dossier(articles, dossier_index)
    a2d_count = sum(len(v) for v in a2d.values())
    print(f"  Article→Dossier: {a2d_count} links")

    d2a = compute_dossier_to_article(articles, a2d)
    d2a_count = sum(len(v) for v in d2a.values())
    print(f"  Dossier→Article: {d2a_count} links")

    a2a = compute_article_to_article(clusters)
    a2a_count = sum(len(v) for v in a2a.values())
    print(f"  Article→Article: {a2a_count} links (from clusters)")

    c2d = compute_cluster_dossier_links(clusters, dossier_index)
    c2d_count = sum(len(v) for v in c2d.values())
    print(f"  Cluster→Dossier: {c2d_count} links")

    model_index = load_model_index()
    print(f"  Loaded {len(model_index)} models")

    m2d, d2m = compute_model_backlinks(dossier_index, model_index)
    m2d_count = sum(len(v) for v in m2d.values())
    d2m_count = sum(len(v) for v in d2m.values())
    print(f"  Model→Dossier: {m2d_count} links")
    print(f"  Dossier→Model: {d2m_count} links")

    backlinks = {
        "dossier_to_dossier": d2d,
        "article_to_dossier": a2d,
        "dossier_to_article": d2a,
        "article_to_article": a2a,
        "cluster_to_dossier": c2d,
        "model_to_dossier": m2d,
        "dossier_to_model": d2m,
    }

    out_path = CONTENT_DIR / "backlinks.json"
    out_path.write_text(json.dumps(backlinks, indent=2, default=str))
    print(f"\n  Written to {out_path}")


if __name__ == "__main__":
    main()
