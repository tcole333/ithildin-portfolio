#!/usr/bin/env python3
"""Attribute a quoted string to a specific sender in a DOJ email chain.

Given an EFTA ID and a quoted string, determines which message in the
chain contains the quote and who sent it.

Usage:
    python tools/attribute_quote.py EFTA02452433 "Happy to explain my thinking on the 8865"
    python tools/attribute_quote.py EFTA02454291 "I will leave it"
    python tools/attribute_quote.py EFTA02452433 "8865" --json
"""
import argparse
import json
import os
import re
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.parse_email_chain import parse_email_chain, get_ocr_text


def _normalize_for_match(text):
    """Normalize text for fuzzy matching."""
    if not text:
        return ""
    # OCR artifacts
    text = re.sub(r'=\n', '', text)
    text = re.sub(r'=br>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def attribute_quote(efta_id, quote_text):
    """Find which message in an email chain contains a quote.

    Returns dict with:
        - sender: who sent the message containing the quote
        - recipients: list of recipients
        - date: date of the message
        - subject: subject line
        - chain_position: position in chain (0=outermost)
        - match_location: 'body', 'subject', or 'raw'
        - confidence: parser confidence level
        - found: True if quote was located
    """
    ocr_text = get_ocr_text(efta_id)
    if not ocr_text:
        return {"found": False, "error": f"EFTA {efta_id} not found in documents.db"}

    messages = parse_email_chain(ocr_text)
    if not messages:
        return {"found": False, "error": "No messages parsed from OCR text"}

    quote_norm = _normalize_for_match(quote_text)
    if not quote_norm:
        return {"found": False, "error": "Empty quote text"}

    # Search through messages, starting from outermost (most likely to be cited)
    for msg in messages:
        body_norm = _normalize_for_match(msg.body)
        subj_norm = _normalize_for_match(msg.subject)
        raw_norm = _normalize_for_match(msg.raw_text)

        match_location = None
        if quote_norm in body_norm:
            match_location = "body"
        elif quote_norm in subj_norm:
            match_location = "subject"
        elif quote_norm in raw_norm:
            match_location = "raw"

        if match_location:
            recip_str = ", ".join(msg.recipients) if msg.recipients else "(unknown)"
            return {
                "found": True,
                "sender": msg.sender,
                "recipients": msg.recipients,
                "date": msg.date,
                "subject": msg.subject,
                "chain_position": msg.chain_position,
                "match_location": match_location,
                "confidence": msg.confidence,
                "attribution": f"{msg.sender} -> {recip_str} ({msg.date})",
            }

    # Quote not found in any parsed message — try raw OCR
    ocr_norm = _normalize_for_match(ocr_text)
    if quote_norm in ocr_norm:
        return {
            "found": True,
            "sender": messages[0].sender if messages else "(unknown)",
            "recipients": messages[0].recipients if messages else [],
            "date": messages[0].date if messages else "",
            "subject": messages[0].subject if messages else "",
            "chain_position": -1,
            "match_location": "ocr_fallback",
            "confidence": "low",
            "attribution": f"Found in raw OCR but not in any parsed message",
        }

    return {
        "found": False,
        "error": "Quote not found in document",
        "messages_checked": len(messages),
    }


def main():
    parser = argparse.ArgumentParser(description="Attribute a quote to a sender in an email chain")
    parser.add_argument("efta_id", help="EFTA ID of the document")
    parser.add_argument("quote", help="Quote text to search for")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    result = attribute_quote(args.efta_id, args.quote)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return

    if result.get("found"):
        recip = ", ".join(result["recipients"]) if result["recipients"] else "(unknown)"
        print(f"Quote attributed to: {result['sender']}")
        print(f"  Recipients: {recip}")
        print(f"  Date: {result['date']}")
        if result.get("subject"):
            print(f"  Subject: {result['subject']}")
        print(f"  Chain position: {result['chain_position']}")
        print(f"  Match location: {result['match_location']}")
        print(f"  Confidence: {result['confidence']}")
    else:
        print(f"Quote NOT found: {result.get('error', 'unknown error')}")
        if result.get("messages_checked"):
            print(f"  Messages checked: {result['messages_checked']}")


if __name__ == "__main__":
    main()
