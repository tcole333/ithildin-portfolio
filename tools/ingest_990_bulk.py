#!/usr/bin/env python3
"""
IRS 990 Bulk Database — grants, officers, financials from all e-filed 990s.

Uses the Giving Tuesday Data Lake (public S3, no auth) to download IRS 990
e-file XMLs and extract structured data into a searchable database.

Data lake: s3://gt990datalake-rawdata (us-east-1)
Index:     Indices/990xmls/index_all_years_efiledata_xmls_created_on_2025-12-09.parquet
XMLs:      EfileData/XmlFiles/{ObjectId}_public.xml

Output DB: datasets/irs990_grants.db (separate from investigation.db)

Usage:
    python tools/ingest_990_bulk.py download-index
    python tools/ingest_990_bulk.py explore-index
    python tools/ingest_990_bulk.py process --form-type 990PF [--workers 32] [--year-start 2018] [--year-end 2018]
    python tools/ingest_990_bulk.py process --form-type 990 [--workers 32]
    python tools/ingest_990_bulk.py process-full [--workers 32] [--year-start 2020] [--year-end 2024]
    python tools/ingest_990_bulk.py resume [--workers 32]
    python tools/ingest_990_bulk.py build-fts
    python tools/ingest_990_bulk.py stats
"""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.parse_990_xml import parse_filing_bytes, has_grant_data
except ImportError:
    from parse_990_xml import parse_filing_bytes, has_grant_data

DB_PATH = Path(__file__).parent.parent / "datasets" / "irs990_grants.db"
INDEX_DIR = Path(__file__).parent.parent / "datasets" / "irs990_bulk"

S3_BASE = "https://gt990datalake-rawdata.s3.us-east-1.amazonaws.com"
INDEX_KEY = "Indices/990xmls/index_all_years_efiledata_xmls_created_on_2025-12-09.parquet"
XML_PATTERN = "EfileData/XmlFiles/{object_id}_public.xml"

USER_AGENT = "OSINT-Research/1.0 (academic research)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    object_id TEXT PRIMARY KEY,
    ein TEXT NOT NULL,
    filer_name TEXT,
    return_type TEXT,
    tax_period TEXT,
    tax_year INTEGER,
    has_schedule_i INTEGER DEFAULT 0,
    has_schedule_r INTEGER DEFAULT 0,
    grant_count INTEGER DEFAULT 0,
    total_grants_amount INTEGER DEFAULT 0,
    processed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL REFERENCES filings(object_id),
    filer_ein TEXT NOT NULL,
    filer_name TEXT,
    tax_year INTEGER,
    recipient_name TEXT,
    recipient_ein TEXT,
    recipient_address TEXT,
    cash_amount INTEGER DEFAULT 0,
    non_cash_amount INTEGER DEFAULT 0,
    purpose TEXT,
    recipient_type TEXT
);

CREATE TABLE IF NOT EXISTS related_orgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL REFERENCES filings(object_id),
    filer_ein TEXT NOT NULL,
    filer_name TEXT,
    tax_year INTEGER,
    related_name TEXT,
    related_ein TEXT,
    related_address TEXT,
    relationship_type TEXT,
    primary_activities TEXT,
    legal_domicile TEXT,
    total_income INTEGER,
    end_of_year_assets INTEGER,
    direct_controlling_entity TEXT
);

CREATE TABLE IF NOT EXISTS process_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    form_type TEXT,
    year_start INTEGER,
    year_end INTEGER,
    filings_attempted INTEGER DEFAULT 0,
    filings_with_grants INTEGER DEFAULT 0,
    grants_stored INTEGER DEFAULT 0,
    related_orgs_stored INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_filings_ein ON filings(ein);
CREATE INDEX IF NOT EXISTS idx_filings_tax_year ON filings(tax_year);
CREATE INDEX IF NOT EXISTS idx_filings_return_type ON filings(return_type);
CREATE INDEX IF NOT EXISTS idx_filings_processed ON filings(processed_at);
CREATE INDEX IF NOT EXISTS idx_grants_filer_ein ON grants(filer_ein);
CREATE INDEX IF NOT EXISTS idx_grants_recipient_ein ON grants(recipient_ein);
CREATE INDEX IF NOT EXISTS idx_grants_tax_year ON grants(tax_year);
CREATE INDEX IF NOT EXISTS idx_grants_cash ON grants(cash_amount);
CREATE INDEX IF NOT EXISTS idx_grants_object_id ON grants(object_id);
CREATE INDEX IF NOT EXISTS idx_related_filer_ein ON related_orgs(filer_ein);
CREATE INDEX IF NOT EXISTS idx_related_related_ein ON related_orgs(related_ein);
CREATE INDEX IF NOT EXISTS idx_related_object_id ON related_orgs(object_id);

-- Officers/directors from Part VII (990) and officer group (990-PF)
CREATE TABLE IF NOT EXISTS officers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL REFERENCES filings(object_id),
    ein TEXT NOT NULL,
    filer_name TEXT,
    tax_year INTEGER,
    person_name TEXT NOT NULL,
    title TEXT,
    avg_hours_per_week REAL,
    comp_from_org INTEGER DEFAULT 0,
    comp_from_related INTEGER DEFAULT 0,
    other_comp INTEGER DEFAULT 0,
    total_comp INTEGER DEFAULT 0,
    is_director INTEGER DEFAULT 0,
    is_officer INTEGER DEFAULT 0,
    is_key_employee INTEGER DEFAULT 0,
    is_highest_comp INTEGER DEFAULT 0,
    is_former INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_officers_ein ON officers(ein);
CREATE INDEX IF NOT EXISTS idx_officers_name ON officers(person_name);
CREATE INDEX IF NOT EXISTS idx_officers_tax_year ON officers(tax_year);
CREATE INDEX IF NOT EXISTS idx_officers_object_id ON officers(object_id);
CREATE INDEX IF NOT EXISTS idx_officers_comp ON officers(total_comp);

