#!/usr/bin/env python3
"""Investigation profile system for Ithildin.

Manages investigation profiles stored as YAML files in investigations/<name>/config.yaml.
The active profile is tracked in investigation.db's investigation_config table.

Usage:
    uv run python tools/investigation_context.py show          # Show active profile
    uv run python tools/investigation_context.py show --json   # JSON output
    uv run python tools/investigation_context.py set epstein   # Set active profile
    uv run python tools/investigation_context.py list          # List available profiles
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

PROJECT_ROOT = Path(__file__).parent.parent
INVESTIGATIONS_DIR = PROJECT_ROOT / "investigations"
DB_PATH = PROJECT_ROOT / "investigation.db"


@dataclass
class CorpusTool:
    """A document corpus tool available for this investigation."""
    tool: str          # path relative to project root, e.g. "tools/query_doj.py"
    name: str          # display name, e.g. "DOJ Vol 11"
    description: str   # brief description
    commands: list = field(default_factory=list)  # available subcommands


@dataclass
class ThreadDef:
    """An investigation thread definition."""
    id: int
    name: str
    description: str = ""
    targets: list = field(default_factory=list)    # lowercase target names for classification
    keywords: list = field(default_factory=list)   # regex patterns for classification


@dataclass
class KeyDate:
    """A key date in the investigation timeline."""
    date: str
    event: str
    category: str = "milestone"


@dataclass
class SeedPillar:
    """An institutional pillar to seed."""
    name: str
    pillar_type: str
    sub_type: str
    status: str = "active"
    founded: Optional[str] = None
    dissolved: Optional[str] = None
    jurisdiction: Optional[str] = None
    significance: str = ""


@dataclass
class InvestigationProfile:
    """Complete investigation profile loaded from YAML config."""
    name: str
    primary_subject: str
    description: str = ""

    # Key persons and addresses for priority escalation
    key_persons: list = field(default_factory=list)
    known_addresses: dict = field(default_factory=dict)  # pattern -> description

    # Investigation threads
    threads: list = field(default_factory=list)  # list of ThreadDef-like dicts

    # Document corpus tools specific to this investigation
    corpus_tools: list = field(default_factory=list)  # list of CorpusTool-like dicts

    # Timeline
    key_dates: list = field(default_factory=list)  # list of KeyDate-like dicts

    # Institutional pillars to seed
    seed_pillars: list = field(default_factory=list)  # list of SeedPillar-like dicts

    # Evidence system
    evidence_id_prefix: str = ""  # e.g. "EFTA" for Epstein DOJ docs

    # Graph display
    exclude_from_graph: str = ""  # primary subject name to optionally exclude

    # Source reliability overrides
    source_overrides: dict = field(default_factory=dict)

    # Bridge threads — thread IDs from other profiles to include in scoped queries
    bridge_threads: list = field(default_factory=list)


def _parse_yaml(path: Path) -> dict:
    """Parse YAML file, with fallback if PyYAML not installed."""
    text = path.read_text()
    if yaml is not None:
        return yaml.safe_load(text) or {}
    # Minimal fallback: try JSON (config.yaml could also be valid JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("ERROR: PyYAML not installed and file is not valid JSON.", file=sys.stderr)
        print("Install with: uv add pyyaml", file=sys.stderr)
        sys.exit(1)


def load_profile(name: str) -> InvestigationProfile:
    """Load an investigation profile from investigations/<name>/config.yaml."""
    config_path = INVESTIGATIONS_DIR / name / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Investigation profile not found: {config_path}")

    data = _parse_yaml(config_path)

    return InvestigationProfile(
        name=data.get("name", name),
        primary_subject=data.get("primary_subject", ""),
        description=data.get("description", ""),
        key_persons=data.get("key_persons", []),
        known_addresses=data.get("known_addresses", {}),
        threads=data.get("threads", []),
        corpus_tools=data.get("corpus_tools", []),
        key_dates=data.get("key_dates", []),
        seed_pillars=data.get("seed_pillars", []),
        evidence_id_prefix=data.get("evidence_id_prefix", ""),
        exclude_from_graph=data.get("exclude_from_graph", ""),
        source_overrides=data.get("source_overrides", {}),
        bridge_threads=data.get("bridge_threads", []),
    )


def _get_db():
    """Get DB connection, ensuring investigation_config table exists."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS investigation_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()
    return db


def get_active_profile_name() -> str:
    """Get the name of the active investigation profile from DB."""
    db = _get_db()
    row = db.execute(
        "SELECT value FROM investigation_config WHERE key = 'active_profile'"
    ).fetchone()
    db.close()

    if row:
        return row["value"]

    # No profile set — check if exactly one profile exists and use it
    if INVESTIGATIONS_DIR.exists():
        profiles = [d.name for d in INVESTIGATIONS_DIR.iterdir()
                    if d.is_dir() and d.name != "_template" and (d / "config.yaml").exists()]
        if len(profiles) == 1:
            return profiles[0]
    return ""


