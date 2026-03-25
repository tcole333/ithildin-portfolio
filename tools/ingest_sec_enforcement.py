#!/usr/bin/env python3
"""Ingest SEC enforcement actions (litigation releases, admin proceedings, AAERs).

Scrapes SEC enforcement index pages, parses defendant/respondent names,
and stores everything in datasets/sec_enforcement.db for cross-referencing
against investigation entities and corporate registry officers.

Usage:
    python tools/ingest_sec_enforcement.py ingest                          # All sources, all pages
    python tools/ingest_sec_enforcement.py ingest --source litigation       # One source type
    python tools/ingest_sec_enforcement.py ingest --pages 3                 # First 3 pages only
    python tools/ingest_sec_enforcement.py ingest --incremental             # Stop at existing entries
    python tools/ingest_sec_enforcement.py stats                            # Summary counts
    python tools/ingest_sec_enforcement.py reparse                          # Re-run defendant parsing
"""

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "datasets" / "sec_enforcement.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS enforcement_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_number TEXT NOT NULL,
    source_type TEXT NOT NULL,
    date_published TEXT NOT NULL,
    datetime_published TEXT,
    respondent_text TEXT NOT NULL,
    release_url TEXT,
    file_number TEXT,
    see_also_text TEXT,
    see_also_url TEXT,
    body_text TEXT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(release_number, source_type)
);

CREATE TABLE IF NOT EXISTS enforcement_defendants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES enforcement_actions(id),
    name_raw TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    defendant_type TEXT,
    is_et_al INTEGER DEFAULT 0,
    UNIQUE(action_id, name_normalized)
);

CREATE TABLE IF NOT EXISTS enforcement_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    defendant_id INTEGER NOT NULL REFERENCES enforcement_defendants(id),
    match_source TEXT NOT NULL,
    match_source_id INTEGER,
    match_name TEXT NOT NULL,
    match_type TEXT NOT NULL,
    match_score REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(defendant_id, match_source, match_source_id)
);

CREATE TABLE IF NOT EXISTS enforcement_ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    pages_scraped INTEGER NOT NULL,
    actions_found INTEGER NOT NULL,
    actions_new INTEGER NOT NULL,
    defendants_parsed INTEGER NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ea_source ON enforcement_actions(source_type);
