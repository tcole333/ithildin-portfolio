#!/usr/bin/env python3
"""
MuckRock FOIA API wrapper for OSINT investigations.

Searches public FOIA requests on MuckRock, downloads released documents,
and navigates the Epstein FOIA project (ID 507, 21 requests).

API: https://www.muckrock.com/api_v1/
Auth: None required for public read.
Pagination: Django REST (count/next/previous/results), page_size up to 100.
Rate limit: 1 req/sec.

IMPORTANT: The `search=` and `project=` query params on /foia/ are BROKEN
(return all 114K results unfiltered). For project listing, fetch the project
detail to get request IDs, then fetch each individually. For search, use
the `tags=` parameter which works correctly.

Usage:
    python tools/query_muckrock.py project                          # Epstein project (507)
    python tools/query_muckrock.py project 507
    python tools/query_muckrock.py request 12345
    python tools/query_muckrock.py download 12345 --dir datasets/muckrock
    python tools/query_muckrock.py search epstein
    python tools/query_muckrock.py agencies "Federal Bureau"
"""

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://www.muckrock.com/api_v1"
CDN_BASE = "https://cdn.muckrock.com"
USER_AGENT = "OSINT-Research/1.0"
RATE_LIMIT_DELAY = 1.0  # 1 request per second
DEFAULT_PROJECT_ID = None  # Set via --project or MUCKROCK_PROJECT_ID env var
DEFAULT_DOWNLOAD_DIR = "datasets/muckrock"

_last_request_time = 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url, _retries=2):
    """Fetch JSON from MuckRock API, respecting rate limit.

    Handles gzip-encoded responses and retries on transient failures.
    """
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_DELAY:
        time.sleep(RATE_LIMIT_DELAY - elapsed)

    req = Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": USER_AGENT,
    })

    try:
        _last_request_time = time.time()
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "")
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", errors="replace"))
    except HTTPError as e:
        body_raw = e.read()
        try:
            body = gzip.decompress(body_raw).decode()[:500]
        except Exception:
            try:
                body = body_raw.decode()[:500]
            except Exception:
                body = str(body_raw[:200])
        if e.code == 429 and _retries > 0:
            wait = RATE_LIMIT_DELAY * 3
            print(f"  Rate limited, waiting {wait:.0f}s...", file=sys.stderr)
            time.sleep(wait)
            return _fetch(url, _retries=_retries - 1)
        if e.code >= 500 and _retries > 0:
            print(f"  Server error {e.code}, retrying...", file=sys.stderr)
            time.sleep(RATE_LIMIT_DELAY * 2)
            return _fetch(url, _retries=_retries - 1)
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except URLError as e:
        if _retries > 0:
            print(f"  Connection error, retrying: {e.reason}", file=sys.stderr)
            time.sleep(RATE_LIMIT_DELAY * 2)
            return _fetch(url, _retries=_retries - 1)
        print(f"ERROR: {e.reason}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON response: {e}", file=sys.stderr)
        return None


def _fetch_endpoint(endpoint, params=None):
    """Fetch from a MuckRock API endpoint (relative path)."""
    url = f"{BASE_URL}/{endpoint.lstrip('/')}/"
    if params:
        url += "?" + urlencode(params, doseq=True)
    return _fetch(url)


def _paginate(endpoint, params=None, max_results=100):
    """Paginate through Django REST results."""
    if params is None:
        params = {}
    params["format"] = "json"
    params["page_size"] = min(100, max_results)

    all_results = []
    url = f"{BASE_URL}/{endpoint.lstrip('/')}/"
    url += "?" + urlencode(params, doseq=True)

    while url and len(all_results) < max_results:
        data = _fetch(url)
        if not data:
            break

        results = data.get("results", [])
        all_results.extend(results)

        url = data.get("next")  # Full URL for next page

    total = data.get("count", len(all_results)) if data else len(all_results)
    return all_results[:max_results], total


