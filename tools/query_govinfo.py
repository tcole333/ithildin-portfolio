#!/usr/bin/env python3
"""
GovInfo (GPO) congressional hearings, committee reports, GAO reports, and CRS reports.

Covers CHRG (hearings, 1997+), CRPT (committee reports), GAOREPORTS, and CRS collections.
Free API — uses DEMO_KEY by default, or set GOVINFO_API_KEY for higher rate limits.
Rate limit: ~1,000/hour (DEMO_KEY), higher with registered key.

The /search endpoint requires POST with JSON body. Collection filtering uses
the query string syntax: "Deutsche Bank collection:CHRG".

Usage:
    python tools/query_govinfo.py search "Deutsche Bank" --collection CHRG
    python tools/query_govinfo.py search "shell companies" --collection GAOREPORTS --limit 10
    python tools/query_govinfo.py search "beneficial ownership" --collection CRS
    python tools/query_govinfo.py document GOVPUB-Y4_J89_2-PURL-LPS113630
    python tools/query_govinfo.py hearing GOVPUB-Y4_J89_2-PURL-LPS113630
    python tools/query_govinfo.py ingest GOVPUB-Y4_J89_2-PURL-LPS113630
    python tools/query_govinfo.py ingest-search "Epstein" --collection CHRG --limit 5
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

BASE_URL = "https://api.govinfo.gov"
RATE_LIMIT = 0.5
COLLECTIONS = ["BILLS", "CHRG", "CRPT", "GAOREPORTS", "CPRT", "CDOC", "USCOURTS"]

PROJECT_ROOT = Path(__file__).parent.parent


def _get_api_key():
    """Get GovInfo API key. Falls back to DEMO_KEY (works but lower rate limit)."""
    key = os.environ.get("GOVINFO_API_KEY")
    if not key:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("GOVINFO_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return key or "DEMO_KEY"


def _get(endpoint, params=None):
    """GET request to GovInfo API (for packages, collections, etc.)."""
    api_key = _get_api_key()
    if params is None:
        params = {}
    params["api_key"] = api_key

    url = f"{BASE_URL}{endpoint}?{urlencode(params, doseq=True)}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    time.sleep(RATE_LIMIT)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if e.code == 404:
            return None
        if e.code == 429:
            print("ERROR: Rate limit exceeded. Wait and retry.", file=sys.stderr)
        else:
            print(f"ERROR: HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None


def _post_search(query, page_size=20, offset_mark="*"):
    """POST search request to GovInfo API."""
    api_key = _get_api_key()
    url = f"{BASE_URL}/search?api_key={api_key}"

    body = json.dumps({
        "query": query,
        "pageSize": page_size,
        "offsetMark": offset_mark,
        "sorts": [{"field": "score", "sortOrder": "DESC"}],
    }).encode("utf-8")

    req = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    time.sleep(RATE_LIMIT)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        if e.code == 429:
            print("ERROR: Rate limit exceeded. Wait and retry.", file=sys.stderr)
        else:
            print(f"ERROR: HTTP {e.code}: {body_text[:200]}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None


def _flatten_result(item):
    """Flatten a GovInfo search result into a clean dict."""
    gov_author = item.get("governmentAuthor")
    author_str = ", ".join(gov_author) if isinstance(gov_author, list) else (gov_author or "")
    return {
        "packageId": item.get("packageId"),
        "granuleId": item.get("granuleId"),
        "title": item.get("title"),
        "dateIssued": item.get("dateIssued"),
        "collectionCode": item.get("collectionCode"),
        "governmentAuthor": author_str,
        "lastModified": item.get("lastModified"),
        "resultLink": item.get("resultLink"),
        "pdfLink": (item.get("download") or {}).get("pdfLink"),
    }


def _print_result(r):
    """Pretty-print a single search result."""
    pkg = r.get("packageId", "?")
    granule = r.get("granuleId")
    title = r.get("title", "")
    date = r.get("dateIssued", "?")
    coll = r.get("collectionCode", "?")
    author = r.get("governmentAuthor", "")

    label = f"{pkg}/{granule}" if granule else pkg
    print(f"\n  [{label}]")
    print(f"  {title}")
    parts = [f"Collection: {coll}"]
    if date:
        parts.insert(0, f"Date: {date}")
    if author:
        parts.append(f"Author: {author}")
    print(f"  {' | '.join(parts)}")


def cmd_search(args):
    """Full-text search across GovInfo collections."""
    # Collection filtering is done via query string syntax
    query = args.query
    if args.collection:
        query = f"{args.query} collection:{args.collection}"

    data = _post_search(query, page_size=min(args.limit, 100))
    if not data:
        print("No results or API error.")
        return

    results = [_flatten_result(r) for r in data.get("results", [])]
    total = data.get("count", len(results))
    output = {"total": total, "query": args.query, "collection": args.collection, "results": results}

    collection_label = args.collection or "all"
    log_search(f"govinfo_{collection_label.lower()}", args.query, total)

    if not write_output(output, args, summary=f"GovInfo '{args.query}' ({collection_label}, {total} total)"):
        print(f"GovInfo: {total} results for '{args.query}' in {collection_label}")
        for r in results:
            _print_result(r)


def cmd_document(args):
    """Fetch full document metadata by package ID."""
    data = _get(f"/packages/{args.package_id}/summary")
    if not data or "message" in data:
        print(f"No document found for {args.package_id}")
        if data and "message" in data:
            print(f"  {data['message']}", file=sys.stderr)
        sys.exit(1)

    log_search("govinfo_document", f"doc:{args.package_id}", 1)

    if not write_output(data, args, summary=f"GovInfo document {args.package_id}"):
        title = data.get("title", "?")
        date = data.get("dateIssued", "?")
        coll = data.get("collectionCode", "?")
        congress = data.get("congress", "")

        print(f"\n  Package: {args.package_id}")
        print(f"  Title: {title}")
        print(f"  Date: {date} | Collection: {coll}")
        if congress:
            print(f"  Congress: {congress}")

        committees = data.get("committees", [])
        if committees:
            for c in committees:
                name = c.get("committeeName") or c.get("authorityId", "?")
                print(f"  Committee: {name}")

        download = data.get("download", {})
        if download:
            print(f"\n  Downloads:")
            for fmt, url in download.items():
                print(f"    {fmt}: {url}")


def cmd_hearing(args):
    """Fetch hearing details including committee, witnesses, and links."""
    data = _get(f"/packages/{args.package_id}/summary")
    if not data or "message" in data:
        print(f"No hearing found for {args.package_id}")
        sys.exit(1)

    # Get granules (individual testimony, sections)
    granules = _get(f"/packages/{args.package_id}/granules", {"pageSize": 100, "offsetMark": "*"})
    if granules and "granules" in granules:
        data["granules"] = granules["granules"]

    log_search("govinfo_hearing", f"hearing:{args.package_id}", 1)

    if not write_output(data, args, summary=f"GovInfo hearing {args.package_id}"):
        title = data.get("title", "?")
        date = data.get("dateIssued", "?")
        committees = data.get("committees", [])

        print(f"\n  Hearing: {args.package_id}")
        print(f"  Title: {title}")
        print(f"  Date: {date}")
        if committees:
            for c in committees:
                name = c.get("committeeName") or c.get("authorityId", "?")
                print(f"  Committee: {name}")

        members = data.get("members", [])
        if members:
            print(f"\n  Members/Witnesses ({len(members)}):")
            for m in members[:20]:
                print(f"    - {m.get('memberName', '?')}")

        grans = data.get("granules", [])
        if grans:
            print(f"\n  Granules ({len(grans)} sections):")
            for g in grans[:10]:
                print(f"    [{g.get('granuleId', '?')}] {g.get('title', '?')}")


def cmd_ingest(args):
    """Download PDF and ingest via ingest_pdf.py pipeline."""
    data = _get(f"/packages/{args.package_id}/summary")
    if not data or "message" in data:
        print(f"No document found for {args.package_id}")
        sys.exit(1)

    download = data.get("download", {})
    pdf_url = download.get("pdfLink")
    api_key = _get_api_key()
    if pdf_url:
        pdf_url = f"{pdf_url}?api_key={api_key}"
    else:
        # Most packages have PDF at the direct /pdf endpoint
        pdf_url = f"{BASE_URL}/packages/{args.package_id}/pdf?api_key={api_key}"

    title = data.get("title", args.package_id)
    date = data.get("dateIssued", "")
    year = date[:4] if date else None
    collection = data.get("collectionCode", "CHRG")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    print(f"Downloading {args.package_id}...")
    try:
        req = Request(pdf_url, headers={"User-Agent": "OSINT-Research/1.0"})
        with urlopen(req, timeout=120) as resp:
            with open(tmp_path, "wb") as f:
                f.write(resp.read())
    except (HTTPError, URLError) as e:
        print(f"ERROR: Failed to download PDF: {e}", file=sys.stderr)
        Path(tmp_path).unlink(missing_ok=True)
        sys.exit(1)

    category_map = {
        "CHRG": "congressional",
        "CRPT": "congressional",
        "GAOREPORTS": "government_report",
        "CRS": "government_report",
        "GOVPUB": "congressional",
    }
    category = category_map.get(collection, "congressional")

    ingest_cmd = [
        sys.executable, str(PROJECT_ROOT / "tools" / "ingest_pdf.py"),
        "ingest", tmp_path,
        "--title", title[:200],
        "--source", f"GovInfo:{args.package_id}",
        "--category", category,
    ]
    if year:
        ingest_cmd.extend(["--year", year])

    result = subprocess.run(ingest_cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)

    Path(tmp_path).unlink(missing_ok=True)
    log_search("govinfo_ingest", f"ingest:{args.package_id}", 1)


def cmd_ingest_search(args):
    """Search and bulk ingest matching documents."""
    query = args.query
    if args.collection:
        query = f"{args.query} collection:{args.collection}"

    data = _post_search(query, page_size=min(args.limit, 100))
    if not data:
        print("No results or API error.")
        return

    results = data.get("results", [])
    total = data.get("count", len(results))
    print(f"Found {total} results for '{args.query}'. Ingesting up to {len(results)}...")

    ingested = 0
    for r in results:
        pkg_id = r.get("packageId")
        if not pkg_id:
            continue

        print(f"\n--- Ingesting {pkg_id} ---")
        ingest_args = argparse.Namespace(package_id=pkg_id)
        try:
            cmd_ingest(ingest_args)
            ingested += 1
        except SystemExit:
            print(f"  Skipped {pkg_id} (download/ingest failed)")
            continue

    collection_label = args.collection or "all"
    log_search(f"govinfo_{collection_label.lower()}", f"ingest-search:{args.query}", total)
    print(f"\nIngested {ingested}/{len(results)} documents.")


def main():
    parser = argparse.ArgumentParser(
        description="GovInfo congressional hearings, reports, GAO, and CRS",
        epilog="Auth: GOVINFO_API_KEY or DEMO_KEY (default). Get a key at https://www.govinfo.gov/api-signup",
    )
    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Full-text search across GovInfo collections")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--collection", choices=COLLECTIONS, help="Limit to collection (CHRG, CRPT, GAOREPORTS, CRS)")
    p_search.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    add_output_args(p_search)

    # document
    p_doc = sub.add_parser("document", help="Fetch document metadata by package ID")
    p_doc.add_argument("package_id", help="GovInfo package ID")
    add_output_args(p_doc)

    # hearing
    p_hearing = sub.add_parser("hearing", help="Fetch hearing details (committee, witnesses, sections)")
    p_hearing.add_argument("package_id", help="Hearing package ID")
    add_output_args(p_hearing)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Download PDF and ingest via ingest_pdf.py")
    p_ingest.add_argument("package_id", help="Package ID to download and ingest")

    # ingest-search
    p_isearch = sub.add_parser("ingest-search", help="Search and bulk ingest matching documents")
    p_isearch.add_argument("query", help="Search query")
    p_isearch.add_argument("--collection", choices=COLLECTIONS, help="Limit to collection")
    p_isearch.add_argument("--limit", type=int, default=10, help="Max documents to ingest (default: 10)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "search": cmd_search,
        "document": cmd_document,
        "hearing": cmd_hearing,
        "ingest": cmd_ingest,
        "ingest-search": cmd_ingest_search,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