CREATE INDEX IF NOT EXISTS idx_ea_date ON enforcement_actions(date_published);
CREATE INDEX IF NOT EXISTS idx_ed_action ON enforcement_defendants(action_id);
CREATE INDEX IF NOT EXISTS idx_ed_name ON enforcement_defendants(name_normalized);
CREATE INDEX IF NOT EXISTS idx_em_defendant ON enforcement_matches(defendant_id);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS enforcement_actions_fts USING fts5(
    respondent_text, release_number, body_text,
    content=enforcement_actions, content_rowid=id,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS enforcement_defendants_fts USING fts5(
    name_raw, name_normalized,
    content=enforcement_defendants, content_rowid=id,
    tokenize='porter unicode61'
);
"""

# FTS sync triggers
FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS ea_ai AFTER INSERT ON enforcement_actions BEGIN
    INSERT INTO enforcement_actions_fts(rowid, respondent_text, release_number, body_text)
    VALUES (new.id, new.respondent_text, new.release_number, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS ea_ad AFTER DELETE ON enforcement_actions BEGIN
    INSERT INTO enforcement_actions_fts(enforcement_actions_fts, rowid, respondent_text, release_number, body_text)
    VALUES ('delete', old.id, old.respondent_text, old.release_number, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS ea_au AFTER UPDATE ON enforcement_actions BEGIN
    INSERT INTO enforcement_actions_fts(enforcement_actions_fts, rowid, respondent_text, release_number, body_text)
    VALUES ('delete', old.id, old.respondent_text, old.release_number, old.body_text);
    INSERT INTO enforcement_actions_fts(rowid, respondent_text, release_number, body_text)
    VALUES (new.id, new.respondent_text, new.release_number, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS ed_ai AFTER INSERT ON enforcement_defendants BEGIN
    INSERT INTO enforcement_defendants_fts(rowid, name_raw, name_normalized)
    VALUES (new.id, new.name_raw, new.name_normalized);
END;

CREATE TRIGGER IF NOT EXISTS ed_ad AFTER DELETE ON enforcement_defendants BEGIN
    INSERT INTO enforcement_defendants_fts(enforcement_defendants_fts, rowid, name_raw, name_normalized)
    VALUES ('delete', old.id, old.name_raw, old.name_normalized);
END;

CREATE TRIGGER IF NOT EXISTS ed_au AFTER UPDATE ON enforcement_defendants BEGIN
    INSERT INTO enforcement_defendants_fts(enforcement_defendants_fts, rowid, name_raw, name_normalized)
    VALUES ('delete', old.id, old.name_raw, old.name_normalized);
    INSERT INTO enforcement_defendants_fts(rowid, name_raw, name_normalized)
    VALUES (new.id, new.name_raw, new.name_normalized);
END;
"""


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    # FTS tables and triggers (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS sometimes)
    for stmt in FTS_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Already exists
    for stmt in FTS_TRIGGERS_SQL.strip().split("END;"):
        stmt = stmt.strip()
        if stmt:
            try:
                db.execute(stmt + "END;")
            except sqlite3.OperationalError:
                pass  # Already exists
    db.commit()
    return db


# ---------------------------------------------------------------------------
# SEC HTTP client
# ---------------------------------------------------------------------------

USER_AGENT = "OSINT-Research osint-research@proton.me"
MIN_INTERVAL = 0.11  # 10 req/sec max
_last_request = 0.0

BASE_URL = "https://www.sec.gov"

SOURCE_URLS = {
    "litigation": f"{BASE_URL}/enforcement-litigation/litigation-releases",
    "admin": f"{BASE_URL}/enforcement-litigation/administrative-proceedings",
    "aaer": f"{BASE_URL}/enforcement-litigation/accounting-auditing-enforcement-releases",
}


def _request(url):
    """Rate-limited GET returning HTML string, or None on error."""
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            _last_request = time.time()
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        if e.code == 403:
            print("ERROR: 403 Forbidden — SEC requires User-Agent", file=sys.stderr)
        elif e.code == 429:
            print("  Rate limited — backing off 30s", file=sys.stderr)
            time.sleep(30)
            return _request(url)  # Retry once
        elif e.code == 404:
            return None
        else:
            print(f"ERROR: HTTP {e.code} from {url}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach SEC: {e.reason}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

ROW_RE = re.compile(r'<tr class="pr-list-page-row">(.*?)</tr>', re.DOTALL)
DATE_RE = re.compile(r'<time datetime="([^"]+)"[^>]*>([^<]+)</time>')
RESP_RE = re.compile(r"class='release-view__respondents'>(.*?)</div>", re.DOTALL)
RESP_LINK_RE = re.compile(r"<a\s+href='([^']+)'[^>]*>([^<]+)</a>")
REL_NO_RE = re.compile(
    r"subfield_release_number.*?subfield_value\">([^<]+)", re.DOTALL
)
FILE_NO_RE = re.compile(
    r"subfield_file_number.*?subfield_value\">([^<]+)", re.DOTALL
)
SEE_ALSO_RE = re.compile(
    r'subfield_see_also.*?<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', re.DOTALL
)


def parse_page(html, source_type):
    """Parse one SEC enforcement index page. Returns list of action dicts."""
    actions = []
    for row_html in ROW_RE.findall(html):
        action = {"source_type": source_type}

        # Date
        date_m = DATE_RE.search(row_html)
        if date_m:
            action["datetime_published"] = date_m.group(1)
            # Extract ISO date from datetime
            action["date_published"] = date_m.group(1)[:10]
        else:
            continue  # Skip rows without dates

        # Respondent text and URL
        resp_m = RESP_RE.search(row_html)
        if resp_m:
            link_m = RESP_LINK_RE.search(resp_m.group(1))
            if link_m:
                href = link_m.group(1)
                if not href.startswith("http"):
                    href = BASE_URL + href
                action["release_url"] = href
                action["respondent_text"] = _clean_html(link_m.group(2))
            else:
                # Text without link
                action["respondent_text"] = _clean_html(resp_m.group(1))
        else:
            continue  # Skip rows without respondent info

        # Release number
        rel_m = REL_NO_RE.search(row_html)
        if rel_m:
            action["release_number"] = rel_m.group(1).strip()
        else:
            continue  # Skip rows without release number

        # File number (optional)
        file_m = FILE_NO_RE.search(row_html)
        if file_m:
            action["file_number"] = file_m.group(1).strip()

        # See-also (optional)
        see_m = SEE_ALSO_RE.search(row_html)
        if see_m:
            href = see_m.group(1)
            if not href.startswith("http"):
                href = BASE_URL + href
            action["see_also_url"] = href
            action["see_also_text"] = _clean_html(see_m.group(2))

        actions.append(action)
    return actions


def _clean_html(text):
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Defendant name parsing
# ---------------------------------------------------------------------------

# Entity indicators — terms that signal a corporate/organizational name
ENTITY_SUFFIX_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|Inc|Corp|Corporation|Ltd|LLP|L\.L\.P\.|L\.P\.|LP|Co\.|"
    r"Company|Group|Holdings|Ventures|Partners|Capital|Management|"
    r"Financial|Services|Fund|Trust|Foundation|Association|"
    r"Aktiengesellschaft|Pty|N\.?V\.?|B\.?V\.?|S\.?A\.?|GmbH|PLC|"
    r"Advisors|Advisers|Investments|Securities|Technologies|"
    r"International|Enterprises|Industries|Solutions|Network|"
    r"d/b/a|n/k/a)\b",
    re.IGNORECASE,
)

# Person suffixes that follow a comma (don't split on these commas)
PERSON_SUFFIX_RE = re.compile(
    r"^\s*(Jr|Sr|II|III|IV|Esq|CPA|MD|Ph\.?D|CFP|CFA)\.?\s*$", re.IGNORECASE
)

# Noise to strip from respondent text
NOISE_PATTERNS = [
    re.compile(r",?\s*et\.?\s*al\.?\b", re.IGNORECASE),
    re.compile(r",?\s*as\s+Defendants?\b", re.IGNORECASE),
    re.compile(r",?\s*as\s+Relief\s+Defendants?\b", re.IGNORECASE),
    re.compile(r"\s*\(relief\s+defendants?\)", re.IGNORECASE),
    re.compile(r"\bRelief\s+Defendants?\s+", re.IGNORECASE),
    re.compile(r",?\s*and\s+\d+\s+other\s+related\s+entit\w+", re.IGNORECASE),
    re.compile(r",?\s*as\s+Respondents?\b", re.IGNORECASE),
    re.compile(r"\s*f/k/a\s+[^,;]+", re.IGNORECASE),
]

# Lowercase particles in person names (don't count against capitalization check)
NAME_PARTICLES = {"de", "van", "von", "al", "el", "bin", "la", "di", "del", "le", "da"}


def parse_defendants(raw_text):
    """Parse defendant names from SEC enforcement respondent text.

    Returns list of dicts with: name_raw, name_normalized, defendant_type, is_et_al.
    """
    if not raw_text or not raw_text.strip():
        return []

    text = raw_text.strip()
    is_et_al = bool(re.search(r"\bet\.?\s*al\.?\b", text, re.IGNORECASE))

    # Strip noise
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    text = text.strip().rstrip(",").strip()

    if not text:
        return []

    # Step 1: Split on semicolons (unambiguous separator)
    parts = [p.strip() for p in text.split(";") if p.strip()]

    # Step 2: For each part, split on " and " with entity-awareness
    defendants = []
    for part in parts:
        sub_names = _split_on_and(part)
        defendants.extend(sub_names)

    # Step 3: For each candidate, further split on commas with heuristics
    final = []
    for name in defendants:
        split_names = _split_on_commas(name)
        final.extend(split_names)

    # Step 4: Classify and normalize each
    results = []
    seen = set()
    for name in final:
        name = name.strip().rstrip(",").strip()
        # Strip leading "and "
        if name.lower().startswith("and "):
            name = name[4:].strip()
        # Handle "dba" / "d/b/a" prefix — split into separate entity
        dba_m = re.match(r"^(.*?)\s*(?:d/?b/?a|dba)\s+(.+)$", name, re.IGNORECASE)
        if dba_m and dba_m.group(1).strip():
            # Keep the primary name, add d/b/a name as separate entry
            name = dba_m.group(1).strip()
            dba_name = dba_m.group(2).strip()
            if dba_name and len(dba_name) >= 2:
                final.append(dba_name)
        elif dba_m:
            name = dba_m.group(2).strip()
        if not name or len(name) < 2:
            continue
        # Strip parenthetical noise
        name = re.sub(r"\s*\(relief\s+defendants?\)", "", name, flags=re.IGNORECASE).strip()
        # Skip parenthetical state labels that leaked through
        if re.match(r"^\(?\w+\)?\s*$", name) and len(name) < 15:
            continue

        dtype = _classify_type(name)
        norm = _normalize(name, dtype)
        if not norm or norm in seen:
            continue
        seen.add(norm)

        results.append(
            {
                "name_raw": name,
                "name_normalized": norm,
                "defendant_type": dtype,
                "is_et_al": is_et_al,
            }
        )

    return results


def _split_on_and(text):
    """Split text on ' and ' but not when inside an entity name.

    E.g. 'Goldman, Sachs & Co. and Fabrice Tourre' -> ['Goldman, Sachs & Co.', 'Fabrice Tourre']
    But 'Landes and Compagnie Trust' stays together (entity indicators present).
    """
    # Handle ' and ' splits
    parts = re.split(r"\s+and\s+", text)
    if len(parts) <= 1:
        return [text]

    # Try to reaggregate if a split broke an entity name
    results = []
    i = 0
    while i < len(parts):
        current = parts[i].strip()
        # If next part starts with entity-ish words and current has no entity suffix,
        # they might belong together. But default is to split.
        # Heuristic: if current ends with an entity indicator (Co., Inc, etc.) or
        # next starts looking like a person name (capitalized first + last), split.
        if i + 1 < len(parts):
            next_part = parts[i + 1].strip()
            # If current looks incomplete (ends with comma or entity-like word without suffix)
            # and next part continues the entity name, re-join
            if _looks_like_entity_continuation(current, next_part):
                current = current + " and " + next_part
                i += 1
        results.append(current)
        i += 1
    return results


def _looks_like_entity_continuation(before, after):
    """Check if 'after' is a continuation of an entity name started by 'before'.

    E.g. 'Landes' + 'Compagnie Trust Prive KB' -> True (entity indicators in after)
    But 'Goldman, Sachs & Co.' + 'Fabrice Tourre' -> False (after looks like person)
    """
    # If 'after' has entity indicators, it's likely a standalone entity
    if ENTITY_SUFFIX_RE.search(after):
        return False
    # If 'before' ends with entity suffix, it's complete
    if ENTITY_SUFFIX_RE.search(before.split(",")[-1]):
        return False
    # If 'after' looks like a person name (2-4 words, first word capitalized)
    words = after.split()
    if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if len(w) > 1):
        return False
    # Default: if unsure, don't re-join (prefer splitting)
    return False


