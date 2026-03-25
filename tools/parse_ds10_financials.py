#!/usr/bin/env python3
"""
Parse DS10 (Deutsche Bank) financial documents from LMSBAND into structured tables.

Extracts:
  - Wire transfers / money movements (ds10_transactions)
  - Account balance snapshots (ds10_balances)
  - Investment position snapshots (ds10_positions)

Usage:
  python tools/parse_ds10_financials.py create-tables
  python tools/parse_ds10_financials.py parse-statements [--limit N]
  python tools/parse_ds10_financials.py parse-wires [--limit N]
  python tools/parse_ds10_financials.py parse-positions [--limit N]
  python tools/parse_ds10_financials.py parse-wm-transactions [--limit N]
  python tools/parse_ds10_financials.py parse-fedwire [--limit N]
  python tools/parse_ds10_financials.py parse-all [--limit N]
  python tools/parse_ds10_financials.py report
  python tools/parse_ds10_financials.py normalize                                  # Apply entity name normalization
  python tools/parse_ds10_financials.py query --entity "Plan D"                    # Transactions for entity
  python tools/parse_ds10_financials.py query --date-start 2018-01-01 --date-end 2019-06-30
  python tools/parse_ds10_financials.py query --amount-min 1000000                 # Large transactions
  python tools/parse_ds10_financials.py query --counterparty "Bank of America"     # By counterparty
  python tools/parse_ds10_financials.py balances --entity "Plan D"                 # Balance history
  python tools/parse_ds10_financials.py entities                                   # Entity summary
  python tools/parse_ds10_financials.py flows                                      # Entity-to-entity flows
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'datasets', 'lmsband_epstein_files.db')
PARSER_VERSION = "ds10_parser_v2"


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def create_tables(db):
    """Create the extraction target tables."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ds10_transactions (
            id INTEGER PRIMARY KEY,
            file_id INTEGER REFERENCES files(id),
            efta_id TEXT,
            tx_date TEXT,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            direction TEXT,
            sender TEXT,
            sender_account TEXT,
            receiver TEXT,
            receiver_account TEXT,
            bank TEXT,
            reference TEXT,
            raw_extract TEXT,
            confidence REAL,
            statement_id TEXT,
            statement_seq INTEGER,
            running_balance REAL,
            running_balance_raw TEXT,
            parsed_from_statement INTEGER DEFAULT 0,
            qa_status TEXT DEFAULT 'pending',
            qa_flags_json TEXT,
            extract_run_id TEXT,
            parser_version TEXT,
            UNIQUE(file_id, tx_date, amount, sender, receiver)
        );

        CREATE TABLE IF NOT EXISTS ds10_balances (
            id INTEGER PRIMARY KEY,
            file_id INTEGER REFERENCES files(id),
            efta_id TEXT,
            account_holder TEXT,
            account_number TEXT,
            account_type TEXT,
            balance_date TEXT,
            balance REAL,
            bank TEXT,
            raw_extract TEXT,
            UNIQUE(file_id, account_holder, balance_date, account_type)
        );

        CREATE TABLE IF NOT EXISTS ds10_positions (
            id INTEGER PRIMARY KEY,
            file_id INTEGER REFERENCES files(id),
            efta_id TEXT,
            entity TEXT,
            investment TEXT,
            position_date TEXT,
            value REAL,
            cost_basis REAL,
            raw_extract TEXT,
            UNIQUE(file_id, entity, investment, position_date)
        );

        CREATE TABLE IF NOT EXISTS ds10_statement_recon (
            id INTEGER PRIMARY KEY,
            statement_id TEXT UNIQUE,
            file_id INTEGER,
            efta_id TEXT,
            account_holder TEXT,
            account_number TEXT,
            statement_start_date TEXT,
            statement_end_date TEXT,
            beginning_balance REAL,
            ending_balance REAL,
            parsed_inflow_total REAL,
            parsed_outflow_total REAL,
            recomputed_ending_balance REAL,
            recon_delta REAL,
            recon_eligible INTEGER DEFAULT 0,
            eligibility_reason TEXT,
            recon_status TEXT DEFAULT 'pending',
            run_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_tx_sender ON ds10_transactions(sender);
        CREATE INDEX IF NOT EXISTS idx_tx_receiver ON ds10_transactions(receiver);
        CREATE INDEX IF NOT EXISTS idx_tx_date ON ds10_transactions(tx_date);
        CREATE INDEX IF NOT EXISTS idx_tx_amount ON ds10_transactions(amount);
        CREATE INDEX IF NOT EXISTS idx_tx_efta ON ds10_transactions(efta_id);
        CREATE INDEX IF NOT EXISTS idx_tx_statement_id ON ds10_transactions(statement_id);
        CREATE INDEX IF NOT EXISTS idx_tx_statement_seq ON ds10_transactions(statement_id, statement_seq);
        CREATE INDEX IF NOT EXISTS idx_tx_qa_status ON ds10_transactions(qa_status);
        CREATE INDEX IF NOT EXISTS idx_tx_extract_run_id ON ds10_transactions(extract_run_id);
        CREATE INDEX IF NOT EXISTS idx_bal_holder ON ds10_balances(account_holder);
        CREATE INDEX IF NOT EXISTS idx_bal_date ON ds10_balances(balance_date);
        CREATE INDEX IF NOT EXISTS idx_bal_efta ON ds10_balances(efta_id);
        CREATE INDEX IF NOT EXISTS idx_pos_entity ON ds10_positions(entity);
        CREATE INDEX IF NOT EXISTS idx_pos_investment ON ds10_positions(investment);
        CREATE INDEX IF NOT EXISTS idx_pos_date ON ds10_positions(position_date);
        CREATE INDEX IF NOT EXISTS idx_pos_efta ON ds10_positions(efta_id);
        CREATE INDEX IF NOT EXISTS idx_recon_statement_id ON ds10_statement_recon(statement_id);
        CREATE INDEX IF NOT EXISTS idx_recon_status ON ds10_statement_recon(recon_status);
        CREATE INDEX IF NOT EXISTS idx_recon_run_id ON ds10_statement_recon(run_id);
    """)

    # Column migrations for existing tables.
    tx_columns = [
        ("statement_id", "TEXT"),
        ("statement_seq", "INTEGER"),
        ("running_balance", "REAL"),
        ("running_balance_raw", "TEXT"),
        ("parsed_from_statement", "INTEGER DEFAULT 0"),
        ("qa_status", "TEXT DEFAULT 'pending'"),
        ("qa_flags_json", "TEXT"),
        ("extract_run_id", "TEXT"),
        ("parser_version", "TEXT"),
    ]
    for col, col_def in tx_columns:
        try:
            db.execute(f"ALTER TABLE ds10_transactions ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass

    db.commit()
    print("Tables created successfully.")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def extract_efta_id(filename):
    """Extract EFTA ID from filename like EFTA01344228.pdf."""
    m = re.search(r'(EFTA\d+)', filename)
    return m.group(1) if m else None


def _default_extract_run_id():
    env_id = os.getenv("DS10_EXTRACT_RUN_ID")
    if env_id:
        return env_id
    return datetime.utcnow().strftime("run_%Y%m%dT%H%M%SZ")


def _compact_name_component(name):
    if not name:
        return "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name.strip().lower()).strip("_")
    if not cleaned:
        return "unknown"
    return cleaned[:40]


def build_statement_id(file_id, efta_id, account_number, holder, start_date, end_date):
    """Build a deterministic statement key for reconciliation and row ordering."""
    key_parts = [
        str(file_id or ""),
        str(efta_id or ""),
        str(account_number or ""),
        str(start_date or ""),
        str(end_date or ""),
        str(holder or ""),
    ]
    digest = hashlib.sha1("|".join(key_parts).encode("utf-8")).hexdigest()[:10]
    base = f"{efta_id or 'NOEFTA'}_{account_number or _compact_name_component(holder)}_{start_date or 'nostart'}_{end_date or 'noend'}"
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base)
    return f"{base}_{digest}"


def parse_amount(raw):
    """Parse a dollar amount from OCR text. Returns (amount, confidence)."""
    if not raw:
        return None, 0.0
    # Remove common OCR artifacts
    raw = raw.strip()
    raw = raw.replace(' ', '')  # Remove spaces in numbers: "13,501, 622" -> "13,501,622"
    raw = raw.replace('$', '').replace('S', '').replace('(', '').replace(')', '')
    raw = raw.replace(',', '')
    # Sometimes OCR renders periods as commas or vice versa
    # Try to parse
    try:
        val = float(raw)
        confidence = 0.9
        return val, confidence
    except ValueError:
        return None, 0.0


def parse_dollar_amount(text):
    """Parse dollar amount from text like '$2,293,269.00' or '2293269.00' or 'S2,293,269.00'.
    Returns (amount_float, confidence) or (None, 0.0).

    Also handles OCR artifacts:
      - Periods used as thousands separators: "157.500.03" -> 157,500.03
      - Spaces in numbers: "157,500 03" -> 157,500.03
      - Missing decimal: "157500" -> 157,500.00
    """
    if not text:
        return None, 0.0
    # Clean up
    cleaned = text.strip()
    # Remove dollar sign (or S which OCR sometimes produces)
    cleaned = re.sub(r'^[\$S]', '', cleaned)
    # Remove parentheses (negative indicators)
    is_negative = '(' in cleaned and ')' in cleaned
    cleaned = cleaned.replace('(', '').replace(')', '')
    # Remove spaces within number
    cleaned = cleaned.replace(' ', '')
    # Remove trailing non-numeric chars
    cleaned = re.sub(r'[^0-9,.\-]+$', '', cleaned)
    cleaned = re.sub(r'^[^0-9.\-]+', '', cleaned)

    if not cleaned:
        return None, 0.0

    confidence = 0.9

    # Detect if periods are used as thousands separators (European style)
    # Heuristic: if there are multiple periods, they're likely thousands separators
    # except the last one which is decimal
    period_count = cleaned.count('.')
    comma_count = cleaned.count(',')

    if period_count > 1 and comma_count == 0:
        # Multiple periods: "2.706.301.73" -> treat last period as decimal
        parts = cleaned.rsplit('.', 1)
        if len(parts) == 2 and len(parts[1]) == 2:
            # Last part is 2 digits = decimal
            integer_part = parts[0].replace('.', '')
            cleaned = integer_part + '.' + parts[1]
            confidence = 0.85  # Slightly lower confidence for format conversion
        elif len(parts) == 2 and len(parts[1]) >= 4:
            # OCR glitch: "2.000.00000" = "2,000,000.00" where decimal period was eaten
            # Heuristic: if last segment is 4+ chars and ends with "00", insert decimal
            last = parts[1]
            if last.endswith('00') and len(last) >= 4:
                # Reconstruct: integer part + last[:-2] + "." + last[-2:]
                integer_part = parts[0].replace('.', '')
                cleaned = integer_part + last[:-2] + '.' + last[-2:]
                confidence = 0.7
            else:
                # All periods are thousands separators, no decimal
                cleaned = cleaned.replace('.', '')
                confidence = 0.8
        else:
            # All periods are thousands separators, no decimal
            cleaned = cleaned.replace('.', '')
            confidence = 0.8
    elif period_count == 1 and comma_count > 0:
        # Standard US format: "2,706,301.73"
        cleaned = cleaned.replace(',', '')
    elif period_count == 0 and comma_count > 0:
        # Commas only: "2,706,301" or "2,706,30173" (OCR mangled)
        cleaned = cleaned.replace(',', '')
    elif period_count == 1 and comma_count == 0:
        # Single period - check if it's a decimal point
        parts = cleaned.split('.')
        if len(parts[1]) == 2:
            pass  # Normal decimal
        elif len(parts[1]) == 3:
            # Could be thousands separator: "1.200" -> 1200
            # But also could be "1.200" = $1.200 (unusual)
            # Default: treat as thousands if integer part is small
            if int(parts[0]) < 1000:
                cleaned = cleaned.replace('.', '')
                confidence = 0.7
        else:
            pass  # Keep as-is

    try:
        val = float(cleaned)
        if is_negative:
            val = -val
        if val == 0:
            confidence = 0.5
        return val, confidence
    except ValueError:
        return None, 0.0


def normalize_date(raw, context_year=None):
    """Normalize date string to ISO format YYYY-MM-DD.

    Handles:
      - MM-DD with context_year
      - MM/DD/YYYY
      - DD-Mon-YYYY (01-Feb-2017)
      - Mon DD, YYYY
      - YYYY-MM-DD
      - MM.DD.YYYY or MM:DD:YYYY
    """
    if not raw:
        return None
    raw = raw.strip()

    # DD-Mon-YYYY: "01-Feb-2017"
    m = re.match(r'(\d{1,2})-(\w{3})-(\d{4})', raw)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%d-%b-%Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # MM/DD/YYYY or MM-DD-YYYY
    m = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # MM.DD.YYYY or MM:DD:YYYY or MM;DD;YYYY (OCR artifacts)
    m = re.match(r'(\d{1,2})[.:;](\d{1,2})[.:;](\d{4})', raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # YYYY-MM-DD already ISO
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', raw)
    if m:
        return raw

    # MM-DD with context_year (from bank statements: "04-04")
    m = re.match(r'(\d{2})-(\d{2})$', raw)
    if m and context_year:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{context_year:04d}-{month:02d}-{day:02d}"

    # Handle OCR "I0-11" (I=1) by replacing I/l with 1 in date-like patterns
    fixed = raw.replace('I', '1').replace('l', '1').replace('O', '0')
    if fixed != raw:
        m = re.match(r'(\d{2})-(\d{2})$', fixed)
        if m and context_year:
            month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{context_year:04d}-{month:02d}-{day:02d}"

    return None


def extract_statement_date_range(text):
    """Extract statement date range from text like 'October 1.2016 to October 31. 2016'.
    Returns (start_date_str, end_date_str, year) or (None, None, None).

    OCR commonly renders "1" as "I" or "l", so we handle:
      "September I. 2017 to September 30.2017"
      "October 1.2016 to October 31. 2016"
      "December I. 201710 December 31, 2017"  (merged "to")
    """
    months = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12,
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
        'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9,
        'oct': 10, 'nov': 11, 'dec': 12,
    }

    # Pre-process: fix OCR "I" -> "1" in date context, and "10" merged with "to"
    # "September I. 2017" -> "September 1. 2017"
    # "December I. 201710 December" -> "December 1. 2017 to December"
    cleaned = text[:2000]  # Only first part
    # Fix OCR 'I' or 'l' used as digit 1 after month names
    month_names = '|'.join(months.keys())
    cleaned = re.sub(
        r'(' + month_names + r')\s+([Il])[.,\s]',
        lambda m: m.group(1) + ' 1.',
        cleaned, flags=re.IGNORECASE
    )
    # Fix "201710" -> "2017 to" (merged year with "to")
    cleaned = re.sub(r'(\d{4})10\s+', r'\1 to ', cleaned)
    # Fix "2017to" -> "2017 to"
    cleaned = re.sub(r'(\d{4})to\s+', r'\1 to ', cleaned, flags=re.IGNORECASE)
    # Fix "201-1" -> "2014" (OCR hyphen in year)
    cleaned = re.sub(r'(\d{3})-(\d)', r'\1\2', cleaned)

    # Pattern: "Month D, YYYY to Month D, YYYY" or "Month D.YYYY to Month D. YYYY"
    pattern = r'(\w+)\s+(\d{1,2})[.,\s]+(\d{4})\s*to\s*(\w+)\s+(\d{1,2})[.,\s]+(\d{4})'
    m = re.search(pattern, cleaned, re.IGNORECASE)
    if m:
        start_month_name = m.group(1).lower().rstrip('.,')
        start_day = int(m.group(2))
        start_year = int(m.group(3))
        end_month_name = m.group(4).lower().rstrip('.,')
        end_day = int(m.group(5))
        end_year = int(m.group(6))

        start_month = months.get(start_month_name)
        end_month = months.get(end_month_name)

        if start_month and end_month:
            start_date = f"{start_year:04d}-{start_month:02d}-{start_day:02d}"
            end_date = f"{end_year:04d}-{end_month:02d}-{end_day:02d}"
            return start_date, end_date, end_year

    # Numeric pattern: "12/01/2018 - 12/31/2018" or "December 1, 2018 - December 31,2018"
    pattern2 = r'(\d{1,2})/(\d{1,2})/(\d{4})\s*[-–]\s*(\d{1,2})/(\d{1,2})/(\d{4})'
    m = re.search(pattern2, text)
    if m:
        start_date = f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        end_date = f"{int(m.group(6)):04d}-{int(m.group(4)):02d}-{int(m.group(5)):02d}"
        return start_date, end_date, int(m.group(6))

    # Also "December 1,2018 - December 31,2018"
    pattern3 = r'(\w+)\s+(\d{1,2}),?\s*(\d{4})\s*[-–]\s*(\w+)\s+(\d{1,2}),?\s*(\d{4})'
    m = re.search(pattern3, text, re.IGNORECASE)
    if m:
        start_month = months.get(m.group(1).lower())
        end_month = months.get(m.group(4).lower())
        if start_month and end_month:
            start_date = f"{int(m.group(3)):04d}-{start_month:02d}-{int(m.group(2)):02d}"
            end_date = f"{int(m.group(6)):04d}-{end_month:02d}-{int(m.group(5)):02d}"
            return start_date, end_date, int(m.group(6))

    return None, None, None


def clean_entity_name(name):
    """Normalize entity names from OCR text."""
    if not name:
        return None
    name = name.strip()
    # Remove common OCR trailing junk
    name = re.sub(r'\s+$', '', name)
    # Remove trailing address fragments
    name = re.sub(r'\s+\d{4,}.*$', '', name)
    name = re.sub(r'\s+6100\s+.*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+ST\.\s+THOMAS.*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+RED\s+HOOK.*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+345\s+PARK.*$', '', name, flags=re.IGNORECASE)

    # Common OCR fixes
    ocr_fixes = {
        'SOl I I IERN RNANCIAL LLC': 'SOUTHERN FINANCIAL LLC',
        'SOl I HERN FINANCIAL LLC': 'SOUTHERN FINANCIAL LLC',
        'SOUTHERN RNANCIAL LLC': 'SOUTHERN FINANCIAL LLC',
        'Sol I I IERN FINANCIAL LLC': 'SOUTHERN FINANCIAL LLC',
        'DARRI.N Is. INDYKE PLLC': 'DARREN K. INDYKE PLLC',
        'DARRI.N K. INDYKE PLLC': 'DARREN K. INDYKE PLLC',
        'PLAN D. LIC': 'PLAN D, LLC',
        'PLAN D. LIE': 'PLAN D, LLC',
        'PLAN D. L.L.0': 'PLAN D, LLC',
        'PLAN D, LIC': 'PLAN D, LLC',
        'PLAN D, LIE': 'PLAN D, LLC',
        'PLAN D. LLC': 'PLAN D, LLC',
        'PLAN D.LLC': 'PLAN D, LLC',
        'NES. LLC': 'NES, LLC',
        'LSJE. LLC': 'LSJE, LLC',
        'JEGE. LLC': 'JEGE, LLC',
        'JEOE, LLC': 'JEGE, LLC',
        'JEOE,': 'JEGE, LLC',
        'ZORRO MANAGEMENT. LW': 'ZORRO MANAGEMENT, LLC',
        'ZORRO MANAGEMENT, LW': 'ZORRO MANAGEMENT, LLC',
        'ZORRO MANAGEMENT. LLC': 'ZORRO MANAGEMENT, LLC',
        'HYPERION MR. LLC': 'HYPERION AIR, LLC',
        'HYPERION AIR. INC': 'HYPERION AIR, INC',
        'HYPERION MR. INC': 'HYPERION AIR, INC',
        'THE HA ZE TRUST': 'THE HAZE TRUST',
        'SOUTHERN TRUST COMPA': 'SOUTHERN TRUST COMPANY, INC.',
        'SOI I I TERN TRUST': 'SOUTHERN TRUST COMPANY, INC.',
        'SOI I I tERN TRUST': 'SOUTHERN TRUST COMPANY, INC.',
        'DARREN K. 1NDYKE': 'DARREN K. INDYKE PLLC',
        'JEGE. INC': 'JEGE, INC',
        'JEGE. LLC': 'JEGE, LLC',
        'JEGE, LLC': 'JEGE, LLC',
        'JECTE, LLC': 'JEGE, LLC',
        'JECTE. LLC': 'JEGE, LLC',
        'EGE, INC': 'JEGE, INC',
        'EGE. INC': 'JEGE, INC',
        'PRYTANEE. LLC': 'PRYTANCE, LLC',
        'PRYTANEE, LLC': 'PRYTANCE, LLC',
        'PRYTANCE. LLC': 'PRYTANCE, LLC',
        'GRATITUDE AMERICA. LTD': 'GRATITUDE AMERICA, LTD',
        'GRATITUDE AMERICA, LTD': 'GRATITUDE AMERICA, LTD',
        'HBRK ASSOCIATES. INC': 'HBRK ASSOCIATES, INC',
        'SOUTHERN TRUST COM PA NY': 'SOUTHERN TRUST COMPANY, INC.',
        'SOUTHERN FINANCIAL LW': 'SOUTHERN FINANCIAL LLC',
        'SOUTHERN FINANCIAL LLC': 'SOUTHERN FINANCIAL LLC',
        'J EPSTEIN VIRGIN ISLANDS': 'J. EPSTEIN VIRGIN ISLANDS FOUNDATION',
    }
    # Check all OCR fixes
    name_upper = name.upper().strip()
    for bad, good in ocr_fixes.items():
        if name_upper.startswith(bad.upper()):
            name = good
            break

    # Reject names that look like address fragments or garbage
    if re.match(r'^(?:PANY|6100|00802|UNITED|ST\.|345|NEW\s+YORK|RED\s+HOOK|EMENT)', name.upper()):
        return None

    return name.strip()


# ---------------------------------------------------------------------------
# Parser 1: Deutsche Bank Account Statements (balance + embedded transactions)
# ---------------------------------------------------------------------------

def parse_db_statement(file_id, filename, text):
    """Parse a Deutsche Bank monthly account statement.

    Extracts:
      - Account balance (beginning and ending)
      - Transaction lines (incoming/outgoing money, checks, transfers)
    """
    balances = []
    transactions = []

    efta_id = extract_efta_id(filename)

    # Extract account holder name - it appears after the DB address block
    # DB statements have entity name after the bank address and before "For personal assistance"
    holder = None

    # Strategy 1: Look between "NY 10154" and "For personal assistance"
    holder_block = re.search(
        r'(?:NY|New York)[.,\s]+(?:NY\s*)?10154.*?\n(.+?)(?:For\s+personal|Summary\s+of)',
        text[:2000], re.DOTALL | re.IGNORECASE
    )
    if holder_block:
        lines = [l.strip() for l in holder_block.group(1).strip().split('\n') if l.strip()]
        if lines:
            # First non-empty line is the entity name
            candidate = lines[0]
            # Skip if it's just an address or "JEFFREY EPSTEIN" alone (need entity first)
            if re.match(r'^[A-Z][A-Z\s.,&\'\-]+(?:LLC|INC|PLLC|LP|LTD|CORP|TRUST|L\.L\.C\.)', candidate):
                holder = clean_entity_name(candidate)
            elif re.match(r'^[A-Z][A-Z\s.,&\'\-]{3,}', candidate) and not re.match(r'^(?:6100|ST\.|345|NEW|UNITED)', candidate):
                holder = clean_entity_name(candidate)

    # Strategy 2: Direct pattern matching
    if not holder:
        holder_patterns = [
            r'New York[.,]\s*NY\s*10154\s*\n+([A-Z][A-Z\s.,&\'-]+(?:LLC|INC|PLLC|LP|LTD|CORP|TRUST|L\.L\.C\.))',
            r'New York[.,]\s*NY\s*10154\s*\n+([A-Z][A-Z\s.,&\'-]+)\n',
            r'(?:Deutsche Bank.*?\n){2,4}([A-Z][A-Z\s.,&\'-]+(?:LLC|INC|PLLC|LP|LTD|CORP|TRUST|L\.L\.C\.))',
        ]
        for pat in holder_patterns:
            m = re.search(pat, text[:2000])
            if m:
                holder = clean_entity_name(m.group(1).strip())
                break

    # Strategy 3: First line with entity suffix
    if not holder:
        for line in text.split('\n')[:30]:
            line = line.strip()
            if line and re.match(r'^[A-Z][A-Z\s.,&\'-]+(LLC|INC|PLLC|LP|LTD|CORP|TRUST)', line):
                holder = clean_entity_name(line)
                break

    # Strategy 4: Just Jeffrey Epstein personal
    if not holder:
        m = re.search(r'(?:JEFFREY|Jeffrey)\s+(?:EPSTEIN|Epstein)', text[:1000])
        if m:
            holder = 'JEFFREY EPSTEIN'

    # Get date range
    start_date, end_date, year = extract_statement_date_range(text)

    # Extract account type and number
    # Pattern: "Business Checking XXXXXXX" or "Elite Checking With Interest XXXXXXX"
    # or "Elite Money Market Deposit XXXXXXX"
    acct_type = None
    acct_number = None

    acct_patterns = [
        r'(Business\s*C[h]?eckin[gR])\s+(\d[\d,]*\.\d{2}|\d+)',
        r'(Elite\s*Checking\s*With\s*Interest)\s+(\S+)',
        r'(Elite\s*Money\s*Market\s*Deposit)\s+(\S+)',
        r'(Money\s*Market)\s+(\S+)',
        r'(Busines[sa]\s*Checkin[gR])\s+(\d[\d,.]+)',
        r'(NOW\s+and\s+SuperNOW)\s+',
    ]
    for pat in acct_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            acct_type = m.group(1).strip()
            # Normalize
            acct_type_lower = acct_type.lower()
            if 'check' in acct_type_lower:
                acct_type = 'checking'
            elif 'money market' in acct_type_lower:
                acct_type = 'money_market'
            elif 'now' in acct_type_lower:
                acct_type = 'now_account'
            break

    # Get account number from "Account Account Number Balance" header
    m = re.search(r'Account\s+(?:Account\s+)?Number\s+Balance', text[:3000], re.IGNORECASE)
    if m:
        # The next line typically has: type number balance
        after = text[m.end():m.end()+500]
        num_match = re.search(r'(?:Business|Elite|Money|Busines[sa]|NOW|Checking).*?(\d{7,10})\b', after, re.IGNORECASE)
        if num_match:
            acct_number = num_match.group(1)

    # Also try "Account Number" in the line itself or just a standalone long number
    if not acct_number:
        m = re.search(r'Account\s*Number[:\s]+(\d{7,10})', text[:3000], re.IGNORECASE)
        if m:
            acct_number = m.group(1)

    # Try from transaction report format: "Account Number : NNNNNNNNN"
    if not acct_number:
        m = re.search(r'Account\s*Number\s*:\s*(\d{7,10})', text)
        if m:
            acct_number = m.group(1)

    # Also check for the inline balance-line number pattern "42952771"
    # (appears as an 8-digit number on its own line near the bottom)
    if not acct_number:
        m = re.search(r'\n(\d{8})\n', text[:3000])
        if m:
            acct_number = m.group(1)

    # Extract beginning and ending balances
    beg_bal = None
    end_bal = None

    # Fix OCR text for balance extraction: "I" -> "1" near dates, "201-1" -> "2014"
    bal_text = re.sub(r'(\d{3})-(\d)', r'\1\2', text)

    # Beginning Balance - look for $-prefixed amount with exactly 2 decimal places
    beg_patterns = [
        r'[Bb]e(?:ginning|aiming|sinning)\s+[Bb]alance.*?[\$S]([\d,]+\.\d{2})',
    ]
    for pat in beg_patterns:
        m = re.search(pat, bal_text[:3000])
        if m:
            beg_bal, _ = parse_dollar_amount(m.group(1))
            if beg_bal is not None:
                break

    # Ending Balance - search in the "Summary of Account Balance(s)" section first
    # This is the most reliable source: "Business Checking $414,687.72" or "Balance\n...\n$414,687.72"
    summary_match = re.search(r'Summary\s+of\s+Account\s+Balance.*?(?:Account\s+(?:Account\s+)?Number\s+Balance\s*\n)', bal_text[:3000], re.DOTALL | re.IGNORECASE)
    if summary_match:
        summary_after = bal_text[summary_match.end():summary_match.end()+500]
        # First dollar amount on the next line is the ending balance
        bal_line = re.search(r'[\$S]([\d,]+\.\d{2})', summary_after)
        if bal_line:
            end_bal, _ = parse_dollar_amount(bal_line.group(1))

    if end_bal is None:
        # Fallback: look for "Ending Balance as of MONTH DD, YYYY $AMOUNT"
        # But be careful to capture only the dollar amount, not running text
        end_patterns = [
            r'[Ee]nding\s+[Bb]alance.*?[\$S]([\d,]+\.\d{2})',
            r'[Ee]nding\s+[Bb]al(?:ance|ZIICC).*?[\$S]([\d,]+\.\d{2})',
        ]
        for pat in end_patterns:
            m = re.search(pat, bal_text[:5000])
            if m:
                end_bal, _ = parse_dollar_amount(m.group(1))
                if end_bal is not None:
                    break

    statement_id = build_statement_id(file_id, efta_id, acct_number, holder, start_date, end_date)
    statement_seq = 0

    # Record balances
    if holder and end_date:
        if beg_bal is not None:
            if start_date:
                balances.append({
                    'file_id': file_id,
                    'efta_id': efta_id,
                    'account_holder': holder,
                    'account_number': acct_number,
                    'account_type': acct_type or 'unknown',
                    'balance_date': start_date,
                    'balance': beg_bal,
                    'bank': 'Deutsche Bank',
                    'raw_extract': f"Beginning Balance: ${beg_bal:,.2f}" if beg_bal else None,
                })

        if end_bal is not None:
            balances.append({
                'file_id': file_id,
                'efta_id': efta_id,
                'account_holder': holder,
                'account_number': acct_number,
                'account_type': acct_type or 'unknown',
                'balance_date': end_date,
                'balance': end_bal,
                'bank': 'Deutsche Bank',
                'raw_extract': f"Ending Balance: ${end_bal:,.2f}" if end_bal else None,
            })

    # Extract transaction lines from the "Transaction Detail" section
    # Pattern for each line: MM-DD description (amount) or amount running_balance
    if year:
        tx_section = text
        # Find "Transaction Detail" section
        tx_start = re.search(r'Transaction\s+[Dd]etail', tx_section)
        if tx_start:
            tx_section = tx_section[tx_start.end():]

        # Match transaction lines with "Incoming Money" or "Outgoing Money" patterns
        # OCR produces many variants: Trnsf, Tmsf, Trust, Tred', Trmf, etc.
        # Pattern: date [noise] direction_keyword [amount] [running_balance]

        # Generic money transfer word: T followed by mix of letters ending near 'nsf'
        _money_trnsf = r'(?:Money\s+T\w{2,6})'  # Matches Trnsf, Tmsf, Trust, Tred', Trmf, etc.

        incoming_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Incoming\s+' + _money_trnsf + r')\s+'
            r'([\d,. ]+)\s+'    # credit amount
            r'([\d,. ]+)',      # running balance
            re.IGNORECASE
        )

        outgoing_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Outgoing\s+' + _money_trnsf + r'[\']*)\s+'
            r'\(?([\d,. ]+)\)?\s+'    # debit amount
            r'([\d,. ]+)',            # running balance
            re.IGNORECASE
        )

        outgoing_fx_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Outgoing\s+Fx\s+Transfer)\s+'
            r'\(?([\d,. ]+)\)?\s+'
            r'([\d,. ]+)',
            re.IGNORECASE
        )

        transfer_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Transfer\s+Of\s+Funds)\s+'
            r'\(?([\d,. ]+)\)?\s+'
            r'([\d,. ]+)',
            re.IGNORECASE
        )

        cash_mgmt_dr_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Cash\s+Mg?m[ti]?\s+T[ra]sfr?\s+Dr)\s+'
            r'\(?([\d,. ]+)\)?\s+'
            r'([\d,. ]+)',
            re.IGNORECASE
        )

        cash_mgmt_cr_pattern = re.compile(
            r'(\d{2}-\d{2})\s+.*?(?:Cash\s+Mg?m[ti]?\s+T[ra]sfr?\s+Cr)\s+'
            r'([\d,. ]+)\s+'
            r'([\d,. ]+)',
            re.IGNORECASE
        )

        # Process line by line for context extraction
        lines = tx_section.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            # Look for incoming money transfers
            m_in = incoming_pattern.search(line)
            if m_in:
                date_str = normalize_date(m_in.group(1), year)
                amount, conf = parse_dollar_amount(m_in.group(2))
                running_balance_raw = m_in.group(3).strip()
                running_balance, _ = parse_dollar_amount(running_balance_raw)
                if amount and date_str:
                    # Try to get sender from the next lines (ORG= pattern)
                    sender = None
                    raw_context = line.strip()
                    for j in range(1, min(4, len(lines) - i)):
                        next_line = lines[i + j].strip()
                        raw_context += '\n' + next_line
                        org_match = re.search(r'ORG[=-]?\s*[\d]*\s*(.*)', next_line, re.IGNORECASE)
                        if org_match:
                            sender = org_match.group(1).strip()
                        # Also look for entity name in the continuation
                        if not sender and re.match(r'^[A-Z][A-Z\s.,&\'-]+', next_line) and len(next_line) > 5:
                            # Could be continuation of description
                            pass

                    # Extract sender from the line itself
                    if not sender:
                        org_match = re.search(r'ORG[=-]?\s*[\d]*\s*(.*)', line, re.IGNORECASE)
                        if org_match:
                            sender = org_match.group(1).strip()

                    # Try to get sender from context after the amount
                    if not sender:
                        after_amount = line[m_in.end():]
                        for j in range(1, min(4, len(lines) - i)):
                            after_amount += ' ' + lines[i + j].strip()
                        # Look for entity-like names
                        name_match = re.search(r'([A-Z][A-Z\s.,&\'-]{3,}(?:LLC|INC|CORP|BANK|LP|LTD|CO|TRUST))', after_amount)
                        if name_match:
                            sender = name_match.group(1).strip()

                    statement_seq += 1
                    transactions.append({
                        'file_id': file_id,
                        'efta_id': efta_id,
                        'tx_date': date_str,
                        'amount': abs(amount),
                        'currency': 'USD',
                        'direction': 'incoming',
                        'sender': sender,
                        'sender_account': None,
                        'receiver': holder,
                        'receiver_account': acct_number,
                        'bank': 'Deutsche Bank',
                        'reference': None,
                        'raw_extract': raw_context[:500],
                        'confidence': conf,
                        'statement_id': statement_id,
                        'statement_seq': statement_seq,
                        'running_balance': running_balance,
                        'running_balance_raw': running_balance_raw,
                        'parsed_from_statement': 1,
                    })
                i += 1
                continue

            # Look for outgoing money transfers
            m_out = outgoing_pattern.search(line)
            is_fx = False
            if not m_out:
                m_out = outgoing_fx_pattern.search(line)
                is_fx = bool(m_out)
            if m_out:
                date_str = normalize_date(m_out.group(1), year)
                amount, conf = parse_dollar_amount(m_out.group(2))
                running_balance_raw = m_out.group(3).strip()
                running_balance, _ = parse_dollar_amount(running_balance_raw)
                if amount and date_str:
                    # Try to get receiver from "TO BANK A/C ENTITY" pattern
                    receiver = None
                    receiver_account = None
                    raw_context = line.strip()
                    for j in range(1, min(4, len(lines) - i)):
                        next_line = lines[i + j].strip()
                        raw_context += '\n' + next_line

                    # For FX transactions, extract foreign currency metadata
                    # and cross-validate amount against rate * foreign_amount
                    fx_reference = None
                    if is_fx:
                        # OCR garbles currency codes: ELIR/ELI(/ELM/FAIR→EUR, OBP→GBP
                        # Match known codes + common OCR variants
                        fx_match = re.search(
                            r'(EUR|GBP|CAD|CHF|JPY|AUD|SEK|NOK|DKK|ILS'
                            r'|ELI[R(K]|ELM|FAIR|OBP)'
                            r'\s*([\d,. ]+?)\s*'
                            r'RATE\s*([I1]?\s*[\d.]+)',
                            raw_context, re.IGNORECASE
                        )
                        if fx_match:
                            # Normalize OCR'd currency codes
                            fx_code = fx_match.group(1).upper()
                            if fx_code in ('ELIR', 'ELI(', 'ELIK', 'ELM', 'FAIR'):
                                fx_currency = 'EUR'
                            elif fx_code == 'OBP':
                                fx_currency = 'GBP'
                            else:
                                fx_currency = fx_code
                            fx_raw = fx_match.group(2).strip().replace(',', '').replace(' ', '')
                            # Handle RATEI.xxx where I is OCR for 1
                            fx_rate_raw = fx_match.group(3).replace(' ', '')
                            if fx_rate_raw.startswith('I'):
                                fx_rate_raw = '1' + fx_rate_raw[1:]
                            try:
                                fx_amount = float(fx_raw)
                                fx_rate_val = float(fx_rate_raw)
                                computed_usd = fx_amount * fx_rate_val
                                fx_reference = f"FX: {fx_currency} {fx_amount:,.2f} @ {fx_rate_val}"
                                # Cross-validate: only correct when parsed is >10x too high
                                # (catches OCR decimal-point drops like 18,54228 → 1854228)
                                # Don't correct when parsed is too LOW — that means the
                                # foreign amount is garbled, not the USD amount
                                if computed_usd > 0 and abs(amount) > computed_usd * 10:
                                    amount = computed_usd
                                    conf = 0.75
                            except (ValueError, ZeroDivisionError):
                                pass

                    # Extract receiver from TO ... A/C ... pattern
                    to_match = re.search(
                        r'TO\s+([\w\s.,&\'-]+?)(?:\s+A/?C\s*(\S+))?\s*$',
                        line + ' ' + (lines[i+1].strip() if i+1 < len(lines) else ''),
                        re.IGNORECASE
                    )
                    if to_match:
                        bank_or_receiver = to_match.group(1).strip()
                        receiver_account = to_match.group(2)
                        # The receiver is often on the next line after the bank name
                        receiver = bank_or_receiver

                    if not receiver:
                        # Check next lines for TO pattern
                        for j in range(0, min(4, len(lines) - i)):
                            to_m = re.search(r'TO\s+(.+)', lines[i + j], re.IGNORECASE)
                            if to_m:
                                receiver = to_m.group(1).strip()
                                # Remove A/C numbers
                                ac_m = re.search(r'(.+?)\s+A/?C\s*(.+)', receiver)
                                if ac_m:
                                    receiver = ac_m.group(1).strip()
                                    receiver_account = ac_m.group(2).strip()
                                break

                    statement_seq += 1
                    transactions.append({
                        'file_id': file_id,
                        'efta_id': efta_id,
                        'tx_date': date_str,
                        'amount': abs(amount),
                        'currency': 'USD',
                        'direction': 'outgoing',
                        'sender': holder,
                        'sender_account': acct_number,
                        'receiver': receiver,
                        'receiver_account': receiver_account,
                        'bank': 'Deutsche Bank',
                        'reference': fx_reference,
                        'raw_extract': raw_context[:500],
                        'confidence': conf,
                        'statement_id': statement_id,
                        'statement_seq': statement_seq,
                        'running_balance': running_balance,
                        'running_balance_raw': running_balance_raw,
                        'parsed_from_statement': 1,
                    })
                i += 1
                continue

            # Transfer Of Funds (internal transfers)
            m_xfer = transfer_pattern.search(line)
            if m_xfer:
                date_str = normalize_date(m_xfer.group(1), year)
                amount, conf = parse_dollar_amount(m_xfer.group(2))
                running_balance_raw = m_xfer.group(3).strip()
                running_balance, _ = parse_dollar_amount(running_balance_raw)
                if amount and date_str:
                    raw_context = line.strip()
                    for j in range(1, min(3, len(lines) - i)):
                        raw_context += '\n' + lines[i + j].strip()

                    statement_seq += 1
                    transactions.append({
                        'file_id': file_id,
                        'efta_id': efta_id,
                        'tx_date': date_str,
                        'amount': abs(amount),
                        'currency': 'USD',
                        'direction': 'outgoing',
                        'sender': holder,
                        'sender_account': acct_number,
                        'receiver': 'INTERNAL TRANSFER',
                        'receiver_account': None,
                        'bank': 'Deutsche Bank',
                        'reference': 'Transfer Of Funds',
                        'raw_extract': raw_context[:500],
                        'confidence': conf * 0.9,
                        'statement_id': statement_id,
                        'statement_seq': statement_seq,
                        'running_balance': running_balance,
                        'running_balance_raw': running_balance_raw,
                        'parsed_from_statement': 1,
                    })
                i += 1
                continue

            # Cash Management Transfer Debit (internal between DB accounts)
            m_cmd = cash_mgmt_dr_pattern.search(line)
            if m_cmd:
                date_str = normalize_date(m_cmd.group(1), year)
                amount, conf = parse_dollar_amount(m_cmd.group(2))
                running_balance_raw = m_cmd.group(3).strip()
                running_balance, _ = parse_dollar_amount(running_balance_raw)
                if amount and date_str and amount >= 10000:  # Only record significant transfers
                    raw_context = line.strip()
                    for j in range(1, min(3, len(lines) - i)):
                        raw_context += '\n' + lines[i + j].strip()

                    # Try to extract DEP account number
                    dep_match = re.search(r'DEP\s+(\d+)', raw_context)
                    dest_account = dep_match.group(1) if dep_match else None

                    statement_seq += 1
                    transactions.append({
                        'file_id': file_id,
                        'efta_id': efta_id,
                        'tx_date': date_str,
                        'amount': abs(amount),
                        'currency': 'USD',
                        'direction': 'outgoing',
                        'sender': holder,
                        'sender_account': acct_number,
                        'receiver': 'INTERNAL TRANSFER',
                        'receiver_account': dest_account,
                        'bank': 'Deutsche Bank',
                        'reference': 'Cash Mgmt Transfer Debit',
                        'raw_extract': raw_context[:500],
                        'confidence': conf * 0.85,
                        'statement_id': statement_id,
                        'statement_seq': statement_seq,
                        'running_balance': running_balance,
                        'running_balance_raw': running_balance_raw,
                        'parsed_from_statement': 1,
                    })
                i += 1
                continue

            # Cash Management Transfer Credit (internal between DB accounts)
            m_cmc = cash_mgmt_cr_pattern.search(line)
            if m_cmc:
                date_str = normalize_date(m_cmc.group(1), year)
                amount, conf = parse_dollar_amount(m_cmc.group(2))
                running_balance_raw = m_cmc.group(3).strip()
                running_balance, _ = parse_dollar_amount(running_balance_raw)
                if amount and date_str and amount >= 10000:
                    raw_context = line.strip()
                    for j in range(1, min(3, len(lines) - i)):
                        raw_context += '\n' + lines[i + j].strip()

                    dep_match = re.search(r'DEP\s+(\d+)', raw_context)
                    src_account = dep_match.group(1) if dep_match else None

                    statement_seq += 1
                    transactions.append({
                        'file_id': file_id,
                        'efta_id': efta_id,
                        'tx_date': date_str,
                        'amount': abs(amount),
                        'currency': 'USD',
                        'direction': 'incoming',
                        'sender': 'INTERNAL TRANSFER',
                        'sender_account': src_account,
                        'receiver': holder,
                        'receiver_account': acct_number,
                        'bank': 'Deutsche Bank',
                        'reference': 'Cash Mgmt Transfer Credit',
                        'raw_extract': raw_context[:500],
                        'confidence': conf * 0.85,
                        'statement_id': statement_id,
                        'statement_seq': statement_seq,
                        'running_balance': running_balance,
                        'running_balance_raw': running_balance_raw,
                        'parsed_from_statement': 1,
                    })
                i += 1
                continue

            i += 1

    return balances, transactions


# ---------------------------------------------------------------------------
# Parser 2: FEDWIRE Payment Advice
# ---------------------------------------------------------------------------

def parse_fedwire(file_id, filename, text):
    """Parse FEDWIRE PAYMENT DEBIT/CREDIT ADVICE documents.

    These are structured wire transfer records with clear fields.
    """
    transactions = []
    efta_id = extract_efta_id(filename)

    # Split on FEDWIRE headers - a single document may contain multiple wires
    wire_blocks = re.split(r'(FEDWIRE\s+(?:PAYMENT\s+)?(?:RECEIVE\s+)?(?:DEBIT|CREDIT)\s+ADVICE)', text, flags=re.IGNORECASE)

    for i in range(1, len(wire_blocks), 2):
        header = wire_blocks[i]
        block = wire_blocks[i + 1] if i + 1 < len(wire_blocks) else ''

        is_credit = 'CREDIT' in header.upper()
        is_debit = 'DEBIT' in header.upper()

        # Extract date
        date_match = re.search(r'Date\s+(\d{1,2}-\w{3}-\d{4})', block)
        tx_date = None
        if date_match:
            tx_date = normalize_date(date_match.group(1))

        # Extract amounts
        amount = None
        conf = 0.9
        for amt_label in ['Instructed Amount', 'Received Amount', 'Debited Amount', 'Credit Amount', 'Paid Amount']:
            amt_match = re.search(amt_label + r'\s+([\d,. ]+)\s*(USD|EUR|GBP)?', block, re.IGNORECASE)
            if amt_match and amount is None:
                amount, _ = parse_dollar_amount(amt_match.group(1))
                currency = amt_match.group(2) or 'USD'

        if not amount or not tx_date:
            continue

        # Extract originator (sender)
        originator = None
        orig_match = re.search(r'Originator\s+(?:Beneficiar[yia].*?\n)?\s*\n?\s*([A-Z][A-Z\s.,&\'\-/]+)', block)
        if orig_match:
            originator = orig_match.group(1).strip()
            # Clean up multi-line
            originator = originator.split('\n')[0].strip()

        # Extract beneficiary (receiver)
        beneficiary = None
        # The beneficiary is often in the right column opposite originator
        benef_match = re.search(r'Beneficiar[yia]\s*\n\s*([A-Z][A-Z\s.,&\'\-/]+)', block)
        if benef_match:
            beneficiary = benef_match.group(1).strip()
            beneficiary = beneficiary.split('\n')[0].strip()

        # Also try the "Originator to Beneficiary" section for reference
        ref = None
        ref_match = re.search(r'Originator\s+to\s+Beneficiary\s+.*?\n\s*(.+)', block, re.IGNORECASE)
        if ref_match:
            ref = ref_match.group(1).strip()
            # Clean up
            ref = re.sub(r'\s+', ' ', ref)[:200]

        # Extract banks
        sending_bank = None
        receiving_bank = None
        sb_match = re.search(r"Sending\s+Bank\s+Receiving\s+Bank\s*\n\s*([A-Z][A-Z\s.,&\'\-/]+?)(?:\s{2,}|\n)([A-Z][A-Z\s.,&\'\-/]+)", block)
        if sb_match:
            sending_bank = sb_match.group(1).strip()
            receiving_bank = sb_match.group(2).strip()

        # Determine direction
        if is_debit:
            direction = 'outgoing'
            sender = originator
            receiver = beneficiary
        else:
            direction = 'incoming'
            sender = originator
            receiver = beneficiary

        # Extract EFTA from this specific block
        block_efta = efta_id
        block_efta_match = re.search(r'(EFTA[\s_]*\d+)', block)
        if block_efta_match:
            block_efta = block_efta_match.group(1).replace(' ', '').replace('_', '')

        raw = (header + block)[:500]

        transactions.append({
            'file_id': file_id,
            'efta_id': block_efta,
            'tx_date': tx_date,
            'amount': abs(amount),
            'currency': currency if 'currency' in dir() else 'USD',
            'direction': direction,
            'sender': sender,
            'sender_account': None,
            'receiver': receiver,
            'receiver_account': None,
            'bank': sending_bank or 'FEDWIRE',
            'reference': ref,
            'raw_extract': raw,
            'confidence': conf,
        })

    return transactions


# ---------------------------------------------------------------------------
# Parser 3: Deutsche Bank Wealth Management Transaction Reports
# ---------------------------------------------------------------------------

def parse_wm_transaction_report(file_id, filename, text):
    """Parse 'Account Deposits Transactions' reports from DB Wealth Management.

    Format: tabular with columns Transaction Type, Transaction Date, Description To/From,
    Funds Added (USD), Funds Subtracted (USD)
    """
    transactions = []
    efta_id = extract_efta_id(filename)

    # Extract entity name
    entity = None
    m = re.search(r'(?:Account\s+Deposits\s+Transactions.*?\n.*?\n)\s*([A-Z][A-Z\s.,&\'\-]+(?:LLC|INC|PLLC|LP|TRUST|CORP))', text, re.IGNORECASE)
    if m:
        entity = clean_entity_name(m.group(1).strip())
    if not entity:
        m = re.search(r'^([A-Z][A-Z\s.,&\'\-]+(?:LLC|INC|PLLC|LP|TRUST|CORP))', text, re.MULTILINE)
        if m:
            entity = clean_entity_name(m.group(1).strip())

    # Extract account number
    acct_number = None
    m = re.search(r'Account\s*Number\s*[:\s]+(\d{7,10})', text)
    if m:
        acct_number = m.group(1)

    # Parse transaction lines
    # These have format: MM/DD/YYYY or MM;DD;YYYY or MM:DD:YYYY followed by description and amount
    # Pattern for MONEY TRANSFER lines
    # Amount pattern: must end with exactly .DD (2 decimal digits)
    # Use a non-greedy match and require the amount to be bounded
    _amt_pat = r'([\d,.]+\.\d{2})\b'

    money_transfer_pattern = re.compile(
        r'(\d{1,2}[/:;]\d{1,2}[/:;]\d{4})\s+'
        r'(?:MONEY\s+TRANSFER)\s+'
        r'(?:TO\s+)?(.+?)\s+'
        + _amt_pat,
        re.IGNORECASE
    )

    incoming_money_pattern = re.compile(
        r'(\d{1,2}[/:;]\d{1,2}[/:;]\d{4})\s+'
        r'(?:INCOMING\s+MONEY)\s+'
        r'(.+?)\s+'
        + _amt_pat,
        re.IGNORECASE
    )

    cash_mgmt_pattern = re.compile(
        r'(\d{1,2}[/:;]\d{1,2}[/:;]\d{4})\s+'
        r'(?:Cash\s+Mg?m[ti]\s+Transfer)\s+'
        r'(.+?)\s+'
        + _amt_pat,
        re.IGNORECASE
    )

    lines = text.split('\n')
    for idx, line in enumerate(lines):
        # Gather context (next 2-3 lines for multi-line entries)
        context = line
        for j in range(1, min(4, len(lines) - idx)):
            context += ' ' + lines[idx + j].strip()

        # MONEY TRANSFER (outgoing)
        m = money_transfer_pattern.search(context)
        if m:
            raw_date = m.group(1).replace(';', '/').replace(':', '/')
            tx_date = normalize_date(raw_date)
            receiver_text = m.group(2).strip()
            amount, conf = parse_dollar_amount(m.group(3))

            if tx_date and amount:
                # Parse receiver - usually "BANKNAME A/C ENTITY"
                receiver = receiver_text
                receiver_account = None
                ac_m = re.search(r'(.+?)\s+A/?C\s*(.+)', receiver_text)
                if ac_m:
                    receiver = ac_m.group(2).strip()
                    # receiver is the entity, the bank is group(1)

                transactions.append({
                    'file_id': file_id,
                    'efta_id': efta_id,
                    'tx_date': tx_date,
                    'amount': abs(amount),
                    'currency': 'USD',
                    'direction': 'outgoing',
                    'sender': entity,
                    'sender_account': acct_number,
                    'receiver': receiver[:200] if receiver else None,
                    'receiver_account': receiver_account,
                    'bank': 'Deutsche Bank',
                    'reference': None,
                    'raw_extract': context[:500],
                    'confidence': conf,
                })
            continue

        # INCOMING MONEY
        m = incoming_money_pattern.search(context)
        if m:
            raw_date = m.group(1).replace(';', '/').replace(':', '/')
            tx_date = normalize_date(raw_date)
            sender_text = m.group(2).strip()
            amount, conf = parse_dollar_amount(m.group(3))

            if tx_date and amount:
                sender = sender_text
                # Try to extract ORG= pattern
                org_m = re.search(r'ORG[=-]?\s*\S+\s+(.*)', sender_text)
                if org_m:
                    sender = org_m.group(1).strip()

                transactions.append({
                    'file_id': file_id,
                    'efta_id': efta_id,
                    'tx_date': tx_date,
                    'amount': abs(amount),
                    'currency': 'USD',
                    'direction': 'incoming',
                    'sender': sender[:200] if sender else None,
                    'sender_account': None,
                    'receiver': entity,
                    'receiver_account': acct_number,
                    'bank': 'Deutsche Bank',
                    'reference': None,
                    'raw_extract': context[:500],
                    'confidence': conf,
                })
            continue

        # Cash Mgmt Transfer
        m = cash_mgmt_pattern.search(context)
        if m:
            raw_date = m.group(1).replace(';', '/').replace(':', '/')
            tx_date = normalize_date(raw_date)
            desc_text = m.group(2).strip()
            amount, conf = parse_dollar_amount(m.group(3))

            if tx_date and amount and amount >= 10000:
                is_credit = 'CREDIT' in desc_text.upper() or 'FRM' in desc_text.upper()
                direction = 'incoming' if is_credit else 'outgoing'

                transactions.append({
                    'file_id': file_id,
                    'efta_id': efta_id,
                    'tx_date': tx_date,
                    'amount': abs(amount),
                    'currency': 'USD',
                    'direction': direction,
                    'sender': entity if direction == 'outgoing' else 'INTERNAL TRANSFER',
                    'sender_account': acct_number if direction == 'outgoing' else None,
                    'receiver': entity if direction == 'incoming' else 'INTERNAL TRANSFER',
                    'receiver_account': acct_number if direction == 'incoming' else None,
                    'bank': 'Deutsche Bank',
                    'reference': 'Cash Mgmt Transfer',
                    'raw_extract': context[:500],
                    'confidence': conf * 0.85,
                })

    return transactions


# ---------------------------------------------------------------------------
# Parser 4: Portfolio Holdings / Position Snapshots
# ---------------------------------------------------------------------------

def parse_portfolio_holdings(file_id, filename, text):
    """Parse Deutsche Bank Portfolio Holdings statements.

    Extracts investment positions with cost basis and market value.
    """
    positions = []
    efta_id = extract_efta_id(filename)

    # Extract entity name from Portfolio Holdings header
    entity = None
    # Pattern: date range line, then entity name line
    m = re.search(
        r'(?:December|January|February|March|April|May|June|July|August|September|October|November)\s+\d+,?\s*\d{4}\s*[-–]\s*'
        r'(?:December|January|February|March|April|May|June|July|August|September|October|November)\s+\d+,?\s*\d{4}\s*'
        r'\n\s*([A-Z][A-Z\s.,&\'\-]+)',
        text[:1000]
    )
    if m:
        # Take just the first line of the match
        entity = m.group(1).strip().split('\n')[0].strip()
        entity = clean_entity_name(entity)
    if not entity:
        # Try simpler pattern - known entities
        for line in text.split('\n')[:20]:
            line = line.strip()
            if re.match(r'^(?:THE\s+)?(?:SOUTHERN|HAZE|BUTTERFLY|LSJE|JEFFREY|PLAN\s+D|JSC|ZORRO|XGE|BV70|J\s+EPSTEIN|HYPERION|JEGE|NES)', line, re.IGNORECASE):
                entity = clean_entity_name(line.split('\n')[0])
                break

    # Extract statement period end date
    _, end_date, _ = extract_statement_date_range(text)
    if not end_date:
        # Try the date range in the header
        m = re.search(r'(\w+\s+\d+,?\s*\d{4})\s*[-–]\s*(\w+\s+\d+,?\s*\d{4})', text[:500])
        if m:
            # Parse end date
            try:
                dt = datetime.strptime(m.group(2).replace(',', '').strip(), "%B %d %Y")
                end_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    if not entity or not end_date:
        return positions

    # Extract TOTAL PORTFOLIO HOLDINGS line
    total_match = re.search(
        r'TOTAL\s+PORTFOLIO\s+HOLDINGS\s+'
        r'[\$S]?([\d,. ]+)\s+'   # cost basis
        r'[\$S]?([\d,. ]+)',     # market value
        text, re.IGNORECASE
    )
    if total_match:
        cost_basis, _ = parse_dollar_amount(total_match.group(1))
        market_value, _ = parse_dollar_amount(total_match.group(2))
        if market_value:
            positions.append({
                'file_id': file_id,
                'efta_id': efta_id,
                'entity': entity,
                'investment': 'TOTAL PORTFOLIO',
                'position_date': end_date,
                'value': abs(market_value),
                'cost_basis': abs(cost_basis) if cost_basis else None,
                'raw_extract': total_match.group(0)[:500],
            })

    # Extract individual security positions
    # Pattern: SECURITY NAME ... Security Identifier: TICKER
    # Then: date quantity unit_cost cost_basis market_price market_value gain_loss ...
    security_pattern = re.compile(
        r'([A-Z][A-Z\s.,&\'\-/]+?)\s+(?:Security\s+Ident(?:ifier|ifie[rn])|CUSIP)[:\s]*(\w*)',
        re.IGNORECASE
    )

    for m in security_pattern.finditer(text):
        security_name = m.group(1).strip()
        ticker = m.group(2).strip() if m.group(2) else None

        # Skip if too short or generic
        if len(security_name) < 3:
            continue

        # Look for the data line(s) after the security name
        after = text[m.end():m.end()+1000]

        # Find lines with numeric data - cost basis and market value
        # Pattern: date quantity unit_cost cost_basis market_price market_value gain/loss
        # The "Total" line aggregates: Total N cost_basis market_value gain_loss
        total_line = re.search(
            r'Total(?:\s+\w+)?\s+'
            r'[\d,.]+\s+'       # quantity or count
            r'[\$S]?([\d,. ]+)\s+'   # cost basis
            r'[\$S]?([\d,. ]+)',     # market value
            after, re.IGNORECASE
        )

        if total_line:
            cost_basis, _ = parse_dollar_amount(total_line.group(1))
            market_value, _ = parse_dollar_amount(total_line.group(2))

            if market_value and market_value > 0:
                display_name = security_name
                if ticker:
                    display_name = f"{security_name} ({ticker})"

                positions.append({
                    'file_id': file_id,
                    'efta_id': efta_id,
                    'entity': entity,
                    'investment': display_name[:200],
                    'position_date': end_date,
                    'value': abs(market_value),
                    'cost_basis': abs(cost_basis) if cost_basis else None,
                    'raw_extract': (m.group(0) + after[:200])[:500],
                })

    # Extract category totals (TOTAL EQUITIES, TOTAL FIXED INCOME, TOTAL CASH)
    category_pattern = re.compile(
        r'TOTAL\s+(EQUITIES|FIXED\s+INCOME|CASH[,\s]+MONEY\s+FUNDS[,\s]+AND\s+BANK\s+DEPOSITS)\s+'
        r'[\$S]?([\d,. ]+)\s+'
        r'[\$S]?([\d,. ]+)',
        re.IGNORECASE
    )
    for m in category_pattern.finditer(text):
        category = m.group(1).strip()
        cost_basis, _ = parse_dollar_amount(m.group(2))
        market_value, _ = parse_dollar_amount(m.group(3))
        if market_value:
            positions.append({
                'file_id': file_id,
                'efta_id': efta_id,
                'entity': entity,
                'investment': f'TOTAL {category.upper()}',
                'position_date': end_date,
                'value': abs(market_value),
                'cost_basis': abs(cost_basis) if cost_basis else None,
                'raw_extract': m.group(0)[:500],
            })

    return positions


# ---------------------------------------------------------------------------
# Parser 5: Regulation E Wire Disclosures
# ---------------------------------------------------------------------------

def parse_regulation_e(file_id, filename, text):
    """Parse Regulation E Disclosure for Remittance Transfer documents."""
    transactions = []
    efta_id = extract_efta_id(filename)

    # Check it's actually a Reg E disclosure
    if 'regulation e disclosure' not in text.lower():
        return transactions

    # Extract date
    date_match = re.search(r"Today'?s\s+Date[:\s]+(\w+\s+\d{1,2},?\s+\d{4})", text, re.IGNORECASE)
    tx_date = None
    if date_match:
        try:
            raw = date_match.group(1).replace(',', '')
            dt = datetime.strptime(raw, "%B %d %Y")
            tx_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    if not tx_date:
        # Try Value Date
        date_match = re.search(r"Value\s+Date[:\s]+(\w+\s+\d{1,2},?\s+\d{4})", text, re.IGNORECASE)
        if date_match:
            try:
                raw = date_match.group(1).replace(',', '')
                dt = datetime.strptime(raw, "%B %d %Y")
                tx_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Extract amount
    amount_match = re.search(r'Transfer\s+Amount\s+([\d,. ]+)\s*(USD|EUR|GBP)?', text, re.IGNORECASE)
    if not amount_match:
        amount_match = re.search(r'Total\s+([\d,. ]+)\s*(USD|EUR|GBP)?', text, re.IGNORECASE)

    if amount_match and tx_date:
        amount, conf = parse_dollar_amount(amount_match.group(1))
        currency = amount_match.group(2) or 'USD'

        if amount:
            # Extract sender/recipient
            sender = None
            m = re.search(r'Sender\s+Recipient[:\s]*\n\s*(.+)', text, re.IGNORECASE)
            if m:
                sender = m.group(1).strip()

            transactions.append({
                'file_id': file_id,
                'efta_id': efta_id,
                'tx_date': tx_date,
                'amount': abs(amount),
                'currency': currency,
                'direction': 'outgoing',
                'sender': sender or 'JEFFREY EPSTEIN',
                'sender_account': None,
                'receiver': None,
                'receiver_account': None,
                'bank': 'Deutsche Bank',
                'reference': 'Regulation E Remittance',
                'raw_extract': text[:500],
                'confidence': conf,
            })

    return transactions


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def fetch_ds10_docs(db, keyword_filter=None, limit=None):
    """Fetch DS10 documents matching optional keyword filter."""
    if keyword_filter:
        query = '''
            SELECT f.id, f.filename, t.extracted_text, t.char_count
            FROM files f JOIN text_cache t ON f.id = t.file_id
            WHERE f.dataset = 10 AND LOWER(t.extracted_text) LIKE ?
        '''
        if limit:
            query += f' LIMIT {limit}'
        return db.execute(query, (f'%{keyword_filter.lower()}%',)).fetchall()
    else:
        query = '''
            SELECT f.id, f.filename, t.extracted_text, t.char_count
            FROM files f JOIN text_cache t ON f.id = t.file_id
            WHERE f.dataset = 10
        '''
        if limit:
            query += f' LIMIT {limit}'
        return db.execute(query).fetchall()


def insert_transactions(db, transactions):
    """Insert transactions with INSERT OR IGNORE."""
    inserted = 0
    default_run_id = _default_extract_run_id()
    for tx in transactions:
        # Sanity check: skip absurd amounts (> $10B is almost certainly a parsing error)
        if tx.get('amount') and tx['amount'] > 10_000_000_000:
            continue
        # Date validation: must be between 2000 and 2025
        if tx.get('tx_date'):
            try:
                yr = int(tx['tx_date'][:4])
                if yr < 2000 or yr > 2025:
                    continue
            except (ValueError, IndexError):
                continue
        try:
            db.execute('''
                INSERT OR IGNORE INTO ds10_transactions
                (file_id, efta_id, tx_date, amount, currency, direction,
                 sender, sender_account, receiver, receiver_account,
                 bank, reference, raw_extract, confidence,
                 statement_id, statement_seq, running_balance, running_balance_raw,
                 parsed_from_statement, qa_status, qa_flags_json, extract_run_id, parser_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                tx.get('file_id'),
                tx.get('efta_id'),
                tx.get('tx_date'),
                tx.get('amount'),
                tx.get('currency', 'USD'),
                tx.get('direction'),
                tx.get('sender'),
                tx.get('sender_account'),
                tx.get('receiver'),
                tx.get('receiver_account'),
                tx.get('bank'),
                tx.get('reference'),
                tx.get('raw_extract'),
                tx.get('confidence'),
                tx.get('statement_id'),
                tx.get('statement_seq'),
                tx.get('running_balance'),
                tx.get('running_balance_raw'),
                tx.get('parsed_from_statement', 0),
                tx.get('qa_status', 'pending'),
                tx.get('qa_flags_json'),
                tx.get('extract_run_id', default_run_id),
                tx.get('parser_version', PARSER_VERSION),
            ))
            if db.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def insert_balances(db, balances):
    """Insert balances with INSERT OR IGNORE."""
    inserted = 0
    for bal in balances:
        # Sanity check: skip absurd balances (> $1B)
        if bal['balance'] and abs(bal['balance']) > 1_000_000_000:
            continue
        # Skip if holder is None or garbage
        if not bal['account_holder'] or len(bal['account_holder']) < 3:
            continue
        # Date validation
        if bal['balance_date']:
            try:
                yr = int(bal['balance_date'][:4])
                if yr < 2000 or yr > 2025:
                    continue
            except (ValueError, IndexError):
                continue
        try:
            db.execute('''
                INSERT OR IGNORE INTO ds10_balances
                (file_id, efta_id, account_holder, account_number, account_type,
                 balance_date, balance, bank, raw_extract)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                bal['file_id'], bal['efta_id'], bal['account_holder'],
                bal['account_number'], bal['account_type'], bal['balance_date'],
                bal['balance'], bal['bank'], bal['raw_extract'],
            ))
            if db.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def insert_positions(db, positions):
    """Insert positions with INSERT OR IGNORE."""
    inserted = 0
    for pos in positions:
        # Sanity check
        if pos['value'] and pos['value'] > 10_000_000_000:
            continue
        # Clean entity name - strip newlines
        if pos['entity']:
            pos['entity'] = pos['entity'].replace('\n', ' ').strip()
            pos['entity'] = clean_entity_name(pos['entity'])
        if not pos['entity'] or len(pos['entity']) < 3:
            continue
        # Clean investment name
        if pos['investment']:
            pos['investment'] = pos['investment'].replace('\n', ' ').strip()
        try:
            db.execute('''
                INSERT OR IGNORE INTO ds10_positions
                (file_id, efta_id, entity, investment, position_date, value,
                 cost_basis, raw_extract)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pos['file_id'], pos['efta_id'], pos['entity'], pos['investment'],
                pos['position_date'], pos['value'], pos['cost_basis'],
                pos['raw_extract'],
            ))
            if db.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def run_parse_statements(db, limit=None):
    """Parse DB bank statements for balances and embedded transactions."""
    print("=== Parsing Deutsche Bank Account Statements ===")
    # Target: docs with "Summary of Account" and "Beginning Balance"
    docs = db.execute('''
        SELECT f.id, f.filename, t.extracted_text, t.char_count
        FROM files f JOIN text_cache t ON f.id = t.file_id
        WHERE f.dataset = 10
        AND LOWER(t.extracted_text) LIKE '%summary of account%'
        AND LOWER(t.extracted_text) LIKE '%beginning balance%'
    ''' + (f' LIMIT {limit}' if limit else '')).fetchall()

    print(f"  Found {len(docs)} statement documents")

    total_balances = 0
    total_transactions = 0
    errors = 0

    for i, doc in enumerate(docs):
        try:
            balances, transactions = parse_db_statement(doc[0], doc[1], doc[2])
            total_balances += insert_balances(db, balances)
            total_transactions += insert_transactions(db, transactions)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR on {doc[1]}: {e}")

        if (i + 1) % 100 == 0:
            db.commit()
            print(f"  Processed {i+1}/{len(docs)} docs, {total_balances} balances, {total_transactions} transactions, {errors} errors")

    db.commit()
    print(f"  DONE: {total_balances} balances, {total_transactions} transactions from {len(docs)} docs ({errors} errors)")
    return total_balances, total_transactions


def run_parse_fedwire(db, limit=None):
    """Parse FEDWIRE payment advice documents."""
    print("=== Parsing FEDWIRE Payment Advice Documents ===")
    docs = db.execute('''
        SELECT f.id, f.filename, t.extracted_text, t.char_count
        FROM files f JOIN text_cache t ON f.id = t.file_id
        WHERE f.dataset = 10
        AND LOWER(t.extracted_text) LIKE '%fedwire%'
        AND (LOWER(t.extracted_text) LIKE '%debit advice%' OR LOWER(t.extracted_text) LIKE '%credit advice%')
    ''' + (f' LIMIT {limit}' if limit else '')).fetchall()

    print(f"  Found {len(docs)} FEDWIRE documents")

    total = 0
    errors = 0
    for i, doc in enumerate(docs):
        try:
            transactions = parse_fedwire(doc[0], doc[1], doc[2])
            total += insert_transactions(db, transactions)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR on {doc[1]}: {e}")

        if (i + 1) % 50 == 0:
            db.commit()

    db.commit()
    print(f"  DONE: {total} transactions from {len(docs)} docs ({errors} errors)")
    return total


def run_parse_wm_transactions(db, limit=None):
    """Parse Deutsche Bank Wealth Management transaction reports."""
    print("=== Parsing DB Wealth Management Transaction Reports ===")
    docs = db.execute('''
        SELECT f.id, f.filename, t.extracted_text, t.char_count
        FROM files f JOIN text_cache t ON f.id = t.file_id
        WHERE f.dataset = 10
        AND LOWER(t.extracted_text) LIKE '%account deposits transactions%'
    ''' + (f' LIMIT {limit}' if limit else '')).fetchall()

    print(f"  Found {len(docs)} WM transaction report documents")

    total = 0
    errors = 0
    for i, doc in enumerate(docs):
        try:
            transactions = parse_wm_transaction_report(doc[0], doc[1], doc[2])
            total += insert_transactions(db, transactions)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR on {doc[1]}: {e}")

    db.commit()
    print(f"  DONE: {total} transactions from {len(docs)} docs ({errors} errors)")
    return total


def run_parse_positions(db, limit=None):
    """Parse Portfolio Holdings statements."""
    print("=== Parsing Portfolio Holdings Statements ===")
    docs = db.execute('''
        SELECT f.id, f.filename, t.extracted_text, t.char_count
        FROM files f JOIN text_cache t ON f.id = t.file_id
        WHERE f.dataset = 10
        AND LOWER(t.extracted_text) LIKE '%portfolio holdings%'
        AND (LOWER(t.extracted_text) LIKE '%market value%' OR LOWER(t.extracted_text) LIKE '%market price%')
        AND t.char_count < 50000
    ''' + (f' LIMIT {limit}' if limit else '')).fetchall()

    print(f"  Found {len(docs)} portfolio holdings documents")

    total = 0
    errors = 0
    for i, doc in enumerate(docs):
        try:
            positions = parse_portfolio_holdings(doc[0], doc[1], doc[2])
            total += insert_positions(db, positions)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR on {doc[1]}: {e}")

        if (i + 1) % 50 == 0:
            db.commit()

    db.commit()
    print(f"  DONE: {total} positions from {len(docs)} docs ({errors} errors)")
    return total


def run_parse_regulation_e(db, limit=None):
    """Parse Regulation E wire disclosures."""
    print("=== Parsing Regulation E Wire Disclosures ===")
    docs = db.execute('''
        SELECT f.id, f.filename, t.extracted_text, t.char_count
        FROM files f JOIN text_cache t ON f.id = t.file_id
        WHERE f.dataset = 10
        AND LOWER(t.extracted_text) LIKE '%regulation e disclosure%'
    ''' + (f' LIMIT {limit}' if limit else '')).fetchall()

    print(f"  Found {len(docs)} Regulation E documents")

    total = 0
    errors = 0
    for i, doc in enumerate(docs):
        try:
            transactions = parse_regulation_e(doc[0], doc[1], doc[2])
            total += insert_transactions(db, transactions)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR on {doc[1]}: {e}")

    db.commit()
    print(f"  DONE: {total} transactions from {len(docs)} docs ({errors} errors)")
    return total


def run_parse_all(db, limit=None):
    """Run all parsers in sequence."""
    print("=" * 60)
    print("DS10 Financial Document Extraction - Full Run")
    print("=" * 60)

    # 1. Bank statements (balances + embedded transactions)
    bal_count, stmt_tx_count = run_parse_statements(db, limit)
    print()

    # 2. FEDWIRE documents
    fedwire_count = run_parse_fedwire(db, limit)
    print()

    # 3. WM Transaction Reports
    wm_count = run_parse_wm_transactions(db, limit)
    print()

    # 4. Portfolio Holdings
    pos_count = run_parse_positions(db, limit)
    print()

    # 5. Regulation E
    rege_count = run_parse_regulation_e(db, limit)
    print()

    print("=" * 60)
    print("EXTRACTION COMPLETE")
    print(f"  Balances:     {bal_count}")
    print(f"  Transactions: {stmt_tx_count + fedwire_count + wm_count + rege_count}")
    print(f"    - Statements: {stmt_tx_count}")
    print(f"    - FEDWIRE:    {fedwire_count}")
    print(f"    - WM Reports: {wm_count}")
    print(f"    - Reg E:      {rege_count}")
    print(f"  Positions:    {pos_count}")
    print("=" * 60)


def report(db):
    """Print summary statistics of extracted data."""
    print("=" * 60)
    print("DS10 Financial Extraction Report")
    print("=" * 60)

    # Transaction stats
    tx_count = db.execute("SELECT COUNT(*) FROM ds10_transactions").fetchone()[0]
    print(f"\n--- TRANSACTIONS ({tx_count} total) ---")

    if tx_count > 0:
        # Date range
        row = db.execute("SELECT MIN(tx_date), MAX(tx_date) FROM ds10_transactions WHERE tx_date IS NOT NULL").fetchone()
        print(f"  Date range: {row[0]} to {row[1]}")

        # By direction
        for row in db.execute("SELECT direction, COUNT(*), SUM(amount), AVG(amount) FROM ds10_transactions GROUP BY direction"):
            print(f"  {row[0]}: {row[1]} transactions, total ${row[2]:,.2f}, avg ${row[3]:,.2f}")

        # Top senders by total amount
        print("\n  Top 15 senders (by total amount):")
        for row in db.execute("""
            SELECT sender, COUNT(*), SUM(amount)
            FROM ds10_transactions WHERE direction='outgoing' AND sender IS NOT NULL
            GROUP BY sender ORDER BY SUM(amount) DESC LIMIT 15
        """):
            print(f"    {row[0][:50]:50s} {row[1]:4d} tx  ${row[2]:>15,.2f}")

        # Top receivers by total amount
        print("\n  Top 15 receivers (by total amount):")
        for row in db.execute("""
            SELECT receiver, COUNT(*), SUM(amount)
            FROM ds10_transactions WHERE direction='outgoing' AND receiver IS NOT NULL
            GROUP BY receiver ORDER BY SUM(amount) DESC LIMIT 15
        """):
            print(f"    {row[0][:50]:50s} {row[1]:4d} tx  ${row[2]:>15,.2f}")

        # Top incoming senders
        print("\n  Top 15 incoming sources (by total amount):")
        for row in db.execute("""
            SELECT sender, COUNT(*), SUM(amount)
            FROM ds10_transactions WHERE direction='incoming' AND sender IS NOT NULL
            GROUP BY sender ORDER BY SUM(amount) DESC LIMIT 15
        """):
            print(f"    {row[0][:50]:50s} {row[1]:4d} tx  ${row[2]:>15,.2f}")

        # Largest individual transactions
        print("\n  Top 20 largest transactions:")
        for row in db.execute("""
            SELECT tx_date, direction, amount, sender, receiver, efta_id
            FROM ds10_transactions ORDER BY amount DESC LIMIT 20
        """):
            sender = (row[3] or 'UNKNOWN')[:30]
            receiver = (row[4] or 'UNKNOWN')[:30]
            print(f"    {row[0]}  {row[1]:8s}  ${row[2]:>15,.2f}  {sender} -> {receiver}  [{row[5]}]")

        # By bank
        print("\n  By bank:")
        for row in db.execute("SELECT bank, COUNT(*), SUM(amount) FROM ds10_transactions GROUP BY bank ORDER BY SUM(amount) DESC"):
            print(f"    {row[0]:30s} {row[1]:5d} tx  ${row[2]:>15,.2f}")

        # Confidence distribution
        print("\n  Confidence distribution:")
        for row in db.execute("""
            SELECT
                CASE
                    WHEN confidence >= 0.9 THEN 'high (>=0.9)'
                    WHEN confidence >= 0.7 THEN 'medium (0.7-0.9)'
                    ELSE 'low (<0.7)'
                END as conf_level,
                COUNT(*)
            FROM ds10_transactions GROUP BY conf_level
        """):
            print(f"    {row[0]:20s} {row[1]:5d}")

    # Balance stats
    bal_count = db.execute("SELECT COUNT(*) FROM ds10_balances").fetchone()[0]
    print(f"\n--- BALANCES ({bal_count} total) ---")

    if bal_count > 0:
        row = db.execute("SELECT MIN(balance_date), MAX(balance_date) FROM ds10_balances WHERE balance_date IS NOT NULL").fetchone()
        print(f"  Date range: {row[0]} to {row[1]}")

        # By account holder
        print("\n  Top 15 account holders (by max balance):")
        for row in db.execute("""
            SELECT account_holder, COUNT(*), MAX(balance), MIN(balance_date), MAX(balance_date)
            FROM ds10_balances WHERE account_holder IS NOT NULL
            GROUP BY account_holder ORDER BY MAX(balance) DESC LIMIT 15
        """):
            print(f"    {row[0][:45]:45s} {row[1]:4d} snapshots  max ${row[2]:>15,.2f}  ({row[3]} to {row[4]})")

        # By account type
        print("\n  By account type:")
        for row in db.execute("SELECT account_type, COUNT(*), AVG(balance) FROM ds10_balances GROUP BY account_type ORDER BY COUNT(*) DESC"):
            print(f"    {row[0]:20s} {row[1]:5d}  avg ${row[2]:>12,.2f}")

    # Position stats
    pos_count = db.execute("SELECT COUNT(*) FROM ds10_positions").fetchone()[0]
    print(f"\n--- POSITIONS ({pos_count} total) ---")

    if pos_count > 0:
        row = db.execute("SELECT MIN(position_date), MAX(position_date) FROM ds10_positions WHERE position_date IS NOT NULL").fetchone()
        print(f"  Date range: {row[0]} to {row[1]}")

        # By entity
        print("\n  By entity (total portfolio value):")
        for row in db.execute("""
            SELECT entity, COUNT(*), MAX(value)
            FROM ds10_positions WHERE investment = 'TOTAL PORTFOLIO'
            GROUP BY entity ORDER BY MAX(value) DESC
        """):
            print(f"    {row[0][:45]:45s} {row[1]:3d} snapshots  max ${row[2]:>15,.2f}")

        # Top investments by value
        print("\n  Top 15 investments (by max value, excluding totals):")
        for row in db.execute("""
            SELECT entity, investment, MAX(value), MAX(position_date)
            FROM ds10_positions
            WHERE investment NOT LIKE 'TOTAL%'
            GROUP BY entity, investment
            ORDER BY MAX(value) DESC LIMIT 15
        """):
            print(f"    {row[0][:25]:25s} {row[1][:35]:35s} ${row[2]:>15,.2f}  as of {row[3]}")

    print("\n" + "=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Post-processing normalization map
# ---------------------------------------------------------------------------

# Maps raw OCR entity names → canonical names (applied via UPDATE after extraction)
# This catches what clean_entity_name() misses, especially in sender/receiver fields
# where names come from ORG=, TO, and FEDWIRE context — not the entity header.

ENTITY_NORMALIZATION = {
    # === Jeffrey Epstein personal (OCR variants) ===
    'Jolimy Epstein': 'JEFFREY EPSTEIN',
    'Jornemy Epstein': 'JEFFREY EPSTEIN',
    'Jollity Epstein': 'JEFFREY EPSTEIN',
    'Jo/troy Epstein': 'JEFFREY EPSTEIN',
    'Jo/InvEpstein': 'JEFFREY EPSTEIN',
    'Jo/troyEpstein': 'JEFFREY EPSTEIN',
    'Jo!Troy Epstein': 'JEFFREY EPSTEIN',
    'JoStroy Epstein': 'JEFFREY EPSTEIN',
    'Jeffroy Epstein': 'JEFFREY EPSTEIN',
    'Jefhoy Epstein': 'JEFFREY EPSTEIN',
    'Jelhoy Epstein': 'JEFFREY EPSTEIN',
    'Jeffrey Epstein': 'JEFFREY EPSTEIN',

    # === Epstein entities (OCR variants) ===
    'WE. LLC': 'WE, LLC',
    'SOUTHERN TRUST COMPA': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUST COMPAN Y, INC': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUSTCOMPANY. INC.': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTIIERN TRUST COMPANY, INC.': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN FINANCIAI 11.0': 'SOUTHERN FINANCIAL LLC',
    'IRE HAZE TRUST': 'THE HAZE TRUST',
    'TIE HAZE TRUST': 'THE HAZE TRUST',
    'SEGE INC': 'JEGE, INC',
    'SEGE, INC': 'JEGE, INC',
    'JEGE, ILO': 'JEGE, LLC',
    'JEGE LL.0': 'JEGE, LLC',
    'JECTE, LL.0': 'JEGE, LLC',
    'JEGE, NC': 'JEGE, INC',
    'NES. LL.0': 'NES, LLC',
    'NEPTUNE. LLC': 'NEPTUNE, LLC',
    'NEMINE, LLC': 'NEPTUNE, LLC',
    'MOTOCARS. INC': 'MOTOCARS, INC',
    'JEEPERS. INC': 'JEEPERS, INC',
    'PLAN D. LW': 'PLAN D, LLC',
    'PLAN D. L.LC': 'PLAN D, LLC',
    'PLAN D.': 'PLAN D, LLC',
    'HYPERION AIR. LLC': 'HYPERION AIR, LLC',
    'HYPERION AIR. L.L.0': 'HYPERION AIR, LLC',
    'MORT. INC': 'MORT, INC',
    'AC INTERIORS LLC': 'JSC INTERIORS LLC',
    'JSC INTERIORS EEC': 'JSC INTERIORS LLC',
    'ZORRO MANAGEMENT, lit (HOUSE ACCOUNT)': 'ZORRO MANAGEMENT, LLC (HOUSE ACCOUNT)',
    'ZORRO MANAGEMENT, MX (HOUSE ACCOUNT)': 'ZORRO MANAGEMENT, LLC (HOUSE ACCOUNT)',
    'ZORRO DEVELOPMENT CORP': 'ZORRO DEVELOPMENT CORP.',
    'RE SORTS INTERNATIONAL. LLC': 'RESORTS INTERNATIONAL, LLC',
    'E.E.0': 'ENHANCED EDUCATION',

    # === Indyke variants ===
    'DARREN K. IN DYKE PLLC': 'DARREN K. INDYKE PLLC',
    'DARREN K. INDYKE PLLC •': 'DARREN K. INDYKE PLLC',
    'DAMN K. INDYKE PLLC': 'DARREN K. INDYKE PLLC',
    'DWRI \\ K. 1NDYKE FEM.': 'DARREN K. INDYKE PLLC',
    'DAVID1 MITCHELL 110': 'DAVID J. MITCHELL',
    "DAVID.' MITCHELL 110": 'DAVID J. MITCHELL',

    # === HBRK Associates variants ===
    'IMRE ASSOCIATES, INC': 'HBRK ASSOCIATES, INC',
    'RORK ASSOCIATES. INC': 'HBRK ASSOCIATES, INC',
    'IIBRK ASSOCIATES. INC': 'HBRK ASSOCIATES, INC',
    'IIBRK ASSOCIATES. INC..': 'HBRK ASSOCIATES, INC',
    'IIBRX ASSOCIATES, INC': 'HBRK ASSOCIATES, INC',
    'CIO IIBRK ASSOCIATES, INC.,': 'HBRK ASSOCIATES, INC',
    'GO IMRK ASSOCIATES. INC..': 'HBRK ASSOCIATES, INC',
    'OO IMRE ASSOCIATES. INC.,': 'HBRK ASSOCIATES, INC',
    'CO IIIIRK ASSOCIATES. INC': 'HBRK ASSOCIATES, INC',

    # === Bank names (normalize to consistent format) ===
    'RRSTBANK PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'FIRS1BANK PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'E1RSTB.ANK PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'FIRST BANK, PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'FIRST BANK PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'FIRST BANK OF PUERTO RICO': 'FIRSTBANK PUERTO RICO',
    'WELTS FARGO BANK, NA NC': 'WELLS FARGO BANK, NA',
    'WELLS FARGO BANK. NA NC': 'WELLS FARGO BANK, NA',
    'WELLS FARGO BANK. NA': 'WELLS FARGO BANK, NA',
    'JPNIORGAN CHASE BANK, NA AiC': 'JPMORGAN CHASE BANK, NA',
    'JPMOROAN CHASE BANK. NA': 'JPMORGAN CHASE BANK, NA',
    'MICHIGAN CHASE BANK, NA /VC 71163': 'JPMORGAN CHASE BANK, NA',
    'IP MORGAN CHASE AC 0381214683 ME': 'JPMORGAN CHASE BANK, NA',
    'A CHASE BANK NA NC': 'JPMORGAN CHASE BANK, NA',
    'CAPITAL ONE. NA NC LA': 'CAPITAL ONE, N.A.',
    '1ST BANK': 'FIRSTBANK PUERTO RICO',

    # === Known persons ===
    'CO RICHARD KAHN': 'RICHARD KAHN',
    '!SHIA KLEIN': 'ISHIA KLEIN',

    # === Compound entries (ENTITY + BANK merged by OCR) ===
    # These are FEDWIRE format: originator + bank on same line
    'HBRK ASSOCIATES INC SOUTHERN TRUST COMPANY INC': 'HBRK ASSOCIATES, INC',
    'DARREN K INDYKE PLLC SOUTHERN TRUST COMPANY INC': 'DARREN K. INDYKE PLLC',
    'OUTHERN TRUST COMPANY, SOUTHERN TRUST COMPANY INC': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUST COMPANY, SOUTHERN TRUST COMPANY INC': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUST COMPANY INC SOUTHERN TRUST COMPANY, INC': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUST COMPANY INC SOUTHERN TRUST COMPANY INC': 'SOUTHERN TRUST COMPANY, INC.',
    'SOUTHERN TRUST COMPANY INC INTERACTIVE BROKERS LLC': 'SOUTHERN TRUST COMPANY, INC.',
    'RYTANEE, LLC SOUTHERN TRUST COMPANY INC': 'PRYTANCE, LLC',
    'RICHARD KAHN SOUTHERN TRUST COMPANY INC': 'RICHARD KAHN',
    'FT REAL ESTATE, INC SOUTHERN FINANCIAL LLC': 'FT REAL ESTATE, INC',
    'F T REAL ESTATE, INC DAVID J MITCHELL': 'FT REAL ESTATE, INC',
    'F T REAL ESTATE, INC KELLERHALS FERGUSON KROBLIN PLLC': 'FT REAL ESTATE, INC',
    'F T REAL ESTATE, INC KFK GLOBAL INVESTMENTS': 'FT REAL ESTATE, INC',
    'F T REAL ESTATE, INC OSBORNE LANE CAPITAL LLC': 'FT REAL ESTATE, INC',
    'F T REAL ESTATE, INC': 'FT REAL ESTATE, INC',
    'FORTRESS VALUE RECOVERY FUND I LLC JEEPERS INC': 'FORTRESS VALUE RECOVERY FUND I LLC',
    'LSJ EMPLOYEES LLC LEE MCKENZIE CONSULTANTS LLC': 'LSJ EMPLOYEES LLC',
    'JEFFREY EPSTEIN LSJ EMPLOYEES LLC': 'JEFFREY EPSTEIN',
    'EFFREY EPSTEIN LSJ EMPLOYEES LLC': 'JEFFREY EPSTEIN',
    'IEFFREY EPSTEIN LSJ ES LLC': 'JEFFREY EPSTEIN',
    'EFFREY EPSTEIN ES LLC': 'JEFFREY EPSTEIN',
    'J EPSTEIN VIRGIN ISLANDS FOUNDATION JEPSTEIN VIRGIN ISLANDS FOUNDATION': 'J. EPSTEIN VIRGIN ISLANDS FOUNDATION',
    'EDUCATION ADVANCE ENHANCED EDUCATION': 'ENHANCED EDUCATION',
    'DAVID J MITCHELL F T REAL ESTATE, INC': 'DAVID J. MITCHELL',
    'LEON D BLACK DEBRA': 'LEON D. BLACK',
    'BLACK FAMILY PARTNERS. L.P.CO': 'BLACK FAMILY PARTNERS, L.P.',
    'LP. CO': 'BLACK FAMILY PARTNERS, L.P.',
    'GRATITUDE AMERICA, LTD. GRATITUDE AMERICA, LTD': 'GRATITUDE AMERICA, LTD',
    'GRATITUDE AMERICA LTD. GRATITUDE AMERICA, LTD': 'GRATITUDE AMERICA, LTD',
    'GRATITUDE AMERICA, LTD GRATITUDE AMERICA, LTD': 'GRATITUDE AMERICA, LTD',
    'JG A2680 JEFFREY E EPSTEIN CO': 'JEFFREY EPSTEIN',
    '$92533 HARLEQUIN DANE 1.1..C6': 'HARLEQUIN DANE LLC',
    'SEGE INC 6100 RED 1100': 'JEGE, INC',
    'TUDOR FURIRES FUND HA': 'TUDOR FUTURES FUND',
    'KYARA INVESTMENTS I': 'KYARA INVESTMENTS',

    # === Long bank-with-reference entries ===
    'BANK OF AMERICA. N.A . NY At t000.oepa co Other Debts INSURED AIRCRAFT': 'BANK OF AMERICA, N.A.',
    'BANK OF AMERICA, N.A, TX AC': 'BANK OF AMERICA, N.A.',
    'BANK OF AMERICA. N A.. NY PVC': 'BANK OF AMERICA, N.A.',
    'BANK OF AMERICA.N A.. NY NC': 'BANK OF AMERICA, N.A.',
    'BANK OF AMERICA, N.A. NY At 144': 'BANK OF AMERICA, N.A.',
    'BANK OF AMERICA, N.A., CA AC': 'BANK OF AMERICA, N.A.',
    "CMIl.'NK C 40611172 MORGAN STAN": 'CITIBANK / MORGAN STANLEY',
    'CITIBANK NC MORGAN STAN': 'CITIBANK / MORGAN STANLEY',

    # === Garbage entries (map to None-like marker) ===
    'IIIIII': None,
    'S': None,
    'NM': None,
    'THE': None,
    'TUE': None,
    'AND': None,
    'INC.': None,
    'Y, INC': None,
    'TEM, INC': None,
    'RICO': None,
    'RICO Aft': None,
}

# Regex-based normalizations for patterns that occur with variable text
ENTITY_REGEX_NORMALIZATIONS = [
    # "Jornemy Epstein Roils Royce Plc" → just "JEFFREY EPSTEIN" (payment description merged)
    (re.compile(r'^Jo\w*\s*(?:Epstein|epstein).*', re.IGNORECASE), 'JEFFREY EPSTEIN'),
    # "ILC\nJEFFREY EPSTEIN" → "JEFFREY EPSTEIN"
    (re.compile(r'^ILC\s*\n?\s*JEFFREY EPSTEIN', re.IGNORECASE), 'JEFFREY EPSTEIN'),
    # "AND\nKARYNA SIIUIJAK" or "AND\nAARYTA SIIUI IAK" → "KARYNA SHULIAK"
    (re.compile(r'^AND\s*\n?\s*[AK]\w*\s*S[HI]+U[LI]+[AJ]+K', re.IGNORECASE), 'KARYNA SHULIAK'),
    # "EMEND', INC ..." or "EMEND, INC ..." → "EMEND, INC"
    (re.compile(r"^EMEND[',]*\s*INC\b.*", re.IGNORECASE), 'EMEND, INC'),
    # "TO SI NTRUST BANK AC" → garbage
    (re.compile(r'^TO\s+SI\s+NTRUST', re.IGNORECASE), None),
    # "FM I \\I I ." → garbage
    (re.compile(r'^FM\s+I\s+\\', re.IGNORECASE), None),
    # "Cash Mgmt Tnfr..." → garbage (description leaked into entity name)
    (re.compile(r'^Cash\s+Mgmt\s+T', re.IGNORECASE), None),
    # "final collection and receipt..." → garbage
    (re.compile(r'^final\s+collection', re.IGNORECASE), None),
    # "BANK FORWARD At =CHERRINGT" → "BANK FORWARD"
    (re.compile(r'^BANK FORWARD\b.*', re.IGNORECASE), 'BANK FORWARD'),
    # Bank entries with A/C or NC suffixes
    (re.compile(r'^(FIRSTBANK PUERTO RICO)\s+(?:NC|A/?C|At)\s*.*'), 'FIRSTBANK PUERTO RICO'),
    (re.compile(r'^(FIRST BANK[,.]?\s*PUERTO RICO)\s+(?:NC|A/?C|At)\s*.*'), 'FIRSTBANK PUERTO RICO'),
    (re.compile(r'^(WELLS FARGO BANK[,.]?\s*NA)\s+(?:NC|A/?C|AC)\s*.*'), 'WELLS FARGO BANK, NA'),
    (re.compile(r'^(JPMORGAN CHASE\s*(?:BANK)?[,.]?\s*NA)\s+(?:NC|A/?C)\s*.*'), 'JPMORGAN CHASE BANK, NA'),
    (re.compile(r'^(BANCO POPULAR DE PUERTO RICO)\s+At\b.*'), 'BANCO POPULAR DE PUERTO RICO'),
    (re.compile(r'^(BANK OF AMERICA)[,.\s]+N[.]?\s*A[.]?.*', re.IGNORECASE), 'BANK OF AMERICA, N.A.'),
    # "II) BANK. NA a ILARLEQ" → "TD BANK, NA"
    (re.compile(r'^II\)\s*BANK', re.IGNORECASE), 'TD BANK, NA'),
]


def normalize_entity_name(raw):
    """Apply comprehensive post-processing normalization to an entity name."""
    if not raw:
        return raw

    # Exact match first
    if raw in ENTITY_NORMALIZATION:
        return ENTITY_NORMALIZATION[raw]

    # Case-insensitive exact match
    raw_upper = raw.upper().strip()
    for key, val in ENTITY_NORMALIZATION.items():
        if key and key.upper() == raw_upper:
            return val

    # Prefix match (for truncated OCR)
    for key, val in ENTITY_NORMALIZATION.items():
        if key and raw_upper.startswith(key.upper()) and len(key) > 5:
            return val

    # Regex patterns
    for pattern, replacement in ENTITY_REGEX_NORMALIZATIONS:
        if pattern.match(raw):
            return replacement

    return raw


def run_normalize(db):
    """Apply entity normalization to all DS10 tables."""
    print("=== Normalizing DS10 Entity Names ===")

    changes = 0

    # Collect all unique names from all tables
    all_names = set()
    for row in db.execute("SELECT DISTINCT sender FROM ds10_transactions WHERE sender IS NOT NULL"):
        all_names.add(row[0])
    for row in db.execute("SELECT DISTINCT receiver FROM ds10_transactions WHERE receiver IS NOT NULL"):
        all_names.add(row[0])
    for row in db.execute("SELECT DISTINCT account_holder FROM ds10_balances WHERE account_holder IS NOT NULL"):
        all_names.add(row[0])
    for row in db.execute("SELECT DISTINCT entity FROM ds10_positions WHERE entity IS NOT NULL"):
        all_names.add(row[0])

    print(f"  Found {len(all_names)} unique entity names across all tables")

    # Build update map
    updates = {}
    nulls = set()
    for name in all_names:
        normalized = normalize_entity_name(name)
        if normalized is None:
            nulls.add(name)
        elif normalized != name:
            updates[name] = normalized

    print(f"  {len(updates)} names will be normalized, {len(nulls)} will be NULLed")

    # Apply updates to transactions
    for old, new in updates.items():
        c = db.execute("UPDATE ds10_transactions SET sender = ? WHERE sender = ?", (new, old))
        changes += c.rowcount
        c = db.execute("UPDATE ds10_transactions SET receiver = ? WHERE receiver = ?", (new, old))
        changes += c.rowcount

    for old in nulls:
        c = db.execute("UPDATE ds10_transactions SET sender = NULL WHERE sender = ?", (old,))
        changes += c.rowcount
        c = db.execute("UPDATE ds10_transactions SET receiver = NULL WHERE receiver = ?", (old,))
        changes += c.rowcount

    # Apply to balances
    for old, new in updates.items():
        c = db.execute("UPDATE ds10_balances SET account_holder = ? WHERE account_holder = ?", (new, old))
        changes += c.rowcount

    for old in nulls:
        c = db.execute("UPDATE ds10_balances SET account_holder = NULL WHERE account_holder = ?", (old,))
        changes += c.rowcount

    # Apply to positions
    for old, new in updates.items():
        c = db.execute("UPDATE ds10_positions SET entity = ? WHERE entity = ?", (new, old))
        changes += c.rowcount

    for old in nulls:
        c = db.execute("UPDATE ds10_positions SET entity = NULL WHERE entity = ?", (old,))
        changes += c.rowcount

    db.commit()
    print(f"  DONE: {changes} total field updates applied")

    # Print remaining unique entities for review
    print("\n  Canonical entities after normalization:")
    for row in db.execute("""
        SELECT name, SUM(cnt), SUM(amt) FROM (
            SELECT sender AS name, COUNT(*) AS cnt, SUM(amount) AS amt FROM ds10_transactions WHERE sender IS NOT NULL GROUP BY sender
            UNION ALL
            SELECT receiver, COUNT(*), SUM(amount) FROM ds10_transactions WHERE receiver IS NOT NULL GROUP BY receiver
            UNION ALL
            SELECT account_holder, COUNT(*), MAX(balance) FROM ds10_balances WHERE account_holder IS NOT NULL GROUP BY account_holder
        ) GROUP BY name ORDER BY SUM(amt) DESC
    """):
        print(f"    {row[0][:60]:60s} {row[1]:5d} refs  ${row[2]:>15,.2f}")


# ---------------------------------------------------------------------------
# Query subcommands
# ---------------------------------------------------------------------------

def query_entity(db, entity, limit=50):
    """Query all transactions for an entity (as sender or receiver)."""
    print(f"=== Transactions for: {entity} ===\n")

    rows = db.execute("""
        SELECT tx_date, direction, amount, currency, sender, receiver, bank, reference, efta_id
        FROM ds10_transactions
        WHERE sender LIKE ? OR receiver LIKE ?
        ORDER BY tx_date
        LIMIT ?
    """, (f'%{entity}%', f'%{entity}%', limit)).fetchall()

    if not rows:
        print(f"  No transactions found matching '{entity}'")
        return

    total_out = 0
    total_in = 0
    for r in rows:
        sender = (r[4] or 'UNKNOWN')[:35]
        receiver = (r[5] or 'UNKNOWN')[:35]
        direction = r[1]
        if direction == 'outgoing':
            total_out += r[2] or 0
        else:
            total_in += r[2] or 0
        print(f"  {r[0]}  {direction:8s}  ${r[2]:>14,.2f} {r[3]}  {sender} -> {receiver}  [{r[8]}]")

    print(f"\n  Total: {len(rows)} transactions")
    print(f"  Outgoing: ${total_out:,.2f}  |  Incoming: ${total_in:,.2f}")


def query_date_range(db, start, end, limit=100):
    """Query transactions within a date range."""
    print(f"=== Transactions {start} to {end} ===\n")

    rows = db.execute("""
        SELECT tx_date, direction, amount, currency, sender, receiver, bank, efta_id
        FROM ds10_transactions
        WHERE tx_date >= ? AND tx_date <= ?
        ORDER BY tx_date, amount DESC
        LIMIT ?
    """, (start, end, limit)).fetchall()

    total = 0
    for r in rows:
        sender = (r[4] or 'UNKNOWN')[:35]
        receiver = (r[5] or 'UNKNOWN')[:35]
        total += r[2] or 0
        print(f"  {r[0]}  {r[1]:8s}  ${r[2]:>14,.2f}  {sender} -> {receiver}  [{r[7]}]")

    print(f"\n  {len(rows)} transactions, total ${total:,.2f}")


def query_large(db, min_amount, limit=100):
    """Query transactions above a minimum amount."""
    print(f"=== Transactions >= ${min_amount:,.2f} ===\n")

    rows = db.execute("""
        SELECT tx_date, direction, amount, currency, sender, receiver, bank, reference, efta_id
        FROM ds10_transactions
        WHERE amount >= ?
        ORDER BY amount DESC
        LIMIT ?
    """, (min_amount, limit)).fetchall()

    for r in rows:
        sender = (r[4] or 'UNKNOWN')[:35]
        receiver = (r[5] or 'UNKNOWN')[:35]
        print(f"  {r[0]}  {r[1]:8s}  ${r[2]:>14,.2f}  {sender} -> {receiver}  [{r[8]}]")

    print(f"\n  {len(rows)} transactions")


def query_counterparty(db, counterparty, limit=50):
    """Query transactions involving a specific counterparty (not the Epstein entity)."""
    print(f"=== Counterparty: {counterparty} ===\n")

    rows = db.execute("""
        SELECT tx_date, direction, amount, currency, sender, receiver, bank, efta_id
        FROM ds10_transactions
        WHERE (sender LIKE ? AND receiver NOT LIKE ?)
           OR (receiver LIKE ? AND sender NOT LIKE ?)
        ORDER BY amount DESC
        LIMIT ?
    """, (f'%{counterparty}%', f'%{counterparty}%',
          f'%{counterparty}%', f'%{counterparty}%', limit)).fetchall()

    for r in rows:
        sender = (r[4] or 'UNKNOWN')[:35]
        receiver = (r[5] or 'UNKNOWN')[:35]
        print(f"  {r[0]}  {r[1]:8s}  ${r[2]:>14,.2f}  {sender} -> {receiver}  [{r[7]}]")

    print(f"\n  {len(rows)} transactions")


def query_balances(db, entity):
    """Show balance history for an entity."""
    print(f"=== Balance History: {entity} ===\n")

    rows = db.execute("""
        SELECT balance_date, account_holder, account_type, account_number, balance, efta_id
        FROM ds10_balances
        WHERE account_holder LIKE ?
        ORDER BY account_holder, balance_date
    """, (f'%{entity}%',)).fetchall()

    if not rows:
        print(f"  No balances found matching '{entity}'")
        return

    current_holder = None
    for r in rows:
        if r[1] != current_holder:
            current_holder = r[1]
            print(f"\n  --- {current_holder} ({r[2] or 'unknown'}) ---")
        print(f"    {r[0]}  ${r[4]:>14,.2f}  acct:{r[3] or '?':>10s}  [{r[5]}]")


def query_entities_summary(db):
    """List all canonical entities with transaction and balance summaries."""
    print("=== DS10 Entity Summary ===\n")

    print("  --- By Transaction Volume (Outgoing) ---")
    rows = db.execute("""
        SELECT sender, COUNT(*), SUM(amount), MIN(tx_date), MAX(tx_date)
        FROM ds10_transactions
        WHERE direction = 'outgoing' AND sender IS NOT NULL
        GROUP BY sender ORDER BY SUM(amount) DESC
    """).fetchall()
    for r in rows:
        print(f"    {r[0][:50]:50s} {r[1]:4d} tx  ${r[2]:>15,.2f}  ({r[3]} to {r[4]})")

    print("\n  --- By Transaction Volume (Incoming) ---")
    rows = db.execute("""
        SELECT receiver, COUNT(*), SUM(amount), MIN(tx_date), MAX(tx_date)
        FROM ds10_transactions
        WHERE direction = 'incoming' AND receiver IS NOT NULL
        GROUP BY receiver ORDER BY SUM(amount) DESC
    """).fetchall()
    for r in rows:
        print(f"    {r[0][:50]:50s} {r[1]:4d} tx  ${r[2]:>15,.2f}  ({r[3]} to {r[4]})")

    print("\n  --- By Peak Balance ---")
    rows = db.execute("""
        SELECT account_holder, COUNT(*), MAX(balance), MIN(balance_date), MAX(balance_date)
        FROM ds10_balances WHERE account_holder IS NOT NULL
        GROUP BY account_holder ORDER BY MAX(balance) DESC
    """).fetchall()
    for r in rows:
        print(f"    {r[0][:50]:50s} {r[1]:4d} obs  max ${r[2]:>15,.2f}  ({r[3]} to {r[4]})")

    print("\n  --- By Portfolio Value ---")
    rows = db.execute("""
        SELECT entity, COUNT(*), MAX(value), MIN(position_date), MAX(position_date)
        FROM ds10_positions WHERE entity IS NOT NULL AND investment = 'TOTAL PORTFOLIO'
        GROUP BY entity ORDER BY MAX(value) DESC
    """).fetchall()
    for r in rows:
        print(f"    {r[0][:50]:50s} {r[1]:4d} obs  max ${r[2]:>15,.2f}  ({r[3]} to {r[4]})")


def query_flows(db, limit=30):
    """Show entity-to-entity money flow summary (aggregated)."""
    print("=== DS10 Money Flow Summary (Entity-to-Entity) ===\n")

    rows = db.execute("""
        SELECT sender, receiver, COUNT(*), SUM(amount), MIN(tx_date), MAX(tx_date)
        FROM ds10_transactions
        WHERE sender IS NOT NULL AND receiver IS NOT NULL
          AND sender != 'INTERNAL TRANSFER' AND receiver != 'INTERNAL TRANSFER'
          AND sender != 'UNKNOWN' AND receiver != 'UNKNOWN'
        GROUP BY sender, receiver
        ORDER BY SUM(amount) DESC
        LIMIT ?
    """, (limit,)).fetchall()

    for r in rows:
        sender = r[0][:30]
        receiver = r[1][:30]
        print(f"  {sender:30s} -> {receiver:30s}  {r[2]:3d} tx  ${r[3]:>15,.2f}  ({r[4]} to {r[5]})")


def main():
    parser = argparse.ArgumentParser(description='Parse DS10 Deutsche Bank financial documents')
    parser.add_argument('command', choices=[
        'create-tables', 'parse-statements', 'parse-wires', 'parse-fedwire',
        'parse-positions', 'parse-wm-transactions', 'parse-regulation-e',
        'parse-all', 'report', 'normalize',
        'query', 'balances', 'entities', 'flows',
    ])
    parser.add_argument('--limit', type=int, default=None, help='Limit number of documents/results')
    parser.add_argument('--entity', type=str, default=None, help='Entity name to query')
    parser.add_argument('--counterparty', type=str, default=None, help='Counterparty name')
    parser.add_argument('--date-start', type=str, default=None, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--date-end', type=str, default=None, help='End date (YYYY-MM-DD)')
    parser.add_argument('--amount-min', type=float, default=None, help='Minimum transaction amount')

    args = parser.parse_args()
    db = get_db()

    if args.command == 'create-tables':
        create_tables(db)
    elif args.command == 'parse-statements':
        create_tables(db)
        run_parse_statements(db, args.limit)
    elif args.command == 'parse-fedwire':
        create_tables(db)
        run_parse_fedwire(db, args.limit)
    elif args.command == 'parse-wm-transactions':
        create_tables(db)
        run_parse_wm_transactions(db, args.limit)
    elif args.command == 'parse-positions':
        create_tables(db)
        run_parse_positions(db, args.limit)
    elif args.command == 'parse-regulation-e':
        create_tables(db)
        run_parse_regulation_e(db, args.limit)
    elif args.command == 'parse-wires':
        # Alias: run FEDWIRE + Reg E + WM transactions
        create_tables(db)
        run_parse_fedwire(db, args.limit)
        run_parse_wm_transactions(db, args.limit)
        run_parse_regulation_e(db, args.limit)
    elif args.command == 'parse-all':
        create_tables(db)
        run_parse_all(db, args.limit)
    elif args.command == 'report':
        report(db)
    elif args.command == 'normalize':
        run_normalize(db)
    elif args.command == 'query':
        if args.entity:
            query_entity(db, args.entity, args.limit or 50)
        elif args.date_start and args.date_end:
            query_date_range(db, args.date_start, args.date_end, args.limit or 100)
        elif args.amount_min:
            query_large(db, args.amount_min, args.limit or 100)
        elif args.counterparty:
            query_counterparty(db, args.counterparty, args.limit or 50)
        else:
            print("ERROR: query requires --entity, --date-start/--date-end, --amount-min, or --counterparty")
            sys.exit(1)
    elif args.command == 'balances':
        if args.entity:
            query_balances(db, args.entity)
        else:
            # Show all entities with balances
            query_balances(db, '%')
    elif args.command == 'entities':
        query_entities_summary(db)
    elif args.command == 'flows':
        query_flows(db, args.limit or 30)

    db.close()


if __name__ == '__main__':
    main()
