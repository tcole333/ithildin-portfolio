#!/usr/bin/env python3
"""
IRS 990 XML e-file ingest — Schedule I (grants) and Schedule R (related orgs).

Downloads IRS e-file index CSVs to find OBJECT_IDs for tracked EINs, then fetches
individual XML filings via ProPublica's Nonprofit Explorer (signed S3 URLs).
Parses Schedule I/R into investigation.db.

Index source: https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/index_{YEAR}.csv
XML source: https://projects.propublica.org/nonprofits/download-xml?object_id={ID}
Years: 2017-2025 (indexes), 2011+ (XMLs via ProPublica)

Usage:
    python tools/ingest_990_xml.py download-index
    python tools/ingest_990_xml.py lookup 660789697
    python tools/ingest_990_xml.py lookup --tracked
    python tools/ingest_990_xml.py ingest 660789697
    python tools/ingest_990_xml.py ingest --tracked
    python tools/ingest_990_xml.py grants --filer 660789697
    python tools/ingest_990_xml.py grants --recipient "Clinton"
    python tools/ingest_990_xml.py related 660789697
    python tools/ingest_990_xml.py search "Epstein"
    python tools/ingest_990_xml.py stats
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.parse_990_xml import parse_filing, NS
except ImportError:
    from parse_990_xml import parse_filing, NS

DB_PATH = Path(__file__).parent.parent / "investigation.db"
CACHE_DIR = Path(__file__).parent.parent / "datasets" / "irs_990_xml"
XML_CACHE = CACHE_DIR / "xml"

INDEX_BASE = "https://apps.irs.gov/pub/epostcard/990/xml"
PROPUBLICA_XML = "https://projects.propublica.org/nonprofits/download-xml"
YEARS = range(2017, 2026)

TRACKED_EINS = {
    "660789697": "Gratitude America",
    "030213226": "Interscience Processors Inc (IPI)",
    "134028567": "Humpty Dumpty Institute (HDI)",
    "133947890": "Leon Black Foundation",
    "205117734": "Leon D. Black Foundation",
    "521942257": "See Forever Foundation (Weingarten)",
    "134061835": "Ricardo O'Gorman Garden (Landon Thomas)",
    "237320631": "Wexner Foundation",
    "133528667": "Edge Foundation (Brockman)",
    "133863354": "Dubin Family Foundation",
}

USER_AGENT = "OSINT-Research/1.0 (academic research)"


def get_db():
    """Get investigation.db connection (schema managed by lead_tracker)."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    # Ensure tables exist via lead_tracker's schema
    try:
        from tools.lead_tracker import _ensure_schema
    except ImportError:
        from lead_tracker import _ensure_schema
    _ensure_schema(db)
    return db


def _fetch(url, desc=""):
    """Download URL with User-Agent, return bytes."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
            if desc:
                print(f"  Downloaded {desc} ({len(data):,} bytes)")
            return data
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
    except URLError as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
        return None


# ── download-index ──────────────────────────────────────────────

def download_indexes(years=None):
    """Download index CSVs for each year, cache locally."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    years = years or YEARS
    downloaded = 0
    for year in years:
        dest = CACHE_DIR / f"index_{year}.csv"
        if dest.exists():
            age_hours = (time.time() - dest.stat().st_mtime) / 3600
            if age_hours < 24:
                print(f"  index_{year}.csv cached ({age_hours:.0f}h old)")
                continue
        url = f"{INDEX_BASE}/{year}/index_{year}.csv"
        data = _fetch(url, f"index_{year}.csv")
        if data:
            dest.write_bytes(data)
            downloaded += 1
        else:
            print(f"  index_{year}.csv not available (404)")
    print(f"\n{downloaded} index files downloaded, {len(list(CACHE_DIR.glob('index_*.csv')))} total cached")


# ── lookup ──────────────────────────────────────────────────────

def _normalize_ein(ein):
    """Strip dashes, leading zeros for matching, but keep canonical form."""
    return ein.replace("-", "").lstrip("0")