-- Financial summary from Part I + Part IX
CREATE TABLE IF NOT EXISTS financials (
    object_id TEXT PRIMARY KEY REFERENCES filings(object_id),
    ein TEXT NOT NULL,
    filer_name TEXT,
    tax_year INTEGER,
    return_type TEXT,
    total_revenue INTEGER,
    total_expenses INTEGER,
    revenue_less_expenses INTEGER,
    contributions_grants INTEGER,
    program_service_revenue INTEGER,
    investment_income INTEGER,
    total_functional_expenses INTEGER,
    program_expenses INTEGER,
    management_expenses INTEGER,
    fundraising_expenses INTEGER,
    total_assets_eoy INTEGER,
    total_liabilities_eoy INTEGER,
    net_assets_eoy INTEGER,
    qualifying_distributions INTEGER,
    net_investment_income INTEGER,
    program_expense_ratio REAL,
    fundraising_ratio REAL,
    admin_expense_ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_financials_ein ON financials(ein);
CREATE INDEX IF NOT EXISTS idx_financials_year ON financials(tax_year);
CREATE INDEX IF NOT EXISTS idx_financials_program_ratio ON financials(program_expense_ratio);

-- Schedule J detailed compensation
CREATE TABLE IF NOT EXISTS compensation_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL REFERENCES filings(object_id),
    ein TEXT NOT NULL,
    tax_year INTEGER,
    person_name TEXT NOT NULL,
    title TEXT,
    base_comp INTEGER DEFAULT 0,
    bonus INTEGER DEFAULT 0,
    other_comp INTEGER DEFAULT 0,
    deferred_comp INTEGER DEFAULT 0,
    nontaxable_benefits INTEGER DEFAULT 0,
    total_comp_from_org INTEGER DEFAULT 0,
    total_comp_from_related INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_compensation_ein ON compensation_detail(ein);

-- Schedule L insider transactions
CREATE TABLE IF NOT EXISTS insider_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL REFERENCES filings(object_id),
    ein TEXT NOT NULL,
    tax_year INTEGER,
    transaction_type TEXT,
    person_name TEXT,
    relationship TEXT,
    amount INTEGER DEFAULT 0,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_insider_ein ON insider_transactions(ein);
CREATE INDEX IF NOT EXISTS idx_insider_type ON insider_transactions(transaction_type);

-- Part IV checklist red-flag indicators
CREATE TABLE IF NOT EXISTS checklist_flags (
    object_id TEXT PRIMARY KEY REFERENCES filings(object_id),
    ein TEXT NOT NULL,
    tax_year INTEGER,
    excess_benefit_transaction INTEGER DEFAULT 0,
    schedule_j_required INTEGER DEFAULT 0,
    whistleblower_policy INTEGER DEFAULT 0,
    document_retention_policy INTEGER DEFAULT 0,
    compensation_process_ceo INTEGER DEFAULT 0,
    conflict_of_interest_policy INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checklist_ein ON checklist_flags(ein);
"""

SCHEMA_MIGRATION = """
-- Add full_processed_at column if missing (for process-full tracking)
ALTER TABLE filings ADD COLUMN full_processed_at TIMESTAMP;
"""


def get_db():
    """Get grants DB connection, creating schema if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")  # 64MB cache
    db.executescript(SCHEMA)
    # Run migrations (safe to re-run)
    try:
        db.executescript(SCHEMA_MIGRATION)
    except sqlite3.OperationalError:
        pass  # Column already exists
    return db


def _tax_year_from_period(tax_period):
    """Extract year from tax_period like '202312' or '2023-12'."""
    if not tax_period:
        return None
    clean = str(tax_period).replace("-", "")
    if len(clean) >= 4:
        try:
            return int(clean[:4])
        except ValueError:
            return None
    return None


# ── download-index ──────────────────────────────────────────────

def cmd_download_index(args):
    """Download the Giving Tuesday parquet index file."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    dest = INDEX_DIR / "index_all_years.parquet"

    if dest.exists():
        size_mb = dest.stat().st_size / (1024 * 1024)
        age_hours = (time.time() - dest.stat().st_mtime) / 3600
        print(f"Index already cached: {size_mb:.0f}MB, {age_hours:.0f}h old")
        if age_hours < 168:  # 1 week
            print("Use explore-index to inspect contents.")
            return
        print("Re-downloading (>1 week old)...")

    url = f"{S3_BASE}/{INDEX_KEY}"
    print(f"Downloading index from Giving Tuesday Data Lake...")
    print(f"  URL: {url}")

    req = Request(url, headers={"User-Agent": USER_AGENT})
    start = time.time()
    with urlopen(req, timeout=300) as resp:
        data = resp.read()
    elapsed = time.time() - start

    dest.write_bytes(data)
    size_mb = len(data) / (1024 * 1024)
    print(f"  Downloaded {size_mb:.0f}MB in {elapsed:.0f}s → {dest}")


def _load_index():
    """Load parquet index, return pyarrow Table."""
    import pyarrow.parquet as pq
    path = INDEX_DIR / "index_all_years.parquet"
    if not path.exists():
        print("Error: index not downloaded. Run: download-index", file=sys.stderr)
        sys.exit(1)
    return pq.read_table(str(path))


# ── explore-index ───────────────────────────────────────────────

def cmd_explore_index(args):
    """Show index schema, form types, year ranges, and row counts."""
    table = _load_index()
    print(f"Parquet index: {table.num_rows:,} rows, {table.num_columns} columns")
    print(f"\nColumns: {table.column_names}")

    df = table.to_pandas()

    # Form types
    if "FormType" in df.columns:
        ft_col = "FormType"
    elif "RETURN_TYPE" in df.columns:
        ft_col = "RETURN_TYPE"
    else:
        ft_col = None

    if ft_col:
        print(f"\nForm types ({ft_col}):")
        counts = df[ft_col].value_counts()
        for ft, count in counts.items():
            print(f"  {ft}: {count:,}")

    # Tax year range
    for yr_col in ["TaxPeriod", "TAX_PERIOD", "DLN", "TaxYr"]:
        if yr_col in df.columns:
            sample = df[yr_col].dropna().head(5).tolist()
            print(f"\nSample {yr_col}: {sample}")

    # ObjectId sample
    for oid_col in ["ObjectId", "OBJECT_ID"]:
        if oid_col in df.columns:
            sample = df[oid_col].dropna().head(3).tolist()
            print(f"\nSample {oid_col}: {sample}")
            break

    # Date range
    for dc in ["SubmittedOn", "LastUpdated", "SUB_DATE"]:
        if dc in df.columns:
            mn = df[dc].min()
            mx = df[dc].max()
            print(f"\n{dc} range: {mn} → {mx}")

    if hasattr(args, "output") and args.output:
        info = {
            "rows": table.num_rows,
            "columns": table.column_names,
        }
        if ft_col:
            info["form_types"] = df[ft_col].value_counts().to_dict()
        write_output(info, args, summary="990 bulk index exploration")


# ── populate filings table from index ───────────────────────────

def _detect_columns(df):
    """Detect column names (GT index uses different names than IRS)."""
    cols = {}
    for candidate in ["ObjectId", "OBJECT_ID", "object_id"]:
        if candidate in df.columns:
            cols["object_id"] = candidate
            break
    for candidate in ["Ein", "EIN", "ein"]:
        if candidate in df.columns:
            cols["ein"] = candidate
            break
    for candidate in ["OrganizationName", "TaxpayerName", "TAXPAYER_NAME", "taxpayer_name"]:
        if candidate in df.columns:
            cols["name"] = candidate
            break
    for candidate in ["FormType", "RETURN_TYPE", "ReturnType", "return_type"]:
        if candidate in df.columns:
            cols["form_type"] = candidate
            break
    for candidate in ["TaxPeriod", "TAX_PERIOD", "tax_period"]:
        if candidate in df.columns:
            cols["tax_period"] = candidate
            break
    for candidate in ["TaxYear", "TAX_YEAR", "tax_year"]:
        if candidate in df.columns:
            cols["tax_year"] = candidate
            break
    return cols


def _populate_filings(db, df, form_type=None, year_start=None, year_end=None):
    """Insert filings from index DataFrame into filings table (processed_at=NULL)."""
    cols = _detect_columns(df)
    if "object_id" not in cols:
        print("Error: cannot find ObjectId column in index", file=sys.stderr)
        return 0

    # Filter by form type
    if form_type and "form_type" in cols:
        mask = df[cols["form_type"]].str.upper().str.replace("-", "").str.replace(" ", "") == form_type.upper().replace("-", "").replace(" ", "")
        df = df[mask]

    # Extract tax year and filter
    df = df.copy()
    if "tax_year" in cols:
        df["_tax_year"] = df[cols["tax_year"]].astype(str).str[:4]
        df["_tax_year"] = df["_tax_year"].apply(lambda x: int(x) if x.isdigit() else None)
    elif "tax_period" in cols:
        tax_periods = df[cols["tax_period"]].astype(str).str.replace("-", "")
        df["_tax_year"] = tax_periods.str[:4].apply(lambda x: int(x) if x.isdigit() else None)

    if "_tax_year" in df.columns:
        if year_start:
            df = df[df["_tax_year"] >= year_start]
        if year_end:
            df = df[df["_tax_year"] <= year_end]

    print(f"  {len(df):,} filings match filters")

    # Check what's already in DB
    existing = set()
    for row in db.execute("SELECT object_id FROM filings").fetchall():
        existing.add(row["object_id"])
    print(f"  {len(existing):,} already in DB")

    # Batch insert new filings
    batch = []
    skipped = 0
    for _, row in df.iterrows():
        oid = str(row[cols["object_id"]])
        if oid in existing:
            skipped += 1
            continue
        ein = str(row.get(cols.get("ein", ""), "")) if "ein" in cols else ""
        name = str(row.get(cols.get("name", ""), "")) if "name" in cols else ""
        rt = str(row.get(cols.get("form_type", ""), "")) if "form_type" in cols else ""
        tp = str(row.get(cols.get("tax_period", ""), "")) if "tax_period" in cols else ""
        ty = row.get("_tax_year") if "_tax_year" in row.index else _tax_year_from_period(tp)
        if ty is not None:
            try:
                ty = int(ty)
            except (ValueError, TypeError):
                ty = None
        batch.append((oid, ein, name, rt, tp, ty))

        if len(batch) >= 10000:
            db.executemany(
                "INSERT OR IGNORE INTO filings (object_id, ein, filer_name, return_type, tax_period, tax_year) VALUES (?,?,?,?,?,?)",
                batch,
            )
            db.commit()
            batch = []

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO filings (object_id, ein, filer_name, return_type, tax_period, tax_year) VALUES (?,?,?,?,?,?)",
            batch,
        )
        db.commit()

    new_count = len(df) - skipped
    print(f"  {new_count:,} new filings added, {skipped:,} already existed")
    return new_count


# ── download + parse workers ────────────────────────────────────

def _download_xml(object_id, retries=3):
    """Download a single XML from GT S3. Returns (object_id, bytes) or (object_id, None)."""
    url = f"{S3_BASE}/{XML_PATTERN.format(object_id=object_id)}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=15) as resp:
                return (object_id, resp.read())
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt < retries - 1:
                time.sleep(1 * (attempt + 1))
    return (object_id, None)


def _process_batch(db, batch_oids, workers=32):
    """Download and process a batch of filings. Returns (attempted, with_grants, grants_stored, related_stored, errors)."""
    attempted = 0
    with_grants = 0
    grants_stored = 0
    related_stored = 0
    errors = 0

    # Download concurrently
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_xml, oid): oid for oid in batch_oids}
        for future in as_completed(futures):
            oid, data = future.result()
            results[oid] = data

    # Process sequentially (DB writes)
    grant_rows = []
    related_rows = []
    filing_updates = []

    for oid in batch_oids:
        attempted += 1
        data = results.get(oid)

        if data is None:
            errors += 1
            filing_updates.append((0, 0, 0, 0, datetime.now(timezone.utc).isoformat(), oid))
            continue

        # Byte-scan optimization: skip XMLs without grant/related data
        if not has_grant_data(data):
            filing_updates.append((0, 0, 0, 0, datetime.now(timezone.utc).isoformat(), oid))
            continue

        try:
            parsed = parse_filing_bytes(data)
        except Exception:
            errors += 1
            filing_updates.append((0, 0, 0, 0, datetime.now(timezone.utc).isoformat(), oid))
            continue

        n_grants = len(parsed["grants"])
        n_related = len(parsed["related_orgs"])
        has_i = 1 if parsed["grants"] else 0
        has_r = 1 if parsed["related_orgs"] else 0

        filer_ein = parsed["ein"]
        filer_name = parsed["filer_name"]
        tax_year = _tax_year_from_period(parsed["tax_period"])
        total_amount = sum(g.get("cash_amount", 0) or 0 for g in parsed["grants"])

        filing_updates.append((has_i, has_r, n_grants, total_amount, datetime.now(timezone.utc).isoformat(), oid))

        if n_grants > 0:
            with_grants += 1
        grants_stored += n_grants
        related_stored += n_related

        # Update filer info if richer than index
        if filer_ein or filer_name:
            db.execute(
                "UPDATE filings SET ein = COALESCE(NULLIF(?, ''), ein), filer_name = COALESCE(NULLIF(?, ''), filer_name) WHERE object_id = ?",
                (filer_ein, filer_name, oid),
            )

        for g in parsed["grants"]:
            grant_rows.append((
                oid, filer_ein, filer_name, tax_year,
                g["recipient_name"], g["recipient_ein"], g["recipient_address"],
                g.get("cash_amount", 0), g.get("non_cash_amount", 0),
                g.get("purpose", ""), g.get("recipient_type", ""),
            ))

        for r in parsed["related_orgs"]:
            related_rows.append((
                oid, filer_ein, filer_name, tax_year,
                r["related_name"], r["related_ein"], r["related_address"],
                r["relationship_type"], r.get("primary_activities", ""),
                r.get("legal_domicile", ""),
                r.get("total_income", 0), r.get("end_of_year_assets", 0),
                r.get("direct_controlling_entity", ""),
            ))

    # Batch writes
    if grant_rows:
        db.executemany("""
            INSERT INTO grants (object_id, filer_ein, filer_name, tax_year,
                recipient_name, recipient_ein, recipient_address,
                cash_amount, non_cash_amount, purpose, recipient_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, grant_rows)

    if related_rows:
        db.executemany("""
            INSERT INTO related_orgs (object_id, filer_ein, filer_name, tax_year,
                related_name, related_ein, related_address,
                relationship_type, primary_activities, legal_domicile,
                total_income, end_of_year_assets, direct_controlling_entity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, related_rows)

    if filing_updates:
        db.executemany("""
            UPDATE filings SET has_schedule_i=?, has_schedule_r=?, grant_count=?,
                total_grants_amount=?, processed_at=?
            WHERE object_id=?
        """, filing_updates)

    db.commit()
    return attempted, with_grants, grants_stored, related_stored, errors


# ── process command ─────────────────────────────────────────────

def cmd_process(args):
    """Download and process filings, extracting grants."""
    form_type = args.form_type
    workers = args.workers
    batch_size = args.batch_size
    year_start = getattr(args, "year_start", None)
    year_end = getattr(args, "year_end", None)

    print(f"Loading parquet index...")
    table = _load_index()
    df = table.to_pandas()
    print(f"  {len(df):,} total filings in index")

    db = get_db()

    # Populate filings table from index
    print(f"\nPopulating filings table (form_type={form_type}, years={year_start}-{year_end})...")
    _populate_filings(db, df, form_type=form_type, year_start=year_start, year_end=year_end)

    # Build filter for unprocessed filings
    where = "processed_at IS NULL"
    params = []
    if form_type:
        normalized = form_type.upper().replace("-", "").replace(" ", "")
        where += " AND UPPER(REPLACE(REPLACE(return_type, '-', ''), ' ', '')) = ?"
        params.append(normalized)
    if year_start:
        where += " AND tax_year >= ?"
        params.append(year_start)
    if year_end:
        where += " AND tax_year <= ?"
        params.append(year_end)

    total_unprocessed = db.execute(f"SELECT COUNT(*) FROM filings WHERE {where}", params).fetchone()[0]
    print(f"\n{total_unprocessed:,} filings to process")

    if total_unprocessed == 0:
        print("Nothing to process.")
        db.close()
        return

    # Create run record
    run_id = db.execute(
        "INSERT INTO process_runs (form_type, year_start, year_end) VALUES (?,?,?)",
        (form_type, year_start, year_end),
    ).lastrowid
    db.commit()

    total_attempted = 0
    total_with_grants = 0
    total_grants = 0
    total_related = 0
    total_errors = 0
    start_time = time.time()

    while True:
        batch = db.execute(
            f"SELECT object_id FROM filings WHERE {where} LIMIT ?",
            params + [batch_size],
        ).fetchall()
        if not batch:
            break

        batch_oids = [r["object_id"] for r in batch]
        attempted, wg, gs, rs, errs = _process_batch(db, batch_oids, workers=workers)

        total_attempted += attempted
        total_with_grants += wg
        total_grants += gs
        total_related += rs
        total_errors += errs

        elapsed = time.time() - start_time
        rate = total_attempted / elapsed if elapsed > 0 else 0
        remaining = total_unprocessed - total_attempted
        eta_s = remaining / rate if rate > 0 else 0
        eta_m = eta_s / 60

        now = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{now}] {form_type or 'all'}: {total_attempted:,}/{total_unprocessed:,} "
            f"({100*total_attempted/total_unprocessed:.1f}%) | "
            f"{total_grants:,} grants | {rate:.0f} filings/sec | "
            f"ETA: {eta_m:.0f}m"
        )

    # Update run record
    db.execute("""
        UPDATE process_runs SET filings_attempted=?, filings_with_grants=?,
            grants_stored=?, related_orgs_stored=?, errors=?, completed_at=?
        WHERE id=?
    """, (total_attempted, total_with_grants, total_grants, total_related,
          total_errors, datetime.now(timezone.utc).isoformat(), run_id))
    db.commit()

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed/60:.1f} minutes:")
    print(f"  Filings processed: {total_attempted:,}")
    print(f"  With grants:       {total_with_grants:,}")
    print(f"  Grants stored:     {total_grants:,}")
    print(f"  Related orgs:      {total_related:,}")
    print(f"  Errors:            {total_errors:,}")
    db.close()


# ── resume command ──────────────────────────────────────────────

def cmd_resume(args):
    """Resume processing any unprocessed filings in the DB."""
    workers = args.workers
    batch_size = args.batch_size
    db = get_db()

    total_unprocessed = db.execute("SELECT COUNT(*) FROM filings WHERE processed_at IS NULL").fetchone()[0]
    print(f"{total_unprocessed:,} unprocessed filings in DB")
    if total_unprocessed == 0:
        print("Nothing to resume.")
        db.close()
        return

    run_id = db.execute(
        "INSERT INTO process_runs (form_type) VALUES ('resume')",
    ).lastrowid
    db.commit()

    total_attempted = 0
    total_with_grants = 0
    total_grants = 0
    total_related = 0
    total_errors = 0
    start_time = time.time()

    while True:
        batch = db.execute(
            "SELECT object_id FROM filings WHERE processed_at IS NULL LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not batch:
            break

        batch_oids = [r["object_id"] for r in batch]
        attempted, wg, gs, rs, errs = _process_batch(db, batch_oids, workers=workers)

        total_attempted += attempted
        total_with_grants += wg
        total_grants += gs
        total_related += rs
        total_errors += errs

        elapsed = time.time() - start_time
        rate = total_attempted / elapsed if elapsed > 0 else 0
        remaining = total_unprocessed - total_attempted
        eta_m = (remaining / rate / 60) if rate > 0 else 0

        now = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{now}] resume: {total_attempted:,}/{total_unprocessed:,} "
            f"({100*total_attempted/total_unprocessed:.1f}%) | "
            f"{total_grants:,} grants | {rate:.0f} filings/sec | "
            f"ETA: {eta_m:.0f}m"
        )

    db.execute("""
        UPDATE process_runs SET filings_attempted=?, filings_with_grants=?,
            grants_stored=?, related_orgs_stored=?, errors=?, completed_at=?
        WHERE id=?
    """, (total_attempted, total_with_grants, total_grants, total_related,
          total_errors, datetime.now(timezone.utc).isoformat(), run_id))
    db.commit()

    elapsed = time.time() - start_time
    print(f"\nResume completed in {elapsed/60:.1f} minutes:")
    print(f"  Filings: {total_attempted:,}  Grants: {total_grants:,}  Errors: {total_errors:,}")
    db.close()


# ── process-full batch ─────────────────────────────────────────

def _process_full_batch(db, batch_oids, workers=32, already_grant_processed=None):
    """Download and fully process a batch — officers, financials, grants, everything.

    Unlike _process_batch, does NOT skip via byte-scan (we need all filings).
    already_grant_processed: set of object_ids that already have grants stored — skip grant insertion for these.
    """
    if already_grant_processed is None:
        already_grant_processed = set()
    attempted = 0
    errors = 0
    officers_stored = 0
    financials_stored = 0
    grants_stored = 0
    sched_j_stored = 0
    sched_l_stored = 0

    # Download concurrently
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_xml, oid): oid for oid in batch_oids}
        for future in as_completed(futures):
            oid, data = future.result()
            results[oid] = data

    officer_rows = []
    financial_rows = []
    grant_rows = []
    related_rows = []
    comp_detail_rows = []
    insider_rows = []
    checklist_rows = []
    filing_updates = []

    for oid in batch_oids:
        attempted += 1
        data = results.get(oid)

        if data is None:
            errors += 1
            filing_updates.append((datetime.now(timezone.utc).isoformat(), oid))
            continue

        try:
            parsed = parse_filing_bytes(data)
        except Exception:
            errors += 1
            filing_updates.append((datetime.now(timezone.utc).isoformat(), oid))
            continue

        filer_ein = parsed["ein"]
        filer_name = parsed["filer_name"]
        return_type = parsed["return_type"]
        tax_year = _tax_year_from_period(parsed["tax_period"])

        filing_updates.append((datetime.now(timezone.utc).isoformat(), oid))

        # Update filer info
        if filer_ein or filer_name:
            db.execute(
                "UPDATE filings SET ein = COALESCE(NULLIF(?, ''), ein), filer_name = COALESCE(NULLIF(?, ''), filer_name) WHERE object_id = ?",
                (filer_ein, filer_name, oid),
            )

        # Officers
        for o in parsed.get("officers", []):
            officer_rows.append((
                oid, filer_ein, filer_name, tax_year,
                o["person_name"], o.get("title", ""),
                o.get("avg_hours_per_week", 0),
                o.get("comp_from_org", 0), o.get("comp_from_related", 0),
                o.get("other_comp", 0), o.get("total_comp", 0),
                o.get("is_director", 0), o.get("is_officer", 0),
                o.get("is_key_employee", 0), o.get("is_highest_comp", 0),
                o.get("is_former", 0),
            ))
        officers_stored += len(parsed.get("officers", []))

        # Financials
        fin = parsed.get("financials", {})
        if fin and any(v for k, v in fin.items() if k not in ("program_expense_ratio", "fundraising_ratio", "admin_expense_ratio") and v):
            financial_rows.append((
                oid, filer_ein, filer_name, tax_year, return_type,
                fin.get("total_revenue", 0), fin.get("total_expenses", 0),
                fin.get("revenue_less_expenses", 0),
                fin.get("contributions_grants", 0), fin.get("program_service_revenue", 0),
                fin.get("investment_income", 0),
                fin.get("total_functional_expenses", 0), fin.get("program_expenses", 0),
                fin.get("management_expenses", 0), fin.get("fundraising_expenses", 0),
                fin.get("total_assets_eoy", 0), fin.get("total_liabilities_eoy", 0),
                fin.get("net_assets_eoy", 0),
                fin.get("qualifying_distributions", 0), fin.get("net_investment_income", 0),
                fin.get("program_expense_ratio"), fin.get("fundraising_ratio"),
                fin.get("admin_expense_ratio"),
            ))
            financials_stored += 1

        # Grants — skip if already processed by prior `process` run
        n_grants = len(parsed["grants"])
        total_amount = sum(g.get("cash_amount", 0) or 0 for g in parsed["grants"])
        has_i = 1 if parsed["grants"] else 0
        has_r = 1 if parsed["related_orgs"] else 0

        skip_grants = oid in already_grant_processed
        if n_grants > 0 and not skip_grants:
            grants_stored += n_grants
            for g in parsed["grants"]:
                grant_rows.append((
                    oid, filer_ein, filer_name, tax_year,
                    g["recipient_name"], g["recipient_ein"], g["recipient_address"],
                    g.get("cash_amount", 0), g.get("non_cash_amount", 0),
                    g.get("purpose", ""), g.get("recipient_type", ""),
                ))

        if not skip_grants:
            for r in parsed["related_orgs"]:
                related_rows.append((
                    oid, filer_ein, filer_name, tax_year,
                    r["related_name"], r["related_ein"], r["related_address"],
                    r["relationship_type"], r.get("primary_activities", ""),
                    r.get("legal_domicile", ""),
                    r.get("total_income", 0), r.get("end_of_year_assets", 0),
                    r.get("direct_controlling_entity", ""),
                ))

        # Update filing grant metadata (if not already set)
        db.execute("""
            UPDATE filings SET has_schedule_i = MAX(has_schedule_i, ?),
                has_schedule_r = MAX(has_schedule_r, ?),
                grant_count = MAX(grant_count, ?),
                total_grants_amount = MAX(total_grants_amount, ?)
            WHERE object_id = ?
        """, (has_i, has_r, n_grants, total_amount, oid))

        # Schedule J
        for j in parsed.get("schedule_j", []):
            comp_detail_rows.append((
                oid, filer_ein, tax_year,
                j["person_name"], j.get("title", ""),
                j.get("base_comp", 0), j.get("bonus", 0),
                j.get("other_comp", 0), j.get("deferred_comp", 0),
                j.get("nontaxable_benefits", 0),
                j.get("total_comp_from_org", 0), j.get("total_comp_from_related", 0),
            ))
        sched_j_stored += len(parsed.get("schedule_j", []))

        # Schedule L
        for t in parsed.get("schedule_l", []):
            insider_rows.append((
                oid, filer_ein, tax_year,
                t.get("transaction_type", ""), t.get("person_name", ""),
                t.get("relationship", ""), t.get("amount", 0),
                t.get("description", ""),
            ))
        sched_l_stored += len(parsed.get("schedule_l", []))

        # Checklist flags
        flags = parsed.get("checklist_flags", {})
        if flags:
            checklist_rows.append((
                oid, filer_ein, tax_year,
                flags.get("excess_benefit_transaction", 0),
                flags.get("schedule_j_required", 0),
                flags.get("whistleblower_policy", 0),
                flags.get("document_retention_policy", 0),
                flags.get("compensation_process_ceo", 0),
                flags.get("conflict_of_interest_policy", 0),
            ))

    # Batch writes
    if officer_rows:
        db.executemany("""
            INSERT INTO officers (object_id, ein, filer_name, tax_year,
                person_name, title, avg_hours_per_week,
                comp_from_org, comp_from_related, other_comp, total_comp,
                is_director, is_officer, is_key_employee, is_highest_comp, is_former)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, officer_rows)

    if financial_rows:
        db.executemany("""
            INSERT OR REPLACE INTO financials (object_id, ein, filer_name, tax_year, return_type,
                total_revenue, total_expenses, revenue_less_expenses,
                contributions_grants, program_service_revenue, investment_income,
                total_functional_expenses, program_expenses, management_expenses, fundraising_expenses,
                total_assets_eoy, total_liabilities_eoy, net_assets_eoy,
                qualifying_distributions, net_investment_income,
                program_expense_ratio, fundraising_ratio, admin_expense_ratio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, financial_rows)

    if grant_rows:
        db.executemany("""
            INSERT INTO grants (object_id, filer_ein, filer_name, tax_year,
                recipient_name, recipient_ein, recipient_address,
                cash_amount, non_cash_amount, purpose, recipient_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, grant_rows)

    if related_rows:
        db.executemany("""
            INSERT INTO related_orgs (object_id, filer_ein, filer_name, tax_year,
                related_name, related_ein, related_address,
                relationship_type, primary_activities, legal_domicile,
                total_income, end_of_year_assets, direct_controlling_entity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, related_rows)

    if comp_detail_rows:
        db.executemany("""
            INSERT INTO compensation_detail (object_id, ein, tax_year,
                person_name, title, base_comp, bonus, other_comp,
                deferred_comp, nontaxable_benefits,
                total_comp_from_org, total_comp_from_related)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, comp_detail_rows)

    if insider_rows:
        db.executemany("""
            INSERT INTO insider_transactions (object_id, ein, tax_year,
                transaction_type, person_name, relationship, amount, description)
            VALUES (?,?,?,?,?,?,?,?)
        """, insider_rows)

    if checklist_rows:
        db.executemany("""
            INSERT OR REPLACE INTO checklist_flags (object_id, ein, tax_year,
                excess_benefit_transaction, schedule_j_required,
                whistleblower_policy, document_retention_policy,
                compensation_process_ceo, conflict_of_interest_policy)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, checklist_rows)

    if filing_updates:
        db.executemany("UPDATE filings SET full_processed_at = ? WHERE object_id = ?", filing_updates)

    db.commit()
    return {
        "attempted": attempted, "errors": errors,
        "officers": officers_stored, "financials": financials_stored,
        "grants": grants_stored, "sched_j": sched_j_stored,
        "sched_l": sched_l_stored,
    }


# ── process-full command ──────────────────────────────────────

def cmd_process_full(args):
    """Download and fully process filings — officers, financials, grants, schedules."""
    workers = args.workers
    batch_size = args.batch_size
    year_start = getattr(args, "year_start", None)
    year_end = getattr(args, "year_end", None)

    print("Loading parquet index...")
    table = _load_index()
    df = table.to_pandas()
    print(f"  {len(df):,} total filings in index")

    db = get_db()

    # Populate filings table (all form types)
    print(f"\nPopulating filings table (years={year_start}-{year_end})...")
    _populate_filings(db, df, form_type=None, year_start=year_start, year_end=year_end)

    # Filter for unprocessed (full)
    where = "full_processed_at IS NULL"
    params = []
    if year_start:
        where += " AND tax_year >= ?"
        params.append(year_start)
    if year_end:
        where += " AND tax_year <= ?"
        params.append(year_end)

    total_unprocessed = db.execute(f"SELECT COUNT(*) FROM filings WHERE {where}", params).fetchone()[0]
    print(f"\n{total_unprocessed:,} filings to full-process")

    if total_unprocessed == 0:
        print("Nothing to process.")
        db.close()
        return

    # Build set of filings already processed for grants (to avoid duplicates)
    print("Loading already-processed filing IDs (for grant dedup)...")
    already_processed = set()
    for row in db.execute("SELECT object_id FROM filings WHERE processed_at IS NOT NULL"):
        already_processed.add(row["object_id"])
    print(f"  {len(already_processed):,} filings already have grants — will skip grant insertion for these")

    run_id = db.execute(
        "INSERT INTO process_runs (form_type, year_start, year_end) VALUES (?,?,?)",
        ("full", year_start, year_end),
    ).lastrowid
    db.commit()

    totals = {"attempted": 0, "errors": 0, "officers": 0, "financials": 0,
              "grants": 0, "sched_j": 0, "sched_l": 0}
    start_time = time.time()

    while True:
        batch = db.execute(
            f"SELECT object_id FROM filings WHERE {where} LIMIT ?",
            params + [batch_size],
        ).fetchall()
        if not batch:
            break

        batch_oids = [r["object_id"] for r in batch]
        result = _process_full_batch(db, batch_oids, workers=workers,
                                      already_grant_processed=already_processed)

        for k in totals:
            totals[k] += result.get(k, 0)

        elapsed = time.time() - start_time
        rate = totals["attempted"] / elapsed if elapsed > 0 else 0
        remaining = total_unprocessed - totals["attempted"]
        eta_m = (remaining / rate / 60) if rate > 0 else 0

        now = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{now}] full: {totals['attempted']:,}/{total_unprocessed:,} "
            f"({100*totals['attempted']/total_unprocessed:.1f}%) | "
            f"{totals['officers']:,} officers | {totals['financials']:,} financials | "
            f"{rate:.0f}/sec | ETA: {eta_m:.0f}m"
        )

    db.execute("""
        UPDATE process_runs SET filings_attempted=?, filings_with_grants=?,
            grants_stored=?, related_orgs_stored=?, errors=?, completed_at=?
        WHERE id=?
    """, (totals["attempted"], 0, totals["grants"], 0,
          totals["errors"], datetime.now(timezone.utc).isoformat(), run_id))
    db.commit()

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed/60:.1f} minutes:")
    print(f"  Filings processed: {totals['attempted']:,}")
    print(f"  Officers stored:   {totals['officers']:,}")
    print(f"  Financials stored: {totals['financials']:,}")
    print(f"  Grants stored:     {totals['grants']:,}")
    print(f"  Schedule J:        {totals['sched_j']:,}")
    print(f"  Schedule L:        {totals['sched_l']:,}")
    print(f"  Errors:            {totals['errors']:,}")
    db.close()


# ── build-fts ───────────────────────────────────────────────────

def cmd_build_fts(args):
    """Build FTS5 virtual tables for full-text search on grants and related orgs."""
    db = get_db()
    print("Building FTS5 indexes...")

    # Drop existing FTS tables to rebuild
    db.execute("DROP TABLE IF EXISTS grants_fts")
    db.execute("DROP TABLE IF EXISTS related_orgs_fts")

    # Create FTS5 tables
    db.execute("""
        CREATE VIRTUAL TABLE grants_fts USING fts5(
            filer_name, recipient_name, purpose,
            content='grants', content_rowid='id'
        )
    """)
    db.execute("""
        CREATE VIRTUAL TABLE related_orgs_fts USING fts5(
            filer_name, related_name, primary_activities,
            content='related_orgs', content_rowid='id'
        )
    """)

    # Populate grants FTS
    print("  Populating grants_fts...")
    db.execute("""
        INSERT INTO grants_fts(rowid, filer_name, recipient_name, purpose)
        SELECT id, COALESCE(filer_name,''), COALESCE(recipient_name,''), COALESCE(purpose,'')
        FROM grants
    """)

    # Populate related_orgs FTS
    print("  Populating related_orgs_fts...")
    db.execute("""
        INSERT INTO related_orgs_fts(rowid, filer_name, related_name, primary_activities)
        SELECT id, COALESCE(filer_name,''), COALESCE(related_name,''), COALESCE(primary_activities,'')
        FROM related_orgs
    """)

    db.commit()

    # Create triggers for incremental updates
    db.executescript("""
        CREATE TRIGGER IF NOT EXISTS grants_ai AFTER INSERT ON grants BEGIN
            INSERT INTO grants_fts(rowid, filer_name, recipient_name, purpose)
            VALUES (new.id, COALESCE(new.filer_name,''), COALESCE(new.recipient_name,''), COALESCE(new.purpose,''));
        END;

        CREATE TRIGGER IF NOT EXISTS grants_ad AFTER DELETE ON grants BEGIN
            INSERT INTO grants_fts(grants_fts, rowid, filer_name, recipient_name, purpose)
            VALUES ('delete', old.id, COALESCE(old.filer_name,''), COALESCE(old.recipient_name,''), COALESCE(old.purpose,''));
        END;

        CREATE TRIGGER IF NOT EXISTS related_ai AFTER INSERT ON related_orgs BEGIN
            INSERT INTO related_orgs_fts(rowid, filer_name, related_name, primary_activities)
            VALUES (new.id, COALESCE(new.filer_name,''), COALESCE(new.related_name,''), COALESCE(new.primary_activities,''));
        END;

        CREATE TRIGGER IF NOT EXISTS related_ad AFTER DELETE ON related_orgs BEGIN
            INSERT INTO related_orgs_fts(related_orgs_fts, rowid, filer_name, related_name, primary_activities)
            VALUES ('delete', old.id, COALESCE(old.filer_name,''), COALESCE(old.related_name,''), COALESCE(old.primary_activities,''));
        END;
    """)

    gc = db.execute("SELECT COUNT(*) FROM grants_fts").fetchone()[0]
    rc = db.execute("SELECT COUNT(*) FROM related_orgs_fts").fetchone()[0]
    print(f"  FTS5 built: {gc:,} grants, {rc:,} related orgs indexed")
    db.close()


# ── stats ───────────────────────────────────────────────────────

def cmd_stats(args):
    """Show database statistics."""
    if not DB_PATH.exists():
        print("No database yet. Run: download-index → process")
        return

    db = get_db()

    total_filings = db.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    processed = db.execute("SELECT COUNT(*) FROM filings WHERE processed_at IS NOT NULL").fetchone()[0]
    unprocessed = total_filings - processed
    with_grants = db.execute("SELECT COUNT(*) FROM filings WHERE grant_count > 0").fetchone()[0]
    grant_count = db.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
    related_count = db.execute("SELECT COUNT(*) FROM related_orgs").fetchone()[0]
    total_cash = db.execute("SELECT COALESCE(SUM(cash_amount), 0) FROM grants").fetchone()[0]
    unique_filers = db.execute("SELECT COUNT(DISTINCT filer_ein) FROM grants WHERE filer_ein != ''").fetchone()[0]
    unique_recipients = db.execute("SELECT COUNT(DISTINCT recipient_name) FROM grants WHERE recipient_name != ''").fetchone()[0]
    unique_recipient_eins = db.execute("SELECT COUNT(DISTINCT recipient_ein) FROM grants WHERE recipient_ein != ''").fetchone()[0]

    # By form type
    form_stats = db.execute("""
        SELECT return_type, COUNT(*) as cnt,
               SUM(CASE WHEN processed_at IS NOT NULL THEN 1 ELSE 0 END) as done
        FROM filings
        GROUP BY return_type
        ORDER BY cnt DESC
    """).fetchall()

    # By year
    year_stats = db.execute("""
        SELECT tax_year, COUNT(*) as cnt, SUM(grant_count) as grants
        FROM filings
        WHERE tax_year IS NOT NULL AND processed_at IS NOT NULL
        GROUP BY tax_year
        ORDER BY tax_year
    """).fetchall()

    # Process runs
    runs = db.execute("""
        SELECT * FROM process_runs ORDER BY started_at DESC LIMIT 5
    """).fetchall()

    # FTS status
    fts_exists = False
    try:
        db.execute("SELECT COUNT(*) FROM grants_fts").fetchone()
        fts_exists = True
    except sqlite3.OperationalError:
        pass

    print(f"\nIRS 990 Bulk Grant Database ({DB_PATH.name}):")
    print(f"  Filings total:      {total_filings:,}")
    print(f"  Processed:          {processed:,}")
    print(f"  Unprocessed:        {unprocessed:,}")
    print(f"  With grants:        {with_grants:,}")
    print(f"  Grant records:      {grant_count:,}")
    print(f"  Related org records: {related_count:,}")
    print(f"  Total cash granted: ${total_cash:,.0f}")
    print(f"  Unique filer EINs:  {unique_filers:,}")
    print(f"  Unique recipients:  {unique_recipients:,} names, {unique_recipient_eins:,} EINs")
    print(f"  FTS5 indexes:       {'built' if fts_exists else 'not built (run build-fts)'}")

    if form_stats:
        print(f"\n  By form type:")
        for r in form_stats:
            print(f"    {r['return_type'] or '(blank)':10s} {r['cnt']:>10,} total, {r['done']:>10,} processed")

    if year_stats:
        print(f"\n  By year (processed filings with grants):")
        for r in year_stats:
            g = r["grants"] or 0
            print(f"    {r['tax_year']}  {r['cnt']:>8,} filings  {g:>8,} grants")

    if runs:
        print(f"\n  Recent process runs:")
        for r in runs:
            status = "completed" if r["completed_at"] else "in progress"
            print(f"    {r['started_at']} [{r['form_type'] or 'all':8s}] "
                  f"{r['filings_attempted'] or 0:,} filings, {r['grants_stored'] or 0:,} grants "
                  f"({status})")

    db_size = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"\n  Database size: {db_size:.0f}MB")

    stats = {
        "filings_total": total_filings,
        "processed": processed,
        "unprocessed": unprocessed,
        "with_grants": with_grants,
        "grants": grant_count,
        "related_orgs": related_count,
        "total_cash": total_cash,
        "unique_filers": unique_filers,
        "unique_recipients": unique_recipients,
        "fts_built": fts_exists,
    }
    if hasattr(args, "output") and args.output:
        write_output(stats, args, summary="990 bulk stats")

    db.close()


# ── main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IRS 990 Bulk Grant Database")
    sub = parser.add_subparsers(dest="command")

    p_dl = sub.add_parser("download-index", help="Download Giving Tuesday parquet index")
    add_output_args(p_dl)

    p_ex = sub.add_parser("explore-index", help="Show index schema and stats")
    add_output_args(p_ex)

    p_proc = sub.add_parser("process", help="Download + parse + store grants")
    p_proc.add_argument("--form-type", required=True, help="Form type: 990, 990PF, 990EZ")
    p_proc.add_argument("--workers", type=int, default=32, help="Download threads (default: 32)")
    p_proc.add_argument("--batch-size", type=int, default=5000, help="Filings per batch (default: 5000)")
    p_proc.add_argument("--year-start", type=int, help="Start year filter")
    p_proc.add_argument("--year-end", type=int, help="End year filter")
    add_output_args(p_proc)

    p_full = sub.add_parser("process-full", help="Full extraction: officers, financials, grants, schedules")
    p_full.add_argument("--workers", type=int, default=32, help="Download threads (default: 32)")
    p_full.add_argument("--batch-size", type=int, default=2000, help="Filings per batch (default: 2000)")
    p_full.add_argument("--year-start", type=int, help="Start year filter")
    p_full.add_argument("--year-end", type=int, help="End year filter")
    add_output_args(p_full)

    p_res = sub.add_parser("resume", help="Resume processing unprocessed filings")
    p_res.add_argument("--workers", type=int, default=32, help="Download threads")
    p_res.add_argument("--batch-size", type=int, default=5000, help="Filings per batch")
    add_output_args(p_res)

    p_fts = sub.add_parser("build-fts", help="Build FTS5 full-text search indexes")
    add_output_args(p_fts)

    p_st = sub.add_parser("stats", help="Show database statistics")
    add_output_args(p_st)

    args = parser.parse_args()

    if args.command == "download-index":
        cmd_download_index(args)
    elif args.command == "explore-index":
        cmd_explore_index(args)
    elif args.command == "process":
        cmd_process(args)
    elif args.command == "process-full":
        cmd_process_full(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "build-fts":
        cmd_build_fts(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
