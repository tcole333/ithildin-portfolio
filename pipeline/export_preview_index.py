#!/usr/bin/env python3
"""Export a preview index JSON for backlink popovers.

Reads dossier and article content to produce a keyed lookup of preview data
that the backlink popover lazy-loads on first click.
"""

import json
import re
from pathlib import Path

CONTENT_DIR = Path(__file__).parent.parent / "content"
OUTPUT_PATH = Path(__file__).parent.parent / "web" / "public" / "content" / "preview-index.json"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _truncate(text: str, max_len: int) -> str:
    if not text or len(text) <= max_len:
        return text or ""
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def export_dossiers(index: dict) -> int:
    dossier_dir = CONTENT_DIR / "dossiers"
    count = 0

    for path in sorted(dossier_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue

        data = json.loads(path.read_text())
        slug = data.get("slug", path.stem)
        curation = data.get("curation", {})
        stats = data.get("stats", {})

        # Top 3 unique connection names
        seen = set()
        top_connections = []
        for conn in data.get("connections", []):
            other = conn.get("other_person", "")
            if other and other not in seen:
                seen.add(other)
                top_connections.append(other)
                if len(top_connections) == 3:
                    break

        lead_raw = _strip_html(curation.get("lead", ""))

        index[f"dossiers/{slug}"] = {
            "type": "dossier",
            "name": data.get("name", ""),
            "role": _truncate(curation.get("system_role", ""), 150),
            "lead": _truncate(lead_raw, 200),
            "stats": {
                "findings": stats.get("total_findings", 0),
                "connections": stats.get("total_connections", 0),
                "entities": stats.get("total_entities", 0),
            },
            "topConnections": top_connections,
        }
        count += 1

    return count


def export_articles(index: dict) -> int:
    article_dir = CONTENT_DIR / "articles"
    count = 0

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

        slug = fm.get("cluster", path.stem)

        index[f"articles/{slug}"] = {
            "type": "article",
            "title": fm.get("title", path.stem),
            "subtitle": _truncate(fm.get("subtitle", ""), 200),
        }
        count += 1

    return count


def main():
    index: dict = {}

    dossier_count = export_dossiers(index)
    article_count = export_articles(index)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(index, separators=(",", ":")))

    total = dossier_count + article_count
    size = OUTPUT_PATH.stat().st_size
    print(f"  Preview index: {total} entries ({size:,} bytes)")
    print(f"    dossiers: {dossier_count}")
    print(f"    articles: {article_count}")


if __name__ == "__main__":
    main()
