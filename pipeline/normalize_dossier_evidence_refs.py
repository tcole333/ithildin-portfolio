#!/usr/bin/env python3
"""Normalize dossier evidence_ref values in-place.

Expands mixed evidence strings into one canonical evidence_ref per row using
pipeline/evidence_refs.py utilities.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evidence_refs import canonicalize_evidence_rows


DEFAULT_DOSSIER_DIR = Path(__file__).parent.parent / "content" / "dossiers"


def _normalize_block(rows: list[dict]) -> tuple[list[dict], bool]:
    normalized = canonicalize_evidence_rows(rows)
    return normalized, normalized != rows


def normalize_dossier(path: Path) -> tuple[bool, int]:
    data = json.loads(path.read_text())
    changed = False
    total_rows_rewritten = 0

    for finding in data.get("findings", []):
        rows = list(finding.get("evidence") or [])
        normalized, did_change = _normalize_block(rows)
        if did_change:
            finding["evidence"] = normalized
            changed = True
            total_rows_rewritten += 1

    for connection in data.get("connections", []):
        rows = list(connection.get("evidence") or [])
        normalized, did_change = _normalize_block(rows)
        if did_change:
            connection["evidence"] = normalized
            changed = True
            total_rows_rewritten += 1

    if changed:
        path.write_text(f"{json.dumps(data, indent=2, default=str)}\n")

    return changed, total_rows_rewritten


def iter_dossier_paths(dossier_dir: Path, target: str | None) -> list[Path]:
    if target:
        slug = target.lower().replace(" ", "-")
        direct = dossier_dir / f"{slug}.json"
        if direct.exists():
            return [direct]
        raise FileNotFoundError(f"No dossier found for target '{target}' at {direct}")

    return sorted(p for p in dossier_dir.glob("*.json") if not p.name.startswith("_"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize evidence refs in dossier JSON files.")
    parser.add_argument("--dossier-dir", type=Path, default=DEFAULT_DOSSIER_DIR)
    parser.add_argument("--target", help="Optional target name/slug to normalize a single dossier")
    args = parser.parse_args()

    if not args.dossier_dir.exists():
        raise FileNotFoundError(f"Dossier directory not found: {args.dossier_dir}")

    paths = iter_dossier_paths(args.dossier_dir, args.target)

    changed_files = 0
    rewritten_blocks = 0
    for path in paths:
        changed, rewrites = normalize_dossier(path)
        if changed:
            changed_files += 1
            rewritten_blocks += rewrites
            print(f"normalized {path.name} ({rewrites} evidence block(s) rewritten)")

    print(f"done: {changed_files} file(s) updated, {rewritten_blocks} evidence block(s) rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

