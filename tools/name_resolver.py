#!/usr/bin/env python3
"""Name alias resolver for entity/person deduplication.

Loads name_aliases table into memory on first call. Used by write paths
(findings_tracker, etc.) and export pipelines to resolve raw names to
canonical forms.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"

_alias_cache: dict[str, tuple[str, str, int | None]] | None = None


def _load_aliases() -> dict[str, tuple[str, str, int | None]]:
    """Load all aliases into {alias_lower: (canonical_name, alias_type, entity_id)}."""
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache

    _alias_cache = {}
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT canonical_name, alias, alias_type, entity_id FROM name_aliases").fetchall()
        for row in rows:
            _alias_cache[row["alias"].lower()] = (row["canonical_name"], row["alias_type"], row["entity_id"])
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet
    finally:
        db.close()
    return _alias_cache


def invalidate_cache():
    """Force reload on next call (after adding new aliases)."""
    global _alias_cache
    _alias_cache = None


def resolve_canonical(name: str) -> str:
    """Return the canonical name for an alias, or the original name if no alias exists."""
    if not name:
        return name
    cache = _load_aliases()
    entry = cache.get(name.lower())
    return entry[0] if entry else name


def resolve_with_type(name: str) -> tuple[str, str | None, int | None]:
    """Return (canonical_name, alias_type, entity_id) for export pipelines."""
    if not name:
        return (name, None, None)
    cache = _load_aliases()
    entry = cache.get(name.lower())
    if entry:
        return entry
    return (name, None, None)


def get_all_aliases(canonical: str) -> list[str]:
    """Return all aliases that resolve to this canonical name (for aggregation queries)."""
    cache = _load_aliases()
    canonical_lower = canonical.lower()
    return [alias for alias, (canon, _, _) in cache.items() if canon.lower() == canonical_lower]
