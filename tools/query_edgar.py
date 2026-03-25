#!/usr/bin/env python3
"""
SEC EDGAR full-text search (EFTS) wrapper for OSINT investigations.

Searches the full text of all SEC filings. No authentication required,
but a User-Agent header with contact info is mandatory.

Rate limit: 10 requests/second.

Usage:
    # Full-text search with aggregation facets
    python tools/query_edgar.py search "jeffrey epstein" --size 20
    python tools/query_edgar.py search "leon black" "gratitude america" --forms "10-K,DEF 14A"
    python tools/query_edgar.py search "enhanced education" --start 2010-01-01 --end 2020-01-01
    python tools/query_edgar.py search "wexner" --facets          # Show entity/form/state breakdowns

    # Company/person name → CIK resolution
    python tools/query_edgar.py lookup "apollo global"
    python tools/query_edgar.py lookup "JPMorgan Chase"
    python tools/query_edgar.py lookup "leon black"               # Also finds persons

    # Company info by CIK
    python tools/query_edgar.py company 0001411494

    # Filtered filing list by CIK
    python tools/query_edgar.py filings 0001411494 --form 10-K
    python tools/query_edgar.py filings 0001411494 --form "DEF 14A"  # Proxy statements

    # Insider transactions (Forms 3/4/5)
    python tools/query_edgar.py insider 0001411494 --limit 30

    # Fetch and read filing content
    python tools/query_edgar.py read "https://www.sec.gov/Archives/edgar/data/1411494/..."
    python tools/query_edgar.py read "https://www.sec.gov/Archives/edgar/data/..." --lines 200
"""

import argparse
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"

# CRITICAL: SEC requires a User-Agent with contact info or returns 403
USER_AGENT = "OSINT-Research osint-research@proton.me"

# Rate limiting
_last_request = 0.0
MIN_INTERVAL = 0.11  # 10 req/sec max


def _request(url, params=None, accept="application/json"):
    """Make a rate-limited request to SEC. Returns parsed JSON or raw bytes."""
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)

    if params:
        url += "?" + urlencode(params, doseq=True)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            _last_request = time.time()
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if accept == "application/json" or "json" in content_type:
                return json.loads(raw.decode())
            return raw
    except HTTPError as e:
        if e.code == 403:
            print("ERROR: 403 Forbidden — SEC requires User-Agent with contact info", file=sys.stderr)
        elif e.code == 404:
            print(f"ERROR: 404 Not Found — {url}", file=sys.stderr)
        else:
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code} from SEC: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach SEC: {e.reason}", file=sys.stderr)
        return None


def _filing_url(cik, adsh, doc_name=""):
    """Build a filing URL from CIK and accession number."""
    cik_clean = cik.lstrip("0") if cik else ""
    adsh_path = adsh.replace("-", "")
    if cik_clean and doc_name:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_path}/{doc_name}"
    elif cik_clean and adsh:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_path}/"
    return ""


