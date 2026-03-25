#!/usr/bin/env python3
"""Export compact markdown context files for research agents.

Generates one markdown file per dossier in content/agent-context/{slug}.md.
Agents load a single file read instead of querying investigation.db.
"""

import argparse
import json
import sys
from pathlib import Path

DOSSIER_DIR = Path(__file__).parent.parent / "content" / "dossiers"
OUTPUT_DIR = Path(__file__).parent.parent / "content" / "agent-context"


def export_context(dossier: dict) -> str:
    """Generate compact markdown from a dossier JSON."""
    lines = []
    name = dossier["name"]
    slug = dossier["slug"]
    curation = dossier.get("curation") or {}
    stats = dossier.get("stats", {})

    # Header
    lines.append(f"# {name}")
    if dossier.get("aliases"):
        lines.append(f"**Aliases**: {', '.join(dossier['aliases'])}")
    lines.append(f"**Stats**: {stats.get('total_findings', 0)} findings, {stats.get('total_connections', 0)} connections, {stats.get('total_entities', 0)} entities")
    lines.append(f"**Dossier**: /dossiers/{slug}")
    lines.append("")

    # System role
    if curation.get("system_role"):
        lines.append(f"> {curation['system_role']}")
        lines.append("")

    # Overview (strip HTML for markdown)
    if curation.get("overview"):
        import re
        overview_text = re.sub(r'<[^>]+>', '', curation["overview"])
        overview_text = re.sub(r'\s+', ' ', overview_text).strip()
        lines.append("## Overview")
        lines.append(overview_text)
        lines.append("")

    # Key findings
    key_ids = set(curation.get("key_finding_ids", []))
    findings = dossier.get("findings", [])
    if key_ids:
        key_findings = [f for f in findings if f["id"] in key_ids]
    else:
        # Fall back to top 8 by date/confidence
        key_findings = sorted(
            findings,
            key=lambda f: (
                {"confirmed": 4, "high": 3, "medium": 2, "low": 1}.get(f.get("confidence", ""), 0),
                f.get("date_of_event") or "",
            ),
            reverse=True,
        )[:8]

    if key_findings:
        lines.append("## Key Findings")
        for f in key_findings:
            conf = f.get("confidence", "?")
            ftype = f.get("finding_type", "?")
            date = f.get("date_of_event", "")
            date_str = f" ({date})" if date else ""
            lines.append(f"- **[{ftype}/{conf}]** {f['summary']}{date_str} (Finding #{f['id']})")
        lines.append("")

    # Top connections
    connections = dossier.get("connections", [])
    if connections:
        lines.append("## Top Connections")
        for c in connections[:12]:
            strength = c.get("strength", "?")
            rtype = c.get("relationship_type", "?")
            lines.append(f"- **{c['other_person']}** [{rtype}/{strength}]: {c.get('description', '')}")
        if len(connections) > 12:
            lines.append(f"- ... and {len(connections) - 12} more")
        lines.append("")

    # Entities
    entities = dossier.get("entities", [])
    if entities:
        lines.append("## Entity Roles")
        for e in entities[:10]:
            role = e.get("role", "?")
            entity_name = e.get("entity_name", "?")
            jurisdiction = e.get("jurisdiction", "")
            j_str = f" ({jurisdiction})" if jurisdiction else ""
            lines.append(f"- {role} at {entity_name}{j_str}")
        if len(entities) > 10:
            lines.append(f"- ... and {len(entities) - 10} more")
        lines.append("")

    # Open questions
    if curation.get("open_questions"):
        lines.append("## Open Questions")
        for q in curation["open_questions"]:
            lines.append(f"- {q}")
        lines.append("")

    # Applicable models
    if curation.get("applicable_models"):
        lines.append("## Applicable Models")
        for m in curation["applicable_models"]:
            lines.append(f"- {m}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Export agent context markdown from dossier JSON")
    parser.add_argument("--target", help="Export for a single target (name or slug)")
    parser.add_argument("--all", action="store_true", help="Export all dossiers")
    parser.add_argument("--dossier-dir", type=Path, default=DOSSIER_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    if not args.target and not args.all:
        parser.error("Specify --target NAME or --all")

    if not args.dossier_dir.exists():
        print(f"Dossier directory not found: {args.dossier_dir}", file=sys.stderr)
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    dossier_files: list[Path] = []
    if args.target:
        slug_path = args.dossier_dir / f"{args.target.lower().replace(' ', '-')}.json"
        if slug_path.exists():
            dossier_files.append(slug_path)
        else:
            for p in args.dossier_dir.glob("*.json"):
                if p.name.startswith("_"):
                    continue
                try:
                    d = json.loads(p.read_text())
                    if d.get("name", "").lower() == args.target.lower():
                        dossier_files.append(p)
                        break
                except json.JSONDecodeError:
                    continue
        if not dossier_files:
            print(f"No dossier found for: {args.target}", file=sys.stderr)
            sys.exit(1)
    else:
        dossier_files = sorted(
            p for p in args.dossier_dir.glob("*.json") if not p.name.startswith("_")
        )

    print(f"Exporting agent context for {len(dossier_files)} dossier(s)...")
    for path in dossier_files:
        try:
            dossier = json.loads(path.read_text())
            md = export_context(dossier)
            out_path = args.output_dir / f"{dossier['slug']}.md"
            out_path.write_text(md)
            lines = len(md.splitlines())
            print(f"  {dossier['name']}: {lines} lines -> {out_path.name}")
        except Exception as e:
            print(f"  ERROR {path.stem}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
