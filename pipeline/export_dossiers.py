#!/usr/bin/env python3
"""Export investigation.db targets as JSON dossiers for the presentation site.

Uses name_aliases table to merge findings/connections across name variants,
producing one dossier per canonical target instead of split pages.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    from evidence_refs import canonicalize_evidence_rows
except ImportError:
    # Allows importing when executed as a package module.
    from .evidence_refs import canonicalize_evidence_rows

DB_PATH = Path(__file__).parent.parent / "investigation.db"
OUTPUT_DIR = Path(__file__).parent.parent / "content" / "dossiers"

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from tools.investigation_context import get_active_profile_id
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


def slugify(name: str) -> str:
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)  # Remove everything except alphanumeric, spaces, hyphens
    slug = re.sub(r'[\s-]+', '-', slug)  # Collapse whitespace and hyphens
    return slug.strip('-')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if left >= right else right


def get_connection(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _load_alias_groups(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Load aliases and build lookup structures.

    Returns:
        raw_to_canonical: {raw_name_lower: canonical_name}
        canonical_to_aliases: {canonical_name: [alias1, alias2, ...]}
    """
    raw_to_canonical: dict[str, str] = {}
    canonical_to_aliases: dict[str, list[str]] = {}

    try:
        rows = conn.execute("SELECT canonical_name, alias FROM name_aliases").fetchall()
        for row in rows:
            raw_to_canonical[row["alias"].lower()] = row["canonical_name"]
            canon = row["canonical_name"]
            if canon not in canonical_to_aliases:
                canonical_to_aliases[canon] = []
            canonical_to_aliases[canon].append(row["alias"])
    except sqlite3.OperationalError:
        pass

    return raw_to_canonical, canonical_to_aliases


def _resolve(name: str, raw_to_canonical: dict[str, str]) -> str:
    """Resolve a name to its canonical form."""
    return raw_to_canonical.get(name.lower(), name)


def export_target(conn: sqlite3.Connection, canonical_name: str, all_names: list[str],
                  profile_id: str | None = None) -> dict:
    """Export all data for a single target, aggregating across all name variants."""

    # Build placeholders for all name variants
    placeholders = ",".join("?" * len(all_names))

    # Findings with evidence (across all name variants)
    findings = []
    last_updated = None
    profile_cond = " AND f.profile_id = ?" if profile_id else ""
    profile_params = [profile_id] if profile_id else []
    rows = conn.execute(
        f"""
        SELECT f.id, f.finding_type, f.summary, f.detail, f.source_datasets,
               f.confidence, f.date_of_event, f.claim_type, f.verification_status,
               f.created_at, f.target_name, f.profile_id
        FROM findings f
        WHERE f.target_name IN ({placeholders}) AND f.verification_status != 'retracted'{profile_cond}
        ORDER BY f.date_of_event IS NULL, f.date_of_event, f.created_at
        """,
        all_names + profile_params,
    ).fetchall()

    for row in rows:
        finding = dict(row)
        if finding["source_datasets"]:
            try:
                finding["source_datasets"] = json.loads(finding["source_datasets"])
            except (json.JSONDecodeError, TypeError):
                pass

        last_updated = _max_datetime(last_updated, _parse_datetime(row["created_at"]))

        evidence = conn.execute(
            """
            SELECT evidence_type, evidence_ref, source_quote, source_page, assessment
            FROM finding_evidence
            WHERE finding_id = ?
            """,
            (row["id"],),
        ).fetchall()
        finding["evidence"] = canonicalize_evidence_rows([dict(e) for e in evidence])
        findings.append(finding)

    # Connections (across all name variants)
    connections = []
    seen_conn_ids = set()
    raw_to_canonical, _ = _load_alias_groups(conn)

    for name in all_names:
        conn_params = [name, name] + profile_params
        conn_rows = conn.execute(
            f"""
            SELECT c.id, c.person_a, c.person_b, c.relationship_type, c.description,
                   c.strength, c.date_range, c.verification_status, c.created_at,
                   c.profile_id
            FROM connections c
            WHERE (c.person_a = ? OR c.person_b = ?) AND c.verification_status != 'retracted'{profile_cond.replace('f.', 'c.')}
            ORDER BY
                CASE c.strength
                    WHEN 'strong' THEN 1 WHEN 'medium' THEN 2
                    WHEN 'weak' THEN 3 WHEN 'circumstantial' THEN 4 ELSE 5
                END, c.created_at
            """,
            conn_params,
        ).fetchall()

        for row in conn_rows:
            if row["id"] in seen_conn_ids:
                continue
            seen_conn_ids.add(row["id"])

            connection = dict(row)
            # Resolve the "other person" to their canonical name
            raw_other = row["person_b"] if row["person_a"] == name else row["person_a"]
            other_person = _resolve(raw_other, raw_to_canonical)
            connection["other_person"] = other_person
            connection["other_person_slug"] = slugify(other_person)

            evidence = conn.execute(
                """
                SELECT evidence_type, evidence_ref, source_quote, source_page
                FROM connection_evidence
                WHERE connection_id = ?
                """,
                (row["id"],),
            ).fetchall()
            connection["evidence"] = canonicalize_evidence_rows([dict(e) for e in evidence])
            connections.append(connection)
            last_updated = _max_datetime(last_updated, _parse_datetime(row["created_at"]))

    # Entity roles (across all name variants)
    entity_roles = []
    for name in all_names:
        role_rows = conn.execute(
            """
            SELECT er.role, er.date_start, er.date_end, er.source,
                   e.id as entity_id, e.name as entity_name, e.entity_type,
                   e.jurisdiction, e.status as entity_status
            FROM entity_roles er
            JOIN entities e ON er.entity_id = e.id
            WHERE er.person_name = ?
            ORDER BY er.date_start IS NULL, er.date_start
            """,
            (name,),
        ).fetchall()
        entity_roles.extend(dict(r) for r in role_rows)

    # Timeline
    timeline = []
    for f in findings:
        if f.get("date_of_event"):
            timeline.append({
                "date": f["date_of_event"],
                "type": "finding",
                "finding_type": f["finding_type"],
                "summary": f["summary"],
                "confidence": f["confidence"],
                "id": f["id"],
            })
    for c in connections:
        if c.get("date_range"):
            timeline.append({
                "date": c["date_range"],
                "type": "connection",
                "relationship_type": c["relationship_type"],
                "description": c["description"],
                "other_person": c["other_person"],
                "id": c["id"],
            })
    timeline.sort(key=lambda x: x["date"])

    # Summary stats
    finding_types = {}
    for f in findings:
        ft = f["finding_type"] or "unknown"
        finding_types[ft] = finding_types.get(ft, 0) + 1

    connection_types = {}
    for c in connections:
        ct = c["relationship_type"] or "unknown"
        connection_types[ct] = connection_types.get(ct, 0) + 1

    last_updated_str = last_updated.isoformat() if last_updated else None
    generated_at = _utcnow().isoformat()

    # Collect all investigation profiles that contributed data
    profile_ids_set = set()
    for f in findings:
        if f.get("profile_id"):
            profile_ids_set.add(f["profile_id"])
    for c in connections:
        if c.get("profile_id"):
            profile_ids_set.add(c["profile_id"])

    return {
        "name": canonical_name,
        "slug": slugify(canonical_name),
        "aliases": [n for n in all_names if n != canonical_name],
        "profile_ids": sorted(profile_ids_set),
        "generated_at": generated_at,
        "last_updated": last_updated_str,
        "stats": {
            "total_findings": len(findings),
            "total_connections": len(connections),
            "total_entities": len(entity_roles),
            "finding_types": finding_types,
            "connection_types": connection_types,
            "last_updated": last_updated_str,
        },
        "findings": findings,
        "connections": connections,
        "entities": entity_roles,
        "timeline": timeline,
    }