def _split_on_commas(text):
    """Split on commas, but preserve entity names with commas (e.g. 'Goldman, Sachs & Co.').

    Strategy: split on commas, then re-attach tokens that are entity suffixes,
    person suffixes, or continuations of entity names.
    """
    tokens = text.split(",")
    if len(tokens) <= 1:
        return [text]

    results = []
    i = 0
    while i < len(tokens):
        current = tokens[i].strip()
        if not current:
            i += 1
            continue

        # Look ahead: if next token is a person suffix (Jr., CPA), attach it
        while i + 1 < len(tokens):
            next_tok = tokens[i + 1].strip()
            if PERSON_SUFFIX_RE.match(next_tok):
                current = current + ", " + next_tok
                i += 1
            elif _is_entity_suffix_token(next_tok):
                # E.g. "Power Up Lending Group" + "Ltd." or "Integrity Financial AZ" + "LLC"
                current = current + ", " + next_tok
                i += 1
            elif _is_state_label(next_tok):
                # E.g. "Trade with Ayasa, LLC (Texas)"
                current = current + ", " + next_tok
                i += 1
            else:
                break

        # Strip leading "and " from names
        if current.lower().startswith("and "):
            current = current[4:].strip()

        if current:
            results.append(current)
        i += 1

    return results


def _is_entity_suffix_token(token):
    """Check if a token is purely an entity suffix (e.g. 'LLC', 'Inc.', 'Ltd.')."""
    clean = token.strip().rstrip(".").strip().lower()
    suffixes = {
        "llc", "inc", "corp", "ltd", "lp", "llp", "co", "plc", "sa", "ag",
        "gmbh", "nv", "bv", "l.l.c", "l.l.p", "l.p",
    }
    return clean in suffixes


