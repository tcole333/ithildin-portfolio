#!/usr/bin/env python3
"""Automated dossier curation — selects key findings, builds viz data, suggests sections.

Reads raw dossier JSON (from export_dossiers.py) and enriches it with:
  - curation.key_finding_ids: ranked selection of most significant findings
  - curation.key_identifiers: jurisdiction, officers, entities from entity_roles
  - curation.section_suggestions: data-driven section scaffolds for LLM to fill
  - viz_data.ego_network: EgoNetwork component props
  - viz_data.timeline_events: TimelineChart component props

LLM-generated narrative fields (lead, sections, system_role, open_questions)
are added by the /curate-dossier skill and are NOT touched by this script.
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"
DOSSIER_DIR = Path(__file__).parent.parent / "content" / "dossiers"

STRENGTH_MAP = {"strong": 1.0, "medium": 0.7, "weak": 0.4, "circumstantial": 0.2}
CONFIDENCE_RANK = {"confirmed": 4, "high": 3, "medium": 2, "low": 1, "unverified": 0}
PRIORITY_TYPES = {"financial": 5, "corporate": 4, "legal": 3, "communication": 2, "intelligence": 2}

# Minimum findings of a type to warrant its own section
SECTION_THRESHOLD = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _score_finding(f: dict, connection_counts: dict[int, int]) -> float:
    score = 0.0
    evidence = f.get("evidence", [])
    has_primary = any(
        e.get("evidence_type") in ("primary", "court_filing", "government_record")
        or (e.get("evidence_ref") or "").startswith("EFTA")
        for e in evidence
    )
    if has_primary:
        score += 30
    if evidence:
        score += min(len(evidence) * 3, 15)
    score += CONFIDENCE_RANK.get(f.get("confidence", ""), 0) * 5
    score += PRIORITY_TYPES.get(f.get("finding_type", ""), 1)
    if f.get("date_of_event"):
        score += 8
    finding_id = f.get("id", 0)
    score += min(connection_counts.get(finding_id, 0) * 4, 16)
    if f.get("verification_status") == "verified":
        score += 10
    return score


def select_key_findings(dossier: dict, max_count: int = 12) -> list[int]:
    """Select the most significant finding IDs."""
    connection_counts: dict[int, int] = defaultdict(int)
    for conn in dossier.get("connections", []):
        for ev in conn.get("evidence", []):
            ref = ev.get("evidence_ref", "")
            if ref.startswith("Finding #"):
                try:
                    fid = int(ref.split("#")[1].strip())
                    connection_counts[fid] += 1
                except (ValueError, IndexError):
                    pass

    scored = []
    for f in dossier.get("findings", []):
        score = _score_finding(f, connection_counts)
        scored.append((f["id"], score))

    scored.sort(key=lambda x: -x[1])

    selected: list[int] = []
    type_counts: dict[str, int] = defaultdict(int)
    type_limit = max(3, max_count // 3)

    for fid, _score in scored:
        if len(selected) >= max_count:
            break
        finding = next((f for f in dossier["findings"] if f["id"] == fid), None)
        if not finding:
            continue
        ftype = finding.get("finding_type", "unknown")
        if type_counts[ftype] >= type_limit:
            continue
        selected.append(fid)
        type_counts[ftype] += 1

    if len(selected) < max_count:
        for fid, _score in scored:
            if fid in selected:
                continue
            if len(selected) >= max_count:
                break
            selected.append(fid)

    return selected


def suggest_sections(dossier: dict) -> list[dict]:
    """Analyze dossier data and suggest sections based on what exists.

    Returns section scaffolds with:
      - id, title: section identity
      - viz: optional viz type to embed (ego_network, timeline, sankey)
      - finding_ids: relevant finding IDs for this section
      - connection_ids: relevant connection IDs
      - guidance: what the LLM should cover in this section
    """
    findings = dossier.get("findings", [])
    connections = dossier.get("connections", [])
    entities = dossier.get("entities", [])

    # Count findings by type
    type_counts: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        ft = f.get("finding_type") or "unknown"
        type_counts[ft].append(f)

    # Count connections by type and strength
    strong_connections = [c for c in connections if c.get("strength") in ("strong", "medium")]
    connection_types: dict[str, list[dict]] = defaultdict(list)
    for c in connections:
        ct = c.get("relationship_type") or "unknown"
        connection_types[ct].append(c)

    sections: list[dict] = []

    # Key Relationships — if we have strong/medium connections
    if len(strong_connections) >= 2:
        top_conn_ids = [c["id"] for c in strong_connections[:15]]
        # Include relationship findings
        rel_findings = type_counts.get("relationship", [])
        sections.append({
            "id": "key-relationships",
            "title": "Key Relationships",
            "viz": "ego_network",
            "finding_ids": [f["id"] for f in rel_findings[:8]],
            "connection_ids": top_conn_ids,
            "guidance": "Narrative about the most significant relationships. Name each person with a link to their dossier. Explain the nature and significance of each relationship, not just that it exists.",
        })

    # Financial Activity — if financial findings exist
    financial = type_counts.get("financial", [])
    if len(financial) >= SECTION_THRESHOLD:
        sections.append({
            "id": "financial-activity",
            "title": "Financial Activity",
            "viz": None,
            "finding_ids": [f["id"] for f in financial[:10]],
            "connection_ids": [c["id"] for c in connection_types.get("financial", [])[:8]],
            "guidance": "Narrative about financial flows, amounts, counterparties, structures. Include specific dollar amounts and dates woven into prose.",
        })

    # Corporate Structure — if entity roles exist
    if len(entities) >= 2:
        corporate = type_counts.get("corporate", [])
        sections.append({
            "id": "corporate-structure",
            "title": "Corporate Structure",
            "viz": None,
            "finding_ids": [f["id"] for f in corporate[:8]],
            "connection_ids": [c["id"] for c in connection_types.get("corporate", [])[:6]],
            "guidance": "Narrative about corporate entities, roles, jurisdictions. Explain the structure and its purpose, not just list entities.",
        })

    # Legal Proceedings — if legal findings exist
    legal = type_counts.get("legal", [])
    if len(legal) >= SECTION_THRESHOLD:
        sections.append({
            "id": "legal-proceedings",
            "title": "Legal Proceedings",
            "viz": "timeline",
            "finding_ids": [f["id"] for f in legal[:8]],
            "connection_ids": [c["id"] for c in connection_types.get("legal", [])[:6]],
            "guidance": "Narrative about investigations, litigation, regulatory actions. Include case numbers, outcomes, and timeline.",
        })

    # Intelligence / Tradecraft — if intelligence findings exist
    intel = type_counts.get("intelligence", [])
    if len(intel) >= SECTION_THRESHOLD:
        sections.append({
            "id": "intelligence-activity",
            "title": "Intelligence Activity",
            "viz": None,
            "finding_ids": [f["id"] for f in intel[:8]],
            "connection_ids": [c["id"] for c in connection_types.get("intelligence", [])[:6]],
            "guidance": "Narrative about intelligence connections, surveillance, information operations.",
        })

    # Communications — if communication findings exist
    comms = type_counts.get("communication", [])
    if len(comms) >= SECTION_THRESHOLD:
        sections.append({
            "id": "communications",
            "title": "Communications",
            "viz": None,
            "finding_ids": [f["id"] for f in comms[:8]],
            "connection_ids": [],
            "guidance": "Narrative about documented communications — emails, calls, messages. Focus on what they reveal about the relationship or operation.",
        })

    # If no specific sections qualified, create a general "Background" section
    if not sections:
        all_finding_ids = [f["id"] for f in sorted(findings, key=lambda f: _score_finding(f, {}), reverse=True)[:10]]
        sections.append({
            "id": "background",
            "title": "Background",
            "viz": "ego_network" if len(connections) >= 3 else None,
            "finding_ids": all_finding_ids,
            "connection_ids": [c["id"] for c in strong_connections[:8]],
            "guidance": "General overview of this entity's role and significance in the investigation.",
        })

    # If timeline has enough events and no section already has timeline viz,
    # add it to the last section or create a chronology section
    timeline = dossier.get("timeline", [])
    has_timeline_viz = any(s.get("viz") == "timeline" for s in sections)
    if len(timeline) >= 4 and not has_timeline_viz:
        # Add timeline to the section with the most dated findings
        dated_by_section: dict[str, int] = {}
        finding_map = {f["id"]: f for f in findings}
        for s in sections:
            count = sum(1 for fid in s.get("finding_ids", [])
                       if finding_map.get(fid, {}).get("date_of_event"))
            dated_by_section[s["id"]] = count
        if dated_by_section:
            best = max(dated_by_section, key=lambda k: dated_by_section[k])
            for s in sections:
                if s["id"] == best and not s.get("viz"):
                    s["viz"] = "timeline"
                    break

    return sections


def build_ego_network(dossier: dict, db_path: Path = DB_PATH) -> dict:
    """Build EgoNetwork component props from dossier connections."""
    center = dossier["name"]
    connections = []

    for conn in dossier.get("connections", []):
        strength_str = conn.get("strength", "weak")
        strength = STRENGTH_MAP.get(strength_str, 0.3)
        ev_refs = [e.get("evidence_ref", "") for e in conn.get("evidence", [])]
        evidence_ref = ev_refs[0] if ev_refs else None

        connections.append({
            "target": conn["other_person"],
            "type": conn.get("relationship_type", "unknown"),
            "strength": strength,
            "evidence_ref": evidence_ref,
            "description": conn.get("description", ""),
        })

    second_hop: dict[str, list[dict]] = {}
    if connections:
        try:
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            first_hop_names = [c["target"] for c in connections[:20]]
            placeholders = ",".join("?" * len(first_hop_names))
            rows = db.execute(
                f"""
                SELECT c.person_a, c.person_b, c.relationship_type, c.strength, c.description
                FROM connections c
                WHERE c.verification_status != 'retracted'
                  AND (c.person_a IN ({placeholders}) OR c.person_b IN ({placeholders}))
                ORDER BY CASE c.strength
                    WHEN 'strong' THEN 1 WHEN 'medium' THEN 2
                    WHEN 'weak' THEN 3 ELSE 4 END
                LIMIT 200
                """,
                first_hop_names + first_hop_names,
            ).fetchall()

            for row in rows:
                source = row["person_a"] if row["person_a"] in first_hop_names else row["person_b"]
                target = row["person_b"] if source == row["person_a"] else row["person_a"]
                if target == center:
                    continue
                if source not in second_hop:
                    second_hop[source] = []
                if len(second_hop[source]) < 5:
                    second_hop[source].append({
                        "target": target,
                        "type": row["relationship_type"] or "unknown",
                        "strength": STRENGTH_MAP.get(row["strength"] or "weak", 0.3),
                        "description": row["description"] or "",
                    })
            db.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    return {
        "center": center,
        "connections": connections,
        "secondHop": second_hop,
        "depth": 2 if second_hop else 1,
    }


def build_timeline_events(dossier: dict) -> list[dict]:
    """Build TimelineChart component props from dossier timeline."""
    events = []
    for item in dossier.get("timeline", []):
        date = item.get("date")
        if not date:
            continue

        if item.get("type") == "finding":
            events.append({
                "date": date,
                "label": item.get("summary", ""),
                "type": item.get("finding_type", "unknown"),
                "evidence_ref": None,
                "id": item.get("id"),
            })
        elif item.get("type") == "connection":
            events.append({
                "date": date,
                "label": f"{item.get('relationship_type', 'connection')} with {item.get('other_person', '?')}",
                "type": item.get("relationship_type", "unknown"),
                "entity": item.get("other_person"),
                "id": item.get("id"),
            })

    return events


def extract_key_identifiers(dossier: dict) -> dict:
    """Extract key identifiers from entity_roles for sidebar display."""
    jurisdictions: set[str] = set()
    officers: list[dict] = []
    entities: list[dict] = []
    seen_entities: set[int] = set()

    for role in dossier.get("entities", []):
        if role.get("jurisdiction"):
            jurisdictions.add(role["jurisdiction"])

        eid = role.get("entity_id")
        if eid and eid not in seen_entities:
            seen_entities.add(eid)
            entities.append({
                "id": eid,
                "name": role.get("entity_name", ""),
                "type": role.get("entity_type", ""),
                "jurisdiction": role.get("jurisdiction", ""),
                "role": role.get("role", ""),
            })

        if role.get("role") and role.get("entity_name"):
            officers.append({
                "role": role["role"],
                "entity": role["entity_name"],
                "start": role.get("date_start"),
                "end": role.get("date_end"),
            })

    return {
        "jurisdictions": sorted(jurisdictions),
        "officers": officers[:15],
        "entities": entities[:20],
    }


def curate_dossier(dossier_path: Path, db_path: Path = DB_PATH, viz_only: bool = False) -> dict:
    """Add curation scaffold and viz_data to a dossier JSON file.

    Preserves any existing LLM-generated fields in curation
    (lead, sections, system_role, open_questions).
    """
    dossier = json.loads(dossier_path.read_text())

    # Preserve existing LLM-generated curation fields
    existing_curation = dossier.get("curation") or {}
    llm_fields = {
        k: existing_curation[k]
        for k in ("lead", "sections", "system_role", "open_questions", "applicable_models",
                   # Legacy fields — keep if present for backward compat
                   "overview", "financial_summary", "ownership_chain")
        if k in existing_curation
    }

    key_finding_ids = select_key_findings(dossier)
    key_identifiers = extract_key_identifiers(dossier)
    section_suggestions = suggest_sections(dossier)
    ego_network = build_ego_network(dossier, db_path)
    timeline_events = build_timeline_events(dossier)

    curation = {
        "key_finding_ids": key_finding_ids,
        "key_identifiers": key_identifiers,
        "section_suggestions": section_suggestions,
        "curated_at": _utcnow(),
    }

    if not viz_only:
        curation.update(llm_fields)

    dossier["curation"] = curation
    dossier["viz_data"] = {
        "ego_network": ego_network,
        "timeline_events": timeline_events,
    }

    with open(dossier_path, "w") as f:
        json.dump(dossier, f, indent=2, default=str)

    return dossier


def main():
    parser = argparse.ArgumentParser(description="Curate dossier data — select key findings, build viz data, suggest sections")
    parser.add_argument("--target", help="Curate a single target (name or slug)")
    parser.add_argument("--all", action="store_true", help="Curate all dossiers")
    parser.add_argument("--viz-only", action="store_true", help="Only update viz_data, skip curation scaffold")
    parser.add_argument("--dossier-dir", type=Path, default=DOSSIER_DIR)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.target and not args.all:
        parser.error("Specify --target NAME or --all")

    if not args.dossier_dir.exists():
        print(f"Dossier directory not found: {args.dossier_dir}", file=sys.stderr)
        sys.exit(1)

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
                    if args.target.lower() in [a.lower() for a in d.get("aliases", [])]:
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

    print(f"Curating {len(dossier_files)} dossier(s)...")
    for path in dossier_files:
        try:
            dossier = curate_dossier(path, db_path=args.db, viz_only=args.viz_only)
            name = dossier.get("name", path.stem)
            curation = dossier.get("curation", {})
            suggestions = curation.get("section_suggestions", [])
            has_lead = bool(curation.get("lead"))
            has_sections = bool(curation.get("sections"))
            key_count = len(curation.get("key_finding_ids", []))
            ego_count = len(dossier.get("viz_data", {}).get("ego_network", {}).get("connections", []))

            section_names = [s["id"] for s in suggestions]
            status = "[has narrative]" if has_lead and has_sections else "[needs /curate-dossier]"
            print(f"  {name}: {key_count} key findings, {ego_count} connections, sections: {', '.join(section_names)} {status}")
        except Exception as e:
            print(f"  ERROR {path.stem}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
