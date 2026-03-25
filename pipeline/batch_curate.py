#!/usr/bin/env python3
"""Generate curation prompts for dossiers that need narrative content.

Usage:
    uv run python pipeline/batch_curate.py --list          # Show dossiers needing curation
    uv run python pipeline/batch_curate.py --prompt SLUG   # Print curation prompt for a slug
    uv run python pipeline/batch_curate.py --batch N       # Print top N slugs needing curation
"""

import argparse
import json
from pathlib import Path

DOSSIER_DIR = Path("content/dossiers")
AGENT_CONTEXT_DIR = Path("content/agent-context")
MODELS_DIR = Path("content/models")
SUMMARY_LIMIT = 240
DETAIL_LIMIT = 180
EVIDENCE_REF_LIMIT = 4


def get_dossiers_needing_curation():
    """Return list of (slug, name, finding_count) sorted by finding_count desc."""
    results = []
    for f in sorted(DOSSIER_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        d = json.loads(f.read_text())
        c = d.get("curation", {})
        if c.get("lead") and c.get("sections"):
            continue
        slug = f.stem
        name = d.get("name", slug)
        findings = len(d.get("findings", []))
        results.append((slug, name, findings))
    results.sort(key=lambda x: -x[2])
    return results


def get_all_slugs():
    """Return set of all dossier slugs for cross-linking reference."""
    slugs = set()
    for f in DOSSIER_DIR.glob("*.json"):
        if not f.name.startswith("_"):
            slugs.add(f.stem)
    return slugs


def get_model_ids():
    """Return list of analytical model IDs."""
    ids = []
    for f in MODELS_DIR.glob("*.json"):
        ids.append(f.stem)
    return sorted(ids)


def truncate(text: str, limit: int) -> str:
    """Trim long strings for prompt readability."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def format_evidence_refs(evidence: list[dict], limit: int = EVIDENCE_REF_LIMIT) -> str:
    """Summarize evidence refs without dumping full evidence payloads."""
    refs = []
    for ev in evidence or []:
        ref = (ev.get("evidence_ref") or "").strip()
        if ref:
            refs.append(ref)
    if not refs:
        return ""
    shown = refs[:limit]
    suffix = "" if len(refs) <= limit else f" (+{len(refs) - limit} more)"
    return ", ".join(shown) + suffix


def format_finding_summary(finding: dict, key_ids: set[int]) -> str:
    """Format one finding for the prompt."""
    fid = finding.get("id", "?")
    ftype = finding.get("finding_type") or finding.get("type") or "?"
    conf = finding.get("confidence", "?")
    claim_type = finding.get("claim_type", "?")
    verification = finding.get("verification_status", "?")
    summary = truncate(finding.get("summary", ""), SUMMARY_LIMIT)
    detail = truncate(finding.get("detail", ""), DETAIL_LIMIT)
    evidence_refs = format_evidence_refs(finding.get("evidence", []))
    is_key = " [KEY]" if fid in key_ids else ""

    lines = [
        f"  - Finding #{fid} [{ftype}/{conf}/{claim_type}/{verification}]{is_key}: {summary}"
    ]
    if detail:
        lines.append(f"    Detail: {detail}")
    if evidence_refs:
        lines.append(f"    Evidence refs: {evidence_refs}")
    return "\n".join(lines)


def format_connection_summary(connection: dict) -> str:
    """Format one connection for the prompt."""
    left = (
        connection.get("person_a")
        or connection.get("source_name")
        or connection.get("source")
        or "?"
    )
    right = (
        connection.get("person_b")
        or connection.get("target_name")
        or connection.get("target")
        or "?"
    )
    ctype = connection.get("connection_type") or connection.get("relationship_type") or "?"
    strength = connection.get("strength") or connection.get("confidence") or "?"
    detail = truncate(connection.get("detail") or connection.get("description") or "", DETAIL_LIMIT)
    evidence_refs = format_evidence_refs(connection.get("evidence", []))

    lines = [f"  - {left} <-> {right} ({ctype}, {strength})"]
    if detail:
        lines.append(f"    Detail: {detail}")
    if evidence_refs:
        lines.append(f"    Evidence refs: {evidence_refs}")
    return "\n".join(lines)


def generate_prompt(slug: str) -> str:
    """Generate a curation prompt for a given dossier slug."""
    dossier_path = DOSSIER_DIR / f"{slug}.json"
    if not dossier_path.exists():
        raise FileNotFoundError(f"No dossier at {dossier_path}")

    d = json.loads(dossier_path.read_text())
    name = d.get("name", slug)
    c = d.get("curation", {})

    # Get section suggestions
    suggestions = c.get("section_suggestions", [])
    suggestion_text = ""
    for s in suggestions:
        suggestion_text += f"  - {s['id']}: {s['title']} (viz: {s.get('viz', 'null')}, "
        suggestion_text += f"findings: {len(s.get('finding_ids', []))}, "
        suggestion_text += f"conns: {len(s.get('connection_ids', []))})\n"
        suggestion_text += f"    guidance: {s.get('guidance', '')}\n"

    # Get key finding IDs
    key_ids = c.get("key_finding_ids", [])
    key_id_set = {fid for fid in key_ids if isinstance(fid, int)}

    # Index findings and connections for section evidence packs
    findings = d.get("findings", [])
    connections = d.get("connections", [])
    findings_by_id = {f.get("id"): f for f in findings if f.get("id") is not None}
    connections_by_id = {c_item.get("id"): c_item for c_item in connections if c_item.get("id") is not None}

    # Key findings summary
    key_findings_text = ""
    for fid in key_ids:
        finding = findings_by_id.get(fid)
        if finding:
            key_findings_text += format_finding_summary(finding, key_id_set) + "\n"
    if not key_findings_text:
        key_findings_text = "  - No automated key findings available.\n"

    # Section-scoped evidence packs
    section_evidence_text = ""
    for section in suggestions:
        section_evidence_text += f"### {section['title']} [{section['id']}]\n"
        section_evidence_text += f"- Guidance: {section.get('guidance', '')}\n"
        section_evidence_text += f"- Viz: {section.get('viz', 'null')}\n"

        section_finding_ids = section.get("finding_ids", [])
        if section_finding_ids:
            section_evidence_text += "- Findings:\n"
            for fid in section_finding_ids:
                finding = findings_by_id.get(fid)
                if finding:
                    section_evidence_text += format_finding_summary(finding, key_id_set) + "\n"
        else:
            section_evidence_text += "- Findings: none selected\n"

        section_connection_ids = section.get("connection_ids", [])
        if section_connection_ids:
            section_evidence_text += "- Connections:\n"
            for conn_id in section_connection_ids:
                conn = connections_by_id.get(conn_id)
                if conn:
                    section_evidence_text += format_connection_summary(conn) + "\n"
        else:
            section_evidence_text += "- Connections: none selected\n"

        section_evidence_text += "\n"

    # Evidence quality tiers from automated curation pipeline
    evidence_quality = c.get("evidence_quality", {})
    evidence_quality_text = ""
    if evidence_quality:
        evidence_quality_text += f"- Summary: {evidence_quality.get('summary', 'n/a')}\n"
        evidence_quality_text += (
            f"- Strong IDs: {evidence_quality.get('strong_ids', [])}\n"
            f"- Moderate IDs: {evidence_quality.get('moderate_ids', [])}\n"
            f"- Weak IDs: {evidence_quality.get('weak_ids', [])}\n"
        )
    else:
        evidence_quality_text = "- No evidence quality summary available.\n"

    # Get entity roles
    entities = d.get("entities", [])
    entity_text = ""
    for ent in entities:
        role = ent.get("role", "?")
        ename = ent.get("entity_name", "?")
        jurisdiction = ent.get("jurisdiction", "?")
        entity_text += f"  - {role} at {ename} ({jurisdiction})\n"

    # Get all slugs for cross-linking
    all_slugs = get_all_slugs()
    slugs_text = ", ".join(sorted(all_slugs))

    # Get model IDs
    model_ids = get_model_ids()
    models_text = ", ".join(model_ids)

    # Key identifiers
    key_identifiers = c.get("key_identifiers", {})
    jurisdictions = key_identifiers.get("jurisdictions", [])
    officers = key_identifiers.get("officers", [])
    key_identifier_text = ""
    if jurisdictions:
        key_identifier_text += f"- Jurisdictions: {', '.join(jurisdictions)}\n"
    if officers:
        key_identifier_text += "- Officers / roles:\n"
        for officer in officers:
            role = officer.get("role", "?")
            entity = officer.get("entity", "?")
            start = officer.get("start")
            end = officer.get("end")
            date_bits = []
            if start:
                date_bits.append(f"start={start}")
            if end:
                date_bits.append(f"end={end}")
            suffix = f" ({', '.join(date_bits)})" if date_bits else ""
            key_identifier_text += f"  - {role} at {entity}{suffix}\n"
    if not key_identifier_text:
        key_identifier_text = "- No key identifiers available.\n"

    # Read agent context if available
    agent_ctx_path = AGENT_CONTEXT_DIR / f"{slug}.md"
    agent_ctx = ""
    if agent_ctx_path.exists():
        agent_ctx = agent_ctx_path.read_text()

    prompt = f"""You are curating the "{name}" dossier for the Ithildin OSINT investigation site. Generate wiki-style narrative content.

## Agent Context
{agent_ctx}

## Section Suggestions (from automated pipeline)
{suggestion_text}

## Key Finding IDs: {key_ids}

## Key Findings (automated selection)
{key_findings_text}

## Section Evidence Packs
{section_evidence_text}

## Evidence Quality
{evidence_quality_text}

## Key Identifiers
{key_identifier_text}

## Entity Roles
{entity_text}

## Available Dossier Slugs (for cross-linking)
{slugs_text}

## Available Analytical Models
{models_text}

## YOUR TASK

Before writing, read the full dossier JSON at `content/dossiers/{slug}.json`.
The summaries above are scaffolding, not the full record.
Use the full dossier's findings, connections, entities, section suggestions, and evidence quality tiers.

Editorial rules from the current curation workflow:
- Every factual claim must have an inline citation in the same sentence
- Dossiers are reference material, not narrative journalism
- Do not present inference or synthesis findings as confirmed facts; attribute them
- Prefer strong/moderate evidence over weak evidence when multiple findings support the same fact
- If a claim depends only on weak evidence, either attribute it clearly or move it to `open_questions`

Generate narrative content for this dossier. Write the following fields:

### 1. `lead` (HTML, 2-3 paragraphs using <p> tags)
Wikipedia-style lead section:
- **Standalone** — a reader who only reads the lead understands the subject
- **Encyclopedic tone** — neutral, authoritative, information-dense
- **Specific** — names, amounts, dates, jurisdictions
- **Every claim references evidence** — inline citation tokens only
- Structure: (1) Who/what and why it matters, (2) Most significant facts, (3) Current status/unresolved questions
- Adapt structure to whether this is a person, entity, or event

Citation syntax is REQUIRED:
- Good: `[Finding #2108][EFTA01296686]`
- Good: `[SEC:0000909518-01-000297]` / `[EDGAR:0000909518-01-000297]`
- Bad: `(Finding #2108, EFTA01296686)` (parenthetical citations do not reliably render)
- Bad: plain `Finding #2108` without brackets

### 2. `system_role` (plain text, 1-2 sentences)
What this entity reveals about how the network operates.

### 3. `sections` (array of objects)
Each section: {{"id": "...", "title": "...", "content": "<p>HTML prose...</p>", "viz": "ego_network"|"timeline"|null}}

Rules:
- Use section_suggestions as starting point but rename/merge/skip as needed
- Sections are topical, not categorical ("Key Relationships" not "Relationship Findings")
- Content is PROSE PARAGRAPHS, not bullet lists
- Link to other dossiers: <a href="/dossiers/SLUG">Name</a> (only if slug exists in list above)
- Evidence woven into narrative, not appended
- viz: only set where it contextually supports the section
- Don't repeat the lead — sections go deeper

### 4. `open_questions` (array of 3-5 strings)
Specific, actionable investigative questions based on evidence gaps.

### 5. `applicable_models` (array of strings)
Which analytical models apply, from: {models_text}

## WRITE THE JSON

After composing the content, write it to the dossier using this exact command:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

path = Path('content/dossiers/{slug}.json')
dossier = json.loads(path.read_text())

dossier.setdefault('curation', {{}})
dossier['curation']['lead'] = YOUR_LEAD_HTML
dossier['curation']['system_role'] = YOUR_SYSTEM_ROLE
dossier['curation']['sections'] = YOUR_SECTIONS_LIST
dossier['curation']['open_questions'] = YOUR_QUESTIONS_LIST
dossier['curation']['applicable_models'] = YOUR_MODELS_LIST

# Remove old flat fields
for old_field in ['overview', 'financial_summary']:
    dossier['curation'].pop(old_field, None)

path.write_text(json.dumps(dossier, indent=2, default=str))
print('Written successfully')
PY
```

IMPORTANT: Use triple-quoted strings for the HTML content. Escape any quotes properly for JSON.
IMPORTANT: Do NOT use `python -c "..."` to write narrative HTML containing dollar amounts (`$250,000` etc). Shell expansion will corrupt numbers.
IMPORTANT: The content is HTML rendered via set:html — use <p>, <a>, <strong>, <em> tags.
IMPORTANT: Keep sections substantive (2-4 paragraphs each) but focused. Quality over quantity.
IMPORTANT: Read the full dossier JSON before writing so you do not miss high-signal findings outside the automated summaries.
"""
    return prompt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="List dossiers needing curation")
    parser.add_argument("--prompt", type=str, help="Generate prompt for a specific slug")
    parser.add_argument("--batch", type=int, help="Print top N slugs needing curation")
    args = parser.parse_args()

    if args.list:
        dossiers = get_dossiers_needing_curation()
        print(f"{len(dossiers)} dossiers need curation:\n")
        for slug, name, count in dossiers:
            print(f"  {slug}: {name} ({count} findings)")
    elif args.prompt:
        print(generate_prompt(args.prompt))
    elif args.batch:
        dossiers = get_dossiers_needing_curation()
        for slug, name, count in dossiers[:args.batch]:
            print(slug)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
