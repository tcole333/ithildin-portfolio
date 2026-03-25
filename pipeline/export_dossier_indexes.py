#!/usr/bin/env python3
"""Rebuild dossier index and redirect metadata from existing dossier JSON files."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOSSIERS_DIR = ROOT / "content" / "dossiers"
INDEX_PATH = DOSSIERS_DIR / "_index.json"
REDIRECTS_PATH = DOSSIERS_DIR / "_redirects.json"


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\\s-]", "", slug)
    slug = re.sub(r"[\\s-]+", "-", slug)
    return slug.strip("-")


def dossier_paths() -> list[Path]:
    return sorted(path for path in DOSSIERS_DIR.glob("*.json") if not path.name.startswith("_"))


def build_index_entry(data: dict, path: Path) -> dict:
    return {
        "name": data.get("name", path.stem),
        "slug": data.get("slug", path.stem),
        "profile_ids": data.get("profile_ids", []),
        "stats": data.get("stats", {}),
        "last_updated": data.get("last_updated"),
    }


def sort_key(entry: dict) -> tuple[int, int, str]:
    stats = entry.get("stats") or {}
    return (
        -(stats.get("total_findings") or 0),
        -(stats.get("total_connections") or 0),
        entry.get("name", "").lower(),
    )


def main() -> int:
    index: list[dict] = []
    redirects: dict[str, str] = {}

    for path in dossier_paths():
        data = json.loads(path.read_text())
        entry = build_index_entry(data, path)
        index.append(entry)

        slug = entry["slug"]
        for alias in data.get("aliases", []):
            alias_slug = slugify(alias)
            if alias_slug and alias_slug != slug:
                redirects[alias_slug] = slug

    index.sort(key=sort_key)

    INDEX_PATH.write_text(json.dumps(index, indent=2) + "\n")
    REDIRECTS_PATH.write_text(json.dumps(dict(sorted(redirects.items())), indent=2) + "\n")

    print(f"  Dossier index: {len(index)} dossiers")
    print(f"  Redirects: {len(redirects)} aliases")
    print(f"  Wrote {INDEX_PATH}")
    print(f"  Wrote {REDIRECTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