def _download_file(url, dest_path):
    """Download a file to disk. Returns True on success."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(data)
            size_kb = len(data) / 1024
            print(f"  Downloaded {dest_path.name} ({size_kb:.1f} KB)")
            return True
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR downloading {url}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Status display helpers
# ---------------------------------------------------------------------------

FOIA_STATUS_LABELS = {
    "submitted": "Submitted",
    "ack": "Acknowledged",
    "processed": "Processing",
    "appealing": "Appealing",
    "fix": "Fix Required",
    "payment": "Payment Required",
    "rejected": "Rejected",
    "no_docs": "No Responsive Docs",
    "done": "Completed",
    "partial": "Partially Completed",
    "abandoned": "Abandoned",
    "lawsuit": "In Litigation",
}


def _status_label(status):
    """Human-readable status label."""
    return FOIA_STATUS_LABELS.get(status, status or "Unknown")


def _extract_files(foia):
    """Extract all files from a FOIA request's communications."""
    files = []
    for comm in foia.get("communications", []):
        for f in comm.get("files", []):
            f["comm_date"] = comm.get("datetime", "")
            files.append(f)
    return files


_agency_cache = {}


def _resolve_agency(agency_id):
    """Resolve an agency ID to its name, with in-memory caching."""
    if agency_id in _agency_cache:
        return _agency_cache[agency_id]

    data = _fetch_endpoint(f"agency/{agency_id}", {"format": "json"})
    if data and isinstance(data, dict):
        name = data.get("name", f"Agency #{agency_id}")
    else:
        name = f"Agency #{agency_id}"

    _agency_cache[agency_id] = name
    return name