def get_active_profile_id() -> str:
    """Get the active profile ID (name). Convenience alias for get_active_profile_name()."""
    return get_active_profile_name()


def get_active_profile() -> InvestigationProfile:
    """Load the currently active investigation profile."""
    name = get_active_profile_name()
    if not name:
        # Return empty profile if none set
        return InvestigationProfile(name="", primary_subject="")
    return load_profile(name)


def set_active_profile(name: str):
    """Set the active investigation profile in DB."""
    config_path = INVESTIGATIONS_DIR / name / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Investigation profile not found: {config_path}")

    db = _get_db()
    db.execute(
        """INSERT INTO investigation_config (key, value, updated_at)
           VALUES ('active_profile', ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (name,)
    )
    db.commit()
    db.close()


def list_profiles() -> list[dict]:
    """List all available investigation profiles."""
    profiles = []
    if not INVESTIGATIONS_DIR.exists():
        return profiles

    for d in sorted(INVESTIGATIONS_DIR.iterdir()):
        if d.is_dir() and d.name != "_template" and (d / "config.yaml").exists():
            try:
                p = load_profile(d.name)
                profiles.append({
                    "name": p.name,
                    "primary_subject": p.primary_subject,
                    "description": p.description,
                    "threads": len(p.threads),
                    "key_persons": len(p.key_persons),
                    "corpus_tools": len(p.corpus_tools),
                })
            except Exception as e:
                profiles.append({"name": d.name, "error": str(e)})
    return profiles


def profile_to_dict(profile: InvestigationProfile) -> dict:
    """Convert profile to a JSON-serializable dict."""
    return asdict(profile)


# ── CLI ──────────────────────────────────────────────────────

def cmd_show(args):
    """Show the active investigation profile."""
    try:
        profile = get_active_profile()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not profile.name:
        print("No active investigation profile set.")
        print("Use: uv run python tools/investigation_context.py set <name>")
        return

    if args.json_out:
        print(json.dumps(profile_to_dict(profile), indent=2))
        return

    print(f"Active Investigation: {profile.name}")
    print(f"  Primary Subject: {profile.primary_subject}")
    if profile.description:
        print(f"  Description: {profile.description[:100]}")
    print(f"  Key Persons: {len(profile.key_persons)}")
    print(f"  Known Addresses: {len(profile.known_addresses)}")
    print(f"  Threads: {len(profile.threads)}")
    print(f"  Corpus Tools: {len(profile.corpus_tools)}")
    print(f"  Key Dates: {len(profile.key_dates)}")
    print(f"  Seed Pillars: {len(profile.seed_pillars)}")
    if profile.evidence_id_prefix:
        print(f"  Evidence ID Prefix: {profile.evidence_id_prefix}")
    if profile.exclude_from_graph:
        print(f"  Exclude from Graph: {profile.exclude_from_graph}")

    if profile.threads:
        print("\n  Threads:")
        for t in profile.threads:
            tid = t.get("id", "?")
            tname = t.get("name", "unnamed")
            print(f"    [{tid}] {tname}")

    if profile.corpus_tools:
        print("\n  Corpus Tools:")
        for ct in profile.corpus_tools:
            cname = ct.get("name", ct.get("tool", "?"))
            print(f"    - {cname}")


def cmd_set(args):
    """Set the active investigation profile."""
    try:
        set_active_profile(args.name)
        profile = load_profile(args.name)
        print(f"Active profile set to: {args.name}")
        print(f"  Primary subject: {profile.primary_subject}")
        print(f"  {len(profile.threads)} threads, {len(profile.key_persons)} key persons, {len(profile.corpus_tools)} corpus tools")
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """List available investigation profiles."""
    profiles = list_profiles()
    active_name = get_active_profile_name()

    if not profiles:
        print("No investigation profiles found.")
        print(f"Create one at: {INVESTIGATIONS_DIR}/<name>/config.yaml")
        return

    for p in profiles:
        marker = " *" if p["name"] == active_name else "  "
        if "error" in p:
            print(f"{marker}{p['name']} (ERROR: {p['error']})")
        else:
            print(f"{marker}{p['name']} — {p['primary_subject']} ({p.get('threads', 0)} threads, {p.get('key_persons', 0)} key persons)")


def main():
    parser = argparse.ArgumentParser(description="Investigation profile management")
    sub = parser.add_subparsers(dest="command")

    show_p = sub.add_parser("show", help="Show active investigation profile")
    show_p.add_argument("--json", action="store_true", dest="json_out", help="Output as JSON")

    set_p = sub.add_parser("set", help="Set active investigation profile")
    set_p.add_argument("name", help="Profile name (directory under investigations/)")

    sub.add_parser("list", help="List available investigation profiles")

    args = parser.parse_args()

    if args.command == "show":
        cmd_show(args)
    elif args.command == "set":
        cmd_set(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
