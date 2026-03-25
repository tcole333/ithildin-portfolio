#!/usr/bin/env python3
"""Export a static search index JSON for the Cmd+K search modal.

Reads dossiers, articles, and models from content/ and produces a single
search-index.json that MiniSearch consumes client-side.
"""

import json
from pathlib import Path

CONTENT_DIR = Path(__file__).parent.parent / "content"
OUTPUT_PATH = Path(__file__).parent.parent / "web" / "public" / "content" / "search-index.json"


def _truncate(text: str, max_len: int = 300) -> str:
    if not text or len(text) <= max_len:
        return text or ""
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def _format_stats(stats: dict) -> str:
    parts = []
    if f := stats.get("total_findings"):
        parts.append(f"{f} findings")
    if c := stats.get("total_connections"):
        parts.append(f"{c} connections")
    if e := stats.get("total_entities"):
        parts.append(f"{e} entities")
    return ", ".join(parts)


def export_dossiers() -> list[dict]:
    dossier_dir = CONTENT_DIR / "dossiers"
    docs = []

    for path in sorted(dossier_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue

        data = json.loads(path.read_text())
        name = data.get("name", "")
        slug = data.get("slug", path.stem)

        aliases = " ".join(data.get("aliases", []))
        description = _truncate(
            data.get("curation", {}).get("system_role", "")
        )

        # Collect connected person names and entity names as mentions
        mention_names = set()
        connections = data.get("connections", [])
        for conn in connections:
            other = conn.get("other_person", "")
            if other:
                mention_names.add(other)

        entities = data.get("entities", [])
        for ent in entities:
            ent_name = ent.get("entity_name", "")
            if ent_name:
                mention_names.add(ent_name)

        mention_count = len(connections) + len(entities)

        docs.append({
            "id": f"dossier:{slug}",
            "type": "dossier",
            "title": name,
            "slug": slug,
            "aliases": aliases,
            "description": description,
            "mentions": " ".join(sorted(mention_names)),
            "mentionCount": mention_count,
            "stats": _format_stats(data.get("stats", {})),
            "href": f"/dossiers/{slug}",
            "profile_ids": data.get("profile_ids", []),
        })

    return docs


def export_articles() -> list[dict]:
    article_dir = CONTENT_DIR / "articles"
    backlinks_path = Path(__file__).parent.parent / "web" / "public" / "content" / "backlinks.json"
    docs = []

    # Load backlinks for article-to-dossier references
    backlinks = {}
    if backlinks_path.exists():
        bl_data = json.loads(backlinks_path.read_text())
        backlinks = bl_data.get("article_to_dossier", {})

    for path in sorted(article_dir.glob("*.mdx")):
        text = path.read_text()

        # Parse YAML frontmatter
        fm = {}
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                for line in text[3:end].strip().split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        fm[key.strip()] = val.strip().strip('"')

        title = fm.get("title", path.stem)
        subtitle = fm.get("subtitle", "")
        slug = fm.get("cluster", path.stem)

        # Mentioned dossiers from backlinks
        raw_mentioned = backlinks.get(slug, [])
        mention_names = []
        for item in raw_mentioned:
            if isinstance(item, dict):
                mention_names.append(item.get("name", item.get("slug", "")))
            elif isinstance(item, str):
                mention_names.append(item)

        docs.append({
            "id": f"article:{slug}",
            "type": "article",
            "title": title,
            "slug": slug,
            "aliases": "",
            "description": _truncate(subtitle),
            "mentions": " ".join(mention_names),
            "mentionCount": len(mention_names),
            "stats": "",
            "href": f"/articles/{slug}",
        })

    return docs


def export_models() -> list[dict]:
    index_path = CONTENT_DIR / "models" / "_index.json"
    if not index_path.exists():
        return []

    models = json.loads(index_path.read_text())
    docs = []

    for model in models:
        model_id = model.get("id", "")
        docs.append({
            "id": f"model:{model_id}",
            "type": "model",
            "title": model.get("title", ""),
            "slug": model_id,
            "aliases": model.get("subtitle", ""),
            "description": _truncate(model.get("definition", "")),
            "mentions": "",
            "mentionCount": 0,
            "stats": "",
            "href": f"/models/{model_id}",
        })

    return docs


def main():
    all_docs = []
    all_docs.extend(export_dossiers())
    all_docs.extend(export_articles())
    all_docs.extend(export_models())

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(all_docs, separators=(",", ":")))

    print(f"  Search index: {len(all_docs)} documents ({OUTPUT_PATH.stat().st_size:,} bytes)")
    by_type = {}
    for d in all_docs:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    for t, c in sorted(by_type.items()):
        print(f"    {t}: {c}")


if __name__ == "__main__":
    main()
