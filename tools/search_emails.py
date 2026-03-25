#!/usr/bin/env python3
"""
Comprehensive search of Epstein archive email datasets:
  1. jeeproject_yahoo/ - 13,011 .eml files
  2. ehud_barak_emails/ - 1,411 files (.html + .meta + some .eml)

Parses all emails, extracts headers and body text, searches for
high-priority terms, and generates summary statistics.
"""

import email
import email.policy
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from email import policy
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import email_archive_root

# ── Configuration ──────────────────────────────────────────────

BASE_DIR = email_archive_root()
YAHOO_DIR = BASE_DIR / "jeeproject_yahoo"
BARAK_DIR = BASE_DIR / "ehud_barak_emails"

# Search terms: list of (label, [patterns]) where patterns are case-insensitive
# For "Barr", we use word boundary matching to avoid false positives
SEARCH_TERMS = [
    ("Halper / Stefan Halper",       [r"halper", r"stefan\s+halper"]),
    ("Deripaska / Oleg Deripaska",   [r"deripaska", r"oleg\s+deripaska"]),
    ("Churkin / Vitaly Churkin",     [r"churkin", r"vitaly\s+churkin"]),
    ("Rod-Larsen / Terje",           [r"rod[\-\s]?larsen", r"\bterje\b"]),
    ("Bannon / Steve Bannon",        [r"\bbannon\b", r"steve\s+bannon"]),
    ("Barr (William Barr context)",  [r"\bwilliam\s+barr\b", r"\bbarr\b(?!.*(?:amazon|unsubscrib|barrett|barrage|barrel|barrier|barring|barrister|barron))"]),
    ("ProtonMail / Proton",          [r"protonmail", r"\bproton\b"]),
    ("Rybolovlev",                   [r"rybolovlev"]),
    ("MBS / bin Salman / Mohammed bin", [r"\bmbs\b", r"bin\s+salman", r"mohammed\s+bin"]),
    ("SoftBank / Masayoshi / Masa Son", [r"softbank", r"masayoshi", r"masa\s+son\b"]),
    ("Wolff / Michael Wolff",        [r"\bwolff\b", r"michael\s+wolff"]),
    ("Weingarten / Reid Weingarten", [r"weingarten", r"reid\s+weingarten"]),
    ("Karp / Brad Karp",             [r"\bkarp\b", r"brad\s+karp"]),
    ("Ruemmler / Kathy Ruemmler",    [r"ruemmler", r"kathy\s+ruemmler"]),
    ("Zeitlin / Jide Zeitlin",       [r"\bzeitlin\b", r"jide\s+zeitlin"]),
    ("OneWeb / Greg Wyler",          [r"oneweb", r"greg\s+wyler"]),
    ("Abraaj / Arif Naqvi",          [r"abraaj", r"arif\s+naqvi"]),
    ("Woody Allen",                  [r"woody\s+allen"]),
    ("Dershowitz",                   [r"dershowitz"]),
    ("Gates / Bill Gates",           [r"\bgates\b", r"bill\s+gates"]),
]

# Pre-compile all patterns
COMPILED_TERMS = []
for label, patterns in SEARCH_TERMS:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    COMPILED_TERMS.append((label, compiled))


# ── HTML stripping ─────────────────────────────────────────────