def get_targets(conn: sqlite3.Connection, min_findings: int = 5,
                profile_id: str | None = None) -> list[tuple[str, list[str]]]:
    """Get canonical targets with all their name variants.

    Returns: [(canonical_name, [all_names_including_canonical]), ...]
    """
    raw_to_canonical, canonical_to_aliases = _load_alias_groups(conn)

    # Get all target names with finding counts
    if profile_id:
        rows = conn.execute(
            """
            SELECT target_name, COUNT(*) as cnt
            FROM findings
            WHERE verification_status != 'retracted' AND profile_id = ?
            GROUP BY target_name
            """,
            (profile_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT target_name, COUNT(*) as cnt
            FROM findings
            WHERE verification_status != 'retracted'
            GROUP BY target_name
            """,
        ).fetchall()

    # Group by canonical name
    canonical_counts: dict[str, int] = {}
    canonical_raw_names: dict[str, set[str]] = {}

    for row in rows:
        raw_name = row["target_name"]
        canonical = raw_to_canonical.get(raw_name.lower(), raw_name)

        canonical_counts[canonical] = canonical_counts.get(canonical, 0) + row["cnt"]
        if canonical not in canonical_raw_names:
            canonical_raw_names[canonical] = set()
        canonical_raw_names[canonical].add(raw_name)
        # Also add known aliases that might not have findings yet
        if canonical in canonical_to_aliases:
            for alias in canonical_to_aliases[canonical]:
                canonical_raw_names[canonical].add(alias)
        canonical_raw_names[canonical].add(canonical)

    # Filter by min_findings and sort
    targets = [
        (name, sorted(canonical_raw_names[name]))
        for name, cnt in sorted(canonical_counts.items(), key=lambda x: -x[1])
        if cnt >= min_findings
    ]

    return targets


def main():
    parser = argparse.ArgumentParser(description="Export dossiers from investigation.db")
    parser.add_argument("--target", help="Export a single target")
    parser.add_argument("--min-findings", type=int, default=5, help="Minimum findings to export (default: 5)")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--list", action="store_true", help="List targets that would be exported")
    parser.add_argument("--incremental", action="store_true", help="Skip dossiers with no new updates")
    parser.add_argument("--curate", action="store_true", help="Run curation pipeline after export")
    parser.add_argument("--profile", default=None, help="Investigation profile (default: active)")
    parser.add_argument("--all-profiles", action="store_true", help="Include all profiles")
    args = parser.parse_args()

    resolved_profile = _resolve_profile(args.profile, args.all_profiles)
    conn = get_connection()

    if args.list:
        targets = get_targets(conn, args.min_findings, profile_id=resolved_profile)
        print(f"{len(targets)} targets with >= {args.min_findings} findings:")
        for canonical, all_names in targets:
            # Sum findings across all name variants
            profile_cond = " AND profile_id = ?" if resolved_profile else ""
            profile_params_list = [resolved_profile] if resolved_profile else []
            total = sum(
                conn.execute(
                    f"SELECT COUNT(*) FROM findings WHERE target_name = ? AND verification_status != 'retracted'{profile_cond}",
                    [n] + profile_params_list,
                ).fetchone()[0]
                for n in all_names
            )
            aliases = [n for n in all_names if n != canonical]
            alias_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
            print(f"  {canonical}: {total}{alias_str}")
        return

    if args.target:
        # Resolve target to canonical + all names
        raw_to_canonical, canonical_to_aliases = _load_alias_groups(conn)
        canonical = raw_to_canonical.get(args.target.lower(), args.target)
        all_names = [canonical]
        if canonical in canonical_to_aliases:
            all_names.extend(canonical_to_aliases[canonical])
        all_names = sorted(set(all_names))
        targets = [(canonical, all_names)]
    else:
        targets = get_targets(conn, args.min_findings, profile_id=resolved_profile)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    index = []
    redirects = {}  # old_slug -> canonical_slug

    for canonical, all_names in targets:
        dossier = export_target(conn, canonical, all_names, profile_id=resolved_profile)
        out_path = args.output_dir / f"{dossier['slug']}.json"

        dossier_for_index = dossier
        skipped = False
        if args.incremental and out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
            except json.JSONDecodeError:
                existing = None
            if existing:
                existing_last = _parse_datetime(
                    existing.get("last_updated") or existing.get("generated_at")
                )
                new_last = _parse_datetime(dossier.get("last_updated"))
                if existing_last and new_last and existing_last >= new_last:
                    dossier_for_index = existing
                    skipped = True

        if not skipped:
            # Preserve curation content from existing file on disk
            if out_path.exists():
                try:
                    existing = json.loads(out_path.read_text())
                    if existing.get("curation"):
                        dossier["curation"] = existing["curation"]
                except (json.JSONDecodeError, KeyError):
                    pass
            with open(out_path, "w") as f:
                json.dump(dossier, f, indent=2, default=str)

        index.append({
            "name": dossier_for_index["name"],
            "slug": dossier_for_index["slug"],
            "profile_ids": dossier_for_index.get("profile_ids", []),
            "stats": dossier_for_index["stats"],
            "last_updated": dossier_for_index.get("last_updated"),
        })

        # Generate redirects for alias slugs
        for alias in dossier_for_index.get("aliases", []):
            alias_slug = slugify(alias)
            if alias_slug != dossier_for_index["slug"]:
                redirects[alias_slug] = dossier_for_index["slug"]

        alias_info = f" (+{len(dossier_for_index['aliases'])} aliases)" if dossier_for_index.get("aliases") else ""
        action = "Skipped" if skipped else "Exported"
        print(f"  {action} {canonical} ({dossier_for_index['stats']['total_findings']} findings, {dossier_for_index['stats']['total_connections']} connections){alias_info}")

    # Write index — merge into existing when exporting single targets
    index_path = args.output_dir / "_index.json"
    if args.target and index_path.exists():
        try:
            existing_index = json.loads(index_path.read_text())
        except (json.JSONDecodeError, TypeError):
            existing_index = []
        # Replace or append entries for exported targets
        exported_slugs = {e["slug"] for e in index}
        merged = [e for e in existing_index if e["slug"] not in exported_slugs]
        merged.extend(index)
        merged.sort(key=lambda e: e["name"])
        index = merged
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)

    # Write redirects — merge into existing when exporting single targets
    redirects_path = args.output_dir / "_redirects.json"
    if args.target and redirects_path.exists():
        try:
            existing_redirects = json.loads(redirects_path.read_text())
        except (json.JSONDecodeError, TypeError):
            existing_redirects = {}
        existing_redirects.update(redirects)
        redirects = existing_redirects
    if redirects:
        with open(redirects_path, "w") as f:
            json.dump(redirects, f, indent=2)
        print(f"\n  {len(redirects)} redirects written to {redirects_path}")

    print(f"\nExported {len(targets)} dossiers to {args.output_dir}")
    conn.close()

    if args.curate:
        import subprocess

        curate_script = Path(__file__).parent / "curate_dossier.py"
        curate_args = ["uv", "run", "python", str(curate_script)]
        if args.target:
            curate_args.extend(["--target", args.target])
        else:
            curate_args.append("--all")
        curate_args.extend(["--dossier-dir", str(args.output_dir)])

        print("\nRunning curation pipeline...")
        result = subprocess.run(curate_args)
        if result.returncode != 0:
            print("  Curation pipeline failed", file=sys.stderr)


if __name__ == "__main__":
    main()