def _is_state_label(token):
    """Check if token is a parenthetical state label like '(Texas)' or '(Wyoming)'."""
    return bool(re.match(r"^\([A-Z][a-z]+\)$", token.strip()))


def _classify_type(name):
    """Classify a defendant name as 'person', 'entity', or 'unknown'."""
    if ENTITY_SUFFIX_RE.search(name):
        return "entity"
    # Check for common entity patterns without formal suffixes
    lower = name.lower()
    for indicator in ["bank", "credit union", "exchange"]:
        if indicator in lower:
            return "entity"
    # Person heuristic: 2-5 words where non-particle words are capitalized
    words = [w for w in name.split() if w and not w.startswith("(")]
    if 2 <= len(words) <= 5:
        alpha_words = [w for w in words if w[0].isalpha()]
        if alpha_words and all(
            w[0].isupper()
            or w.lower() in NAME_PARTICLES
            or _is_camelcase_name(w)  # e.g. deMora, deLuca
            for w in alpha_words
        ):
            return "person"
    return "unknown"


def _is_camelcase_name(word):
    """Check if word is a camelCase name part (e.g. deMora, deLuca, McBride)."""
    return bool(re.match(r"^[a-z]{1,3}[A-Z]", word))


def _normalize(name, dtype):
    """Normalize a defendant name using entity_resolution functions if available."""
    try:
        from tools.entity_resolution import normalize_entity_name, normalize_person_name
    except ImportError:
        try:
            from entity_resolution import normalize_entity_name, normalize_person_name
        except ImportError:
            # Fallback: basic normalization
            return re.sub(r"\s+", " ", name.strip().lower())

    if dtype == "entity":
        return normalize_entity_name(name)
    elif dtype == "person":
        return normalize_person_name(name)
    else:
        # Try person normalization (strips more noise)
        return normalize_person_name(name)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_source(db, source_type, max_pages=None, incremental=False):
    """Scrape one SEC enforcement source type. Returns (actions_found, actions_new, defendants)."""
    base_url = SOURCE_URLS[source_type]
    page = 0
    total_found = 0
    total_new = 0
    total_defendants = 0

    while True:
        if max_pages is not None and page >= max_pages:
            break

        url = f"{base_url}?page={page}"
        html = _request(url)
        if html is None:
            break

        actions = parse_page(html, source_type)
        if not actions:
            break

        page_new = 0
        page_defendants = 0
        for action in actions:
            # Handle composite release numbers (e.g. "34-105022, AAER-4588")
            release_numbers = [
                rn.strip()
                for rn in action["release_number"].split(",")
                if rn.strip()
            ]

            for rn in release_numbers:
                # Determine source_type from release number prefix
                st = _source_type_from_release(rn, source_type)

                try:
                    db.execute(
                        """INSERT INTO enforcement_actions
                           (release_number, source_type, date_published, datetime_published,
                            respondent_text, release_url, file_number, see_also_text, see_also_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            rn,
                            st,
                            action["date_published"],
                            action.get("datetime_published"),
                            action["respondent_text"],
                            action.get("release_url"),
                            action.get("file_number"),
                            action.get("see_also_text"),
                            action.get("see_also_url"),
                        ),
                    )
                    action_id = db.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]
                    page_new += 1

                    # Parse and insert defendants
                    defs = parse_defendants(action["respondent_text"])
                    for d in defs:
                        try:
                            db.execute(
                                """INSERT INTO enforcement_defendants
                                   (action_id, name_raw, name_normalized, defendant_type, is_et_al)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (
                                    action_id,
                                    d["name_raw"],
                                    d["name_normalized"],
                                    d["defendant_type"],
                                    1 if d["is_et_al"] else 0,
                                ),
                            )
                            page_defendants += 1
                        except sqlite3.IntegrityError:
                            pass  # Duplicate defendant for this action

                except sqlite3.IntegrityError:
                    pass  # Duplicate release_number + source_type

        db.commit()
        total_found += len(actions)
        total_new += page_new
        total_defendants += page_defendants

        print(
            f"  {source_type} page {page}: {len(actions)} actions "
            f"({page_new} new, {page_defendants} defendants)"
        )

        # Incremental mode: stop if entire page already existed
        if incremental and page_new == 0:
            print(f"  {source_type}: all entries on page {page} already exist, stopping")
            break

        page += 1

    return total_found, total_new, total_defendants


