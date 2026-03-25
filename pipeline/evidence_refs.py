#!/usr/bin/env python3
"""Evidence reference canonicalization utilities.

These helpers normalize common evidence_ref variants into stable tokens so
downstream citation rendering and linting operate on predictable inputs.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import quote_plus


def _normalize_url(raw: str) -> str:
    cleaned = raw.rstrip("),.;")
    malformed_port = re.match(r"^(https?://[^/\s:]+):([^0-9][^\s/]*)$", cleaned, re.IGNORECASE)
    if malformed_port:
        return malformed_port.group(1)
    return cleaned


def _normalize_prefixed(token: str, prefix: str) -> str:
    body = token.split(":", 1)[1]
    return f"{prefix}:{body}"


def _normalize_fec(token: str) -> str:
    prefix, body = token.split(":", 1)
    _ = prefix
    body = body.strip()
    committee = re.match(r"^(C\d{8})(.*)$", body, re.IGNORECASE)
    if not committee:
        return f"FEC:{body}"
    committee_id = committee.group(1).upper()
    suffix = committee.group(2) or ""
    if suffix.lower() == "/schedule_a":
        return f"FEC:{committee_id}/schedule_a"
    return f"FEC:{committee_id}{suffix}"


def _normalize_ds10(token: str) -> str:
    return re.sub(r"^ds10", "DS10", token, flags=re.IGNORECASE)


def _digits_to_accession(digits: str) -> str:
    cleaned = re.sub(r"\D+", "", digits)
    if len(cleaned) != 18:
        return cleaned
    return f"{cleaned[:10]}-{cleaned[10:12]}-{cleaned[12:]}"


def _normalize_sec_from_composite(token: str) -> str:
    # Handles forms like SEC:10-K:0001021408-00-001562, SEC:701985:000090951805000716
    match = re.search(r"(\d{10}-\d{2}-\d{6}|\d{18})", token)
    if not match:
        return token
    raw = match.group(1)
    accession = raw if "-" in raw else _digits_to_accession(raw)
    return f"SEC:{accession}"


def _normalize_sec_cik_to_url(token: str) -> str:
    match = re.search(r"CIK[-\s]?0*(\d+)", token, re.IGNORECASE)
    if not match:
        return "https://www.sec.gov/edgar/search/"
    return f"https://www.sec.gov/edgar/browse/?CIK={match.group(1)}"


def _normalize_sec_fallback_to_url(token: str) -> str:
    body = token.split(":", 1)[1].strip()
    if not body:
        return "https://www.sec.gov/edgar/search/"

    parts = [part.strip() for part in body.split(":") if part.strip()]
    lead = parts[0] if parts else body
    if re.fullmatch(r"\d{1,10}", lead):
        cik = str(int(lead))
        if len(parts) == 1:
            return f"https://www.sec.gov/edgar/browse/?CIK={cik}"
        query = " ".join([f"CIK {cik}", *parts[1:]])
        return f"https://www.sec.gov/edgar/search/#/q={quote_plus(query)}"

    match = re.search(r"CIK[-\s]?0*(\d+)", body, re.IGNORECASE)
    if match:
        return f"https://www.sec.gov/edgar/browse/?CIK={match.group(1)}"

    return f"https://www.sec.gov/edgar/search/#/q={quote_plus(body.replace(':', ' '))}"


def _normalize_edgar_fallback_to_url(token: str) -> str:
    body = token.split(":", 1)[1].strip()
    return f"https://www.sec.gov/edgar/search/#/q={quote_plus(body)}"


def _normalize_990_ein(token: str) -> str:
    match = re.search(r"EIN\D*([0-9]{9})", token, re.IGNORECASE)
    if not match:
        return token
    return f"990:{match.group(1)}"


def _normalize_990_prefixed(token: str) -> str:
    match = re.search(r"990:\s*([0-9]{2})-?([0-9]{7})", token, re.IGNORECASE)
    if not match:
        return token
    return f"990:{match.group(1)}{match.group(2)}"


def _normalize_acris_ft(token: str) -> str:
    match = re.search(r"FT[_-]?([0-9]{13,16})", token, re.IGNORECASE)
    if not match:
        return token
    return f"ACRIS:{match.group(1)}"


def _normalize_acris_search_url(_token: str) -> str:
    return "https://a836-acris.nyc.gov/CP/"


def _normalize_fara_search_url(token: str) -> str:
    body = token.split(":", 1)[1].strip()
    return f"https://efile.fara.gov/ords/f?p=1381:200#q={quote_plus(body)}"


def _normalize_fec_search_url(token: str) -> str:
    body = token.split(":", 1)[1].strip()
    return f"https://www.fec.gov/data/search/?q={quote_plus(body)}"


def _normalize_usvi_search_url(_token: str) -> str:
    return "https://www.ltg.gov.vi/division-of-corporations/"


def _normalize_ny_sos_dos(token: str) -> str:
    match = re.search(r"DOS[_\s-]*([0-9]+)", token, re.IGNORECASE)
    if not match:
        return token
    return f"NY-SoS:{match.group(1)}"


def _normalize_fl_sunbiz_search_url(_token: str) -> str:
    return "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"


def _normalize_bare_efta(token: str) -> str:
    # Convert malformed EFTA-like refs into plain text so they no longer masquerade as canonical IDs.
    body = token.strip()
    return f"UNRESOLVED:{body}"


def _normalize_bare_url_with_description(token: str) -> str:
    # Handles malformed tokens like https://fedsoc.org:Federalist Society profiles...
    match = re.match(r"(https?://[^\s:]+)", token, re.IGNORECASE)
    if not match:
        return token
    return match.group(1)


def _normalize_cl_token(token: str) -> str:
    body = token.split(":", 1)[1].strip()
    if not body:
        return token

    opinion_match = re.match(r"opinion[-:_/\s]*(\d+)$", body, re.IGNORECASE)
    if opinion_match:
        return f"https://www.courtlistener.com/opinion/{opinion_match.group(1)}/"

    docket_match = re.match(r"docket[-:_/\s]*(\d+)$", body, re.IGNORECASE)
    if docket_match:
        return f"CL:{docket_match.group(1)}"

    search_body = re.sub(r"^search[-:_/\s]*", "", body, flags=re.IGNORECASE).strip()
    if search_body:
        return f"https://www.courtlistener.com/?q={quote_plus(search_body.replace('/', ' '))}"

    return token


TOKEN_PATTERNS: list[tuple[re.Pattern[str], callable]] = [
    (re.compile(r"https?://[^\s,;]+", re.IGNORECASE), lambda m: _normalize_url(m.group(0))),
    (re.compile(r"EFTA\d{6,}", re.IGNORECASE), lambda m: m.group(0).upper()),
    (re.compile(r"EFTA[0-9]{1,5}\b", re.IGNORECASE), lambda m: _normalize_bare_efta(m.group(0))),
    (re.compile(r"HOUSE_OVERSIGHT_\d+", re.IGNORECASE), lambda m: m.group(0).upper()),
    (re.compile(r"SEC:[^,;\n]*\d{10}-\d{2}-\d{6}[^,;\n]*", re.IGNORECASE), lambda m: _normalize_sec_from_composite(m.group(0))),
    (re.compile(r"SEC:[^,;\n]*\d{18}[^,;\n]*", re.IGNORECASE), lambda m: _normalize_sec_from_composite(m.group(0))),
    (re.compile(r"SEC:CIK[-\s]?\d+", re.IGNORECASE), lambda m: _normalize_sec_cik_to_url(m.group(0))),
    (re.compile(r"SEC:\d{10}-\d{2}-\d{6}", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "SEC")),
    (re.compile(r"SEC:EDGAR:\d{10}-\d{2}-\d{6}", re.IGNORECASE), lambda m: _normalize_sec_from_composite(m.group(0))),
    (re.compile(r"SEC:[^,;\n]+", re.IGNORECASE), lambda m: _normalize_sec_fallback_to_url(m.group(0))),
    (re.compile(r"EDGAR:\d{10}-\d{2}-\d{6}", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "EDGAR")),
    (re.compile(r"EDGAR:[^,;\n]+", re.IGNORECASE), lambda m: _normalize_edgar_fallback_to_url(m.group(0))),
    (re.compile(r"990:EIN[0-9:A-Za-z._-]+", re.IGNORECASE), lambda m: _normalize_990_ein(m.group(0))),
    (re.compile(r"990:[^,;\n]*\d{2}-?\d{7}[^,;\n]*", re.IGNORECASE), lambda m: _normalize_990_prefixed(m.group(0))),
    (re.compile(r"990:\d{9}", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "990")),
    (re.compile(r"ACRIS:FT[_-]?\d{13,16}", re.IGNORECASE), lambda m: _normalize_acris_ft(m.group(0))),
    (re.compile(r"ACRIS:\d{13,16}", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "ACRIS")),
    (re.compile(r"ACRIS:(?:batch|search|bulk)[^,;\n]*", re.IGNORECASE), lambda m: _normalize_acris_search_url(m.group(0))),
    (re.compile(r"CL:\d+", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "CL")),
    (re.compile(r"CL:[^,;\n]+", re.IGNORECASE), lambda m: _normalize_cl_token(m.group(0))),
    (re.compile(r"FEC:C\d{8}(?:-\d{4}|/schedule_a)?", re.IGNORECASE), lambda m: _normalize_fec(m.group(0))),
    (re.compile(r"FEC:[A-Za-z0-9_]+", re.IGNORECASE), lambda m: _normalize_fec(m.group(0))),
    (re.compile(r"FEC:[^,;\n]+", re.IGNORECASE), lambda m: _normalize_fec_search_url(m.group(0))),
    (re.compile(r"FARA:\d+", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "FARA")),
    (re.compile(r"FARA:[^,;\n]+", re.IGNORECASE), lambda m: _normalize_fara_search_url(m.group(0))),
    (re.compile(r"USVI:[A-Za-z0-9]+", re.IGNORECASE), lambda m: _normalize_prefixed(m.group(0), "USVI")),
    (re.compile(r"USVI:(?:search|query)[^,;\n]*", re.IGNORECASE), lambda m: _normalize_usvi_search_url(m.group(0))),
    (
        re.compile(r"REG:([A-Z]{2}):([A-Za-z0-9]+)", re.IGNORECASE),
        lambda m: f"REG:{m.group(1).upper()}:{m.group(2)}",
    ),
    (
        re.compile(r"FL[-_]?SunBiz[:\s]+([A-Za-z0-9]+)", re.IGNORECASE),
        lambda m: f"FL-SunBiz:{m.group(1).upper()}",
    ),
    (
        re.compile(r"NM[-_]?SoS[:\s]+([A-Za-z0-9]+)", re.IGNORECASE),
        lambda m: f"NM-SoS:{m.group(1)}",
    ),
    (
        re.compile(r"NY[-_]?SoS[:\s]+([A-Za-z0-9]+)", re.IGNORECASE),
        lambda m: f"NY-SoS:{m.group(1)}",
    ),
    (
        re.compile(r"NY[-_]?SOS[-_]?DOS[-_]?([0-9]+)", re.IGNORECASE),
        lambda m: f"NY-SoS:{m.group(1)}",
    ),
    (
        re.compile(r"NY_SoS_DOS_[0-9]+", re.IGNORECASE),
        lambda m: _normalize_ny_sos_dos(m.group(0)),
    ),
    (
        re.compile(r"NY\s+SoS\s+DOS\s+ID\s+([A-Za-z0-9]+)", re.IGNORECASE),
        lambda m: f"NY-SoS:{m.group(1)}",
    ),
    (
        re.compile(r"FL_SUNBIZ:(?:bulk|search)[^,;\n]*", re.IGNORECASE),
        lambda m: _normalize_fl_sunbiz_search_url(m.group(0)),
    ),
    (re.compile(r"DS10(?::[A-Za-z0-9_-]+)?", re.IGNORECASE), lambda m: _normalize_ds10(m.group(0))),
    (re.compile(r"https?://[^\s]+:[^\s]+", re.IGNORECASE), lambda m: _normalize_bare_url_with_description(m.group(0))),
]


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def canonicalize_evidence_ref(raw_ref: str | None) -> list[str]:
    """Split and normalize a single evidence_ref string into canonical tokens."""
    raw = (raw_ref or "").strip()
    if not raw:
        return []

    matches: list[tuple[int, int, str]] = []
    for pattern, normalizer in TOKEN_PATTERNS:
        for match in pattern.finditer(raw):
            normalized = normalizer(match).strip()
            if not normalized:
                continue
            matches.append((match.start(), match.end(), normalized))

    if not matches:
        return [raw]

    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected_spans: list[tuple[int, int]] = []
    ordered_tokens: list[str] = []
    seen_tokens: set[str] = set()

    for start, end, token in matches:
        if any(_overlaps(start, end, span_start, span_end) for span_start, span_end in selected_spans):
            continue
        selected_spans.append((start, end))
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        ordered_tokens.append(token)

    return ordered_tokens or [raw]


def canonicalize_evidence_refs(refs: Iterable[str | None]) -> list[str]:
    """Normalize and flatten a list of evidence_ref strings."""
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        for token in canonicalize_evidence_ref(ref):
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
    return out


def canonicalize_evidence_rows(rows: Iterable[dict]) -> list[dict]:
    """Expand a row list so each output row has exactly one canonical evidence_ref."""
    out: list[dict] = []
    seen: set[tuple] = set()

    for row in rows:
        refs = canonicalize_evidence_ref(row.get("evidence_ref"))
        for ref in refs:
            normalized = dict(row)
            normalized["evidence_ref"] = ref
            dedupe_key = (
                normalized.get("evidence_type"),
                normalized.get("evidence_ref"),
                normalized.get("source_quote"),
                normalized.get("source_page"),
                normalized.get("assessment"),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(normalized)

    return out