class HTMLTextExtractor(HTMLParser):
    """Strip HTML tags, decode entities, return plain text."""
    def __init__(self):
        super().__init__()
        self.result = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._skip = False
        if tag in ('br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.result.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self.result.append(data)

    def handle_entityref(self, name):
        from html import unescape
        self.result.append(unescape(f'&{name};'))

    def handle_charref(self, name):
        from html import unescape
        self.result.append(unescape(f'&#{name};'))

    def get_text(self):
        return ''.join(self.result)


def strip_html(html_str):
    """Convert HTML to plain text."""
    if not html_str:
        return ""
    try:
        extractor = HTMLTextExtractor()
        extractor.feed(html_str)
        text = extractor.get_text()
        # Collapse multiple whitespace/newlines
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
    except Exception:
        # Fallback: simple regex strip
        text = re.sub(r'<[^>]+>', ' ', html_str)
        return re.sub(r'\s+', ' ', text).strip()


# ── Email record structure ─────────────────────────────────────

def make_record(filepath, from_addr, to_addr, cc_addr, subject, date_str, body_text, source_type):
    return {
        'filepath': str(filepath),
        'from': (from_addr or '').strip(),
        'to': (to_addr or '').strip(),
        'cc': (cc_addr or '').strip(),
        'subject': (subject or '').strip(),
        'date': (date_str or '').strip(),
        'body': (body_text or '').strip(),
        'source': source_type,
    }


# ── Parser: .eml files ────────────────────────────────────────

def parse_eml_file(filepath):
    """Parse a standard .eml file. Returns a record dict or None."""
    try:
        raw = filepath.read_bytes()
    except Exception as e:
        return None

    # Try multiple encodings
    text = None
    for enc in ['utf-8', 'latin-1', 'cp1252', 'ascii']:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode('utf-8', errors='replace')

    try:
        msg = email.message_from_string(text, policy=email.policy.default)
    except Exception:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None

    from_addr = msg.get('From', '')
    to_addr = msg.get('To', '')
    cc_addr = msg.get('Cc', '')
    subject = msg.get('Subject', '')
    date_str = msg.get('Date', '')

    # Extract body
    body_parts = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == 'text/plain':
                    payload = get_payload_text(part)
                    if payload:
                        body_parts.append(payload)
                elif ctype == 'text/html':
                    payload = get_payload_text(part)
                    if payload:
                        body_parts.append(strip_html(payload))
        else:
            ctype = msg.get_content_type()
            payload = get_payload_text(msg)
            if payload:
                if ctype == 'text/html':
                    body_parts.append(strip_html(payload))
                else:
                    body_parts.append(payload)
    except Exception:
        pass

    body_text = '\n'.join(body_parts)
    return make_record(filepath, from_addr, to_addr, cc_addr, subject, date_str, body_text, 'eml')


def get_payload_text(part):
    """Safely extract text payload from an email part."""
    try:
        payload = part.get_content()
        if isinstance(payload, str):
            return payload
        if isinstance(payload, bytes):
            for enc in ['utf-8', 'latin-1', 'cp1252']:
                try:
                    return payload.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return payload.decode('utf-8', errors='replace')
    except Exception:
        try:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    return payload.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    return payload.decode('utf-8', errors='replace')
        except Exception:
            pass
    return ""


# ── Parser: Barak .html files ─────────────────────────────────

def parse_barak_html(filepath):
    """Parse a Barak-format .html email export file."""
    try:
        raw = filepath.read_bytes()
        for enc in ['utf-8', 'latin-1', 'cp1252']:
            try:
                html = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            html = raw.decode('utf-8', errors='replace')
    except Exception:
        return None

    from_addr = extract_html_field(html, 'from_text')
    to_addr = extract_html_field(html, 'to_text')
    cc_addr = extract_html_field(html, 'cc_text')
    subject = extract_html_field(html, 'subject_text') or extract_subject_from_caption(html)
    date_str = extract_date_from_html(html)

    # Extract body from msg_body div
    body_match = re.search(r'<div\s+id="msg_body"[^>]*>(.*)', html, re.DOTALL | re.IGNORECASE)
    body_text = ""
    if body_match:
        body_text = strip_html(body_match.group(1))
    else:
        # Fallback: strip entire HTML
        body_text = strip_html(html)

    return make_record(filepath, from_addr, to_addr, cc_addr, subject, date_str, body_text, 'barak_html')


def extract_html_field(html, field_id):
    """Extract text content from an element with given id in the Barak HTML format."""
    pattern = rf'id="{field_id}"[^>]*>(.*?)</td>'
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if match:
        return strip_html(match.group(1)).strip()
    return ""


def extract_subject_from_caption(html):
    """Extract subject from the row after subject_caption."""
    # subject is in the next <td> after subject_caption
    pattern = r'id="subject_caption"[^>]*>[^<]*</td>\s*<td[^>]*>(.*?)</td>'
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if match:
        return strip_html(match.group(1)).strip()
    return ""


def extract_date_from_html(html):
    """Extract date from Barak HTML. It's in a td after date_caption."""
    pattern = r'id="date_caption"[^>]*>[^<]*</td>\s*<td[^>]*>(.*?)</td>'
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if match:
        return strip_html(match.group(1)).strip()
    return ""


# ── Parser: Barak .meta files ─────────────────────────────────

def parse_barak_meta(filepath):
    """Parse a Barak .eml.meta JSON sidecar file."""
    try:
        raw = filepath.read_bytes()
        data = json.loads(raw)
    except Exception:
        return None

    from_addr = data.get('sender', '')
    subject = data.get('subject', '')
    date_val = data.get('date', '')
    if isinstance(date_val, (int, float)):
        try:
            date_str = datetime.utcfromtimestamp(date_val).strftime('%Y-%m-%d %H:%M:%S UTC')
        except Exception:
            date_str = str(date_val)
    else:
        date_str = str(date_val)

    # .meta files have limited info; body may be in 'metadata' field
    metadata = data.get('metadata', '')
    path = data.get('Path', '')

    body_text = f"{metadata}"

    return make_record(filepath, from_addr, '', '', subject, date_str, body_text, 'barak_meta')


# ── Search logic ───────────────────────────────────────────────

def search_record(record):
    """Search all fields of a record against all terms. Returns list of matching term labels."""
    # Build searchable text blob
    search_text = ' '.join([
        record.get('from', ''),
        record.get('to', ''),
        record.get('cc', ''),
        record.get('subject', ''),
        record.get('body', ''),
    ])

    matches = []
    for label, compiled_patterns in COMPILED_TERMS:
        for pat in compiled_patterns:
            if pat.search(search_text):
                matches.append(label)
                break  # Only count each term group once per email
    return matches


# ── Address normalization for statistics ───────────────────────

def extract_email_addresses(addr_str):
    """Extract individual email addresses from a header string."""
    if not addr_str:
        return []
    # Find all email-like patterns
    emails = re.findall(r'[\w\.\+\-]+@[\w\.\-]+\.\w+', addr_str, re.IGNORECASE)
    # Also try to extract display names
    results = []
    for e in emails:
        results.append(e.lower())
    if not results and addr_str.strip():
        # Just return the raw string cleaned up
        results.append(addr_str.strip().lower()[:80])
    return results


# ── Main processing ───────────────────────────────────────────

def process_all():
    all_records = []
    errors = []

    # ── Phase 1: jeeproject_yahoo .eml files ──
    print("=" * 80)
    print("PHASE 1: Parsing jeeproject_yahoo directory (.eml files)")
    print("=" * 80)

    yahoo_files = sorted(YAHOO_DIR.glob("*.eml"))
    print(f"Found {len(yahoo_files)} .eml files")

    yahoo_records = []
    for i, fp in enumerate(yahoo_files):
        if (i + 1) % 2000 == 0:
            print(f"  Parsed {i+1}/{len(yahoo_files)}...")
        rec = parse_eml_file(fp)
        if rec:
            yahoo_records.append(rec)
        else:
            errors.append(f"PARSE_FAIL: {fp.name}")

    print(f"Successfully parsed: {len(yahoo_records)} / {len(yahoo_files)}")
    print(f"Parse failures: {len(yahoo_files) - len(yahoo_records)}")
    all_records.extend(yahoo_records)

    # ── Phase 2: ehud_barak_emails ──
    print()
    print("=" * 80)
    print("PHASE 2: Parsing ehud_barak_emails directory")
    print("=" * 80)

    barak_html_files = sorted(BARAK_DIR.glob("*.html"))
    barak_meta_files = sorted(BARAK_DIR.glob("*.meta"))
    barak_eml_files = sorted(BARAK_DIR.glob("*.eml"))

    print(f"Found: {len(barak_html_files)} .html, {len(barak_meta_files)} .meta, {len(barak_eml_files)} .eml")

    barak_records = []

    # Parse .html files
    for fp in barak_html_files:
        rec = parse_barak_html(fp)
        if rec:
            barak_records.append(rec)
        else:
            errors.append(f"PARSE_FAIL_HTML: {fp.name}")

    # Parse .meta files (may overlap with .html but contain different metadata)
    meta_ids_seen = set()
    for fp in barak_meta_files:
        rec = parse_barak_meta(fp)
        if rec:
            # Check if we already have this from .html
            # Meta files are named like 0000000343-Re_ tb Paris, JE.eml.meta
            # The numeric prefix is the ID
            file_id = fp.name.split('-')[0] if '-' in fp.name else fp.name
            if file_id not in meta_ids_seen:
                meta_ids_seen.add(file_id)
                barak_records.append(rec)
            else:
                errors.append(f"DUPE_META: {fp.name}")

    # Parse .eml files in barak dir
    for fp in barak_eml_files:
        rec = parse_eml_file(fp)
        if rec:
            barak_records.append(rec)
        else:
            errors.append(f"PARSE_FAIL_EML: {fp.name}")

    print(f"Barak records parsed: {len(barak_records)}")
    all_records.extend(barak_records)

    # ── Phase 3: Search all records ──
    print()
    print("=" * 80)
    print("PHASE 3: Searching all records for high-priority terms")
    print("=" * 80)

    term_hits = defaultdict(list)  # label -> list of records

    for rec in all_records:
        matches = search_record(rec)
        for label in matches:
            term_hits[label].append(rec)

    # ── Phase 4: Output results ──
    print()
    print("=" * 80)
    print("SEARCH RESULTS")
    print("=" * 80)

    total_terms_with_hits = 0
    for label, compiled_patterns in COMPILED_TERMS:
        hits = term_hits.get(label, [])
        if hits:
            total_terms_with_hits += 1
            print()
            print(f"{'─' * 70}")
            print(f"TERM: {label}")
            print(f"HITS: {len(hits)}")
            print(f"{'─' * 70}")
            for h in hits:
                print()
                print(f"  File: {h['filepath']}")
                print(f"  Source: {h['source']}")
                print(f"  From: {h['from']}")
                print(f"  To: {h['to']}")
                if h['cc']:
                    print(f"  CC: {h['cc']}")
                print(f"  Subject: {h['subject']}")
                print(f"  Date: {h['date']}")
                body_preview = h['body'][:500].replace('\n', ' | ')
                print(f"  Body (first 500 chars): {body_preview}")
                print(f"  {'.' * 50}")
        else:
            print(f"\n  TERM: {label} -- NO HITS")

    print()
    print(f"\nTerms with hits: {total_terms_with_hits} / {len(COMPILED_TERMS)}")

    # ── Phase 5: Summary statistics ──
    print()
    print("=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)

    print(f"\nTotal emails parsed: {len(all_records)}")
    print(f"  - Yahoo (jeeproject): {len(yahoo_records)}")
    print(f"  - Barak: {len(barak_records)}")
    print(f"  - Parse errors: {len(errors)}")

    # Date range
    dates = []
    for rec in all_records:
        d = rec.get('date', '')
        if d:
            # Try to parse various date formats
            for fmt in [
                '%a, %d %b %Y %H:%M:%S %z',
                '%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%d %H:%M:%S %Z',
                '%Y-%m-%d %H:%M:%S',
                '%m/%d/%Y %H:%M:%S %p %Z',
                '%m/%d/%Y %I:%M:%S %p %Z',
                '%d %b %Y %H:%M:%S %z',
            ]:
                try:
                    dt = datetime.strptime(d.strip(), fmt)
                    dates.append(dt)
                    break
                except ValueError:
                    continue

    if dates:
        # Normalize: strip timezone info for sorting
        naive_dates = []
        for d in dates:
            if d.tzinfo is not None:
                naive_dates.append(d.replace(tzinfo=None))
            else:
                naive_dates.append(d)
        naive_dates.sort()
        print(f"\nDate range: {naive_dates[0].strftime('%Y-%m-%d')} to {naive_dates[-1].strftime('%Y-%m-%d')}")
        print(f"  Parseable dates: {len(naive_dates)} / {len(all_records)}")
    else:
        print("\nDate range: Could not parse any dates")

    # Top senders
    sender_counter = Counter()
    for rec in all_records:
        addrs = extract_email_addresses(rec.get('from', ''))
        for a in addrs:
            sender_counter[a] += 1

    print(f"\nTOP 20 SENDERS:")
    for addr, count in sender_counter.most_common(20):
        print(f"  {count:>5}  {addr}")

    # Top recipients
    recip_counter = Counter()
    for rec in all_records:
        for field in ['to', 'cc']:
            addrs = extract_email_addresses(rec.get(field, ''))
            for a in addrs:
                recip_counter[a] += 1

    print(f"\nTOP 20 RECIPIENTS:")
    for addr, count in recip_counter.most_common(20):
        print(f"  {count:>5}  {addr}")

    # Top subjects (cleaned)
    subject_counter = Counter()
    for rec in all_records:
        subj = rec.get('subject', '').strip()
        if subj:
            # Normalize Re: Fwd: prefixes for grouping
            clean = re.sub(r'^(Re:\s*|Fwd?:\s*|Fw:\s*)+', '', subj, flags=re.IGNORECASE).strip()
            if clean:
                subject_counter[clean] += 1

    print(f"\nTOP 20 SUBJECTS (normalized, Re:/Fwd: stripped):")
    for subj, count in subject_counter.most_common(20):
        print(f"  {count:>5}  {subj[:100]}")

    # Extra: Barr filter - show all Barr hits for manual review since it's noisy
    barr_hits = term_hits.get("Barr (William Barr context)", [])
    if barr_hits:
        print()
        print("=" * 80)
        print("BARR HITS - DETAILED REVIEW (may include false positives)")
        print("=" * 80)
        for h in barr_hits:
            # Find the actual matching context
            search_text = ' '.join([h.get('from',''), h.get('to',''), h.get('cc',''),
                                     h.get('subject',''), h.get('body','')])
            # Find where "barr" appears
            barr_contexts = []
            for m in re.finditer(r'(?i)\b\w*barr\w*\b', search_text):
                start = max(0, m.start() - 40)
                end = min(len(search_text), m.end() + 40)
                ctx = search_text[start:end].replace('\n', ' ')
                barr_contexts.append(f"...{ctx}...")
            print(f"\n  File: {os.path.basename(h['filepath'])}")
            print(f"  Subject: {h['subject']}")
            print(f"  Contexts: {'; '.join(barr_contexts[:3])}")

    if errors[:20]:
        print()
        print(f"\nFirst 20 parse errors (of {len(errors)}):")
        for e in errors[:20]:
            print(f"  {e}")

    return all_records, term_hits


if __name__ == '__main__':
    process_all()
