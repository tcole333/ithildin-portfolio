#!/usr/bin/env python3
"""Export investigation profiles as JSON for the frontend.

Reads investigations/*/config.yaml and produces content/investigations.json
with metadata for each active investigation (id, name, description, color).
"""

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: uv pip install pyyaml", file=sys.stderr)
    sys.exit(1)

INVESTIGATIONS_DIR = Path(__file__).parent.parent / "investigations"
OUTPUT_PATH = Path(__file__).parent.parent / "content" / "investigations.json"

# Preset color palette per investigation
INVESTIGATION_COLORS = {
    "epstein": "#d1b36a",     # ember
    "tech-right": "#8fd3e8",  # icy
    "hagee": "#a8c97a",       # sage green
}

DEFAULT_COLOR = "#8c97a3"  # mithril


def export_investigations() -> list[dict]:
    investigations = []

    for config_path in sorted(INVESTIGATIONS_DIR.glob("*/config.yaml")):
        if config_path.parent.name.startswith("_"):
            continue

        with open(config_path) as f:
            config = yaml.safe_load(f)

        name = config.get("name", config_path.parent.name)
        investigations.append({
            "id": name,
            "name": config.get("primary_subject", name),
            "description": (config.get("description", "") or "").strip(),
            "color": INVESTIGATION_COLORS.get(name, DEFAULT_COLOR),
        })

    return investigations


def main():
    investigations = export_investigations()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(investigations, f, indent=2)

    print(f"  Investigations: {len(investigations)} profiles exported to {OUTPUT_PATH}")
    for inv in investigations:
        print(f"    {inv['id']}: {inv['name']} ({inv['color']})")


if __name__ == "__main__":
    main()
