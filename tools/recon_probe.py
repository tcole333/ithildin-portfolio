#!/usr/bin/env python3
"""
Recon probe — fast parallel count-only queries across all data sources.

Returns a structured JSON heat map showing which sources have data and how
much, giving orchestrators the raw material to reason about agent allocation.

Usage:
    uv run python tools/recon_probe.py probe "TARGET NAME" --output recon.json
    uv run python tools/recon_probe.py probe "TARGET" --type entity --output recon.json
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import doj_db_path

PROJECT_ROOT = Path(__file__).parent.parent

# Load .env
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

TIMEOUT = 15  # seconds per probe
MAX_WORKERS = 10


def _http_get(url, headers=None, timeout=TIMEOUT):
    """Simple HTTP GET returning parsed JSON."""
    hdrs = {"User-Agent": "OSINT-Research osint-research@proton.me", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_post(url, body, headers=None, timeout=TIMEOUT):
    """Simple HTTP POST returning parsed JSON."""
    hdrs = {
        "User-Agent": "OSINT-Research osint-research@proton.me",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=hdrs, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _sqlite_count(db_path, query_sql, params=None):
    """Run a COUNT query against a local SQLite database."""
    if not db_path.exists():
        return None
    db = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = db.execute(query_sql, params or []).fetchone()
        return row[0] if row else 0
    except Exception:
        return None
    finally:
        db.close()


# --- Probe functions ---
# Each returns (source_key, count, status, error_msg)


def probe_courtlistener(target):
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        return ("courtlistener", 0, "no_auth", "COURTLISTENER_TOKEN not set")
    url = f"https://www.courtlistener.com/api/rest/v4/search/?q={quote_plus(target)}&type=r&page_size=1"
    data = _http_get(url, headers={"Authorization": f"Token {token}"})
    return ("courtlistener", data.get("count", 0), "ok", None)


def probe_edgar(target):
    url = f"https://efts.sec.gov/LATEST/search-index?q={quote_plus(target)}&from=0"
    data = _http_get(url)
    count = data.get("hits", {}).get("total", {}).get("value", 0)
    return ("edgar", count, "ok", None)


def probe_fec(target):
    api_key = os.environ.get("FEC_API_KEY", "DEMO_KEY")
    url = f"https://api.open.fec.gov/v1/schedules/schedule_a/?contributor_name={quote_plus(target)}&api_key={api_key}&per_page=1"
    data = _http_get(url)
    count = data.get("pagination", {}).get("count", 0)
    return ("fec", count, "ok", None)


def probe_990(target):
    url = f"https://projects.propublica.org/nonprofits/api/v2/search.json?q={quote_plus(target)}"
    try:
        data = _http_get(url)
        return ("990", data.get("total_results", 0), "ok", None)
    except HTTPError as e:
        if e.code == 404:
            # ProPublica returns 404 for queries with no org name matches
            return ("990", 0, "ok", None)
        raise


def probe_lobbying_client(target):
    url = f"https://lda.senate.gov/api/v1/filings/?client_name={quote_plus(target)}&page_size=1&page=1"
    data = _http_get(url, timeout=30)  # LDA is slow
    return ("lobbying_client", data.get("count", 0), "ok", None)


def probe_lobbying_registrant(target):
    url = f"https://lda.senate.gov/api/v1/filings/?registrant_name={quote_plus(target)}&page_size=1&page=1"
    data = _http_get(url, timeout=30)
    return ("lobbying_registrant", data.get("count", 0), "ok", None)


def probe_lobbying_lobbyist(target):
    url = f"https://lda.senate.gov/api/v1/filings/?lobbyist_name={quote_plus(target)}&page_size=1&page=1"
    data = _http_get(url, timeout=30)
    return ("lobbying_lobbyist", data.get("count", 0), "ok", None)


def probe_fara(target):
    db_path = PROJECT_ROOT / "investigation.db"
    if not db_path.exists():
        return ("fara", 0, "no_db", "investigation.db missing")
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        # Try FTS5 first, fall back to LIKE
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM fara_registrants r JOIN fara_registrants_fts f ON r.id = f.rowid WHERE fara_registrants_fts MATCH ?",
                [f'"{target}"'],
            ).fetchone()[0]
        except sqlite3.OperationalError:
            count = db.execute(
                "SELECT COUNT(*) FROM fara_registrants WHERE registrant_name LIKE ?",
                [f"%{target}%"],
            ).fetchone()[0]
        db.close()
        return ("fara", count, "ok", None)
    except Exception as e:
        return ("fara", 0, "error", str(e))


def probe_littlesis(target):
    url = f"https://littlesis.org/api/entities/search?q={quote_plus(target)}"
    data = _http_get(url, timeout=15)
    entities = data.get("data", [])
    return ("littlesis", len(entities), "ok", None)


def probe_aleph(target):
    api_key = os.environ.get("ALEPH_API_KEY")
    if not api_key:
        return ("aleph", 0, "skipped", "No ALEPH_API_KEY (no free tier)")
    headers = {"Authorization": f"Token {api_key}"}
    url = f"https://aleph.occrp.org/api/2/entities?q={quote_plus(target)}&limit=0"
    data = _http_get(url, headers=headers)
    return ("aleph", data.get("total", 0), "ok", None)


def probe_gdelt(target):
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={quote_plus(target)}&mode=artlist&maxrecords=250&format=json&timespan=3m"
    data = _http_get(url, timeout=15)
    articles = data.get("articles", [])
    return ("gdelt", len(articles), "ok", None)


def probe_usaspending(target):
    body = {
        "filters": {
            "recipient_search_text": [target],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "fields": ["Award ID"],
        "limit": 1,
        "page": 1,
    }
    try:
        import certifi
        import ssl
        # USASpending needs certifi SSL bundle
        ctx = ssl.create_default_context(cafile=certifi.where())
        url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
        hdrs = {
            "User-Agent": "OSINT-Research osint-research@proton.me",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = Request(url, data=json.dumps(body).encode(), headers=hdrs, method="POST")
        with urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        count = data.get("page_metadata", {}).get("total", len(data.get("results", [])))
        return ("usaspending", count, "ok", None)
    except ImportError:
        data = _http_post("https://api.usaspending.gov/api/v2/search/spending_by_award/", body)
        count = data.get("page_metadata", {}).get("total", len(data.get("results", [])))
        return ("usaspending", count, "ok", None)


def probe_registry(target):
    db_path = PROJECT_ROOT / "registry.db"
    count = _sqlite_count(
        db_path,
        "SELECT COUNT(*) FROM registry_entities WHERE entity_name LIKE ?",
        [f"%{target}%"],
    )
    if count is None:
        return ("registry", 0, "no_db", "registry.db missing")
    return ("registry", count, "ok", None)


def probe_opensanctions(target):
    db_path = PROJECT_ROOT / "datasets" / "opensanctions.db"
    if not db_path.exists():
        return ("opensanctions", 0, "no_db", "opensanctions.db missing")
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM (SELECT e.id FROM os_entities e JOIN os_entities_fts f ON e.rowid = f.rowid WHERE os_entities_fts MATCH ? LIMIT 100)",
                [f'"{target}"'],
            ).fetchone()[0]
        except sqlite3.OperationalError:
            count = db.execute(
                "SELECT COUNT(*) FROM os_entities WHERE caption LIKE ?",
                [f"%{target}%"],
            ).fetchone()[0]
        db.close()
        return ("opensanctions", count, "ok", None)
    except Exception as e:
        return ("opensanctions", 0, "error", str(e))


def probe_sam_bulk(target):
    db_path = PROJECT_ROOT / "datasets" / "sam.db"
    count = _sqlite_count(
        db_path,
        "SELECT COUNT(*) FROM sam_entities WHERE legal_business_name LIKE ? OR dba_name LIKE ?",
        [f"%{target}%", f"%{target}%"],
    )
    if count is None:
        return ("sam_bulk", 0, "no_db", "sam.db missing")
    return ("sam_bulk", count, "ok", None)


def probe_990_bulk(target):
    """Probe local IRS 990 grants database (22M+ grants, 5M+ officers)."""
    db_path = PROJECT_ROOT / "datasets" / "irs990_grants.db"
    if not db_path.exists():
        return ("990_bulk", 0, "no_db", "irs990_grants.db missing")
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        total = 0
        # Search grants FTS and related_orgs FTS (org names, grant purposes)
        for table in ["grants_fts", "related_orgs_fts"]:
            try:
                count = db.execute(
                    f"SELECT COUNT(*) FROM (SELECT rowid FROM {table} WHERE {table} MATCH ? LIMIT 500)",
                    [f'"{target}"'],
                ).fetchone()[0]
                total += count
            except sqlite3.OperationalError:
                pass
        # Also search officers table by name (catches person searches like "Jeffrey Epstein")
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM (SELECT id FROM officers WHERE person_name LIKE ? LIMIT 500)",
                [f"%{target}%"],
            ).fetchone()[0]
            total += count
        except sqlite3.OperationalError:
            pass
        db.close()
        return ("990_bulk", total, "ok", None)
    except Exception as e:
        return ("990_bulk", 0, "error", str(e))


def probe_crtsh(target):
    # Only useful for domains/orgs, not persons
    url = f"https://crt.sh/?q={quote_plus(target)}&output=json"
    data = _http_get(url, timeout=15)
    if isinstance(data, list):
        return ("crtsh", len(data), "ok", None)
    return ("crtsh", 0, "ok", None)


def probe_shodan(target):
    api_key = os.environ.get("SHODAN_API_KEY")
    if not api_key:
        return ("shodan", 0, "no_auth", "SHODAN_API_KEY not set")
    url = f"https://api.shodan.io/shodan/host/count?key={api_key}&query={quote_plus(target)}"
    data = _http_get(url)
    return ("shodan", data.get("total", 0), "ok", None)


def probe_corpus(target, tool_name, db_path):
    """Probe a local corpus FTS5 database."""
    if not db_path.exists():
        return (f"corpus_{tool_name}", 0, "no_db", f"{db_path} missing")
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        # Try common FTS5 table patterns
        for table in ["documents_fts", "files_fts", "emails_fts", "text_fts"]:
            try:
                count = db.execute(
                    f"SELECT COUNT(*) FROM (SELECT rowid FROM {table} WHERE {table} MATCH ? LIMIT 500)",
                    [f'"{target}"'],
                ).fetchone()[0]
                db.close()
                return (f"corpus_{tool_name}", count, "ok", None)
            except sqlite3.OperationalError:
                continue
        # Fallback: try documents table with LIKE
        for table in ["documents", "files", "emails"]:
            try:
                count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE text LIKE ? OR content LIKE ?",
                    [f"%{target}%", f"%{target}%"],
                ).fetchone()[0]
                db.close()
                return (f"corpus_{tool_name}", count, "ok", None)
            except sqlite3.OperationalError:
                continue
        db.close()
        return (f"corpus_{tool_name}", 0, "error", "No searchable table found")
    except Exception as e:
        return (f"corpus_{tool_name}", 0, "error", str(e))


def _discover_corpus_dbs():
    """Discover corpus databases from the active investigation profile.

    Falls back to scanning datasets/ for known SQLite files if no profile
    is active or corpus_tools don't map to known DB paths.
    """
    # Known tool → DB path mappings
    TOOL_DB_MAP = {
        "tools/query_doj.py": ("doj_vol11", [
            PROJECT_ROOT / "datasets" / "documents.db",
            doj_db_path(),
        ]),
        "tools/query_lmsband.py": ("lmsband", [
            p for p in (PROJECT_ROOT / "datasets").glob("lmsband*.db")
        ]),
        "tools/query_unified.py": ("unified", [
            p for p in (PROJECT_ROOT / "datasets").glob("unified*.db")
        ]),
    }

    dbs = {}
    try:
        from tools.investigation_context import load_profile
        profile = load_profile()
        for ct in profile.corpus_tools:
            tool_path = ct.get("tool", "")
            if tool_path in TOOL_DB_MAP:
                name, candidates = TOOL_DB_MAP[tool_path]
                for candidate in candidates:
                    if candidate.exists():
                        dbs[name] = candidate
                        break
    except Exception:
        # No active profile or import failed — try all known mappings
        for tool_path, (name, candidates) in TOOL_DB_MAP.items():
            for candidate in candidates:
                if candidate.exists():
                    dbs[name] = candidate
                    break

    return dbs


def build_probe_list(target, target_type="person"):
    """Build the list of (name, callable) probe functions for a target."""
    probes = [
        ("courtlistener", lambda: probe_courtlistener(target)),
        ("edgar", lambda: probe_edgar(target)),
        ("fec", lambda: probe_fec(target)),
        ("990", lambda: probe_990(target)),
        ("990_bulk", lambda: probe_990_bulk(target)),
        ("lobbying_client", lambda: probe_lobbying_client(target)),
        ("lobbying_registrant", lambda: probe_lobbying_registrant(target)),
        ("lobbying_lobbyist", lambda: probe_lobbying_lobbyist(target)),
        ("fara", lambda: probe_fara(target)),
        ("littlesis", lambda: probe_littlesis(target)),
        # aleph: deprecated — OCCRP removed free tier (March 2026)
        # gdelt: deprecated — 3-month window + unreliable API; use WebSearch for news
        ("usaspending", lambda: probe_usaspending(target)),
        ("registry", lambda: probe_registry(target)),
        ("opensanctions", lambda: probe_opensanctions(target)),
        ("sam_bulk", lambda: probe_sam_bulk(target)),
        ("shodan", lambda: probe_shodan(target)),
        ("crtsh", lambda: probe_crtsh(target)),
    ]

    # Add corpus probes from investigation profile
    corpus_dbs = _discover_corpus_dbs()
    for corpus_name, db_path in corpus_dbs.items():
        name = corpus_name
        probes.append(
            (f"corpus_{name}", lambda n=name, p=db_path: probe_corpus(target, n, p))
        )

    return probes


def classify_heat(count):
    """Classify a source count into heat levels."""
    if count == 0:
        return "zero"
    if count <= 5:
        return "low"
    if count <= 20:
        return "medium"
    return "high"


def run_probes(target, target_type="person"):
    """Execute all probes in parallel. Returns structured result dict."""
    probes = build_probe_list(target, target_type)
    sources = {}
    errors = []

    start = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for name, fn in probes:
            futures[pool.submit(fn)] = name

        for future in as_completed(futures):
            name = futures[future]
            try:
                source_key, count, status, error = future.result(timeout=30)
                sources[source_key] = {"count": count, "status": status}
                if error:
                    sources[source_key]["error"] = error
            except Exception as e:
                sources[name] = {"count": 0, "status": "error", "error": str(e)}
                errors.append(f"{name}: {e}")

    elapsed = round(time.time() - start, 1)

    # Build heat map
    heat_map = {"high": [], "medium": [], "low": [], "zero": []}
    for source_key, info in sources.items():
        level = classify_heat(info["count"])
        heat_map[level].append(source_key)

    # Sort each heat level by count descending
    for level in heat_map:
        heat_map[level].sort(key=lambda s: sources[s]["count"], reverse=True)

    # Aggregate lobbying into single entry for summary
    lobbying_total = sum(
        sources.get(k, {}).get("count", 0)
        for k in ["lobbying_client", "lobbying_registrant", "lobbying_lobbyist"]
    )

    result = {
        "target": target,
        "target_type": target_type,
        "probe_time_seconds": elapsed,
        "sources": sources,
        "heat_map": heat_map,
        "summary": {
            "total_sources": len(sources),
            "sources_with_data": sum(1 for s in sources.values() if s["count"] > 0),
            "total_records": sum(s["count"] for s in sources.values()),
            "lobbying_total": lobbying_total,
        },
    }

    if errors:
        result["errors"] = errors

    return result


def print_summary(result):
    """Print a human-readable summary of probe results."""
    print(f"\nRecon Probe: {result['target']} ({result['target_type']})")
    print(f"Completed in {result['probe_time_seconds']}s — {result['summary']['sources_with_data']}/{result['summary']['total_sources']} sources with data\n")

    heat = result["heat_map"]
    sources = result["sources"]

    if heat["high"]:
        print("HIGH (>20 results):")
        for s in heat["high"]:
            print(f"  {s}: {sources[s]['count']}")

    if heat["medium"]:
        print("MEDIUM (6-20 results):")
        for s in heat["medium"]:
            print(f"  {s}: {sources[s]['count']}")

    if heat["low"]:
        print("LOW (1-5 results):")
        for s in heat["low"]:
            print(f"  {s}: {sources[s]['count']}")

    if heat["zero"]:
        zero_names = [s for s in heat["zero"] if sources[s]["status"] == "ok"]
        no_auth = [s for s in heat["zero"] if sources[s]["status"] == "no_auth"]
        no_db = [s for s in heat["zero"] if sources[s]["status"] == "no_db"]
        err = [s for s in heat["zero"] if sources[s]["status"] == "error"]

        if zero_names:
            print(f"ZERO: {', '.join(zero_names)}")
        if no_auth:
            print(f"NO AUTH: {', '.join(no_auth)}")
        if no_db:
            print(f"NO DB: {', '.join(no_db)}")
        if err:
            print(f"ERROR: {', '.join(err)}")

    print(f"\nTotal records found: {result['summary']['total_records']}")


def main():
    parser = argparse.ArgumentParser(description="Recon probe — fast source heat map")
    sub = parser.add_subparsers(dest="command")

    probe_p = sub.add_parser("probe", help="Probe all sources for a target")
    probe_p.add_argument("target", help="Target name to probe")
    probe_p.add_argument("--type", dest="target_type", default="person",
                         choices=["person", "entity"], help="Target type")
    add_output_args(probe_p)

    args = parser.parse_args()

    if args.command != "probe":
        parser.print_help()
        sys.exit(1)

    result = run_probes(args.target, args.target_type)

    if not write_output(result, args, summary=f"recon probe '{args.target}'"):
        print_summary(result)


if __name__ == "__main__":
    main()