def _strip_html(text):
    """Strip HTML tags and decode entities. Simple but effective for SEC filings."""
    # Remove script/style blocks
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Replace block elements with newlines
    text = re.sub(r'<(?:br|p|div|tr|li|h[1-6])[^>]*/?>', '\n', text, flags=re.IGNORECASE)
    # Remove remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.split('\n')]
    return '\n'.join(lines)


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_search(args):
    """Full-text search across all SEC filings."""
    # Build query — join multiple terms, quote multi-word terms
    query = " ".join(f'"{q}"' if " " in q else q for q in args.query)

    params = {
        "q": query,
        "from": args.offset,
    }
    if args.start:
        params["startdt"] = args.start
    if args.end:
        params["enddt"] = args.end
    if args.forms:
        params["forms"] = args.forms

    data = _request(EFTS_URL, params)
    if not data:
        print("No results.")
        return

    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    results = hits.get("hits", [])

    # API ignores size param (returns up to 100), slice client-side
    results = results[:args.size]

    # Client-side date sort if requested
    if args.sort == "date":
        results.sort(key=lambda h: h.get("_source", {}).get("file_date", ""), reverse=True)
    elif args.sort == "date-asc":
        results.sort(key=lambda h: h.get("_source", {}).get("file_date", ""))

    _log(query, "edgar", total)

    # Check output options FIRST
    if write_output(data, args, summary=f"EDGAR search '{query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Pretty-print output
    print(f"SEC EDGAR: '{query}' — {total:,} total results (showing {len(results)})")
    print()

    for i, hit in enumerate(results, 1):
        src = hit.get("_source", {})
        filing_date = src.get("file_date", "?")
        form = src.get("form", "?")
        file_type = src.get("file_type", "")
        display_names = src.get("display_names", [])
        entity = display_names[0] if display_names else "?"
        # Clean CIK from display name for readability
        entity_clean = re.sub(r'\s+\(CIK \d+\)', '', entity)
        ciks = src.get("ciks", [])
        adsh = src.get("adsh", "")
        file_desc = src.get("file_description", "")
        period = src.get("period_ending", "")

        form_display = f"{form}" if form == file_type else f"{form} ({file_type})" if file_type else form
        print(f"  [{i}] {filing_date} | {form_display} | {entity_clean}")
        if file_desc and file_desc != form:
            print(f"      {file_desc}")
        if period:
            print(f"      Period: {period}")
        if len(ciks) > 1:
            other_names = [re.sub(r'\s+\(CIK \d+\)', '', n) for n in display_names[1:]]
            print(f"      Also: {', '.join(other_names)}")

        # Build URL
        doc_id = hit.get("_id", "").split(":", 1)
        doc_name = doc_id[1] if len(doc_id) > 1 else ""
        url = _filing_url(ciks[0] if ciks else "", adsh, doc_name)
        if url:
            print(f"      {url}")
        print()

    # Show aggregation facets
    if args.facets:
        aggs = data.get("aggregations", {})
        _print_facets(aggs, total)


def _print_facets(aggs, total):
    """Display aggregation facets from EFTS search."""
    if not aggs:
        return

    entity_agg = aggs.get("entity_filter", {}).get("buckets", [])
    form_agg = aggs.get("form_filter", {}).get("buckets", [])
    sic_agg = aggs.get("sic_filter", {}).get("buckets", [])
    state_agg = aggs.get("biz_states_filter", {}).get("buckets", [])

    if entity_agg:
        print("─── Top Entities ───────────────────────────────────────")
        for b in entity_agg[:15]:
            name = re.sub(r'\s+\(CIK \d+\)', '', b["key"])
            name = re.sub(r'\s+\([A-Z, ]+\)', '', name)  # strip ticker
            pct = b["doc_count"] / total * 100 if total else 0
            bar = "█" * max(1, int(pct / 3))
            print(f"  {b['doc_count']:>5} {bar:<20} {name}")
        print()

    if form_agg:
        print("─── Form Types ─────────────────────────────────────────")
        for b in form_agg[:15]:
            pct = b["doc_count"] / total * 100 if total else 0
            bar = "█" * max(1, int(pct / 3))
            print(f"  {b['doc_count']:>5} {bar:<20} {b['key']}")
        print()

    if state_agg:
        top_states = state_agg[:10]
        states_str = ", ".join(f"{b['key']}({b['doc_count']})" for b in top_states)
        print(f"─── States: {states_str}")
        print()


def cmd_lookup(args):
    """Find CIK numbers for a company or person name."""
    query = " ".join(args.name)

    # Strategy 1: Search company_tickers.json for exact/partial matches
    print(f"Looking up: {query}")
    print()

    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    tickers_data = _request(tickers_url)

    matches = []
    query_lower = query.lower()
    query_words = query_lower.split()

    if tickers_data:
        for _, entry in tickers_data.items():
            title = entry.get("title", "").lower()
            # Check if all query words appear in the title
            if all(w in title for w in query_words):
                matches.append(entry)

    if matches:
        # Deduplicate by CIK (same company can have multiple tickers)
        seen_ciks = {}
        for m in matches:
            cik = m["cik_str"]
            if cik not in seen_ciks:
                seen_ciks[cik] = m
            else:
                # Append ticker
                existing = seen_ciks[cik]
                if m.get("ticker") and m["ticker"] not in existing.get("_tickers", existing.get("ticker", "")):
                    existing.setdefault("_tickers", existing.get("ticker", ""))
                    existing["_tickers"] += f", {m['ticker']}"

        deduped = list(seen_ciks.values())
        print(f"  Public companies ({len(deduped)} matches):")
        for m in deduped[:20]:
            cik = str(m["cik_str"]).zfill(10)
            tickers = m.get("_tickers", m.get("ticker", ""))
            print(f"    CIK {cik} | {tickers:<10} | {m['title']}")
        print()

    # Strategy 2: Use EFTS entity aggregation to find which entities
    # are most associated with this name in filing text
    efts_data = _request(EFTS_URL, {"q": f'"{query}"', "size": 0})
    if efts_data:
        entity_agg = efts_data.get("aggregations", {}).get("entity_filter", {}).get("buckets", [])
        total = efts_data.get("hits", {}).get("total", {}).get("value", 0)

        if entity_agg:
            print(f"  Entities mentioning \"{query}\" in filings ({total:,} total filings):")
            for b in entity_agg[:15]:
                name_raw = b["key"]
                # Extract CIK from display name
                cik_match = re.search(r'CIK (\d+)', name_raw)
                cik = cik_match.group(1).zfill(10) if cik_match else "?"
                name_clean = re.sub(r'\s+\(CIK \d+\)', '', name_raw)
                print(f"    CIK {cik} | {b['doc_count']:>5} filings | {name_clean}")
            print()

    # Strategy 3: Search the submissions endpoint for person/company by name
    # (Browse EDGAR company search — returns XML/Atom)
    browse_url = "https://www.sec.gov/cgi-bin/browse-edgar"
    browse_params = {
        "company": query,
        "CIK": "",
        "type": "",
        "dateb": "",
        "owner": "include",
        "count": "10",
        "search_text": "",
        "action": "getcompany",
        "output": "atom",
    }
    atom_data = _request(browse_url, browse_params, accept="application/atom+xml")
    if atom_data and isinstance(atom_data, bytes):
        _parse_atom_results(atom_data)


def _parse_atom_results(atom_bytes):
    """Parse EDGAR company search Atom XML results."""
    try:
        root = ET.fromstring(atom_bytes)
        # All elements are in the Atom namespace
        ns = "http://www.w3.org/2005/Atom"
        entries = root.findall(f"{{{ns}}}entry")

        if entries:
            print(f"  EDGAR registered entities ({len(entries)} matches):")
            for entry in entries[:15]:
                content = entry.find(f"{{{ns}}}content")
                if content is None:
                    continue
                ci = content.find(f"{{{ns}}}company-info")
                if ci is None:
                    continue

                cik = (ci.findtext(f"{{{ns}}}cik") or "?").strip().zfill(10)
                name = (ci.findtext(f"{{{ns}}}name") or "?").strip()
                sic = (ci.findtext(f"{{{ns}}}sic") or "").strip()
                state = (ci.findtext(f"{{{ns}}}state-of-incorporation") or "").strip()

                extra = f" SIC:{sic}" if sic else ""
                extra += f" [{state}]" if state else ""
                print(f"    CIK {cik} | {name}{extra}")
            print()
    except ET.ParseError:
        pass


def cmd_company(args):
    """Get company info by CIK number."""
    cik = args.cik.lstrip("0").zfill(10)
    url = f"{SUBMISSIONS_URL}/CIK{cik}.json"

    data = _request(url)
    if not data:
        return

    # Check output options FIRST
    if write_output(data, args, summary=f"EDGAR company CIK {args.cik}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2, default=str))
        return

    # Pretty-print output
    name = data.get("name", "?")
    cik_display = data.get("cik", "?")
    sic = data.get("sic", "")
    sic_desc = data.get("sicDescription", "")
    tickers = data.get("tickers", [])
    exchanges = data.get("exchanges", [])
    state = data.get("stateOfIncorporation", "")
    fiscal_year = data.get("fiscalYearEnd", "")

    addresses = data.get("addresses", {})
    business = addresses.get("business", {})
    mailing = addresses.get("mailing", {})

    print(f"=== {name} (CIK: {cik_display}) ===")
    if sic:
        print(f"  SIC: {sic} — {sic_desc}")
    if tickers:
        print(f"  Tickers: {', '.join(tickers)}")
    if exchanges:
        print(f"  Exchanges: {', '.join(exchanges)}")
    if state:
        print(f"  State of Incorporation: {state}")
    if fiscal_year:
        print(f"  Fiscal Year End: {fiscal_year}")

    for label, addr in [("Business", business), ("Mailing", mailing)]:
        if addr and addr.get("street1"):
            street = f"{addr.get('street1', '')} {addr.get('street2', '') or ''}".strip()
            city_state = f"{addr.get('city', '')}, {addr.get('stateOrCountry', '')} {addr.get('zipCode', '')}"
            print(f"  {label}: {street}, {city_state}")

    # Recent filings summary
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    if forms:
        # Show form type distribution
        form_counts = {}
        for f in forms:
            form_counts[f] = form_counts.get(f, 0) + 1
        top_forms = sorted(form_counts.items(), key=lambda x: -x[1])[:10]
        print(f"\n  Filing Types ({len(forms)} total): {', '.join(f'{f}({c})' for f, c in top_forms)}")

        show = min(len(forms), 20)
        print(f"\n  Recent Filings (showing {show}):")
        for i in range(show):
            form = forms[i] if i < len(forms) else "?"
            date = dates[i] if i < len(dates) else "?"
            acc = accessions[i] if i < len(accessions) else ""
            doc = primary_docs[i] if i < len(primary_docs) else ""
            desc = descriptions[i] if i < len(descriptions) else ""
            url = _filing_url(cik_display, acc, doc)
            desc_str = f" — {desc}" if desc and desc != form else ""
            print(f"    {date} | {form}{desc_str}")
            if url:
                print(f"      {url}")


def cmd_filings(args):
    """Get filings for a company by CIK, filtered by form type."""
    cik = args.cik.lstrip("0").zfill(10)
    url = f"{SUBMISSIONS_URL}/CIK{cik}.json"

    data = _request(url)
    if not data:
        return

    name = data.get("name", "?")
    cik_display = data.get("cik", "")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    # Support comma-separated form types and prefix matching
    form_filters = []
    if args.form:
        form_filters = [f.strip() for f in args.form.split(",")]

    print(f"Filings for {name} (CIK: {args.cik})")
    if form_filters:
        print(f"Filter: {', '.join(form_filters)}")
    print()

    count = 0
    for i in range(len(forms)):
        form = forms[i] if i < len(forms) else "?"
        if form_filters:
            # Match exact or prefix (e.g., "DEF" matches "DEF 14A", "DEFA14A")
            if not any(form == f or form.startswith(f) for f in form_filters):
                continue

        date = dates[i] if i < len(dates) else "?"
        acc = accessions[i] if i < len(accessions) else ""
        doc = primary_docs[i] if i < len(primary_docs) else ""
        desc = descriptions[i] if i < len(descriptions) else ""
        doc_url = _filing_url(cik_display, acc, doc)

        desc_str = f" — {desc}" if desc and desc != form else ""
        print(f"  {date} | {form}{desc_str}")
        if doc_url:
            print(f"    {doc_url}")

        count += 1
        if count >= args.limit:
            break

    print(f"\nShowing {count} filings" + (f" (of {len(forms)} total)" if not form_filters else ""))


def cmd_insider(args):
    """Show insider transactions (Forms 3/4/5) for a CIK."""
    cik = args.cik.lstrip("0").zfill(10)
    url = f"{SUBMISSIONS_URL}/CIK{cik}.json"

    data = _request(url)
    if not data:
        return

    name = data.get("name", "?")
    cik_display = data.get("cik", "")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    # Filter to insider forms
    insider_forms = {"3", "4", "5", "3/A", "4/A", "5/A"}
    insider_filings = []
    for i in range(len(forms)):
        if forms[i] in insider_forms:
            insider_filings.append({
                "form": forms[i],
                "date": dates[i] if i < len(dates) else "?",
                "accession": accessions[i] if i < len(accessions) else "",
                "doc": primary_docs[i] if i < len(primary_docs) else "",
            })

    print(f"Insider Transactions for {name} (CIK: {cik_display})")
    print(f"Total insider filings: {len(insider_filings)}")
    print()

    if not insider_filings:
        print("  No insider transaction filings found.")
        return

    # Show filings, optionally fetch XML details for first N
    show = min(len(insider_filings), args.limit)
    for idx, filing in enumerate(insider_filings[:show]):
        url = _filing_url(cik_display, filing["accession"], filing["doc"])
        print(f"  [{idx+1}] {filing['date']} | Form {filing['form']}")

        if args.detail and filing["doc"].endswith(".xml"):
            _fetch_insider_detail(cik_display, filing["accession"], filing["doc"])
        elif url:
            print(f"      {url}")
        print()

    if len(insider_filings) > show:
        print(f"  ... {len(insider_filings) - show} more insider filings")


def _fetch_insider_detail(cik, accession, doc_name):
    """Fetch and parse a Form 3/4/5 XML for transaction details."""
    # Get the filing index to find the raw XML (not XSLT version)
    acc_path = accession.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_path}/"
    index_data = _request(index_url, accept="text/html")
    if not index_data:
        return

    index_text = index_data.decode("utf-8", errors="replace") if isinstance(index_data, bytes) else str(index_data)
    xml_files = re.findall(r'href="([^"]+\.xml)"', index_text)
    # Prefer non-xsl XML files
    raw_xmls = [f for f in xml_files if "xsl" not in f.lower()]

    if not raw_xmls:
        return

    xml_path = raw_xmls[0]
    if xml_path.startswith("/"):
        xml_url = f"https://www.sec.gov{xml_path}"
    else:
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_path}/{xml_path}"

    xml_data = _request(xml_url, accept="application/xml")
    if not xml_data:
        return

    try:
        xml_text = xml_data.decode("utf-8", errors="replace") if isinstance(xml_data, bytes) else str(xml_data)
        root = ET.fromstring(xml_text)

        # Issuer info
        issuer = root.find(".//issuer")
        if issuer is None:
            issuer = root.find(".//{*}issuer")
        if issuer is not None:
            issuer_name = (issuer.findtext("issuerName") or issuer.findtext("{*}issuerName") or "?").strip()
            issuer_ticker = (issuer.findtext("issuerTradingSymbol") or issuer.findtext("{*}issuerTradingSymbol") or "").strip()
            print(f"      Issuer: {issuer_name} ({issuer_ticker})")

        # Owner relationship
        rel = root.find(".//reportingOwnerRelationship")
        if rel is None:
            rel = root.find(".//{*}reportingOwnerRelationship")
        if rel is not None:
            roles = []
            if rel.findtext("isDirector") == "1" or rel.findtext("{*}isDirector") == "1":
                roles.append("Director")
            if rel.findtext("isOfficer") == "1" or rel.findtext("{*}isOfficer") == "1":
                title = (rel.findtext("officerTitle") or rel.findtext("{*}officerTitle") or "Officer").strip()
                roles.append(title)
            if rel.findtext("isTenPercentOwner") == "1" or rel.findtext("{*}isTenPercentOwner") == "1":
                roles.append("10%+ Owner")
            if roles:
                print(f"      Role: {', '.join(roles)}")

        # Non-derivative transactions
        for txn in root.findall(".//nonDerivativeTransaction") + root.findall(".//{*}nonDerivativeTransaction"):
            security = (txn.findtext(".//securityTitle/value") or txn.findtext(".//{*}securityTitle/{*}value") or "?").strip()
            date = (txn.findtext(".//transactionDate/value") or txn.findtext(".//{*}transactionDate/{*}value") or "?").strip()
            code = (txn.findtext(".//transactionCode") or txn.findtext(".//{*}transactionCode") or "?").strip()
            shares = (txn.findtext(".//transactionShares/value") or txn.findtext(".//{*}transactionShares/{*}value") or "?").strip()
            price = (txn.findtext(".//transactionPricePerShare/value") or txn.findtext(".//{*}transactionPricePerShare/{*}value") or "").strip()
            acq_disp = (txn.findtext(".//transactionAcquiredDisposedCode/value") or txn.findtext(".//{*}transactionAcquiredDisposedCode/{*}value") or "").strip()

            code_map = {"P": "Purchase", "S": "Sale", "A": "Grant", "D": "Disposition",
                        "F": "Tax", "M": "Exercise", "G": "Gift", "C": "Conversion"}
            action = code_map.get(code, code)
            direction = "Acquired" if acq_disp == "A" else "Disposed" if acq_disp == "D" else ""
            price_str = f" @ ${price}" if price else ""

            print(f"      {date} | {action} {shares} {security}{price_str} ({direction})")

        # Holdings
        for holding in root.findall(".//nonDerivativeHolding") + root.findall(".//{*}nonDerivativeHolding"):
            security = (holding.findtext(".//securityTitle/value") or holding.findtext(".//{*}securityTitle/{*}value") or "?").strip()
            shares = (holding.findtext(".//sharesOwnedFollowingTransaction/value") or
                      holding.findtext(".//{*}sharesOwnedFollowingTransaction/{*}value") or
                      holding.findtext(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value") or "?").strip()
            print(f"      Holds: {shares} {security}")

    except ET.ParseError:
        print("      (Could not parse XML)")


def _get_edgartools_filing(ticker_or_cik, form="10-K", index=0):
    """Get a filing via edgartools. Returns (filing, company) or (None, None)."""
    import os
    os.environ.setdefault("EDGAR_IDENTITY", "Ithildin Research research@example.com")
    from edgar import Company
    company = Company(str(ticker_or_cik))
    filings = company.get_filings(form=form)
    if not filings or index >= len(filings):
        return None, company
    return filings[index], company


def cmd_read(args):
    """Fetch and display clean, readable text from a SEC filing.

    Uses edgartools for iXBRL-aware text extraction. Falls back to raw HTML
    stripping for non-standard URLs.
    """
    # If given a ticker/CIK + form, use edgartools directly
    if hasattr(args, "ticker") and args.ticker:
        try:
            filing, company = _get_edgartools_filing(
                args.ticker, form=getattr(args, "form_type", "10-K") or "10-K"
            )
            if not filing:
                print(f"ERROR: No {args.form_type or '10-K'} filings found for {args.ticker}", file=sys.stderr)
                return
            text = filing.text()
            print(f"─── {filing.form} filed {filing.filing_date} | {company.name} ({len(text):,} chars) ───")
            print()
            lines = text.split("\n")
            for line in lines[:args.lines]:
                print(line)
            if len(lines) > args.lines:
                print(f"\n... ({len(lines) - args.lines} more lines, use --lines {len(lines)} to see all)")
            return
        except Exception as e:
            print(f"WARNING: edgartools failed ({e}), falling back to raw fetch", file=sys.stderr)

    # URL-based read: try edgartools first for accession-based URLs, then fall back
    url = args.url if hasattr(args, "url") and args.url else None
    if not url:
        print("ERROR: Provide a URL or use --ticker", file=sys.stderr)
        return

    # Try edgartools accession number extraction from URL
    try:
        import os
        os.environ.setdefault("EDGAR_IDENTITY", "Ithildin Research research@example.com")
        # Extract accession from URL pattern: /data/<CIK>/<ACCESSION>/
        m = re.search(r"/data/(\d+)/([\d-]+)/", url)
        if m:
            cik, accession_raw = m.group(1), m.group(2)
            from edgar import find
            company = find(int(cik))
            if company:
                filings = company.get_filings()
                for f in filings[:50]:  # check recent filings
                    if accession_raw in (f.accession_no or "").replace("-", ""):
                        text = f.text()
                        print(f"─── {f.form} filed {f.filing_date} | {company.name} ({len(text):,} chars) ───")
                        print()
                        lines = text.split("\n")
                        for line in lines[:args.lines]:
                            print(line)
                        if len(lines) > args.lines:
                            print(f"\n... ({len(lines) - args.lines} more lines)")
                        return
    except Exception as e:
        print(f"WARNING: edgartools URL parse failed ({e}), falling back to raw fetch", file=sys.stderr)

    # Raw HTML fallback (original implementation)
    data = _request(url, accept="text/html")
    if not data:
        return
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    if text.strip().startswith("<?xml") or text.strip().startswith("<XML>"):
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
    elif "<html" in text.lower()[:500] or "<body" in text.lower()[:500]:
        text = _strip_html(text)
    lines = text.split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    total_lines = len(lines)
    show_lines = lines[:args.lines]
    print(f"─── Filing Content ({total_lines} lines, showing {len(show_lines)}) ───")
    print()
    for line in show_lines:
        print(line)
    if total_lines > args.lines:
        print(f"\n... ({total_lines - args.lines} more lines, use --lines {total_lines} to see all)")


def _get_financial_statement(obj, stmt_type):
    """Get a financial statement from a parsed filing object.

    balance_sheet and income_statement are direct attributes.
    cashflow_statement requires going through financials.xb.statements.
    """
    # Try direct attribute first
    stmt = getattr(obj, stmt_type, None)
    if stmt and not callable(stmt):
        return stmt

    # Cash flow often needs the financials.xb.statements path
    try:
        stmts = obj.financials.xb.statements
        accessor = getattr(stmts, stmt_type, None)
        if accessor:
            return accessor() if callable(accessor) else accessor
    except Exception:
        pass
    return None


def _statement_to_json(stmt, stmt_type, filing):
    """Convert an edgartools Statement to structured JSON for analysis.

    Returns dict with metadata + line_items list. Each line item has:
    label, concept (XBRL tag), values (period->amount), and hierarchy info.
    """
    import math
    df = stmt.to_dataframe()
    period_cols = sorted([c for c in df.columns if c.startswith("20")])

    line_items = []
    for _, row in df.iterrows():
        if row.get("abstract", False):
            continue
        if row.get("dimension", False):
            continue

        values = {}
        for col in period_cols:
            v = row.get(col)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                values[col] = v

        if not values:
            continue

        item = {
            "label": row.get("label", ""),
            "concept": row.get("concept", ""),
            "values": values,
        }
        if row.get("balance"):
            item["balance"] = row["balance"]
        line_items.append(item)

    # Try to get clean company name
    company_name = None
    for attr in ("company_name", "company"):
        val = getattr(filing, attr, None)
        if val and isinstance(val, str):
            company_name = val
            break

    return {
        "statement_type": stmt_type,
        "company": company_name or str(filing),
        "form": filing.form,
        "filing_date": str(filing.filing_date),
        "accession": filing.accession_no,
        "periods": period_cols,
        "line_items": line_items,
    }


def cmd_sections(args):
    """Extract specific sections from a 10-K or 10-Q filing using edgartools."""
    try:
        filing, company = _get_edgartools_filing(
            args.ticker, form=args.form or "10-K", index=args.index
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return

    if not filing:
        print(f"ERROR: No {args.form or '10-K'} filings found for {args.ticker}", file=sys.stderr)
        return

    print(f"─── {filing.form} filed {filing.filing_date} | {company.name} ───")
    print(f"    Accession: {filing.accession_no}")
    print()

    try:
        obj = filing.obj()
    except Exception as e:
        print(f"WARNING: Could not parse filing structure ({e}). Falling back to full text.", file=sys.stderr)
        text = filing.text()
        lines = text.split("\n")[:args.lines]
        for line in lines:
            print(line)
        return

    # Financial statement section names
    financial_sections = {
        "balance_sheet", "income_statement", "cashflow_statement",
        "cash_flow", "income", "balance",
    }
    financial_aliases = {
        "balance": "balance_sheet",
        "income": "income_statement",
        "cash_flow": "cashflow_statement",
    }

    # If a specific section requested, show just that
    if args.section:
        section_map = {
            "business": ("business", "Item 1"),
            "item1": ("business", "Item 1"),
            "risk": ("risk_factors", "Item 1A"),
            "risk_factors": ("risk_factors", "Item 1A"),
            "item1a": ("risk_factors", "Item 1A"),
            "mda": ("management_discussion", "Item 7"),
            "item7": ("management_discussion", "Item 7"),
            "legal": ("legal_proceedings", "Item 3"),
            "item3": ("legal_proceedings", "Item 3"),
        }
        key = args.section.lower().replace(" ", "_").replace("-", "_")

        # Handle financial statement sections
        if key in financial_sections:
            resolved = financial_aliases.get(key, key)
            stmt = _get_financial_statement(obj, resolved)
            if stmt:
                result = _statement_to_json(stmt, resolved, filing)
                if hasattr(args, "output") and args.output:
                    write_output(result, args)
                else:
                    print(json.dumps(result, indent=2, default=str))
            else:
                print(f"Financial statement '{key}' not available in this filing.")
            return

        if key in section_map:
            attr, label = section_map[key]
            section = getattr(obj, attr, None)
            if section:
                text = str(section)
                print(f"─── {label} ({len(text):,} chars) ───")
                print()
                lines = text.split("\n")[:args.lines]
                for line in lines:
                    print(line)
                if len(text.split("\n")) > args.lines:
                    print(f"\n... (truncated, use --lines to see more)")
            else:
                # Try bracket access
                try:
                    section = obj[args.section]
                    text = str(section)
                    print(f"─── {args.section} ({len(text):,} chars) ───")
                    print()
                    for line in text.split("\n")[:args.lines]:
                        print(line)
                except Exception:
                    print(f"Section '{args.section}' not found in this filing.")
        else:
            # Try direct bracket access
            try:
                section = obj[args.section]
                text = str(section)
                print(f"─── {args.section} ({len(text):,} chars) ───")
                print()
                for line in text.split("\n")[:args.lines]:
                    print(line)
            except Exception:
                print(f"Section '{args.section}' not found. Try: business, risk, mda, legal, balance_sheet, income_statement, cashflow_statement")
        return

    # No specific section — show overview of all available sections
    sections_found = []
    for attr, label in [
        ("business", "Item 1 - Business"),
        ("risk_factors", "Item 1A - Risk Factors"),
        ("legal_proceedings", "Item 3 - Legal Proceedings"),
        ("management_discussion", "Item 7 - MD&A"),
    ]:
        section = getattr(obj, attr, None)
        if section:
            text = str(section)
            sections_found.append((label, len(text)))
            print(f"  {label}: {len(text):,} chars")
        else:
            print(f"  {label}: not available")

    # Also try financial statements
    for stmt_type, label in [
        ("balance_sheet", "Balance Sheet"),
        ("income_statement", "Income Statement"),
        ("cashflow_statement", "Cash Flow Statement"),
    ]:
        stmt = _get_financial_statement(obj, stmt_type)
        if stmt:
            df = stmt.to_dataframe()
            periods = [c for c in df.columns if c.startswith("20")]
            print(f"  {label}: available ({len(periods)} periods: {', '.join(periods)})")
        else:
            print(f"  {label}: not available")

    print(f"\nUse --section <name> to extract a specific section")
    print(f"  Text:       business, risk, mda, legal")
    print(f"  Financial:  balance_sheet, income_statement, cashflow_statement")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR search for OSINT investigation")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Full-text search across SEC filings")
    p.add_argument("query", nargs="+", help="Search terms (multiple terms are AND'd)")
    p.add_argument("--forms", help="Filter by form types (e.g., '10-K,DEF 14A')")
    p.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", help="End date (YYYY-MM-DD)")
    p.add_argument("--size", type=int, default=20, help="Number of results (max ~100)")
    p.add_argument("--offset", type=int, default=0, help="Offset for pagination")
    p.add_argument("--sort", choices=["relevance", "date", "date-asc"], default="relevance",
                   help="Sort order (client-side)")
    p.add_argument("--facets", action="store_true", help="Show entity/form/state aggregation facets")
    add_output_args(p)

    # lookup
    p = sub.add_parser("lookup", help="Find CIK numbers for a company or person")
    p.add_argument("name", nargs="+", help="Company or person name")

    # company
    p = sub.add_parser("company", help="Get company info by CIK")
    p.add_argument("cik", help="CIK number (e.g., 0001411494)")
    add_output_args(p)

    # filings
    p = sub.add_parser("filings", help="Get filings by CIK")
    p.add_argument("cik", help="CIK number")
    p.add_argument("--form", help="Filter by form type(s), comma-separated (e.g., 10-K,DEF)")
    p.add_argument("--limit", type=int, default=30)

    # insider
    p = sub.add_parser("insider", help="Show insider transactions (Forms 3/4/5)")
    p.add_argument("cik", help="CIK number")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--detail", action="store_true", help="Fetch and parse XML for transaction details")

    # read
    p = sub.add_parser("read", help="Fetch and display clean filing text (iXBRL-aware)")
    p.add_argument("url", nargs="?", help="Full URL to a SEC filing document")
    p.add_argument("--ticker", help="Ticker or CIK (uses edgartools for clean extraction)")
    p.add_argument("--form-type", default="10-K", help="Form type when using --ticker (default: 10-K)")
    p.add_argument("--lines", type=int, default=500, help="Number of lines to show (default: 500)")

    # sections (structured section extraction via edgartools)
    p = sub.add_parser("sections", help="Extract specific sections from 10-K/10-Q (clean text or structured financials)")
    p.add_argument("ticker", help="Ticker symbol or CIK")
    p.add_argument("--section", help="Section: business, risk, mda, legal | balance_sheet, income_statement, cashflow_statement")
    p.add_argument("--form", default="10-K", help="Form type (default: 10-K)")
    p.add_argument("--index", type=int, default=0, help="Filing index (0=most recent)")
    p.add_argument("--lines", type=int, default=1000, help="Max lines to show")
    add_output_args(p)

    args = parser.parse_args()
    # Set default json_out for subparsers that don't have output args
    if not hasattr(args, "json_out"):
        args.json_out = False
    if not hasattr(args, "output"):
        args.output = None

    handlers = {
        "search": cmd_search,
        "lookup": cmd_lookup,
        "company": cmd_company,
        "filings": cmd_filings,
        "insider": cmd_insider,
        "read": cmd_read,
        "sections": cmd_sections,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
