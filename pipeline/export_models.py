#!/usr/bin/env python3
"""Validate model JSON files and produce _index.json for the models index page.

Models are hand-authored editorial content (not auto-generated from investigation.db).
This script validates schema compliance and builds a summary index.
"""

import json
import sys
from pathlib import Path

MODELS_DIR = Path(__file__).parent.parent / "content" / "models"

REQUIRED_FIELDS = [
    "id", "version", "title", "subtitle", "archetype", "definition",
    "case_intro", "mechanism", "mechanism_details", "canonical_instances",
    "detection_markers", "limitations", "related_models", "visualization",
    "last_updated",
]

VALID_IDS = [
    "manufactured-dependency", "bridge-tax", "private-order", "narrative-shield",
    "jurisdictional-arbitrage", "parallel-financial-system", "enabler-gradient",
    "complexity-as-credential",
]


def validate_model(path: Path) -> tuple[dict | None, list[str]]:
    """Validate a single model JSON file. Returns (data, errors)."""
    errors: list[str] = []

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, [f"Invalid JSON: {e}"]

    for field in REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if data.get("id") and data["id"] != path.stem:
        errors.append(f"ID mismatch: file={path.stem}, id={data['id']}")

    if data.get("id") and data["id"] not in VALID_IDS:
        errors.append(f"Unknown model ID: {data['id']}")

    mechanism = data.get("mechanism", [])
    details = data.get("mechanism_details", {})
    if isinstance(mechanism, list) and isinstance(details, dict):
        for step in mechanism:
            if step not in details:
                errors.append(f"Mechanism step '{step}' missing from mechanism_details")

    for inst in data.get("canonical_instances", []):
        if "label" not in inst:
            errors.append("Canonical instance missing 'label'")
        if "summary" not in inst:
            errors.append("Canonical instance missing 'summary'")

    related = data.get("related_models", [])
    for rel in related:
        if rel not in VALID_IDS:
            errors.append(f"Related model '{rel}' not in valid IDs")
        if rel == data.get("id"):
            errors.append("Model references itself in related_models")

    viz = data.get("visualization", {})
    if isinstance(viz, dict):
        if "primary_type" not in viz:
            errors.append("visualization missing 'primary_type'")

    return data, errors


def build_index(models: list[dict]) -> list[dict]:
    """Build summary index from validated models."""
    index = []
    for m in models:
        index.append({
            "id": m["id"],
            "title": m["title"],
            "subtitle": m["subtitle"],
            "archetype": m["archetype"],
            "definition": m["definition"],
            "instance_count": len(m.get("canonical_instances", [])),
            "mechanism_count": len(m.get("mechanism", [])),
            "related_models": m.get("related_models", []),
            "visualization_type": m.get("visualization", {}).get("primary_type", ""),
            "last_updated": m.get("last_updated", ""),
        })
    return index


def main():
    print("Validating & indexing models...")

    if not MODELS_DIR.exists():
        print(f"  Models directory not found: {MODELS_DIR}")
        sys.exit(1)

    model_files = sorted(MODELS_DIR.glob("*.json"))
    model_files = [f for f in model_files if not f.name.startswith("_")]

    if not model_files:
        print("  No model files found")
        sys.exit(1)

    print(f"  Found {len(model_files)} model files")

    all_errors: dict[str, list[str]] = {}
    valid_models: list[dict] = []

    for path in model_files:
        data, errors = validate_model(path)
        if errors:
            all_errors[path.name] = errors
        if data:
            valid_models.append(data)

    if all_errors:
        print("\n  Validation errors:")
        for filename, errors in all_errors.items():
            for err in errors:
                print(f"    {filename}: {err}")

    if all_errors:
        print(f"\n  {len(all_errors)} file(s) with errors")
        sys.exit(1)

    # Check all expected models are present
    found_ids = {m["id"] for m in valid_models}
    missing = set(VALID_IDS) - found_ids
    if missing:
        print(f"\n  Missing models: {', '.join(sorted(missing))}")
        sys.exit(1)

    # Build and write index
    index = build_index(valid_models)
    index_path = MODELS_DIR / "_index.json"
    index_path.write_text(json.dumps(index, indent=2))
    print(f"  Written {len(index)} models to {index_path}")


if __name__ == "__main__":
    main()
