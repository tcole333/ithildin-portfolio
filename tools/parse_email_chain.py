#!/usr/bin/env python3
"""Parse email chains from DOJ Vol 11 OCR text into structured messages.

DOJ emails follow patterns like:
    From:\n<sender>\nSent:\n<date>\nTo:\n<recipient>\nSubject:\n<subject>\n<body>

Forwarding chains nest with:
    Begin forwarded message:\nFrom: <sender>\nDate: <date>\nSubject: <subject>

The parser splits OCR text on From: markers, extracts structured fields,
and handles common OCR artifacts (=continuation, =br>, truncated names).

Usage:
    python tools/parse_email_chain.py EFTA02452433
    python tools/parse_email_chain.py EFTA02454291 --json
    python tools/parse_email_chain.py --text "From:\nJohn Doe\nSent:..."
"""
import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import doj_db_path

DOCUMENTS_DB = doj_db_path()


@dataclass
class EmailMessage:
    sender: str
    recipients: list[str]
    date: str
    subject: str
    body: str
    chain_position: int  # 0 = outermost/newest, N = original
    raw_text: str
    confidence: str = "high"  # high, medium, low — parser confidence


def clean_ocr_field(text):
    """Clean OCR artifacts from a field value."""
    if not text:
        return ""
    # Quoted-Printable continuation
    text = re.sub(r'=\n', '', text)
    text = re.sub(r'=br>', ' ', text, flags=re.IGNORECASE)
    # HTML remnants
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def clean_ocr_body(text):
    """Clean OCR artifacts from body text, preserving paragraph structure."""
    if not text:
        return ""
    text = re.sub(r'=\n', '', text)
    text = re.sub(r'=br>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    # Remove email footer boilerplate
    footer_patterns = [
        r'(?:please note|This email and any files|The infor.?ation contained).*$',
        r'(?:conversation-id|date-last-viewed|date-received|flags|gmail-label-ids|remote-id)\s+\d+.*$',
        r'EFTA_R\d+_\d+\s*\nEFTA\d+\s*$',
    ]
    for pat in footer_patterns:
        text = re.sub(pat, '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _split_on_from_markers(text):
    """Split OCR text into message segments using From: markers.

    Returns list of (raw_segment, is_forwarded) tuples, ordered outermost first.
    """
    # Patterns that indicate a message boundary
    # Pattern 1: "From:\n<name>" (DOJ standard)
    # Pattern 2: "From: <name>" after "Begin forwarded message:" or "wrote:"
    # Pattern 3: "Fran:\n<name>" (OCR typo)

    # First, find all From: positions
    markers = []

    # Standard DOJ format: "From:\n" or "Fran:\n" at start or after blank lines
    for m in re.finditer(r'(?:^|\n)(?:From|Fran):\s*\n', text):
        markers.append((m.start(), False))

    # Forwarded/inline: "From: " after "Begin forwarded" or "wrote:"
    for m in re.finditer(r'(?:Begin forwarded message:|wrote:)\s*\n\s*From:\s+', text, re.IGNORECASE):
        # Find the actual From: position
        from_pos = text.index('From:', m.start() + 5)
        markers.append((from_pos, True))

    # Inline reply: "On <date>, <name> wrote:"
    for m in re.finditer(r'\nOn\s+\w+.*?wrote:\s*\n', text, re.DOTALL):
        markers.append((m.start(), True))

    if not markers:
        return [(text, False)]

    # Sort by position
    markers.sort(key=lambda x: x[0])

    # Deduplicate markers that are very close together
    deduped = [markers[0]]
    for pos, fwd in markers[1:]:
        if pos - deduped[-1][0] > 10:
            deduped.append((pos, fwd))
    markers = deduped

    segments = []
    for i, (pos, fwd) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        seg = text[pos:end].strip()
        if seg:
            segments.append((seg, fwd))

    return segments


def _is_header_line(line):
    """Check if a line starts a known email header field."""
    lower = line.strip().lower()
    return (lower.startswith(('from:', 'fran:', 'to:', 'cc:', 'subject:', 'date:'))
            or re.match(r'^sent:\s*', lower)
            or re.match(r'^sent\s*$', lower))


def _parse_doj_segment(segment):
    """Parse a DOJ-format email segment into fields.

    Handles patterns like:
        From:\n<sender>\nSent:\n<date>\nTo:\n<recipient>\nSubject:\n<subject>\n<body>

    OCR often splits header values across multiple lines. We collect continuation
    lines until we see the next header keyword or the body starts.
    """
    sender = ""
    recipients = []
    date = ""
    subject = ""
    body = ""

    lines = segment.split('\n')
    i = 0
    current_field = None
    # Track whether we're still in headers (haven't seen body yet)
    in_headers = True

    while i < len(lines):
        line = lines[i].strip()
        lower = line.lower()

        # Check for header field transitions
        if in_headers and lower.startswith(('from:', 'fran:')):
            current_field = 'from'
            val = re.sub(r'^(?:From|Fran):\s*', '', line, flags=re.IGNORECASE)
            if val:
                sender = val
        elif in_headers and re.match(r'^sent:?\s*$', lower):
            current_field = 'sent'
        elif in_headers and re.match(r'^sent:\s+\S', lower):
            current_field = 'sent'
            val = re.sub(r'^Sent:\s*', '', line, flags=re.IGNORECASE)
            date = val
        elif in_headers and lower.startswith('date:'):
            current_field = 'date'
            val = re.sub(r'^Date:\s*', '', line, flags=re.IGNORECASE)
            if val:
                date = val
        elif in_headers and lower.startswith('to:'):
            current_field = 'to'
            val = re.sub(r'^To:\s*', '', line, flags=re.IGNORECASE)
            if val:
                recipients = [r.strip() for r in val.split(',') if r.strip()]
        elif in_headers and lower.startswith('cc:'):
            current_field = 'cc'
        elif in_headers and lower.startswith('subject:'):
            current_field = 'subject'
            val = re.sub(r'^Subject:\s*', '', line, flags=re.IGNORECASE)
            if val:
                subject = val
        elif in_headers and current_field and not line:
            # Blank line in headers — skip (OCR artifact)
            pass
        elif in_headers and current_field:
            # Continuation line — check if it's a value for the current field
            # or if we should switch to body
            if current_field == 'from' and not sender:
                sender = line
            elif current_field == 'sent' and not date:
                date = line
            elif current_field == 'date' and not date:
                date = line
            elif current_field == 'to' and not recipients:
                recipients = [r.strip() for r in line.split(',') if r.strip()]
            elif current_field == 'subject' and not subject:
                subject = line
            elif current_field in ('sent', 'date') and date:
                # Multi-line date (OCR split) — append if it looks like a continuation
                if re.match(r'^[\d.:]+', line) and len(date) < 40:
                    date = date + ' ' + line
                elif not _is_header_line(lines[i] if i < len(lines) else ''):
                    # Not a header, not a date continuation — body starts
                    in_headers = False
                    body = '\n'.join(lines[i:])
                    break
            else:
                # We've filled the current field and this isn't a new header
                # Check if next lines have more headers
                if _is_header_line(line):
                    continue  # Will be caught by header checks on next iteration
                else:
                    in_headers = False
                    body = '\n'.join(lines[i:])
                    break
        else:
            # Body text
            in_headers = False
            body = '\n'.join(lines[i:])
            break

        i += 1

    # Clean fields
    sender = clean_ocr_field(sender)
    # Strip email addresses from sender for cleaner display
    sender_clean = re.sub(r'<[^>]+>', '', sender).strip()
    if sender_clean:
        sender = sender_clean

    recipients = [clean_ocr_field(r) for r in recipients]
    recipients = [re.sub(r'<[^>]+>', '', r).strip() for r in recipients if r]
    date = clean_ocr_field(date)
    subject = clean_ocr_field(subject)
    body = clean_ocr_body(body)

    return sender, recipients, date, subject, body


def parse_email_chain(ocr_text):
    """Parse OCR text into a list of EmailMessage objects.

    Returns messages ordered by chain_position (0 = outermost/newest).
    Sets confidence flag based on parsing quality.
    """
    if not ocr_text or len(ocr_text.strip()) < 20:
        return []

    segments = _split_on_from_markers(ocr_text)
    messages = []

    for i, (segment, is_forwarded) in enumerate(segments):
        sender, recipients, date, subject, body = _parse_doj_segment(segment)

        # Determine confidence
        confidence = "high"
        if not sender:
            confidence = "low"
        elif not date and not subject:
            confidence = "medium"
        elif len(segments) > 3:
            confidence = "medium"  # Longer chains are harder to parse

        msg = EmailMessage(
            sender=sender,
            recipients=recipients,
            date=date,
            subject=subject,
            body=body,
            chain_position=i,
            raw_text=segment,
            confidence=confidence,
        )
        messages.append(msg)

    return messages


def get_ocr_text(efta_id):
    """Retrieve OCR text for an EFTA ID from documents.db."""
    db = sqlite3.connect(DOCUMENTS_DB)
    db.row_factory = sqlite3.Row
    doc = db.execute("SELECT ocr_text FROM documents WHERE bates_id = ?", (efta_id,)).fetchone()
    db.close()
    if doc:
        return doc["ocr_text"]
    return None


def main():
    parser = argparse.ArgumentParser(description="Parse email chains from DOJ OCR text")
    parser.add_argument("efta_id", nargs="?", help="EFTA ID to parse")
    parser.add_argument("--text", help="Raw OCR text to parse (instead of EFTA lookup)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.text:
        ocr_text = args.text
    elif args.efta_id:
        ocr_text = get_ocr_text(args.efta_id)
        if not ocr_text:
            print(f"EFTA {args.efta_id} not found in documents.db")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    messages = parse_email_chain(ocr_text)

    if args.json:
        print(json.dumps([asdict(m) for m in messages], indent=2, default=str))
        return

    if not messages:
        print("No messages parsed from text.")
        return

    print(f"Parsed {len(messages)} message(s):\n")
    for msg in messages:
        recip = ", ".join(msg.recipients) if msg.recipients else "(unknown)"
        print(f"  [{msg.chain_position}] {msg.sender} -> {recip}")
        print(f"      Date: {msg.date}")
        if msg.subject:
            print(f"      Subject: {msg.subject}")
        print(f"      Confidence: {msg.confidence}")
        body_preview = msg.body[:150].replace('\n', ' ')
        if body_preview:
            print(f"      Body: {body_preview}...")
        print()


if __name__ == "__main__":
    main()
