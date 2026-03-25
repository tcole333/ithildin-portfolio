#!/usr/bin/env python3
"""Orchestrate full site data export pipeline."""

import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).parent
SITE_DIR = PIPELINE_DIR.parent
WEB_DIR = SITE_DIR / "web"


def run(cmd: list[str], label: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=str(PIPELINE_DIR.parent.parent))
    if result.returncode != 0:
        print(f"  FAILED: {label}")
        return False
    return True


def main():
    steps = [
        (["uv", "run", "python", str(PIPELINE_DIR / "export_investigations.py")], "Export investigations"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_dossiers.py"), "--all-profiles"], "Export dossiers"),
        (["uv", "run", "python", str(PIPELINE_DIR / "curate_dossier.py"), "--all", "--all-profiles"], "Curate dossiers"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_network.py")], "Export network graph"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_financials.py")], "Export financial flows"),
        (["uv", "run", "python", str(PIPELINE_DIR / "story_clustering.py")], "Export story clusters"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_structures.py")], "Export corporate structures"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_models.py")], "Validate & index models"),
        (["uv", "run", "python", str(PIPELINE_DIR / "compute_backlinks.py")], "Compute backlinks"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_agent_context.py"), "--all"], "Export agent context"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_search_index.py")], "Export search index"),
        (["uv", "run", "python", str(PIPELINE_DIR / "export_preview_index.py")], "Export preview index"),
    ]

    failed = []
    for cmd, label in steps:
        if not run(cmd, label):
            failed.append(label)

    print(f"\n{'='*60}")
    if failed:
        print(f"  {len(failed)} step(s) failed: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("  All pipeline steps completed successfully.")
        print(f"\n  Content dir: {SITE_DIR / 'content'}")
        print(f"  Next: cd {WEB_DIR} && npm run build")


if __name__ == "__main__":
    main()