def lookup_ein(ein):
    """Scan cached index CSVs for filings matching an EIN."""
    target = _normalize_ein(ein)
    results = []
    for csv_path in sorted(CACHE_DIR.glob("index_*.csv")):
        year = csv_path.stem.split("_")[1]
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_ein = row.get("EIN", "").replace("-", "").lstrip("0")
                if row_ein == target:
                    results.append({
                        "year": year,
                        "object_id": row.get("OBJECT_ID", "").strip(),
                        "batch_id": row.get("XML_BATCH_ID", "").strip(),
                        "return_type": row.get("RETURN_TYPE", "").strip(),
                        "tax_period": row.get("TAX_PERIOD", "").strip(),
                        "sub_date": row.get("SUB_DATE", "").strip(),
                        "name": row.get("TAXPAYER_NAME", "").strip(),
                        "ein": row.get("EIN", "").strip(),
                    })
    return results


def cmd_lookup(args):
    """List filings for an EIN or all tracked EINs."""
    if args.tracked:
        eins = TRACKED_EINS
    elif args.ein:
        eins = {args.ein: args.ein}
    else:
        print("Error: provide EIN or --tracked", file=sys.stderr)
        return

    all_results = {}
    for ein, label in eins.items():
        filings = lookup_ein(ein)
        all_results[ein] = {"label": label, "filings": filings}
        print(f"\n{label} (EIN {ein}): {len(filings)} filings")
        for f in filings:
            print(f"  {f['tax_period']} {f['return_type']:6s} obj={f['object_id']} batch={f['batch_id']}")

    if hasattr(args, "output") and args.output:
        write_output(all_results, args, summary="990 XML lookup")


# ── download via ProPublica ──────────────────────────────────────

