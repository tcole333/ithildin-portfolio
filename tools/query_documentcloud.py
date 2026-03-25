#!/usr/bin/env python3
"""
DocumentCloud API wrapper for OSINT investigations.

Searches DocumentCloud's public document archive. No authentication needed
for public documents. Text and PDF access via S3 URLs.

Key project: Epstein Documents (ID 216915) — 6 docs, 6,613 pages
(Giuffre v. Maxwell unsealed docs + MCC records)

Usage:
    # Full-text search across all DocumentCloud
    python tools/query_documentcloud.py search "Jeffrey Epstein" --limit 20
    python tools/query_documentcloud.py search "Maxwell" --project 216915

    # List documents in Epstein project
    python tools/query_documentcloud.py project
    python tools/query_documentcloud.py project 216915

    # Get document detail + text preview
    python tools/query_documentcloud.py document 24466257
    python tools/query_documentcloud.py document 24466257 --full

    # Fetch full text or specific page text
    python tools/query_documentcloud.py text 24466257
    python tools/query_documentcloud.py text 24466257 --page 5
    python tools/query_documentcloud.py text 24466257 --output /tmp/doc.txt

    # Download PDF
    python tools/query_documentcloud.py download 24466257
    python tools/query_documentcloud.py download 24466257 --dir /tmp/pdfs
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

API_BASE = "https://api.www.documentcloud.org/api/"
S3_BASE = "https://s3.documentcloud.org/documents"
USER_AGENT = "OSINT-Research/1.0"
DEFAULT_PROJECT = 216915  # Epstein Documents
RATE_LIMIT_DELAY = 0.5  # seconds between paginated requests


def _request(url, _retries=2):
    """Make an API request with User-Agent header and retry on transient errors."""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in (429, 500, 502, 503) and _retries > 0:
            wait = 3 if e.code == 429 else 2
            print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            return _request(url, _retries=_retries - 1)
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach DocumentCloud: {e.reason}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON response: {e}", file=sys.stderr)
        return None


def _fetch_text(url):
    """Fetch raw text from an S3 URL. Returns string or None."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        if e.code == 404:
            print(f"  Text not available (404)", file=sys.stderr)
        else:
            print(f"ERROR: HTTP {e.code} fetching text", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot fetch text: {e.reason}", file=sys.stderr)
        return None


def _fetch_binary(url):
    """Fetch binary content from a URL. Returns bytes or None."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    except HTTPError as e:
        if e.code == 404:
            print(f"  File not available (404)", file=sys.stderr)
        else:
            print(f"ERROR: HTTP {e.code} fetching file", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot fetch file: {e.reason}", file=sys.stderr)
        return None


def _s3_text_url(doc_id, slug):
    """Full document text URL."""
    return f"{S3_BASE}/{doc_id}/{slug}.txt"


def _s3_page_text_url(doc_id, slug, page):
    """Single page text URL (1-indexed)."""
    return f"{S3_BASE}/{doc_id}/pages/{slug}-p{page}.txt"


def _s3_pdf_url(doc_id, slug):
    """PDF download URL."""
    return f"{S3_BASE}/{doc_id}/{slug}.pdf"


def _format_doc_row(doc):
    """Format a document dict for display."""
    doc_id = doc.get("id", "?")
    title = doc.get("title", "Untitled")
    pages = doc.get("page_count", 0)
    source = doc.get("source", "")
    org = doc.get("organization", "")
    if isinstance(org, dict):
        org = org.get("name", "")
    created = doc.get("created_at", "")[:10]
    return doc_id, title, pages, source, org, created


# ── Commands ───────────────────────────────────────────────────────────


def cmd_search(args):
    """Full-text search across DocumentCloud documents."""
    query = args.query
    project_id = getattr(args, "project", None)
    limit = args.limit

    params = {"q": query, "per_page": min(limit, 100)}
    if project_id:
        params["project"] = project_id

    url = f"{API_BASE}documents/?{urlencode(params)}"

    all_results = []
    page_num = 0
    while url and len(all_results) < limit:
        page_num += 1
        if page_num > 1:
            time.sleep(RATE_LIMIT_DELAY)

        data = _request(url)
        if not data:
            break

        results = data.get("results", [])
        if not results:
            break

        all_results.extend(results)
        url = data.get("next")  # cursor-based pagination

    # Trim to limit
    all_results = all_results[:limit]

    # Output
    scope = f" in project {project_id}" if project_id else ""
    summary = f"DocumentCloud search '{query}'{scope}"

    if write_output(all_results, args, summary=summary):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(all_results, indent=2, default=str))
        return

    if not all_results:
        print(f"No documents found for '{query}'{scope}")
        return

    print(f"Found {len(all_results)} documents for '{query}'{scope}")
    print()
    for doc in all_results:
        doc_id, title, pages, source, org, created = _format_doc_row(doc)
        print(f"  [{doc_id}] {title}")
        print(f"    Pages: {pages}  Source: {source or '-'}  Org: {org or '-'}  Created: {created}")
        desc = doc.get("description", "")
        if desc:
            desc_trunc = desc[:120] + "..." if len(desc) > 120 else desc
            print(f"    {desc_trunc}")
        print()


def cmd_project(args):
    """List documents in a project."""
    project_id = args.project_id or DEFAULT_PROJECT

    params = {"project": project_id, "per_page": 100}
    url = f"{API_BASE}documents/?{urlencode(params)}"

    all_docs = []
    page_num = 0
    while url:
        page_num += 1
        if page_num > 1:
            time.sleep(RATE_LIMIT_DELAY)

        data = _request(url)
        if not data:
            break

        results = data.get("results", [])
        if not results:
            break

        all_docs.extend(results)
        url = data.get("next")

    summary = f"DocumentCloud project {project_id}"

    if write_output(all_docs, args, summary=summary):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(all_docs, indent=2, default=str))
        return

    if not all_docs:
        print(f"No documents found in project {project_id}")
        return

    total_pages = sum(d.get("page_count", 0) for d in all_docs)
    print(f"Project {project_id}: {len(all_docs)} documents, {total_pages:,} total pages")
    print()
    for doc in all_docs:
        doc_id, title, pages, source, org, created = _format_doc_row(doc)
        print(f"  [{doc_id}] {title}")
        print(f"    Pages: {pages}  Source: {source or '-'}  Created: {created}")
        desc = doc.get("description", "")
        if desc:
            desc_trunc = desc[:120] + "..." if len(desc) > 120 else desc
            print(f"    {desc_trunc}")
        print()


def cmd_document(args):
    """Get document detail and text preview."""
    doc_id = args.doc_id
    show_full = args.full

    url = f"{API_BASE}documents/{doc_id}/"
    data = _request(url)
    if not data:
        print(f"Document {doc_id} not found", file=sys.stderr)
        return

    title = data.get("title", "Untitled")
    slug = data.get("slug", "")
    pages = data.get("page_count", 0)
    source = data.get("source", "")
    description = data.get("description", "")
    org = data.get("organization", "")
    if isinstance(org, dict):
        org = org.get("name", "")
    status = data.get("status", "")
    access = data.get("access", "")
    created = data.get("created_at", "")[:19]
    updated = data.get("updated_at", "")[:19]
    canonical = data.get("canonical_url", "")
    projects = data.get("projects", [])

    # Fetch text from S3
    text = None
    if slug:
        text_url = _s3_text_url(doc_id, slug)
        text = _fetch_text(text_url)

    result = {
        "id": doc_id,
        "title": title,
        "slug": slug,
        "page_count": pages,
        "source": source,
        "description": description,
        "organization": org,
        "status": status,
        "access": access,
        "created_at": created,
        "updated_at": updated,
        "canonical_url": canonical,
        "projects": projects,
        "text_preview": (text[:2000] if text and not show_full else text),
        "text_length": len(text) if text else 0,
    }

    if write_output(result, args, summary=f"DocumentCloud doc {doc_id}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    print(f"Document: {title}")
    print(f"ID: {doc_id}")
    print(f"Slug: {slug}")
    print(f"Pages: {pages}")
    print(f"Source: {source or '-'}")
    print(f"Organization: {org or '-'}")
    print(f"Status: {status}  Access: {access}")
    print(f"Created: {created}  Updated: {updated}")
    if description:
        print(f"Description: {description}")
    if canonical:
        print(f"URL: {canonical}")
    if projects:
        print(f"Projects: {projects}")
    print()

    if text:
        text_display = text if show_full else text[:2000]
        print(f"--- Text ({len(text):,} chars{'' if show_full else ', first 2000'}) ---")
        print(text_display)
        if not show_full and len(text) > 2000:
            print(f"\n... [{len(text) - 2000:,} more chars — use --full to see all]")
    else:
        print("  [Text not available]")


def cmd_text(args):
    """Fetch full text or specific page text from S3."""
    doc_id = args.doc_id
    page = getattr(args, "page", None)

    # Need slug to build S3 URL — fetch doc metadata first
    url = f"{API_BASE}documents/{doc_id}/"
    data = _request(url)
    if not data:
        print(f"Document {doc_id} not found", file=sys.stderr)
        return

    slug = data.get("slug", "")
    title = data.get("title", "Untitled")
    total_pages = data.get("page_count", 0)

    if not slug:
        print(f"ERROR: No slug for document {doc_id}", file=sys.stderr)
        return

    if page:
        if page < 1 or (total_pages and page > total_pages):
            print(f"ERROR: Page {page} out of range (1-{total_pages})", file=sys.stderr)
            return
        text_url = _s3_page_text_url(doc_id, slug, page)
        label = f"page {page}"
    else:
        text_url = _s3_text_url(doc_id, slug)
        label = "full text"

    text = _fetch_text(text_url)
    if text is None:
        print(f"Text not available for {label} of '{title}'", file=sys.stderr)
        return

    # --output writes text to file (not JSON)
    output_path = getattr(args, "output", None)
    if output_path:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"{len(text):,} chars ({label} of '{title}') saved to {output_path}")
        return

    if getattr(args, "json_out", False):
        result = {
            "id": doc_id,
            "title": title,
            "slug": slug,
            "page": page,
            "text": text,
            "length": len(text),
        }
        print(json.dumps(result, indent=2, default=str))
        return

    page_label = f" (page {page}/{total_pages})" if page else f" ({total_pages} pages)"
    print(f"--- {title}{page_label} --- {len(text):,} chars ---")
    print(text)


def cmd_download(args):
    """Download PDF to local directory."""
    doc_id = args.doc_id
    out_dir = args.dir or "datasets/documentcloud"

    # Fetch metadata for slug and title
    url = f"{API_BASE}documents/{doc_id}/"
    data = _request(url)
    if not data:
        print(f"Document {doc_id} not found", file=sys.stderr)
        return

    slug = data.get("slug", "")
    title = data.get("title", "Untitled")
    pages = data.get("page_count", 0)

    if not slug:
        print(f"ERROR: No slug for document {doc_id}", file=sys.stderr)
        return

    os.makedirs(out_dir, exist_ok=True)
    filename = f"{doc_id}-{slug}.pdf"
    filepath = os.path.join(out_dir, filename)

    if os.path.exists(filepath):
        size = os.path.getsize(filepath)
        print(f"Already exists: {filepath} ({size:,} bytes)")
        return

    pdf_url = _s3_pdf_url(doc_id, slug)
    print(f"Downloading: {title} ({pages} pages)")
    print(f"  URL: {pdf_url}")

    content = _fetch_binary(pdf_url)
    if content is None:
        print(f"ERROR: Failed to download PDF for {doc_id}", file=sys.stderr)
        return

    with open(filepath, "wb") as f:
        f.write(content)

    print(f"  Saved: {filepath} ({len(content):,} bytes)")


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Query DocumentCloud API for public documents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.required = True

    # search
    sp = subparsers.add_parser("search", help="Full-text search across DocumentCloud")
    sp.add_argument("query", help="Search query")
    sp.add_argument("--project", type=int, help="Scope search to project ID (e.g. 216915)")
    sp.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    add_output_args(sp)
    sp.set_defaults(func=cmd_search)

    # project
    sp = subparsers.add_parser("project", help="List documents in a project")
    sp.add_argument("project_id", nargs="?", type=int, default=None,
                    help=f"Project ID (default: {DEFAULT_PROJECT} = Epstein Documents)")
    add_output_args(sp)
    sp.set_defaults(func=cmd_project)

    # document
    sp = subparsers.add_parser("document", help="Get document detail + text preview")
    sp.add_argument("doc_id", help="Document ID")
    sp.add_argument("--full", action="store_true", help="Show full text (not just first 2000 chars)")
    add_output_args(sp)
    sp.set_defaults(func=cmd_document)

    # text
    sp = subparsers.add_parser("text", help="Fetch document text from S3")
    sp.add_argument("doc_id", help="Document ID")
    sp.add_argument("--page", type=int, help="Specific page number (1-indexed)")
    add_output_args(sp)
    sp.set_defaults(func=cmd_text)

    # download
    sp = subparsers.add_parser("download", help="Download PDF")
    sp.add_argument("doc_id", help="Document ID")
    sp.add_argument("--dir", help="Output directory (default: datasets/documentcloud)")
    sp.set_defaults(func=cmd_download)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