def _agency_name(agency):
    """Extract agency name from agency object, string, or integer ID."""
    if isinstance(agency, dict):
        return agency.get("name", "Unknown Agency")
    if isinstance(agency, int):
        return _resolve_agency(agency)
    if isinstance(agency, str):
        return agency
    return "Unknown Agency"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_project(args):
    """List FOIA requests in a MuckRock project."""
    project_id = args.project_id

    # Fetch project detail to get request IDs
    print(f"Fetching project {project_id}...", file=sys.stderr)
    project = _fetch_endpoint(f"project/{project_id}", {"format": "json"})
    if not project:
        print(f"ERROR: Could not fetch project {project_id}", file=sys.stderr)
        sys.exit(1)

    request_ids = project.get("requests", [])
    title = project.get("title", f"Project {project_id}")
    print(f"Project: {title} ({len(request_ids)} FOIA requests)", file=sys.stderr)

    # Fetch each FOIA request detail
    requests = []
    for i, rid in enumerate(request_ids):
        print(f"  Fetching request {rid} ({i+1}/{len(request_ids)})...", file=sys.stderr)
        foia = _fetch_endpoint(f"foia/{rid}", {"format": "json"})
        if foia:
            file_count = sum(
                len(comm.get("files", []))
                for comm in foia.get("communications", [])
            )
            requests.append({
                "id": foia.get("id"),
                "title": foia.get("title", "Untitled"),
                "status": foia.get("status", "unknown"),
                "agency": _agency_name(foia.get("agency")),
                "date_submitted": foia.get("datetime_submitted", ""),
                "file_count": file_count,
                "tracking_id": foia.get("tracking_id", ""),
            })

    # Output
    output_data = {
        "project_id": project_id,
        "project_title": title,
        "request_count": len(requests),
        "requests": requests,
    }

    if write_output(output_data, args, summary=f"MuckRock project {project_id} '{title}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(output_data, indent=2, default=str))
        return

    # Pretty print
    print(f"\n{'='*70}")
    print(f"MuckRock Project: {title} (ID {project_id})")
    print(f"FOIA Requests: {len(requests)}")
    print(f"{'='*70}\n")

    for r in requests:
        status = _status_label(r["status"])
        files = f"{r['file_count']} files" if r["file_count"] else "no files"
        date = r["date_submitted"][:10] if r["date_submitted"] else "no date"
        print(f"  [{r['id']}] {r['title']}")
        print(f"    Status: {status}  |  Agency: {r['agency']}  |  {files}  |  {date}")
        if r.get("tracking_id"):
            print(f"    Tracking: {r['tracking_id']}")
        print()


def cmd_request(args):
    """Show detail for a single FOIA request."""
    rid = args.request_id

    print(f"Fetching FOIA request {rid}...", file=sys.stderr)
    foia = _fetch_endpoint(f"foia/{rid}", {"format": "json"})
    if not foia:
        print(f"ERROR: Could not fetch request {rid}", file=sys.stderr)
        sys.exit(1)

    # Build structured output
    files = _extract_files(foia)
    comms = []
    for comm in foia.get("communications", []):
        comms.append({
            "date": comm.get("datetime", ""),
            "from_who": comm.get("from_who", {}).get("name", "") if isinstance(comm.get("from_who"), dict) else str(comm.get("from_who", "")),
            "to_who": comm.get("to_who", {}).get("name", "") if isinstance(comm.get("to_who"), dict) else str(comm.get("to_who", "")),
            "subject": comm.get("subject", ""),
            "response": comm.get("response", False),
            "file_count": len(comm.get("files", [])),
            "files": [
                {
                    "id": f.get("id"),
                    "title": f.get("title", ""),
                    "url": f.get("ffile", ""),
                    "pages": f.get("pages"),
                }
                for f in comm.get("files", [])
            ],
        })

    output_data = {
        "id": foia.get("id"),
        "title": foia.get("title", "Untitled"),
        "status": foia.get("status", "unknown"),
        "agency": _agency_name(foia.get("agency")),
        "date_submitted": foia.get("datetime_submitted", ""),
        "date_done": foia.get("datetime_done", ""),
        "tracking_id": foia.get("tracking_id", ""),
        "slug": foia.get("slug", ""),
        "total_files": len(files),
        "total_pages": sum(f.get("pages") or 0 for f in files),
        "communications": comms,
    }

    if write_output(output_data, args, summary=f"MuckRock FOIA #{rid}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(output_data, indent=2, default=str))
        return

    # Pretty print
    status = _status_label(output_data["status"])
    print(f"\n{'='*70}")
    print(f"FOIA Request #{output_data['id']}: {output_data['title']}")
    print(f"{'='*70}")
    print(f"  Status:    {status}")
    print(f"  Agency:    {output_data['agency']}")
    print(f"  Submitted: {output_data['date_submitted'][:10] if output_data['date_submitted'] else 'N/A'}")
    if output_data["date_done"]:
        print(f"  Completed: {output_data['date_done'][:10]}")
    if output_data["tracking_id"]:
        print(f"  Tracking:  {output_data['tracking_id']}")
    print(f"  Files:     {output_data['total_files']} ({output_data['total_pages']} pages)")
    print()

    for i, comm in enumerate(comms, 1):
        date = comm["date"][:10] if comm["date"] else "no date"
        direction = "RESPONSE" if comm["response"] else "SENT"
        from_str = f" from {comm['from_who']}" if comm["from_who"] else ""
        to_str = f" to {comm['to_who']}" if comm["to_who"] else ""
        print(f"  --- Communication {i} [{direction}] {date}{from_str}{to_str} ---")
        if comm["subject"]:
            print(f"  Subject: {comm['subject']}")
        if comm["files"]:
            for f in comm["files"]:
                pages = f" ({f['pages']} pages)" if f.get("pages") else ""
                title = f["title"] or "Untitled"
                print(f"    FILE: {title}{pages}")
                if f["url"]:
                    print(f"          {f['url']}")
        print()


def cmd_download(args):
    """Download all files from a FOIA request."""
    rid = args.request_id
    base_dir = Path(args.dir) if args.dir else Path(DEFAULT_DOWNLOAD_DIR)
    dest_dir = base_dir / str(rid)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching FOIA request {rid}...", file=sys.stderr)
    foia = _fetch_endpoint(f"foia/{rid}", {"format": "json"})
    if not foia:
        print(f"ERROR: Could not fetch request {rid}", file=sys.stderr)
        sys.exit(1)

    files = _extract_files(foia)
    if not files:
        print(f"No files found for FOIA request {rid}")
        return

    title = foia.get("title", "Untitled")
    print(f"FOIA #{rid}: {title}")
    print(f"  {len(files)} files to download -> {dest_dir}")
    print()

    downloaded = 0
    skipped = 0
    failed = 0

    for f in files:
        url = f.get("ffile", "")
        if not url:
            print(f"  SKIP: No URL for file {f.get('id', '?')}", file=sys.stderr)
            failed += 1
            continue

        # Derive filename from URL path or file title
        parsed = urlparse(url)
        url_filename = Path(parsed.path).name
        if not url_filename or url_filename == "/":
            # Fall back to title + id
            ext = ".pdf"  # Most FOIA files are PDFs
            safe_title = "".join(c if c.isalnum() or c in "-_." else "_" for c in (f.get("title", "") or ""))
            url_filename = f"{f.get('id', 'file')}_{safe_title}{ext}" if safe_title else f"{f.get('id', 'file')}{ext}"

        dest_path = dest_dir / url_filename

        if dest_path.exists():
            skipped += 1
            continue

        # Rate limit between downloads
        time.sleep(RATE_LIMIT_DELAY)

        if _download_file(url, dest_path):
            downloaded += 1
        else:
            failed += 1

    print(f"\nDone: {downloaded} downloaded, {skipped} skipped (exist), {failed} failed")
    print(f"Files in: {dest_dir}")


def cmd_search(args):
    """Search FOIA requests by tag."""
    query = args.query

    params = {
        "tags": query,
        "format": "json",
        "page_size": min(100, args.limit),
    }

    results, total = _paginate("foia", params, max_results=args.limit)

    # Enrich with file counts (already in list response)
    output_results = []
    for r in results:
        file_count = sum(
            len(comm.get("files", []))
            for comm in r.get("communications", [])
        )
        output_results.append({
            "id": r.get("id"),
            "title": r.get("title", "Untitled"),
            "status": r.get("status", "unknown"),
            "agency": _agency_name(r.get("agency")),
            "date_submitted": r.get("datetime_submitted", ""),
            "file_count": file_count,
            "tracking_id": r.get("tracking_id", ""),
        })

    output_data = {
        "query": query,
        "total": total,
        "showing": len(output_results),
        "results": output_results,
    }

    if write_output(output_data, args, summary=f"MuckRock search tags='{query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(output_data, indent=2, default=str))
        return

    # Pretty print
    print(f"\nMuckRock FOIA search: tags='{query}'")
    print(f"Found {total} results (showing {len(output_results)})\n")

    for r in output_results:
        status = _status_label(r["status"])
        files = f"{r['file_count']} files" if r["file_count"] else "no files"
        date = r["date_submitted"][:10] if r["date_submitted"] else "no date"
        print(f"  [{r['id']}] {r['title']}")
        print(f"    Status: {status}  |  Agency: {r['agency']}  |  {files}  |  {date}")
        print()


def cmd_agencies(args):
    """Search agencies on MuckRock.

    NOTE: The MuckRock agency API does not support partial text search (search=,
    q=, name__icontains= all return unfiltered results). Only exact name= works.
    Strategy: try exact match first; if no results, fetch pages and filter
    client-side by case-insensitive substring match.
    """
    query = args.query

    # Try exact match first
    exact = _fetch_endpoint("agency", {"format": "json", "name": query, "page_size": 100})
    if exact and exact.get("count", 0) > 0:
        results = exact.get("results", [])
        total = exact.get("count", len(results))
    else:
        # Client-side filter: fetch up to 500 agencies and filter
        # (API has no working partial search)
        print(f"No exact match for '{query}', searching by substring...", file=sys.stderr)
        query_lower = query.lower()
        results = []
        page = 1
        max_pages = 5  # 100 per page * 5 = 500 scanned
        while page <= max_pages and len(results) < args.limit:
            data = _fetch_endpoint("agency", {
                "format": "json",
                "page_size": 100,
                "page": page,
            })
            if not data or not data.get("results"):
                break
            for a in data["results"]:
                name = a.get("name", "")
                if query_lower in name.lower():
                    results.append(a)
            if not data.get("next"):
                break
            page += 1
        total = len(results)

    output_results = []
    for a in results[:args.limit]:
        jurisdiction = a.get("jurisdiction", {})
        if isinstance(jurisdiction, dict):
            jur_name = jurisdiction.get("name", "")
            jur_level = jurisdiction.get("level", "")
            jur_str = f"{jur_name} ({jur_level})" if jur_level else jur_name
        elif isinstance(jurisdiction, int):
            jur_str = ""  # Jurisdiction is an ID; resolving would cost extra requests
        else:
            jur_str = str(jurisdiction) if jurisdiction else ""

        output_results.append({
            "id": a.get("id"),
            "name": a.get("name", "Unknown"),
            "slug": a.get("slug", ""),
            "status": a.get("status", ""),
            "jurisdiction": jur_str,
            "jurisdiction_id": a.get("jurisdiction") if isinstance(a.get("jurisdiction"), int) else None,
            "average_response_time": a.get("average_response_time"),
            "success_rate": a.get("success_rate"),
            "number_requests": a.get("number_requests", 0),
            "number_requests_completed": a.get("number_requests_done", 0),
        })

    output_data = {
        "query": query,
        "total": total,
        "showing": len(output_results),
        "results": output_results,
    }

    if write_output(output_data, args, summary=f"MuckRock agencies '{query}'"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(output_data, indent=2, default=str))
        return

    # Pretty print
    print(f"\nMuckRock Agency search: '{query}'")
    print(f"Found {total} agencies (showing {len(output_results)})\n")

    for a in output_results:
        reqs = a["number_requests"] or 0
        done = a["number_requests_completed"] or 0
        resp_time = a["average_response_time"]
        success = a["success_rate"]

        stats_parts = [f"{reqs} requests ({done} completed)"]
        if resp_time is not None:
            stats_parts.append(f"avg {resp_time} days")
        if success is not None:
            # success_rate is a percentage (26.0 = 26%). List endpoint may
            # return it as int*100 (2600); normalize if > 100.
            try:
                pct = float(success)
                if pct > 100:
                    pct = pct / 100.0
                stats_parts.append(f"{pct:.1f}% success")
            except (ValueError, TypeError):
                stats_parts.append(f"{success} success")

        print(f"  [{a['id']}] {a['name']}")
        print(f"    Jurisdiction: {a['jurisdiction'] or 'N/A'}")
        print(f"    Stats: {' | '.join(stats_parts)}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MuckRock FOIA API tool for OSINT investigations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s project                          # List Epstein project (507)
  %(prog)s project 507                      # Same, explicit ID
  %(prog)s request 12345                    # Detail for one FOIA request
  %(prog)s download 12345                   # Download all files from request
  %(prog)s search epstein                   # Search by tag
  %(prog)s agencies "Federal Bureau"        # Search agencies
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # project
    p_project = sub.add_parser("project", help="List FOIA requests in a project")
    p_project.add_argument("project_id", nargs="?", type=int, default=DEFAULT_PROJECT_ID,
                           help=f"Project ID (default: {DEFAULT_PROJECT_ID} = Epstein)")
    add_output_args(p_project)

    # request
    p_request = sub.add_parser("request", help="Show detail for a FOIA request")
    p_request.add_argument("request_id", type=int, help="FOIA request ID")
    add_output_args(p_request)

    # download
    p_download = sub.add_parser("download", help="Download files from a FOIA request")
    p_download.add_argument("request_id", type=int, help="FOIA request ID")
    p_download.add_argument("--dir", help=f"Download directory (default: {DEFAULT_DOWNLOAD_DIR})")

    # search
    p_search = sub.add_parser("search", help="Search FOIA requests by tag")
    p_search.add_argument("query", help="Tag to search for")
    p_search.add_argument("--limit", type=int, default=100, help="Max results (default: 100)")
    add_output_args(p_search)

    # agencies
    p_agencies = sub.add_parser("agencies", help="Search agencies")
    p_agencies.add_argument("query", help="Agency name to search for")
    p_agencies.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    add_output_args(p_agencies)

    args = parser.parse_args()

    commands = {
        "project": cmd_project,
        "request": cmd_request,
        "download": cmd_download,
        "search": cmd_search,
        "agencies": cmd_agencies,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