def _fetch_xml_propublica(object_id):
    """Fetch a single 990 XML filing from ProPublica's Nonprofit Explorer.

    ProPublica redirects to a signed S3 URL. Rate limit: 1 request per minute.
    Returns XML bytes or None.
    """
    url = f"{PROPUBLICA_XML}?object_id={object_id}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=60) as resp:
            # ProPublica returns 302 → S3 signed URL, urllib follows automatically
            data = resp.read()
            # Handle UTF-8 BOM (some IRS XMLs start with \xef\xbb\xbf)
            if data and (data[:5] == b"<?xml" or data[:8] == b"\xef\xbb\xbf<?xml"):
                return data
            # Rate-limited or error page
            text = data.decode("utf-8", errors="replace")[:200]
            if "429" in text or "Too Many" in text:
                return "RATE_LIMITED"
            return None
    except HTTPError as e:
        if e.code == 429:
            return "RATE_LIMITED"
        if e.code == 404:
            return None
        print(f"    HTTP {e.code} for object_id {object_id}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"    Error fetching {object_id}: {e}", file=sys.stderr)
        return None


def download_xmls(targets):
    """Download individual XML filings via ProPublica. Returns list of xml paths."""
    XML_CACHE.mkdir(parents=True, exist_ok=True)

    # Separate cached from uncached
    extracted = []
    uncached = []
    for t in targets:
        xml_path = XML_CACHE / f"{t['object_id']}_public.xml"
        if xml_path.exists():
            extracted.append(xml_path)
        else:
            uncached.append(t)

    if not uncached:
        return extracted

    print(f"  Downloading {len(uncached)} XMLs from ProPublica (1/min rate limit)...")
    for i, t in enumerate(uncached):
        obj_id = t["object_id"]
        tp = t.get("tax_period", "?")

        # Rate limit: wait 62s between requests (ProPublica enforces 1/min)
        if i > 0:
            print(f"    Waiting 62s for rate limit... ({i}/{len(uncached)})")
            time.sleep(62)

        data = _fetch_xml_propublica(obj_id)
        if data == "RATE_LIMITED":
            print(f"    Rate limited on {obj_id} (period {tp}), waiting 120s...")
            time.sleep(120)
            data = _fetch_xml_propublica(obj_id)

        if data and data != "RATE_LIMITED":
            out_path = XML_CACHE / f"{obj_id}_public.xml"
            out_path.write_bytes(data)
            extracted.append(out_path)
            print(f"    {tp} {t.get('return_type', '?'):6s} → {len(data):,} bytes")
        else:
            print(f"    {tp} {t.get('return_type', '?'):6s} → not available")

    return extracted


# ── store ───────────────────────────────────────────────────────

def store_filing(db, object_id, filing_meta, parsed):
    """Store parsed filing data into investigation.db. Returns filing_id."""
    has_i = 1 if parsed["grants"] else 0
    has_r = 1 if parsed["related_orgs"] else 0

    try:
        cur = db.execute("""
            INSERT INTO irs990_filings (object_id, ein, taxpayer_name, return_type,
                tax_period, sub_date, xml_batch_id, has_schedule_i, has_schedule_r)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            object_id,
            parsed["ein"] or filing_meta.get("ein", ""),
            parsed["filer_name"] or filing_meta.get("name", ""),
            parsed["return_type"] or filing_meta.get("return_type", ""),
            parsed["tax_period"] or filing_meta.get("tax_period", ""),
            filing_meta.get("sub_date", ""),
            filing_meta.get("batch_id", ""),
            has_i, has_r,
        ))
        filing_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # Already ingested
        row = db.execute("SELECT id FROM irs990_filings WHERE object_id = ?", (object_id,)).fetchone()
        return row["id"] if row else None

    filer_ein = parsed["ein"] or filing_meta.get("ein", "")
    filer_name = parsed["filer_name"] or filing_meta.get("name", "")
    tax_period = parsed["tax_period"] or filing_meta.get("tax_period", "")

    for g in parsed["grants"]:
        db.execute("""
            INSERT INTO irs990_grants (filing_id, filer_ein, filer_name, tax_period,
                recipient_name, recipient_ein, recipient_address, cash_amount,
                non_cash_amount, purpose, recipient_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            filing_id, filer_ein, filer_name, tax_period,
            g["recipient_name"], g["recipient_ein"], g["recipient_address"],
            g["cash_amount"], g["non_cash_amount"], g["purpose"], g["recipient_type"],
        ))

    for r in parsed["related_orgs"]:
        db.execute("""
            INSERT INTO irs990_related_orgs (filing_id, filer_ein, filer_name, tax_period,
                related_name, related_ein, related_address, relationship_type,
                primary_activities, legal_domicile, total_income, end_of_year_assets,
                direct_controlling_entity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            filing_id, filer_ein, filer_name, tax_period,
            r["related_name"], r["related_ein"], r["related_address"],
            r["relationship_type"], r["primary_activities"], r["legal_domicile"],
            r["total_income"], r["end_of_year_assets"], r["direct_controlling_entity"],
        ))

    db.commit()
    return filing_id


# ── ingest ──────────────────────────────────────────────────────

def ingest_ein(ein, label=""):
    """Full pipeline: lookup → download → parse → store for one EIN."""
    filings = lookup_ein(ein)
    if not filings:
        print(f"  No filings found for EIN {ein}")
        return 0

    print(f"\n{label or ein}: {len(filings)} filings found")

    # Filter out already-ingested filings that aren't cached (avoid wasting rate-limited downloads)
    db = get_db()
    ingested_ids = set()
    for row in db.execute("SELECT object_id FROM irs990_filings WHERE ein = ?", (ein,)).fetchall():
        ingested_ids.add(row["object_id"])
    db.close()

    need_download = []
    already_done = 0
    for f in filings:
        xml_path = XML_CACHE / f"{f['object_id']}_public.xml"
        if xml_path.exists() or f["object_id"] not in ingested_ids:
            need_download.append(f)
        else:
            already_done += 1
    if already_done:
        print(f"  Skipping {already_done} already-ingested filings (XMLs not cached)")

    # Download XMLs
    xml_paths = download_xmls(need_download)
    print(f"  {len(xml_paths)} XMLs available")

    # Parse and store
    db = get_db()
    stored = 0
    total_grants = 0
    total_related = 0
    for xml_path in xml_paths:
        object_id = xml_path.stem.replace("_public", "")
        # Find matching filing meta
        meta = next((f for f in filings if f["object_id"] == object_id), {})
        try:
            parsed = parse_filing(xml_path)
            filing_id = store_filing(db, object_id, meta, parsed)
            if filing_id:
                stored += 1
                total_grants += len(parsed["grants"])
                total_related += len(parsed["related_orgs"])
                if parsed["grants"] or parsed["related_orgs"]:
                    print(f"  {meta.get('tax_period', '?')} {meta.get('return_type', '?')}: "
                          f"{len(parsed['grants'])} grants, {len(parsed['related_orgs'])} related orgs")
        except ET.ParseError as e:
            print(f"  XML parse error for {object_id}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  Error processing {object_id}: {e}", file=sys.stderr)

    db.close()
    print(f"  Stored: {stored} filings, {total_grants} grants, {total_related} related orgs")
    return stored


def cmd_ingest(args):
    """Ingest filings for an EIN or all tracked EINs."""
    if args.tracked:
        total = 0
        for ein, label in TRACKED_EINS.items():
            total += ingest_ein(ein, label)
        print(f"\nTotal: {total} filings ingested across {len(TRACKED_EINS)} EINs")
    elif args.ein:
        label = TRACKED_EINS.get(args.ein, args.ein)
        ingest_ein(args.ein, label)
    else:
        print("Error: provide EIN or --tracked", file=sys.stderr)


# ── grants query ────────────────────────────────────────────────

def cmd_grants(args):
    """Query grants by filer or recipient."""
    db = get_db()
    results = []

    if args.filer:
        ein = _normalize_ein(args.filer)
        rows = db.execute("""
            SELECT g.*, f.object_id, f.taxpayer_name as filing_name
            FROM irs990_grants g
            JOIN irs990_filings f ON g.filing_id = f.id
            WHERE REPLACE(REPLACE(g.filer_ein, '-', ''), ' ', '') LIKE ?
            ORDER BY g.tax_period DESC
        """, (f"%{ein}%",)).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nGrants made by EIN {args.filer}: {len(results)}")
        total_cash = sum(r.get("cash_amount", 0) or 0 for r in results)
        print(f"Total cash grants: ${total_cash:,.0f}")
        for r in results:
            amt = r.get("cash_amount", 0) or 0
            print(f"  {r.get('tax_period', '?'):10s} ${amt:>12,.0f}  → {r.get('recipient_name', '?')}")
            if r.get("purpose"):
                print(f"{'':26s}{r['purpose'][:80]}")

    elif args.recipient:
        rows = db.execute("""
            SELECT g.*, f.object_id
            FROM irs990_grants g
            JOIN irs990_filings f ON g.filing_id = f.id
            WHERE g.recipient_name LIKE ? OR g.recipient_ein LIKE ?
            ORDER BY g.cash_amount DESC
        """, (f"%{args.recipient}%", f"%{args.recipient}%")).fetchall()
        results = [dict(r) for r in rows]
        print(f"\nGrants to '{args.recipient}': {len(results)}")
        for r in results:
            amt = r.get("cash_amount", 0) or 0
            print(f"  {r.get('tax_period', '?'):10s} ${amt:>12,.0f}  ← {r.get('filer_name', '?')} ({r.get('filer_ein', '?')})")

    else:
        print("Error: provide --filer EIN or --recipient NAME", file=sys.stderr)
        db.close()
        return

    if not write_output(results, args, summary=f"990 grants query"):
        pass  # already printed above

    db.close()


# ── related orgs query ──────────────────────────────────────────

def cmd_related(args):
    """Query related organizations for an EIN."""
    db = get_db()
    ein = _normalize_ein(args.ein)
    rows = db.execute("""
        SELECT r.*, f.object_id
        FROM irs990_related_orgs r
        JOIN irs990_filings f ON r.filing_id = f.id
        WHERE REPLACE(REPLACE(r.filer_ein, '-', ''), ' ', '') LIKE ?
        ORDER BY r.tax_period DESC
    """, (f"%{ein}%",)).fetchall()
    results = [dict(r) for r in rows]

    print(f"\nRelated orgs for EIN {args.ein}: {len(results)}")
    for r in results:
        income = r.get("total_income", 0) or 0
        assets = r.get("end_of_year_assets", 0) or 0
        print(f"  {r.get('tax_period', '?'):10s} [{r.get('relationship_type', '?'):20s}] "
              f"{r.get('related_name', '?')}")
        if income or assets:
            print(f"{'':34s}Income: ${income:,.0f}  Assets: ${assets:,.0f}")
        if r.get("direct_controlling_entity"):
            print(f"{'':34s}Controlled by: {r['direct_controlling_entity']}")

    if not write_output(results, args, summary=f"990 related orgs for {args.ein}"):
        pass

    db.close()


# ── search ──────────────────────────────────────────────────────

def cmd_search(args):
    """Keyword search across grants and related orgs."""
    db = get_db()
    query = args.query
    pattern = f"%{query}%"

    # Search grants
    grant_rows = db.execute("""
        SELECT 'grant' as record_type, g.*, f.object_id, f.taxpayer_name as filing_name
        FROM irs990_grants g
        JOIN irs990_filings f ON g.filing_id = f.id
        WHERE g.recipient_name LIKE ? OR g.filer_name LIKE ?
            OR g.purpose LIKE ? OR g.recipient_ein LIKE ?
            OR g.filer_ein LIKE ?
        ORDER BY g.cash_amount DESC
    """, (pattern, pattern, pattern, pattern, pattern)).fetchall()

    # Search related orgs
    rel_rows = db.execute("""
        SELECT 'related_org' as record_type, r.*, f.object_id, f.taxpayer_name as filing_name
        FROM irs990_related_orgs r
        JOIN irs990_filings f ON r.filing_id = f.id
        WHERE r.related_name LIKE ? OR r.filer_name LIKE ?
            OR r.primary_activities LIKE ? OR r.related_ein LIKE ?
            OR r.filer_ein LIKE ? OR r.direct_controlling_entity LIKE ?
        ORDER BY r.total_income DESC
    """, (pattern, pattern, pattern, pattern, pattern, pattern)).fetchall()

    grants = [dict(r) for r in grant_rows]
    related = [dict(r) for r in rel_rows]

    print(f"\nSearch '{query}': {len(grants)} grants, {len(related)} related orgs")

    if grants:
        print(f"\n  GRANTS ({len(grants)}):")
        for g in grants[:20]:
            amt = g.get("cash_amount", 0) or 0
            print(f"    {g.get('tax_period', '?'):10s} ${amt:>12,.0f}  "
                  f"{g.get('filer_name', '?')} → {g.get('recipient_name', '?')}")

    if related:
        print(f"\n  RELATED ORGS ({len(related)}):")
        for r in related[:20]:
            print(f"    {r.get('tax_period', '?'):10s} [{r.get('relationship_type', ''):20s}] "
                  f"{r.get('filer_name', '?')} ↔ {r.get('related_name', '?')}")

    results = {"grants": grants, "related_orgs": related}
    if not write_output(results, args, summary=f"990 search '{query}'"):
        pass

    db.close()


# ── stats ───────────────────────────────────────────────────────

def cmd_stats(args):
    """Show summary statistics."""
    db = get_db()

    filing_count = db.execute("SELECT COUNT(*) FROM irs990_filings").fetchone()[0]
    grant_count = db.execute("SELECT COUNT(*) FROM irs990_grants").fetchone()[0]
    related_count = db.execute("SELECT COUNT(*) FROM irs990_related_orgs").fetchone()[0]
    total_cash = db.execute("SELECT COALESCE(SUM(cash_amount), 0) FROM irs990_grants").fetchone()[0]
    unique_filers = db.execute("SELECT COUNT(DISTINCT filer_ein) FROM irs990_grants").fetchone()[0]
    unique_recipients = db.execute("SELECT COUNT(DISTINCT recipient_name) FROM irs990_grants WHERE recipient_name != ''").fetchone()[0]
    sched_i_count = db.execute("SELECT COUNT(*) FROM irs990_filings WHERE has_schedule_i = 1").fetchone()[0]
    sched_r_count = db.execute("SELECT COUNT(*) FROM irs990_filings WHERE has_schedule_r = 1").fetchone()[0]

    # Top grantmakers
    top_filers = db.execute("""
        SELECT filer_name, filer_ein, COUNT(*) as grant_count, SUM(cash_amount) as total
        FROM irs990_grants
        GROUP BY filer_ein
        ORDER BY total DESC
        LIMIT 10
    """).fetchall()

    # Top recipients
    top_recipients = db.execute("""
        SELECT recipient_name, COUNT(*) as times_received, SUM(cash_amount) as total
        FROM irs990_grants
        WHERE recipient_name != ''
        GROUP BY recipient_name
        ORDER BY total DESC
        LIMIT 10
    """).fetchall()

    stats = {
        "filings": filing_count,
        "filings_with_schedule_i": sched_i_count,
        "filings_with_schedule_r": sched_r_count,
        "grants": grant_count,
        "related_orgs": related_count,
        "total_cash_granted": total_cash,
        "unique_filers": unique_filers,
        "unique_recipients": unique_recipients,
    }

    print(f"\nIRS 990 XML Stats:")
    print(f"  Filings:     {filing_count:,}")
    print(f"    w/ Sched I: {sched_i_count:,}")
    print(f"    w/ Sched R: {sched_r_count:,}")
    print(f"  Grants:      {grant_count:,}")
    print(f"  Related orgs: {related_count:,}")
    print(f"  Total cash:  ${total_cash:,.0f}")
    print(f"  Unique filers:     {unique_filers:,}")
    print(f"  Unique recipients: {unique_recipients:,}")

    if top_filers:
        print(f"\n  Top grantmakers:")
        for r in top_filers:
            print(f"    ${r['total'] or 0:>14,.0f}  {r['filer_name']} ({r['grant_count']} grants)")

    if top_recipients:
        print(f"\n  Top recipients:")
        for r in top_recipients:
            print(f"    ${r['total'] or 0:>14,.0f}  {r['recipient_name']} ({r['times_received']}x)")

    # Cached indexes
    index_count = len(list(CACHE_DIR.glob("index_*.csv"))) if CACHE_DIR.exists() else 0
    xml_count = len(list(XML_CACHE.glob("*.xml"))) if XML_CACHE.exists() else 0
    print(f"\n  Cache: {index_count} index CSVs, {xml_count} XMLs")

    if not write_output(stats, args, summary="990 XML stats"):
        pass

    db.close()


# ── main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IRS 990 XML e-file ingest (Schedule I/R)")
    sub = parser.add_subparsers(dest="command")

    # download-index
    p_dl = sub.add_parser("download-index", help="Download index CSVs for all years")
    add_output_args(p_dl)

    # lookup
    p_lu = sub.add_parser("lookup", help="Look up filings for an EIN")
    p_lu.add_argument("ein", nargs="?", help="EIN to look up")
    p_lu.add_argument("--tracked", action="store_true", help="All tracked EINs")
    add_output_args(p_lu)

    # ingest
    p_in = sub.add_parser("ingest", help="Download + parse + store for an EIN")
    p_in.add_argument("ein", nargs="?", help="EIN to ingest")
    p_in.add_argument("--tracked", action="store_true", help="All tracked EINs")
    add_output_args(p_in)

    # grants
    p_gr = sub.add_parser("grants", help="Query grants")
    p_gr.add_argument("--filer", help="Filer EIN")
    p_gr.add_argument("--recipient", help="Recipient name or EIN")
    add_output_args(p_gr)

    # related
    p_rel = sub.add_parser("related", help="Query related organizations")
    p_rel.add_argument("ein", help="Filer EIN")
    add_output_args(p_rel)

    # search
    p_sr = sub.add_parser("search", help="Keyword search grants + related orgs")
    p_sr.add_argument("query", help="Search term")
    add_output_args(p_sr)

    # stats
    p_st = sub.add_parser("stats", help="Summary statistics")
    add_output_args(p_st)

    args = parser.parse_args()

    if args.command == "download-index":
        download_indexes()
    elif args.command == "lookup":
        cmd_lookup(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "grants":
        cmd_grants(args)
    elif args.command == "related":
        cmd_related(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