def _source_type_from_release(release_number, default_type):
    """Infer source_type from release number prefix."""
    rn = release_number.strip().upper()
    if rn.startswith("LR-"):
        return "litigation"
    elif rn.startswith("AAER-"):
        return "aaer"
    elif rn.startswith("IA-") or rn.startswith("34-") or rn.startswith("33-"):
        return "admin"
    return default_type


# ---------------------------------------------------------------------------
# Reparse
# ---------------------------------------------------------------------------


def reparse_defendants(db):
    """Re-run defendant parsing on all stored respondent_text."""
    # Clear existing defendants
    db.execute("DELETE FROM enforcement_defendants")
    # Rebuild FTS
    db.execute(
        "INSERT INTO enforcement_defendants_fts(enforcement_defendants_fts) VALUES('rebuild')"
    )
    db.commit()

    rows = db.execute(
        "SELECT id, respondent_text FROM enforcement_actions ORDER BY id"
    ).fetchall()

    total = 0
    for row in rows:
        defs = parse_defendants(row["respondent_text"])
        for d in defs:
            try:
                db.execute(
                    """INSERT INTO enforcement_defendants
                       (action_id, name_raw, name_normalized, defendant_type, is_et_al)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        row["id"],
                        d["name_raw"],
                        d["name_normalized"],
                        d["defendant_type"],
                        1 if d["is_et_al"] else 0,
                    ),
                )
                total += 1
            except sqlite3.IntegrityError:
                pass

    db.commit()
    print(f"Reparsed {len(rows)} actions → {total} defendants")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def show_stats(db, args):
    """Show summary statistics."""
    results = {}

    # Action counts by source
    rows = db.execute(
        """SELECT source_type, COUNT(*) as cnt
           FROM enforcement_actions GROUP BY source_type ORDER BY source_type"""
    ).fetchall()
    results["actions_by_source"] = {r["source_type"]: r["cnt"] for r in rows}
    results["total_actions"] = sum(r["cnt"] for r in rows)

    # Defendant counts by type
    rows = db.execute(
        """SELECT defendant_type, COUNT(*) as cnt
           FROM enforcement_defendants GROUP BY defendant_type ORDER BY cnt DESC"""
    ).fetchall()
    results["defendants_by_type"] = {r["defendant_type"]: r["cnt"] for r in rows}
    results["total_defendants"] = sum(r["cnt"] for r in rows)

    # Date range
    row = db.execute(
        "SELECT MIN(date_published) as earliest, MAX(date_published) as latest FROM enforcement_actions"
    ).fetchone()
    results["date_range"] = {"earliest": row["earliest"], "latest": row["latest"]}

    # Actions by year (top 10)
    rows = db.execute(
        """SELECT SUBSTR(date_published, 1, 4) as year, COUNT(*) as cnt
           FROM enforcement_actions GROUP BY year ORDER BY year DESC LIMIT 10"""
    ).fetchall()
    results["actions_by_year"] = {r["year"]: r["cnt"] for r in rows}

    # Repeat offenders
    row = db.execute(
        """SELECT COUNT(*) as cnt FROM (
            SELECT name_normalized FROM enforcement_defendants
            GROUP BY name_normalized HAVING COUNT(DISTINCT action_id) >= 2
        )"""
    ).fetchone()
    results["repeat_offenders"] = row["cnt"]

    # Et al actions
    row = db.execute(
        "SELECT COUNT(DISTINCT action_id) as cnt FROM enforcement_defendants WHERE is_et_al = 1"
    ).fetchone()
    results["et_al_actions"] = row["cnt"]

    # Ingest log (last 5)
    rows = db.execute(
        "SELECT * FROM enforcement_ingest_log ORDER BY id DESC LIMIT 5"
    ).fetchall()
    results["recent_ingests"] = [dict(r) for r in rows]

    if write_output(results, args, summary="SEC enforcement stats"):
        return

    print(f"SEC Enforcement Database: {DB_PATH}")
    print(f"  Total actions:     {results['total_actions']:,}")
    for src, cnt in sorted(results["actions_by_source"].items()):
        print(f"    {src:12s} {cnt:,}")
    print(f"  Total defendants:  {results['total_defendants']:,}")
    for dtype, cnt in sorted(results["defendants_by_type"].items(), key=lambda x: -x[1]):
        print(f"    {dtype or 'null':12s} {cnt:,}")
    print(f"  Date range:        {results['date_range']['earliest']} to {results['date_range']['latest']}")
    print(f"  Repeat offenders:  {results['repeat_offenders']:,} (appeared in 2+ actions)")
    print(f"  Et al. actions:    {results['et_al_actions']:,}")
    print(f"\n  Actions by year (recent):")
    for year, cnt in sorted(results["actions_by_year"].items(), reverse=True):
        print(f"    {year}: {cnt:,}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_ingest(args):
    db = get_db()
    sources = [args.source] if args.source else ["litigation", "admin", "aaer"]

    for source_type in sources:
        print(f"Ingesting {source_type} releases...")
        found, new, defs = ingest_source(
            db,
            source_type,
            max_pages=args.pages,
            incremental=args.incremental,
        )
        # Log the ingest
        db.execute(
            """INSERT INTO enforcement_ingest_log
               (source_type, pages_scraped, actions_found, actions_new, defendants_parsed)
               VALUES (?, ?, ?, ?, ?)""",
            (source_type, args.pages or -1, found, new, defs),
        )
        db.commit()
        print(f"  {source_type} done: {found} found, {new} new actions, {defs} defendants\n")

    db.close()


def cmd_stats(args):
    db = get_db()
    show_stats(db, args)
    db.close()


def cmd_reparse(args):
    db = get_db()
    reparse_defendants(db)
    db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Ingest SEC enforcement actions"
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Scrape SEC enforcement pages")
    p_ingest.add_argument(
        "--source",
        choices=["litigation", "admin", "aaer"],
        help="Source type (default: all)",
    )
    p_ingest.add_argument(
        "--pages", type=int, help="Max pages per source (default: all)"
    )
    p_ingest.add_argument(
        "--incremental",
        action="store_true",
        help="Stop when hitting existing entries",
    )

    # stats
    p_stats = sub.add_parser("stats", help="Show database statistics")
    add_output_args(p_stats)

    # reparse
    sub.add_parser("reparse", help="Re-run defendant name parsing")

    args = parser.parse_args()
    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "reparse":
        cmd_reparse(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
